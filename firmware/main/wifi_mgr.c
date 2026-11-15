/* Station-mode Wi-Fi. Joins only WPA2/WPA3-personal networks and prefers a
 * 5 GHz access point when one with usable signal carries the configured SSID. */
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_log.h"
#include "sdkconfig.h"
#include "app.h"

#define BIT_CONNECTED BIT0
#define BIT_DISCONNECTED BIT1
#define MIN_5G_RSSI (-78)
#define BONUS_5G 15

static const char *TAG = "wifi";
static EventGroupHandle_t s_eg;
static int s_fail;

bool wifi_auth_ok(int authmode)
{
    switch (authmode) {
    case WIFI_AUTH_WPA2_PSK:
    case WIFI_AUTH_WPA_WPA2_PSK:
    case WIFI_AUTH_WPA3_PSK:
    case WIFI_AUTH_WPA2_WPA3_PSK:
        return true;
    default:
        return false;      /* open, WEP, WPA1-only, OWE, enterprise: refused */
    }
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_eg, BIT_CONNECTED);
        xEventGroupSetBits(s_eg, BIT_DISCONNECTED);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_eg, BIT_CONNECTED);
    }
}

void wifi_mgr_init(void)
{
    s_eg = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&ic));
    esp_wifi_set_storage(WIFI_STORAGE_RAM);          /* credentials live in our (encrypted) NVS only */
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL);
}

/* Scan for the SSID and choose the best acceptable access point. */
static bool pick_ap(const char *ssid, wifi_ap_record_t *best)
{
    wifi_scan_config_t sc = { .ssid = (uint8_t *)ssid, .show_hidden = true };
    if (esp_wifi_scan_start(&sc, true) != ESP_OK) return false;
    uint16_t n = 16;
    wifi_ap_record_t *recs = calloc(n, sizeof(*recs));
    if (!recs || esp_wifi_scan_get_ap_records(&n, recs) != ESP_OK) {
        free(recs);
        return false;
    }
    int best_score = -1000;
    for (int i = 0; i < n; i++) {
        if (!wifi_auth_ok(recs[i].authmode)) continue;
        bool is_5g = recs[i].primary > 14;
        if (is_5g && recs[i].rssi < MIN_5G_RSSI) continue;
        int score = recs[i].rssi + (is_5g ? BONUS_5G : 0);
        if (score > best_score) {
            best_score = score;
            *best = recs[i];
        }
    }
    free(recs);
    return best_score > -1000;
}

static void wifi_task(void *arg)
{
    char *ssid = cfg_get_str("ssid"), *pass = cfg_get_str("pass");
    for (;;) {
        wifi_ap_record_t ap;
        if (ssid && pass && pick_ap(ssid, &ap)) {
            wifi_config_t wc = { 0 };
            strlcpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid));
            strlcpy((char *)wc.sta.password, pass, sizeof(wc.sta.password));
            memcpy(wc.sta.bssid, ap.bssid, 6);
            wc.sta.bssid_set = true;
            wc.sta.channel = ap.primary;
            wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
            wc.sta.pmf_cfg.capable = true;
            wc.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
            esp_wifi_set_config(WIFI_IF_STA, &wc);
            xEventGroupClearBits(s_eg, BIT_CONNECTED | BIT_DISCONNECTED);
            ESP_LOGI(TAG, "joining %s on channel %d (%d dBm)", ssid, ap.primary, ap.rssi);
            esp_wifi_connect();
            EventBits_t bits = xEventGroupWaitBits(s_eg, BIT_CONNECTED | BIT_DISCONNECTED, pdFALSE, pdFALSE,
                                                   pdMS_TO_TICKS(30000));
            if (bits & BIT_CONNECTED) {
                s_fail = 0;
                if (!cfg_get_u32("verified", 0)) cfg_set_u32("verified", 1);
                xEventGroupWaitBits(s_eg, BIT_DISCONNECTED, pdFALSE, pdFALSE, portMAX_DELAY);
                ESP_LOGW(TAG, "connection lost");
            } else {
                s_fail++;
                esp_wifi_disconnect();
            }
        } else {
            s_fail++;
            ESP_LOGW(TAG, "no WPA2/WPA3 access point found for the configured network");
        }
        int wait_s = s_fail == 0 ? 1 : (s_fail > 4 ? 30 : 2 << s_fail);
        vTaskDelay(pdMS_TO_TICKS(wait_s * 1000));
    }
}

void wifi_mgr_start_sta(void)
{
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
#if CONFIG_SOC_WIFI_SUPPORT_5G
    esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO);     /* scan and join on both bands */
#endif
    xTaskCreate(wifi_task, "wifi", 4096, NULL, 5, NULL);
}

bool wifi_mgr_wait_connected(uint32_t timeout_ms)
{
    return xEventGroupWaitBits(s_eg, BIT_CONNECTED, pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_ms)) & BIT_CONNECTED;
}

bool wifi_mgr_connected(void) { return xEventGroupGetBits(s_eg) & BIT_CONNECTED; }
int wifi_mgr_fail_count(void) { return s_fail; }
