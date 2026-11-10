#include "minivectordb/judge_protocol.hpp"

#include <filesystem>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

using namespace minivectordb;

namespace {

std::vector<std::string> lines_of(const std::string& text) {
  std::vector<std::string> lines;
  std::istringstream stream(text);
  std::string line;
  while (std::getline(stream, line)) lines.push_back(line);
  return lines;
}

void expect_equal(const std::string& actual, const std::string& expected, const char* context) {
  if (actual != expected) {
    throw std::runtime_error(std::string(context) + ": expected " + expected + ", got " + actual);
  }
}

// Drives the judge protocol against a real database directory.
std::vector<std::string> exchange(const std::filesystem::path& directory,
                                  const std::vector<std::string>& requests) {
  Database database(directory);
  SqlSession session(database);
  std::ostringstream output;
  std::ostringstream buffer;
  for (const auto& request : requests) buffer << request << '\n';
  std::istringstream input(buffer.str());
  judge::run(session, input, output);
  return lines_of(output.str());
}

void check_handshake_and_execute(const std::filesystem::path& directory) {
  const std::vector<std::string> requests = {
      R"({"id":1,"op":"execute","sql":"CREATE TABLE docs (id BIGINT PRIMARY KEY, content VARCHAR(16));","search_mode":"exact"})",
      R"({"id":2,"op":"execute","sql":"INSERT INTO docs VALUES (1, 'x');","search_mode":"exact"})",
      R"({"id":3,"op":"execute","sql":"SELECT id FROM docs ORDER BY id;","search_mode":"exact"})",
      R"({"id":4,"op":"shutdown"})",
  };
  const auto responses = exchange(directory, requests);
  if (responses.size() != 5) throw std::runtime_error("Expected five protocol lines");
  expect_equal(responses[0], R"({"type":"ready"})", "handshake");
  expect_equal(responses[1], R"({"id":1,"ok":true})", "create table response");
  expect_equal(responses[2], R"({"id":2,"ok":true,"affected_rows":1})", "insert response");
  expect_equal(responses[3], R"({"id":3,"ok":true,"columns":["id"],"rows":[[1]]})", "select response");
  expect_equal(responses[4], R"({"id":4,"ok":true})", "shutdown response");
}

void check_identifier_type_and_errors(const std::filesystem::path& directory) {
  const std::vector<std::string> requests = {
      R"({"id":"abc","op":"execute","sql":"CREATE TABLE docs (id BIGINT PRIMARY KEY);","search_mode":"exact"})",
      R"({"id":2,"op":"execute","sql":"INSERT INTO docs VALUES (1),(1);","search_mode":"exact"})",
      R"({"id":3,"op":"execute","sql":"SELECT id FROM docs;","search_mode":"exact"})",
      R"({"id":4,"op":"execute","sql":"ALTER TABLE docs ADD COLUMN extra INT;","search_mode":"exact"})",
      R"({"id":5,"op":"execute","sql":"NOT SQL AT ALL;","search_mode":"exact"})",
      R"({"id":6,"op":"shutdown"})",
  };
  const auto responses = exchange(directory, requests);
  if (responses.size() != 7) throw std::runtime_error("Expected seven protocol lines");
  expect_equal(responses[1], R"({"id":"abc","ok":true})", "string identifier is echoed as a string");
  if (responses[2].find(R"("ok":false)") == std::string::npos) {
    throw std::runtime_error("Duplicate primary key must fail: " + responses[2]);
  }
  if (responses[2].find(R"("code":"duplicate_key")") == std::string::npos) {
    throw std::runtime_error("Duplicate primary key must use the published code: " + responses[2]);
  }
  expect_equal(responses[3], R"({"id":3,"ok":true,"columns":["id"],"rows":[]})",
               "a failed statement must not partially apply");
  if (responses[4].find(R"("code":"unsupported_sql")") == std::string::npos) {
    throw std::runtime_error("Unsupported SQL must use the published code: " + responses[4]);
  }
  if (responses[5].find(R"("ok":false)") == std::string::npos) {
    throw std::runtime_error("Invalid SQL must fail: " + responses[5]);
  }
}

void check_search_mode(const std::filesystem::path& directory) {
  const std::vector<std::string> requests = {
      R"({"id":1,"op":"execute","sql":"CREATE TABLE docs (id BIGINT PRIMARY KEY, embedding VECTOR(2));","search_mode":"exact"})",
      R"({"id":2,"op":"execute","sql":"INSERT INTO docs VALUES (1, '[0, 0]'), (2, '[3, 4]');","search_mode":"exact"})",
      R"({"id":3,"op":"execute","sql":"SELECT id FROM docs WHERE id = 2;","search_mode":"ann"})",
      R"({"id":4,"op":"shutdown"})",
  };
  const auto responses = exchange(directory, requests);
  expect_equal(responses[3], R"({"id":3,"ok":true,"columns":["id"],"rows":[[2]]})",
               "ann mode still answers the request");
}

void check_duplicate_key_keeps_the_original_record(const std::filesystem::path& directory) {
  const std::vector<std::string> requests = {
      R"({"id":1,"op":"execute","sql":"CREATE TABLE docs (id BIGINT PRIMARY KEY, content VARCHAR(16));","search_mode":"exact"})",
      R"({"id":2,"op":"execute","sql":"INSERT INTO docs VALUES (1, 'first');","search_mode":"exact"})",
      R"({"id":3,"op":"execute","sql":"INSERT INTO docs VALUES (1, 'second');","search_mode":"exact"})",
      R"({"id":4,"op":"execute","sql":"SELECT id, content FROM docs;","search_mode":"exact"})",
      R"({"id":5,"op":"shutdown"})",
  };
  const auto responses = exchange(directory, requests);
  if (responses[3].find(R"("ok":false)") == std::string::npos) {
    throw std::runtime_error("Duplicate primary key must fail: " + responses[3]);
  }
  expect_equal(responses[4], R"({"id":4,"ok":true,"columns":["id","content"],"rows":[[1,"first"]]})",
               "the original record survives the rejected duplicate");
}

}  // namespace

int main() {
  const std::filesystem::path root =
      std::filesystem::temp_directory_path() / "minivectordb-judge-protocol-tests";
  std::filesystem::remove_all(root);
  std::filesystem::create_directories(root / "first");
  std::filesystem::create_directories(root / "second");
  std::filesystem::create_directories(root / "third");
  std::filesystem::create_directories(root / "fourth");
  try {
    check_handshake_and_execute(root / "first");
    check_identifier_type_and_errors(root / "second");
    check_search_mode(root / "third");
    check_duplicate_key_keeps_the_original_record(root / "fourth");
    std::filesystem::remove_all(root);
    std::cout << "Judge protocol contracts passed\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
