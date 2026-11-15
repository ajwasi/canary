/* PC test build: stand-in for esp_ota_ops.h. sim_ota.c keeps the running and previous
 * firmware versions in a file and acts out the bootloader's rollback rules. */
#pragma once
#include "esp_err.h"
#include "esp_partition.h"

typedef enum {
    ESP_OTA_IMG_NEW = 0x0U, ESP_OTA_IMG_PENDING_VERIFY = 0x1U, ESP_OTA_IMG_VALID = 0x2U,
    ESP_OTA_IMG_INVALID = 0x3U, ESP_OTA_IMG_ABORTED = 0x4U, ESP_OTA_IMG_UNDEFINED = 0xFFFFFFFFU,
} esp_ota_img_states_t;

const esp_partition_t *esp_ota_get_running_partition(void);
esp_err_t esp_ota_get_state_partition(const esp_partition_t *partition, esp_ota_img_states_t *ota_state);
esp_err_t esp_ota_mark_app_valid_cancel_rollback(void);
esp_err_t esp_ota_mark_app_invalid_rollback_and_reboot(void);
