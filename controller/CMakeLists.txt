cmake_minimum_required(VERSION 3.16)
project(tsc_demo C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 99)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

# Restricted C++ subset for the controller: no exceptions, no RTTI, no heap after start-up
set(TSC_FLAGS -Wall -Wextra -Wno-unused-parameter -fno-exceptions -fno-rtti)

find_package(OpenSSL REQUIRED)
set(MOSQ_ROOT ${CMAKE_SOURCE_DIR}/../third_party/mosquitto/usr)
set(MOSQ_LIB ${MOSQ_ROOT}/lib/x86_64-linux-gnu/libmosquitto.so.1)

add_library(tsc_core STATIC
  src/common/base.cpp
  src/hal/hal.cpp
  src/services/time_service.cpp
  src/services/detector_service.cpp
  src/services/message_service.cpp
  src/services/config_store.cpp
  src/services/center_agent.cpp
  src/app/rule_interpreter.cpp
  src/app/phase_controller.cpp)
target_include_directories(tsc_core PUBLIC src ${MOSQ_ROOT}/include)
target_compile_options(tsc_core PRIVATE ${TSC_FLAGS})
target_link_libraries(tsc_core PUBLIC OpenSSL::Crypto ${MOSQ_LIB} pthread)

add_executable(tsc src/main.cpp)
target_compile_options(tsc PRIVATE ${TSC_FLAGS})
target_link_libraries(tsc tsc_core)
set_target_properties(tsc PROPERTIES BUILD_RPATH ${MOSQ_ROOT}/lib/x86_64-linux-gnu)

# Independent safety unit (conflict monitor), deliberately a separate C program
add_executable(mmu ../mmu/mmu.c)
target_compile_options(mmu PRIVATE -Wall -Wextra -O2)

enable_testing()
add_executable(test_crypto tests/test_crypto.cpp)
target_link_libraries(test_crypto tsc_core)
set_target_properties(test_crypto PROPERTIES BUILD_RPATH ${MOSQ_ROOT}/lib/x86_64-linux-gnu)
add_test(NAME aes_cmac_rfc4493 COMMAND test_crypto)

add_executable(test_rules tests/test_rules.cpp)
target_link_libraries(test_rules tsc_core)
set_target_properties(test_rules PROPERTIES BUILD_RPATH ${MOSQ_ROOT}/lib/x86_64-linux-gnu)
add_test(NAME rule_vectors COMMAND test_rules ${CMAKE_SOURCE_DIR}/../shared/rule_vectors.txt)
