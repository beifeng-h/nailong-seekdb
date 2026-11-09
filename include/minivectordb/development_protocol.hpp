#pragma once

#include "minivectordb/sql.hpp"

#include <iosfwd>

namespace minivectordb::development {

inline constexpr std::string_view protocol_name = "mvdb-dev-jsonl-v1";

void write_string(std::ostream& output, std::string_view value);
void write_value(std::ostream& output, const SqlValue& value);

// Development-only line framing. EOF or @quit returns to the owner for shutdown.
void run(SqlSession& session, std::istream& input, std::ostream& output);

}  // namespace minivectordb::development
