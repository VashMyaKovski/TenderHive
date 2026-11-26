cmake_minimum_required(VERSION 3.20)
project(th_docintel_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
option(TH_DOCINTEL_BUILD_BENCHMARKS "Build the C++ extraction benchmark" ON)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

find_package(Protobuf REQUIRED)
find_package(gRPC CONFIG REQUIRED)
find_package(nlohmann_json CONFIG REQUIRED)

get_filename_component(PROTO_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../proto" REALPATH)
set(PROTO_FILE "${PROTO_DIRECTORY}/docintel.proto")
set(GENERATED_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated")
file(MAKE_DIRECTORY "${GENERATED_DIR}")

set(PROTO_SOURCES
    "${GENERATED_DIR}/docintel.pb.cc"
    "${GENERATED_DIR}/docintel.grpc.pb.cc")
set(PROTO_HEADERS
    "${GENERATED_DIR}/docintel.pb.h"
    "${GENERATED_DIR}/docintel.grpc.pb.h")

add_custom_command(
    OUTPUT ${PROTO_SOURCES} ${PROTO_HEADERS}
    COMMAND $<TARGET_FILE:protobuf::protoc>
    ARGS
        "--proto_path=${PROTO_DIRECTORY}"
        "--cpp_out=${GENERATED_DIR}"
        "--grpc_out=${GENERATED_DIR}"
        --plugin=protoc-gen-grpc=$<TARGET_FILE:gRPC::grpc_cpp_plugin>
        "${PROTO_FILE}"
    DEPENDS "${PROTO_FILE}" protobuf::protoc gRPC::grpc_cpp_plugin
    VERBATIM)

add_library(docintel_proto ${PROTO_SOURCES})
target_include_directories(docintel_proto PUBLIC "${GENERATED_DIR}")
target_link_libraries(docintel_proto PUBLIC gRPC::grpc++ protobuf::libprotobuf)

add_library(docintel_core src/core.cpp)
target_include_directories(docintel_core PUBLIC include)
target_link_libraries(docintel_core PUBLIC nlohmann_json::nlohmann_json)
target_compile_options(docintel_core PRIVATE -Wall -Wextra -Wpedantic -Werror)

add_executable(docintel-core src/server.cpp)
target_link_libraries(docintel-core PRIVATE docintel_core docintel_proto gRPC::grpc++)
target_compile_options(docintel-core PRIVATE -Wall -Wextra -Wpedantic -Werror)

if(TH_DOCINTEL_BUILD_BENCHMARKS)
    add_executable(docintel-core-benchmark benchmarks/benchmark.cpp)
    target_link_libraries(docintel-core-benchmark PRIVATE docintel_core)
    target_compile_options(docintel-core-benchmark PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

include(CTest)
if(BUILD_TESTING)
    find_package(GTest CONFIG REQUIRED)
    add_executable(docintel-core-tests tests/core_test.cpp)
    target_link_libraries(docintel-core-tests PRIVATE docintel_core GTest::gtest_main)
    target_compile_options(docintel-core-tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
    include(GoogleTest)
    gtest_discover_tests(docintel-core-tests)
    if(TH_DOCINTEL_BUILD_BENCHMARKS)
        add_test(
            NAME docintel-benchmark-smoke
            COMMAND ${CMAKE_COMMAND} -E env
                DOCINTEL_TEMPLATE_DIR=${CMAKE_CURRENT_SOURCE_DIR}/../templates
                DOCINTEL_TEMPLATE_SET_VERSION=ts-2026-11-1
                DOCINTEL_CONFIDENCE_FLOOR=0.75
                DOCINTEL_BENCHMARK_ITERATIONS=2
                DOCINTEL_BENCHMARK_TEMPLATE_MULTIPLIERS=1,2
                DOCINTEL_BENCHMARK_TEXT=Penalty-is-0.15-percent-per-day
                DOCINTEL_BENCHMARK_TEXT_REPEATS=1
                DOCINTEL_BENCHMARK_BLOCKS=1
                $<TARGET_FILE:docintel-core-benchmark>)
    endif()
endif()
