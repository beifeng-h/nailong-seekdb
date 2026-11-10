#pragma once

#include <filesystem>
#include <iosfwd>

namespace minivectordb::stdio {

// Opens and recovers the database before ready. Shutdown closes it before acknowledgement.
void run(const std::filesystem::path& directory, std::istream& input, std::ostream& output);

}  // namespace minivectordb::stdio
