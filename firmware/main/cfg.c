/* Settings stored in NVS namespace "cfg". With sdkconfig.prod.defaults NVS is encrypted. */
#include <stdlib.h>
#include <sys/time.h>
#include "nvs_flash.h"
#include "nvs.h"
#include "app.h"

static nvs_handle_t s_nvs;
uint32_t g_boot_id;

int64_t now_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

esp_err_t cfg_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) return err;
    err = nvs_open("cfg", NVS_READWRITE, &s_nvs);
    if (err != ESP_OK) return err;
    g_boot_id = cfg_get_u32("boot", 0) + 1;
    cfg_set_u32("boot", g_boot_id);
    return ESP_OK;
}

char *cfg_get_str(const char *key)
{
    size_t len = 0;
    if (nvs_get_str(s_nvs, key, NULL, &len) != ESP_OK || len == 0) return NULL;
    char *buf = malloc(len);
    if (buf && nvs_get_str(s_nvs, key, buf, &len) != ESP_OK) {
        free(buf);
        buf = NULL;
    }
    return buf;
}

esp_err_t cfg_set_str(const char *key, const char *val)
{
    esp_err_t err = nvs_set_str(s_nvs, key, val);
    return err == ESP_OK ? nvs_commit(s_nvs) : err;
}

void cfg_erase(const char *key)
{
    nvs_erase_key(s_nvs, key);
    nvs_commit(s_nvs);
}

uint32_t cfg_get_u32(const char *key, uint32_t def)
{
    uint32_t v = def;
    nvs_get_u32(s_nvs, key, &v);
    return v;
}

void cfg_set_u32(const char *key, uint32_t val)
{
    nvs_set_u32(s_nvs, key, val);
    nvs_commit(s_nvs);
}
