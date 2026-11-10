#include "minivectordb/stdio_protocol.hpp"

#include <iostream>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

namespace {

int usage() {
  std::cerr << "Usage: minivectordb_server [--data DIRECTORY]\n";
  return 2;
}

int run(const std::filesystem::path& directory) {
#ifdef _WIN32
  if (_setmode(_fileno(stdin), _O_BINARY) == -1 || _setmode(_fileno(stdout), _O_BINARY) == -1) {
    std::cerr << "Cannot configure binary standard streams\n";
    return 1;
  }
#endif
  try {
    minivectordb::stdio::run(directory, std::cin, std::cout);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "Database process failed: " << error.what() << '\n';
    return 1;
  }
}

}  // namespace

#ifdef _WIN32
int wmain(int argc, wchar_t** argv) {
  if (argc == 1) return run(L"/data");
  if (argc != 3 || std::wstring_view(argv[1]) != L"--data") return usage();
  return run(std::filesystem::path(argv[2]));
}
#else
int main(int argc, char** argv) {
  if (argc == 1) return run("/data");
  if (argc != 3 || std::string_view(argv[1]) != "--data") return usage();
  return run(std::filesystem::path(argv[2]));
}
#endif
