#pragma once

#include "minivectordb/sql.hpp"

#include <iosfwd>
#include <string_view>

namespace minivectordb::judge {

inline constexpr std::string_view protocol_name = "mvdb-judge-json-v1";

// Official contest framing: one JSON request per line on stdin, one JSON
// response per line on stdout. Diagnostics belong on stderr.
void run(SqlSession& session, std::istream& input, std::ostream& output);

}  // namespace minivectordb::judge
