/* Press queue kept in NVS so presses made while offline survive a reboot.
 * "head" is the oldest unsent sequence number, "tail" the next one to assign. */
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_timer.h"
#include "nvs.h"
#include "app.h"

#define EVQ_MAX 200

static nvs_handle_t s_nvs;
static SemaphoreHandle_t s_lock;
static uint32_t s_head, s_tail;

static void key_for(uint32_t seq, char *key) { snprintf(key, 16, "e%08lx", (unsigned long)seq); }

static void drop_head(void)
{
    char key[16];
    key_for(s_head, key);
    nvs_erase_key(s_nvs, key);
    s_head++;
    nvs_set_u32(s_nvs, "head", s_head);
}

void evq_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    ESP_ERROR_CHECK(nvs_open("evq", NVS_READWRITE, &s_nvs));
    nvs_get_u32(s_nvs, "head", &s_head);
    nvs_get_u32(s_nvs, "tail", &s_tail);
}

void evq_push(void)
{
    evq_item_t it = {
        .boot_id = g_boot_id,
        .ts = time_is_valid() ? now_ms() : 0,
        .up_ms = esp_timer_get_time() / 1000,
    };
    char key[16];
    xSemaphoreTake(s_lock, portMAX_DELAY);
    it.seq = s_tail;
    key_for(it.seq, key);
    nvs_set_blob(s_nvs, key, &it, sizeof(it));
    s_tail++;
    nvs_set_u32(s_nvs, "tail", s_tail);
    while (s_tail - s_head > EVQ_MAX) drop_head();   /* keep the newest EVQ_MAX */
    nvs_commit(s_nvs);
    xSemaphoreGive(s_lock);
}

bool evq_peek(evq_item_t *out)
{
    bool found = false;
    char key[16];
    xSemaphoreTake(s_lock, portMAX_DELAY);
    while (s_head != s_tail) {
        size_t len = sizeof(*out);
        key_for(s_head, key);
        if (nvs_get_blob(s_nvs, key, out, &len) == ESP_OK && len == sizeof(*out)) {
            found = true;
            break;
        }
        drop_head();                                  /* unreadable entry: skip it */
        nvs_commit(s_nvs);
    }
    xSemaphoreGive(s_lock);
    return found;
}

void evq_pop(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (s_head != s_tail) {
        drop_head();
        nvs_commit(s_nvs);
    }
    xSemaphoreGive(s_lock);
}

uint32_t evq_pending(void) { return s_tail - s_head; }
