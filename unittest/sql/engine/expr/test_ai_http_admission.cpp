/*
 * Copyright (c) 2026 OceanBase.
 * Licensed under the Apache License, Version 2.0.
 */

#include <gtest/gtest.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <future>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#include "lib/allocator/ob_allocator.h"
#include "query/ai/ob_ai_endpoint_resolver.h"
#include "sql/engine/expr/ob_expr_ai/ob_ai_func_client.h"
#include "sql/engine/expr/ob_expr_ai/ob_ai_func_utils.h"

namespace oceanbase
{
namespace sql
{
namespace
{
using namespace common;
using Milliseconds = std::chrono::milliseconds;
constexpr int TEST_TIMEOUT_MS = 5000;

// Only supplies metadata. The actual client's admission, timeout, curl and
// cleanup paths remain under test, including process-wide URL sharing.
class FixedCapResolver : public query::ObIAiEndpointResolver
{
public:
  explicit FixedCapResolver(int64_t cap) : cap_(cap) {}
  int resolve_by_model_name(const ObString &, ObIAllocator &,
      share::ObAiModelEndpointInfo &, bool = true) const override
  {
    return OB_NOT_SUPPORTED;
  }
  int resolve_url_max_concurrency(const ObString &, int64_t abs_timeout_ts,
      int64_t &cap) const override
  {
    if (abs_timeout_ts <= 0) {
      return OB_INVALID_ARGUMENT;
    }
    cap = cap_;
    return OB_SUCCESS;
  }
private:
  const int64_t cap_;
};

class RejectingAllocator : public ObWrapperAllocator
{
public:
  explicit RejectingAllocator(ObIAllocator &backing)
      : ObWrapperAllocator(backing), rejecting_(false), rejected_(0) {}
  void reject_allocations() { rejecting_ = true; }
  int rejected() const { return rejected_; }
  void *alloc(const int64_t size) override
  {
    if (rejecting_) { ++rejected_; return nullptr; }
    return ObWrapperAllocator::alloc(size);
  }
  void *alloc(const int64_t size, const ObMemAttr &attr) override
  {
    if (rejecting_) { ++rejected_; return nullptr; }
    return ObWrapperAllocator::alloc(size, attr);
  }
  void *realloc(const void *ptr, const int64_t size, const ObMemAttr &attr) override
  {
    if (rejecting_) { ++rejected_; return nullptr; }
    return ObWrapperAllocator::realloc(ptr, size, attr);
  }
  void *realloc(void *ptr, const int64_t old_size, const int64_t new_size) override
  {
    if (rejecting_) { ++rejected_; return nullptr; }
    return ObWrapperAllocator::realloc(ptr, old_size, new_size);
  }
private:
  bool rejecting_;
  int rejected_;
};

// One request per connection, ephemeral loopback port, no external services.
// Responses are released explicitly by request id. All waits have deadlines;
// teardown releases gates, shuts down sockets and joins every owned thread.
class LoopbackHttp
{
public:
  LoopbackHttp() : listen_fd_(-1), stopping_(false), release_all_(false),
      peak_(0), active_(0), errors_(0)
  {
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) { return; }
    sockaddr_in address = {};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    if (::bind(listen_fd_, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0
        || ::listen(listen_fd_, 16) != 0) {
      ::close(listen_fd_);
      listen_fd_ = -1;
      return;
    }
    socklen_t length = sizeof(address);
    if (::getsockname(listen_fd_, reinterpret_cast<sockaddr *>(&address), &length) != 0) {
      ::close(listen_fd_);
      listen_fd_ = -1;
      return;
    }
    url_ = "http://127.0.0.1:" + std::to_string(ntohs(address.sin_port)) + "/inference";
    accept_thread_ = std::thread([this] { accept_connections(); });
  }
  ~LoopbackHttp()
  {
    stopping_.store(true);
    release_all();
    if (listen_fd_ >= 0) { ::shutdown(listen_fd_, SHUT_RDWR); }
    if (accept_thread_.joinable()) { accept_thread_.join(); }
    {
      std::lock_guard<std::mutex> guard(mutex_);
      for (int fd : open_fds_) { ::shutdown(fd, SHUT_RDWR); }
    }
    for (auto &thread : handlers_) { thread.join(); }
    if (listen_fd_ >= 0) { ::close(listen_fd_); }
  }
  bool valid() const { return listen_fd_ >= 0; }
  const std::string &url() const { return url_; }
  void set_status(int id, int status)
  {
    std::lock_guard<std::mutex> guard(mutex_);
    statuses_[id] = status;
  }
  void release(int id)
  {
    std::lock_guard<std::mutex> guard(mutex_);
    released_.insert(id);
    condition_.notify_all();
  }
  void release_all()
  {
    std::lock_guard<std::mutex> guard(mutex_);
    release_all_ = true;
    condition_.notify_all();
  }
  bool wait_received(size_t count, int timeout_ms = TEST_TIMEOUT_MS)
  {
    std::unique_lock<std::mutex> lock(mutex_);
    return condition_.wait_for(lock, Milliseconds(timeout_ms),
        [&] { return received_.size() >= count; });
  }
  bool wait_responded(size_t count)
  {
    std::unique_lock<std::mutex> lock(mutex_);
    return condition_.wait_for(lock, Milliseconds(TEST_TIMEOUT_MS),
        [&] { return responded_.size() >= count; });
  }
  std::vector<int> received() const
  {
    std::lock_guard<std::mutex> guard(mutex_);
    return received_;
  }
  int peak() const
  {
    std::lock_guard<std::mutex> guard(mutex_);
    return peak_;
  }
  int errors() const
  {
    std::lock_guard<std::mutex> guard(mutex_);
    return errors_;
  }
private:
  bool readable(int fd, const std::chrono::steady_clock::time_point &deadline)
  {
    while (!stopping_.load() && std::chrono::steady_clock::now() < deadline) {
      pollfd event = {fd, POLLIN, 0};
      const int ready = ::poll(&event, 1, 100);
      if (ready > 0) { return (event.revents & POLLIN) != 0; }
      if (ready < 0 && errno != EINTR) { return false; }
    }
    return false;
  }
  bool read_body(int fd, std::string &body)
  {
    std::string request;
    const auto deadline = std::chrono::steady_clock::now() + Milliseconds(TEST_TIMEOUT_MS);
    size_t header_end = std::string::npos;
    size_t content_length = 0;
    while (request.size() < 65536 && readable(fd, deadline)) {
      char chunk[2048];
      const ssize_t count = ::recv(fd, chunk, sizeof(chunk), 0);
      if (count <= 0) { return false; }
      request.append(chunk, static_cast<size_t>(count));
      if (header_end == std::string::npos
          && (header_end = request.find("\r\n\r\n")) != std::string::npos) {
        std::string headers = request.substr(0, header_end);
        std::transform(headers.begin(), headers.end(), headers.begin(),
            [](unsigned char c) { return c >= 'A' && c <= 'Z' ? c - 'A' + 'a' : c; });
        const size_t field = headers.find("\r\ncontent-length:");
        if (field == std::string::npos) { return false; }
        content_length = std::strtoul(headers.c_str() + field + 17, nullptr, 10);
        if (content_length > 32768) { return false; }
      }
      if (header_end != std::string::npos && request.size() >= header_end + 4 + content_length) {
        body = request.substr(header_end + 4, content_length);
        return true;
      }
    }
    return false;
  }
  bool send_response(int fd, int status, const std::string &body)
  {
    const std::string response = "HTTP/1.1 " + std::to_string(status)
        + (status == 200 ? " OK\r\n" : " Service Unavailable\r\n")
        + "Content-Type: application/json\r\nConnection: close\r\nContent-Length: "
        + std::to_string(body.size()) + "\r\n\r\n" + body;
    size_t sent = 0;
    while (sent < response.size()) {
      const ssize_t count = ::send(fd, response.data() + sent, response.size() - sent, MSG_NOSIGNAL);
      if (count > 0) { sent += static_cast<size_t>(count); }
      else if (count < 0 && errno == EINTR) { continue; }
      else { return false; }
    }
    return true;
  }
  void serve(int fd)
  {
    std::string body;
    if (!read_body(fd, body)) {
      std::lock_guard<std::mutex> guard(mutex_);
      if (!stopping_.load()) { ++errors_; }
      return;
    }
    const size_t field = body.find("\"id\"");
    const size_t colon = body.find(':', field);
    if (field == std::string::npos || colon == std::string::npos) {
      std::lock_guard<std::mutex> guard(mutex_);
      ++errors_;
      return;
    }
    const int id = static_cast<int>(std::strtol(body.c_str() + colon + 1, nullptr, 10));
    int status = 200;
    {
      std::unique_lock<std::mutex> lock(mutex_);
      received_.push_back(id);
      ++active_;
      peak_ = std::max(peak_, active_);
      condition_.notify_all();
      const bool released = condition_.wait_for(lock, Milliseconds(TEST_TIMEOUT_MS),
          [&] { return release_all_ || released_.count(id) != 0 || stopping_.load(); });
      --active_; // Endpoint processing stops before the response is delivered.
      if (!released) { ++errors_; return; }
      const auto configured = statuses_.find(id);
      if (configured != statuses_.end()) { status = configured->second; }
    }
    const bool sent = send_response(fd, status, body);
    {
      std::lock_guard<std::mutex> guard(mutex_);
      if (!sent && !stopping_.load()) { ++errors_; }
      responded_.push_back(id);
      condition_.notify_all();
    }
  }
  void accept_connections()
  {
    while (!stopping_.load()) {
      pollfd event = {listen_fd_, POLLIN, 0};
      const int ready = ::poll(&event, 1, 100);
      if (ready <= 0) { continue; }
      if (!(event.revents & POLLIN)) { break; }
      const int fd = ::accept(listen_fd_, nullptr, nullptr);
      if (fd < 0) { continue; }
      const timeval timeout = {TEST_TIMEOUT_MS / 1000, 0};
      ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
      {
        std::lock_guard<std::mutex> guard(mutex_);
        open_fds_.insert(fd);
      }
      handlers_.emplace_back([this, fd] {
        serve(fd);
        std::lock_guard<std::mutex> guard(mutex_);
        open_fds_.erase(fd);
        ::close(fd);
      });
    }
  }
  int listen_fd_;
  std::atomic<bool> stopping_;
  std::string url_;
  std::thread accept_thread_;
  std::vector<std::thread> handlers_;
  mutable std::mutex mutex_;
  std::condition_variable condition_;
  bool release_all_;
  std::set<int> open_fds_;
  std::set<int> released_;
  std::map<int, int> statuses_;
  std::vector<int> received_;
  std::vector<int> responded_;
  int peak_;
  int active_;
  int errors_;
};

struct CallResult
{
  int ret = OB_ERR_UNEXPECTED;
  std::vector<int64_t> ids;
};

// Releasing the server before joining also makes failed assertions safe.
class ClientThreads
{
public:
  explicit ClientThreads(LoopbackHttp &server) : server_(server) {}
  ~ClientThreads()
  {
    server_.release_all();
    for (auto &thread : threads_) { thread.join(); }
  }
  template <typename Work>
  std::future<CallResult> start(Work work)
  {
    std::packaged_task<CallResult()> task(std::move(work));
    auto result = task.get_future();
    threads_.emplace_back(std::move(task));
    return result;
  }
private:
  LoopbackHttp &server_;
  std::vector<std::thread> threads_;
};

int make_body(ObArenaAllocator &allocator, int id, ObJsonObject *&body)
{
  const std::string json = "{\"id\":" + std::to_string(id) + "}";
  return ObAIFuncJsonUtils::get_json_object_form_str(allocator, ObString(json.c_str()), body);
}

int read_id(ObJsonObject *response, std::vector<int64_t> &ids)
{
  ObIJsonBase *id = response == nullptr ? nullptr : response->get_value("id");
  if (id == nullptr || id->json_type() != ObJsonNodeType::J_INT) { return OB_INVALID_DATA; }
  ids.push_back(id->get_int());
  return OB_SUCCESS;
}

CallResult post(ObAIFuncClient &client, ObArenaAllocator &allocator,
    const std::string &url, int id)
{
  CallResult result;
  ObArray<ObString> headers;
  ObJsonObject *body = nullptr;
  ObJsonObject *response = nullptr;
  result.ret = headers.push_back(ObString("Content-Type: application/json"));
  if (result.ret == OB_SUCCESS) { result.ret = make_body(allocator, id, body); }
  if (result.ret == OB_SUCCESS) {
    result.ret = client.send_post(allocator, ObString(url.c_str()), headers, body, response);
  }
  if (result.ret == OB_SUCCESS) { result.ret = read_id(response, result.ids); }
  return result;
}

CallResult scalar(FixedCapResolver &resolver, const std::string &url, int id, int timeout_sec = 5)
{
  ObArenaAllocator allocator;
  ObAIFuncClient client(&resolver);
  client.set_max_retry_times(0);
  client.set_timeout_sec(timeout_sec);
  return post(client, allocator, url, id);
}

class AIHttpAdmission : public ::testing::Test
{
protected:
  void SetUp() override
  {
    // The existing contract-test main does not perform observer startup.
    // Initialize before any client threads can trigger curl's lazy init.
    ASSERT_EQ(CURLE_OK, curl_global_init(CURL_GLOBAL_DEFAULT));
  }
  void TearDown() override
  {
    // Test-body locals, including both groups of owned threads, are gone.
    curl_global_cleanup();
  }
};

TEST_F(AIHttpAdmission, eight_scalar_clients_share_three_url_slots_and_make_progress)
{
  LoopbackHttp server;
  ASSERT_TRUE(server.valid());
  FixedCapResolver resolver(3);
  ClientThreads clients(server);
  std::vector<std::future<CallResult>> results;
  for (int id = 0; id < 8; ++id) {
    results.push_back(clients.start([&, id] { return scalar(resolver, server.url(), id); }));
  }
  EXPECT_TRUE(server.wait_received(3));
  EXPECT_FALSE(server.wait_received(4, 150));
  EXPECT_EQ(3, server.peak());
  server.release_all();
  for (int id = 0; id < 8; ++id) {
    const CallResult result = results[id].get();
    EXPECT_EQ(OB_SUCCESS, result.ret);
    EXPECT_EQ((std::vector<int64_t>{id}), result.ids);
  }
  EXPECT_EQ(8u, server.received().size());
  EXPECT_EQ(3, server.peak());
  EXPECT_EQ(0, server.errors());
}

TEST_F(AIHttpAdmission, queue_timeout_sends_no_http_and_http_error_reset_releases_slot)
{
  LoopbackHttp server;
  ASSERT_TRUE(server.valid());
  server.set_status(100, 503);
  FixedCapResolver resolver(1);
  ObArenaAllocator holder_allocator;
  ObAIFuncClient holder(&resolver);
  holder.set_max_retry_times(0);
  holder.set_timeout_sec(5);
  ClientThreads clients(server);
  auto held = clients.start([&] { return post(holder, holder_allocator, server.url(), 100); });
  EXPECT_TRUE(server.wait_received(1));
  auto waiting = clients.start([&] { return scalar(resolver, server.url(), 101, 1); });
  const bool timed_out = waiting.wait_for(Milliseconds(2500)) == std::future_status::ready;
  EXPECT_TRUE(timed_out);
  // Always unblock before collecting results, including on a broken timeout path.
  if (!timed_out) { server.release_all(); }
  EXPECT_EQ(OB_TIMEOUT, waiting.get().ret);
  EXPECT_EQ((std::vector<int>{100}), server.received());
  server.release(100);
  EXPECT_EQ(OB_CURL_ERROR, held.get().ret);
  holder.reset();
  server.release_all();
  EXPECT_EQ(OB_SUCCESS, post(holder, holder_allocator, server.url(), 102).ret);
  EXPECT_EQ(OB_SUCCESS, scalar(resolver, server.url(), 103).ret);
  EXPECT_EQ((std::vector<int>{100, 102, 103}), server.received());
  EXPECT_EQ(1, server.peak());
  EXPECT_EQ(0, server.errors());
}

TEST_F(AIHttpAdmission, eight_batch_requests_use_three_three_two_waves_and_keep_input_order)
{
  LoopbackHttp server;
  ASSERT_TRUE(server.valid());
  FixedCapResolver resolver(3);
  ClientThreads clients(server);
  auto batch = clients.start([&] {
    CallResult result;
    ObArenaAllocator allocator;
    ObAIFuncClient client(&resolver);
    client.set_max_retry_times(0);
    client.set_timeout_sec(5);
    ObArray<ObString> headers;
    ObArray<ObJsonObject *> bodies;
    ObArray<ObJsonObject *> responses;
    result.ret = headers.push_back(ObString("Content-Type: application/json"));
    for (int id = 0; result.ret == OB_SUCCESS && id < 8; ++id) {
      ObJsonObject *body = nullptr;
      result.ret = make_body(allocator, id, body);
      if (result.ret == OB_SUCCESS) { result.ret = bodies.push_back(body); }
    }
    if (result.ret == OB_SUCCESS) {
      result.ret = client.send_post_batch(allocator, ObString(server.url().c_str()), headers, bodies, responses);
    }
    for (int64_t i = 0; result.ret == OB_SUCCESS && i < responses.count(); ++i) {
      result.ret = read_id(responses.at(i), result.ids);
    }
    return result;
  });
  size_t completed = 0;
  for (size_t end : {3u, 6u, 8u}) {
    const bool arrived = server.wait_received(end);
    EXPECT_TRUE(arrived);
    if (!arrived) { break; }
    if (end != 8) { EXPECT_FALSE(server.wait_received(end + 1, 150)); }
    const std::vector<int> arrivals = server.received();
    // Complete each wave in reverse arrival order; responses still belong to
    // the original input positions, not to curl's completion queue order.
    for (size_t i = end; i > completed; --i) {
      server.release(arrivals[i - 1]);
      EXPECT_TRUE(server.wait_responded(completed + (end - i) + 1));
    }
    completed = end;
  }
  server.release_all();
  const CallResult result = batch.get();
  EXPECT_EQ(OB_SUCCESS, result.ret);
  EXPECT_EQ((std::vector<int64_t>{0, 1, 2, 3, 4, 5, 6, 7}), result.ids);
  std::vector<int> arrivals = server.received();
  std::sort(arrivals.begin(), arrivals.end());
  EXPECT_EQ((std::vector<int>{0, 1, 2, 3, 4, 5, 6, 7}), arrivals);
  EXPECT_EQ(3, server.peak());
  EXPECT_EQ(0, server.errors());
}

TEST_F(AIHttpAdmission, no_wait_owns_request_json_after_input_arena_reset)
{
  LoopbackHttp server;
  ASSERT_TRUE(server.valid());
  FixedCapResolver resolver(1);
  ObArenaAllocator output_allocator;
  ObAIFuncClient client(&resolver);
  client.set_max_retry_times(0);
  client.set_timeout_sec(5);
  ObArray<ObString> headers;
  ASSERT_EQ(OB_SUCCESS, headers.push_back(ObString("Content-Type: application/json")));
  ASSERT_EQ(OB_SUCCESS, client.init(output_allocator, ObString(server.url().c_str()), headers));

  ObArenaAllocator input_allocator;
  ObArray<ObJsonObject *> bodies;
  for (int id = 0; id < 4; ++id) {
    ObJsonObject *body = nullptr;
    ASSERT_EQ(OB_SUCCESS, make_body(input_allocator, id, body));
    const std::string original = "original-" + std::to_string(id);
    ObString owned_text;
    ObJsonString *text = nullptr;
    ASSERT_EQ(OB_SUCCESS, ob_write_string(input_allocator, ObString(original.c_str()), owned_text));
    ASSERT_EQ(OB_SUCCESS, ObAIFuncJsonUtils::get_json_string(input_allocator, owned_text, text));
    ASSERT_EQ(OB_SUCCESS, body->add(ObString("text"), text));
    ASSERT_EQ(OB_SUCCESS, bodies.push_back(body));
  }
  ASSERT_EQ(OB_SUCCESS, client.send_post_batch_no_wait(bodies));

  const auto deadline = std::chrono::steady_clock::now() + Milliseconds(TEST_TIMEOUT_MS);
  // The public polling method advances curl. Keep the first response gated
  // while establishing that later bodies have not yet reached the endpoint.
  bool finished = false;
  while (!server.wait_received(1, 5) && std::chrono::steady_clock::now() < deadline) {
    if (client.check_batch_finished()) { finished = true; break; }
  }
  ASSERT_TRUE(server.wait_received(1, 0));
  EXPECT_FALSE(finished);
  EXPECT_FALSE(server.wait_received(2, 100));

  // Mutating values before freeing their arena makes accidental shallow
  // retention observable, in addition to exercising the released lifetime.
  for (int64_t i = 1; i < bodies.count(); ++i) {
    ObJsonNode *id = bodies.at(i)->get_value("id");
    ASSERT_NE(nullptr, id);
    ASSERT_EQ(ObJsonNodeType::J_INT, id->json_type());
    static_cast<ObJsonInt *>(id)->set_value(900 + i);
    ObJsonNode *text = bodies.at(i)->get_value("text");
    ASSERT_NE(nullptr, text);
    ASSERT_EQ(ObJsonNodeType::J_STRING, text->json_type());
    ObString source_text = static_cast<ObJsonString *>(text)->get_str();
    std::memset(source_text.ptr(), 'x', source_text.length());
  }
  input_allocator.reset();
  bodies.reset();
  server.release_all();

  while (!finished && std::chrono::steady_clock::now() < deadline) {
    finished = client.check_batch_finished();
    if (!finished) {
      // Wake promptly for endpoint activity, with a bounded wait when curl
      // needs another polling turn. No test thread sleeps or busy-spins.
      server.wait_received(server.received().size() + 1, 5);
    }
  }
  EXPECT_TRUE(finished);
  if (finished) {
    ObArray<ObJsonObject *> responses;
    ASSERT_EQ(OB_SUCCESS, client.get_batch_result(responses));
    std::vector<int64_t> ids;
    for (int64_t i = 0; i < responses.count(); ++i) {
      ASSERT_EQ(OB_SUCCESS, read_id(responses.at(i), ids));
      ObJsonNode *text = responses.at(i)->get_value("text");
      ASSERT_NE(nullptr, text);
      ASSERT_EQ(ObJsonNodeType::J_STRING, text->json_type());
      const ObString echoed = static_cast<ObJsonString *>(text)->get_str();
      EXPECT_EQ("original-" + std::to_string(i), std::string(echoed.ptr(), echoed.length()));
    }
    EXPECT_EQ((std::vector<int64_t>{0, 1, 2, 3}), ids);
  }
  client.reset();
  EXPECT_EQ((std::vector<int>{0, 1, 2, 3}), server.received());
  EXPECT_EQ(1, server.peak());
  EXPECT_EQ(0, server.errors());
}

TEST_F(AIHttpAdmission, snapshot_allocation_failure_sends_no_http_and_leaves_capacity_usable)
{
  LoopbackHttp server;
  ASSERT_TRUE(server.valid());
  FixedCapResolver resolver(1);
  ObArenaAllocator backing_allocator;
  RejectingAllocator output_allocator(backing_allocator);
  ObAIFuncClient client(&resolver);
  client.set_max_retry_times(0);
  client.set_timeout_sec(5);
  ObArray<ObString> headers;
  ASSERT_EQ(OB_SUCCESS, headers.push_back(ObString("Content-Type: application/json")));
  ASSERT_EQ(OB_SUCCESS, client.init(output_allocator, ObString(server.url().c_str()), headers));
  ObArenaAllocator input_allocator;
  ObArray<ObJsonObject *> bodies;
  for (int id = 0; id < 2; ++id) {
    ObJsonObject *body = nullptr;
    ASSERT_EQ(OB_SUCCESS, make_body(input_allocator, id, body));
    ASSERT_EQ(OB_SUCCESS, bodies.push_back(body));
  }

  // Initialization succeeded. Reject only the later body snapshot allocations
  // through the same public allocator interface supplied by ordinary callers.
  output_allocator.reject_allocations();
  EXPECT_EQ(OB_ALLOCATE_MEMORY_FAILED, client.send_post_batch_no_wait(bodies));
  EXPECT_GT(output_allocator.rejected(), 0);
  EXPECT_FALSE(server.wait_received(1, 150));
  EXPECT_TRUE(server.received().empty());
  client.reset();

  server.release_all();
  const CallResult recovered = scalar(resolver, server.url(), 10);
  EXPECT_EQ(OB_SUCCESS, recovered.ret);
  EXPECT_EQ((std::vector<int64_t>{10}), recovered.ids);
  EXPECT_EQ((std::vector<int>{10}), server.received());
  EXPECT_EQ(1, server.peak());
  EXPECT_EQ(0, server.errors());
}
} // namespace
} // namespace sql
} // namespace oceanbase
