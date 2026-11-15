# Simulated hardware for the PC test build (idf.py --preview set-target linux).
# It stands in for the parts of ESP-IDF that need a real chip: Wi-Fi, pins, the I2S
# amplifier, firmware slots and the sound file store. Nothing here is built for a real unit.
idf_build_get_property(target IDF_TARGET)
if(NOT "${target}" STREQUAL "linux")
    idf_component_register()
    return()
endif()

idf_build_get_property(build_dir BUILD_DIR)
idf_component_register(
    SRCS "sim.c" "sim_wifi.c" "sim_ota.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "../../main"
    REQUIRES esp_event esp_http_client esp_partition soc
    PRIV_REQUIRES nvs_flash json mbedtls esp_timer esp_hw_support esp_system esp_app_format log)
target_compile_definitions(${COMPONENT_LIB} PRIVATE "SIM_BUILD_DIR=\"${build_dir}\"")
target_link_libraries(${COMPONENT_LIB} PUBLIC m)
