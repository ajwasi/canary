/* PC test build: stand-in for esp_netif_sntp.h. The simulated clock is set when the
 * simulated Wi-Fi is up and the test allows a time server to be reached. */
#pragma once
#include "freertos/FreeRTOS.h"
#include "esp_err.h"

typedef struct { const char *server; } esp_sntp_config_t;
#define ESP_NETIF_SNTP_DEFAULT_CONFIG(name) { .server = (name) }

esp_err_t esp_netif_sntp_init(const esp_sntp_config_t *config);
esp_err_t esp_netif_sntp_sync_wait(TickType_t wait);
