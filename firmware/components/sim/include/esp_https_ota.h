/* PC test build: stand-in for esp_https_ota.h. The image is downloaded with the real
 * esp_http_client (so TLS is exercised), checked for an ESP app header, and "flashed" by sim_ota.c. */
#pragma once
#include "esp_err.h"
#include "esp_http_client.h"

typedef struct { const esp_http_client_config_t *http_config; } esp_https_ota_config_t;

esp_err_t esp_https_ota(const esp_https_ota_config_t *ota_config);
