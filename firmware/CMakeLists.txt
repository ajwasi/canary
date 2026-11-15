cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# Build only what main/ asks for. This also keeps chip-only components out of the PC test build.
idf_build_set_property(MINIMAL_BUILD ON)
project(button_notifier)
