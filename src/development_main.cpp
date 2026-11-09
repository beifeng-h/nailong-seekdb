#include "minivectordb/development_protocol.hpp"

#include <iostream>

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

int run(const std::filesystem::path& directory) {
  using namespace minivectordb;
#ifdef _WIN32
  if (_setmode(_fileno(stdin), _O_BINARY) == -1 || _setmode(_fileno(stdout), _O_BINARY) == -1) {
    std::cerr << "Cannot configure binary standard streams\n";
    return 1;
  }
#endif
  try {
    std::cerr << "MiniVectorDB development protocol " << development::protocol_name << ", formal-ready=false\n";
    Database database(directory);
    SqlSession session(database);
    development::run(session, std::cin, std::cout);
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
      std::wstring_view(argv[2]) != L"mvdb-dev-jsonl-v1" || std::wstring_view(argv[3]) != L"--data") return usage();
  return run(std::filesystem::path(argv[4]));
}
#else
int main(int argc, char** argv) {
  if (argc != 5 || std::string_view(argv[1]) != "--development-protocol" ||
      std::string_view(argv[2]) != minivectordb::development::protocol_name || std::string_view(argv[3]) != "--data") return usage();
  return run(std::filesystem::path(argv[4]));
}
#endif
