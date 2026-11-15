idf_component_register(
    SRCS "app_main.c" "cfg.c" "button.c" "evq.c" "audio.c" "wifi_mgr.c"
         "provisioning.c" "identity.c" "mqtt_link.c"
    INCLUDE_DIRS "."
    EMBED_TXTFILES "portal.html")
