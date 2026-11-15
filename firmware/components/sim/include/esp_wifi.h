/* PC test build: the small part of esp_wifi.h the firmware uses, backed by sim_wifi.c. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_event.h"

typedef enum {
    WIFI_AUTH_OPEN = 0, WIFI_AUTH_WEP, WIFI_AUTH_WPA_PSK, WIFI_AUTH_WPA2_PSK, WIFI_AUTH_WPA_WPA2_PSK,
    WIFI_AUTH_ENTERPRISE, WIFI_AUTH_WPA3_PSK, WIFI_AUTH_WPA2_WPA3_PSK, WIFI_AUTH_WAPI_PSK, WIFI_AUTH_OWE,
    WIFI_AUTH_MAX
} wifi_auth_mode_t;
typedef enum { WIFI_MODE_NULL = 0, WIFI_MODE_STA, WIFI_MODE_AP, WIFI_MODE_APSTA } wifi_mode_t;
typedef enum { WIFI_IF_STA = 0, WIFI_IF_AP } wifi_interface_t;
typedef enum { WIFI_STORAGE_FLASH = 0, WIFI_STORAGE_RAM } wifi_storage_t;
typedef enum {
    WPA3_SAE_PWE_UNSPECIFIED = 0, WPA3_SAE_PWE_HUNT_AND_PECK, WPA3_SAE_PWE_HASH_TO_ELEMENT, WPA3_SAE_PWE_BOTH
} wifi_sae_pwe_method_t;

typedef struct {
    uint8_t bssid[6];
    uint8_t ssid[33];
    uint8_t primary;            /* channel */
    int8_t rssi;
    wifi_auth_mode_t authmode;
} wifi_ap_record_t;

typedef struct {
    uint8_t *ssid;
    uint8_t *bssid;
    uint8_t channel;
    bool show_hidden;
} wifi_scan_config_t;

typedef struct { bool capable; bool required; } wifi_pmf_config_t;
typedef struct { int8_t rssi; wifi_auth_mode_t authmode; } wifi_scan_threshold_t;

typedef struct {
    uint8_t ssid[32];
    uint8_t password[64];
    bool bssid_set;
    uint8_t bssid[6];
    uint8_t channel;
    wifi_scan_threshold_t threshold;
    wifi_pmf_config_t pmf_cfg;
    wifi_sae_pwe_method_t sae_pwe_h2e;
} wifi_sta_config_t;

typedef struct {
    uint8_t ssid[32];
    uint8_t password[64];
    uint8_t channel;
    wifi_auth_mode_t authmode;
    uint8_t max_connection;
} wifi_ap_config_t;

typedef union { wifi_ap_config_t ap; wifi_sta_config_t sta; } wifi_config_t;
typedef struct { int unused; } wifi_init_config_t;
#define WIFI_INIT_CONFIG_DEFAULT() { 0 }

ESP_EVENT_DECLARE_BASE(WIFI_EVENT);
typedef enum { WIFI_EVENT_STA_START = 2, WIFI_EVENT_STA_CONNECTED = 4, WIFI_EVENT_STA_DISCONNECTED = 5 } wifi_event_t;

esp_err_t esp_wifi_init(const wifi_init_config_t *config);
esp_err_t esp_wifi_set_storage(wifi_storage_t storage);
esp_err_t esp_wifi_set_mode(wifi_mode_t mode);
esp_err_t esp_wifi_start(void);
esp_err_t esp_wifi_stop(void);
esp_err_t esp_wifi_set_config(wifi_interface_t interface, wifi_config_t *conf);
esp_err_t esp_wifi_scan_start(const wifi_scan_config_t *config, bool block);
esp_err_t esp_wifi_scan_get_ap_records(uint16_t *number, wifi_ap_record_t *ap_records);
esp_err_t esp_wifi_connect(void);
esp_err_t esp_wifi_disconnect(void);
esp_err_t esp_wifi_sta_get_ap_info(wifi_ap_record_t *ap_info);
