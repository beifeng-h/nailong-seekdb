#include "minivectordb/development_protocol.hpp"

#include <bit>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <limits>
#include <locale>
#include <random>
#include <sstream>

using namespace minivectordb;

namespace {

std::string serialized(const SqlValue& value) {
  std::ostringstream stream;
  stream.imbue(std::locale::classic());
  stream << std::setprecision(17);
  development::write_value(stream, value);
  return stream.str();
}

void check_double(double value) {
  std::ostringstream reference;
  reference.imbue(std::locale::classic());
  reference << std::setprecision(17) << std::scientific << value;
  if (serialized(SqlValue{value}) != reference.str()) throw std::runtime_error("Scientific double bytes differ");
}

void check_vector(const Vector& vector) {
  std::ostringstream reference;
  reference.imbue(std::locale::classic());
  reference << std::setprecision(17) << '[';
  for (std::size_t i = 0; i < vector.size(); ++i) {
    if (i != 0) reference << ',';
    reference << std::scientific << vector[i];
  }
  reference << ']';
  if (serialized(SqlValue{vector}) != reference.str()) throw std::runtime_error("Scientific vector bytes differ");
}

}  // namespace

int main() {
  try {
    for (double value : {0.0, -0.0, 0.1, -0.1, 1e-200, 1e200, std::numeric_limits<double>::max(),
                         std::numeric_limits<double>::lowest(), std::numeric_limits<double>::min(),
                         std::numeric_limits<double>::denorm_min()}) check_double(value);
    Vector vector{0, -0.0f, 0.1f, -0.1f, std::numeric_limits<float>::max(),
                  std::numeric_limits<float>::lowest(), std::numeric_limits<float>::min(),
                  std::numeric_limits<float>::denorm_min()};
    std::mt19937_64 random(902110);
    unsigned doubles = 10;
    for (unsigned i = 0; i < 4096; ++i) {
      const auto value = std::bit_cast<double>(random());
      if (std::isfinite(value)) { check_double(value); ++doubles; }
      const auto number = std::bit_cast<float>(static_cast<std::uint32_t>(random()));
      if (std::isfinite(number)) vector.push_back(number);
    }
    check_vector({}); check_vector(vector);
    if (serialized(SqlValue{std::numeric_limits<std::int64_t>::min()}) != "-9223372036854775808" ||
        serialized(SqlValue{std::numeric_limits<std::int64_t>::max()}) != "9223372036854775807") {
      throw std::runtime_error("BIGINT bytes differ");
    }
    std::cout << "PASS scientific_bytes doubles=" << doubles << " float32_components=" << vector.size()
              << " bigint_extremes=2\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
