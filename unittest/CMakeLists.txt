# Focused regressions use the production shared implementation. This target is
# excluded from normal builds and never downloads test dependencies.
set(_ai_contracts_gtest_include "${DEP_DIR}/include")
find_file(_ai_contracts_gtest_archive
  NAMES libgtest.a
  PATHS "${DEP_DIR}/lib" "${DEP_DIR}/lib64"
  NO_DEFAULT_PATH)
if(NOT _ai_contracts_gtest_archive)
  # A concrete path keeps missing test dependencies out of configure-time
  # NOTFOUND validation. The explicit build checks them before compilation.
  set(_ai_contracts_gtest_archive "${DEP_DIR}/lib/libgtest.a")
endif()

set(_ai_contracts_dependency_check
  "${CMAKE_CURRENT_BINARY_DIR}/check_ai_function_contracts_dependencies.cmake")
file(GENERATE OUTPUT "${_ai_contracts_dependency_check}" CONTENT
"if(NOT EXISTS \"${_ai_contracts_gtest_include}/gtest/gtest.h\")
  message(FATAL_ERROR \"ai_function_contracts requires pinned GTest headers at ${_ai_contracts_gtest_include}/gtest/gtest.h. Restore the repository dependencies and reconfigure.\")
endif()
if(NOT EXISTS \"${_ai_contracts_gtest_archive}\")
  message(FATAL_ERROR \"ai_function_contracts requires pinned libgtest.a under ${DEP_DIR}/lib or lib64. Restore the repository dependencies and reconfigure.\")
endif()
")
add_custom_target(ai_function_contracts_check_dependencies
  COMMAND "${CMAKE_COMMAND}" -P "${_ai_contracts_dependency_check}"
  VERBATIM)

add_executable(ai_function_contracts EXCLUDE_FROM_ALL
  ai_function_contracts_main.cpp
  sql/engine/expr/test_ai_function_contracts.cpp)
target_include_directories(ai_function_contracts PRIVATE
  "${_ai_contracts_gtest_include}")
target_link_libraries(ai_function_contracts PRIVATE
  oceanbase
  "${_ai_contracts_gtest_archive}"
  pthread
  dl)
add_dependencies(ai_function_contracts
  ai_function_contracts_check_dependencies
  oceanbase)

add_test(NAME ai_function_contracts COMMAND ai_function_contracts)
set_tests_properties(ai_function_contracts PROPERTIES
  WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}")
