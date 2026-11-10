#include "minivectordb/development_protocol.hpp"
#include "minivectordb/judge_protocol.hpp"

#include <iostream>
#include <string>
#include <string_view>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

namespace {

int usage() {
  std::cerr << "Usage: minivectordb_development_server --development-protocol "
            << minivectordb::development::protocol_name << " --data DIRECTORY\n";
  return 2;
}

int run(const std::filesystem::path& directory, std::string_view protocol) {
  using namespace minivectordb;
#ifdef _WIN32
  if (_setmode(_fileno(stdin), _O_BINARY) == -1 || _setmode(_fileno(stdout), _O_BINARY) == -1) {
    std::cerr << "Cannot configure binary standard streams\n";
    return 1;
  }
#endif
  try {
    Database database(directory);
    SqlSession session(database);
    if (protocol == judge::protocol_name) {
      std::cerr << "MiniVectorDB protocol " << judge::protocol_name << '\n';
      judge::run(session, std::cin, std::cout);
    } else {
      std::cerr << "MiniVectorDB development protocol " << development::protocol_name << ", formal-ready=false\n";
      development::run(session, std::cin, std::cout);
    }
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "Development process failed: " << error.what() << '\n';
    return 1;
  }
}

}  // namespace

#ifdef _WIN32
int wmain(int argc, wchar_t** argv) {
  if (argc != 5 || std::wstring_view(argv[1]) != L"--development-protocol" ||
      std::wstring_view(argv[3]) != L"--data") return usage();
  const std::wstring_view protocol = argv[2];
  if (protocol != L"mvdb-dev-jsonl-v1" && protocol != L"mvdb-judge-json-v1") return usage();
  const std::string narrow(protocol.begin(), protocol.end());
  return run(std::filesystem::path(argv[4]), narrow);
}
#else
int main(int argc, char** argv) {
  if (argc != 5 || std::string_view(argv[1]) != "--development-protocol" ||
      std::string_view(argv[3]) != "--data") return usage();
  const std::string_view protocol = argv[2];
  if (protocol != minivectordb::development::protocol_name && protocol != minivectordb::judge::protocol_name) {
    return usage();
  }
  return run(std::filesystem::path(argv[4]), protocol);
}
#endif
