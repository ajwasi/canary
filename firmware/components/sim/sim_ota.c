/* Simulated firmware slots and bootloader. PC test build only.
 *
 * <SIM_DIR>/ota.txt holds "running previous state". The rules follow ESP-IDF's app rollback:
 *   - a freshly written image is "new"; on its first boot it becomes "pending";
 *   - the firmware confirms it (esp_ota_mark_app_valid_cancel_rollback) or rejects it;
 *   - a restart while still "pending" makes the bootloader return to the previous version.
 * The program itself is not replaced: only the version it reports changes. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_app_desc.h"
#include "esp_https_ota.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_log.h"
#include "sim_priv.h"

#define SLOT_BYTES 0x300000             /* ota_0 / ota_1 size in partitions.csv */
#define APP_DESC_OFFSET 32              /* image header (24) + first segment header (8) */

static const char *TAG = "sim_ota";
static char s_running[32], s_previous[32], s_state[12];

static void save(void)
{
    char path[240];
    snprintf(path, sizeof(path), "%s/ota.txt", g_sim.dir);
    FILE *f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "%s %s %s\n", s_running, s_previous[0] ? s_previous : "-", s_state);
    fclose(f);
}

void sim_ota_load(void)
{
    char path[240];
    snprintf(path, sizeof(path), "%s/ota.txt", g_sim.dir);
    FILE *f = fopen(path, "r");
    if (!f || fscanf(f, "%31s %31s %11s", s_running, s_previous, s_state) != 3) {
        snprintf(s_running, sizeof(s_running), "%s", esp_app_get_description()->version);
        strcpy(s_previous, "-");
        strcpy(s_state, "valid");
    }
    if (f) fclose(f);

    if (!strcmp(s_state, "new")) {
        strcpy(s_state, "pending");
        save();
    } else if (!strcmp(s_state, "pending")) {
        printf("sim bootloader: firmware %s was not confirmed; back to %s\n", s_running, s_previous);
        snprintf(s_running, sizeof(s_running), "%s", s_previous);
        strcpy(s_previous, "-");
        strcpy(s_state, "valid");
        save();
    }
}

const char *sim_ota_running(void) { return s_running; }
bool sim_ota_pending(void) { return strcmp(s_state, "valid") != 0; }

const esp_partition_t *esp_ota_get_running_partition(void)
{
    static esp_partition_t running;
    return &running;
}

esp_err_t esp_ota_get_state_partition(const esp_partition_t *partition, esp_ota_img_states_t *ota_state)
{
    *ota_state = !strcmp(s_state, "pending") ? ESP_OTA_IMG_PENDING_VERIFY : ESP_OTA_IMG_VALID;
    return ESP_OK;
}

esp_err_t esp_ota_mark_app_valid_cancel_rollback(void)
{
    if (strcmp(s_state, "valid") != 0) {
        strcpy(s_state, "valid");
        save();
    }
    return ESP_OK;
}

esp_err_t esp_ota_mark_app_invalid_rollback_and_reboot(void)
{
    if (strcmp(s_state, "pending") != 0) return ESP_FAIL;
    snprintf(s_running, sizeof(s_running), "%s", s_previous);
    strcpy(s_previous, "-");
    strcpy(s_state, "valid");
    save();
    esp_restart();
    return ESP_OK;
}

esp_err_t esp_https_ota(const esp_https_ota_config_t *ota_config)
{
    esp_err_t err = ESP_FAIL;
    uint8_t *image = malloc(SLOT_BYTES);
    esp_http_client_handle_t http = esp_http_client_init(ota_config->http_config);
    if (!image || !http) goto done;
    if (esp_http_client_open(http, 0) != ESP_OK) goto done;
    esp_http_client_fetch_headers(http);
    if (esp_http_client_get_status_code(http) != 200) goto done;

    int total = 0, n;
    while ((n = esp_http_client_read(http, (char *)image + total, SLOT_BYTES - total)) > 0) total += n;
    if (n < 0 || total >= SLOT_BYTES) goto done;

    /* The checks a real unit makes before it accepts an image: header and app description. */
    static const uint8_t desc_magic[4] = { 0x32, 0x54, 0xCD, 0xAB };
    const esp_app_desc_t *desc = (const esp_app_desc_t *)(image + APP_DESC_OFFSET);
    if (total < APP_DESC_OFFSET + (int)sizeof(*desc) || image[0] != 0xE9 ||
        memcmp(image + APP_DESC_OFFSET, desc_magic, 4) != 0 || !memchr(desc->version, 0, sizeof(desc->version))) {
        ESP_LOGE(TAG, "downloaded file is not a firmware image");
        goto done;
    }

    snprintf(s_previous, sizeof(s_previous), "%s", s_running);
    snprintf(s_running, sizeof(s_running), "%s", desc->version);
    strcpy(s_state, "new");
    save();
    ESP_LOGI(TAG, "image %s written (%d bytes)", s_running, total);
    err = ESP_OK;

done:
    if (http) {
        esp_http_client_close(http);
        esp_http_client_cleanup(http);
    }
    free(image);
    return err;
}
