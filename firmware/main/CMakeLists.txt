idf_build_get_property(target IDF_TARGET)

set(srcs "app_main.c" "cfg.c" "button.c" "evq.c" "audio.c" "wifi_mgr.c"
         "provisioning.c" "identity.c" "mqtt_link.c")
set(requires nvs_flash esp_event esp_timer esp_http_client esp_http_server esp-tls
             mbedtls mqtt json esp_hw_support esp_system log)

if(${target} STREQUAL "linux")
    # PC test build: the radio, pins, amplifier and firmware slots are simulated (components/sim).
    list(APPEND requires sim)
else()
    list(APPEND srcs "port_esp.c")
    list(APPEND requires esp_wifi esp_netif lwip esp_driver_gpio esp_driver_i2s spiffs
                         app_update esp_https_ota esp_app_format)
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "."
    PRIV_REQUIRES ${requires}
    EMBED_TXTFILES "portal.html")
