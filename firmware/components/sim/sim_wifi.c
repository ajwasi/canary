/* Simulated Wi-Fi radio and the access points around the unit. PC test build only.
 *
 * The networks match emulator/button_emulator.py. Joining succeeds only when the site
 * Wi-Fi is "up", the chosen access point exists, is secured and the password is right.
 * The unit's TCP traffic is real; the test harness cuts it when the site Wi-Fi is "down". */
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "sim_priv.h"

ESP_EVENT_DEFINE_BASE(WIFI_EVENT);
ESP_EVENT_DEFINE_BASE(IP_EVENT);

typedef struct {
    const char *ssid, *password;        /* password NULL = open network */
    int channel, rssi;
    wifi_auth_mode_t auth;
} net_t;

static const net_t NETS[] = {
    { "Warehouse-Staff", "blue-forklift-42", 44, -48, WIFI_AUTH_WPA2_WPA3_PSK },
    { "Warehouse-Staff", "blue-forklift-42", 6, -44, WIFI_AUTH_WPA2_PSK },
    { "Scanner-Net", "scan-scan-scan", 11, -61, WIFI_AUTH_WPA2_PSK },
    { "Tulsa-Guest", NULL, 1, -57, WIFI_AUTH_OPEN },
};
#define NET_COUNT ((int)(sizeof(NETS) / sizeof(NETS[0])))

static wifi_mode_t s_mode;
static volatile int s_started, s_connected, s_connect_at_tick, s_connect_pending;
static int s_joined = -1;
static wifi_sta_config_t s_sta;
static wifi_ap_config_t s_ap;
static wifi_ap_record_t s_scan[NET_COUNT];
static int s_scan_count;

static void record_for(int i, wifi_ap_record_t *r)
{
    memset(r, 0, sizeof(*r));
    r->bssid[0] = 0x02;
    r->bssid[5] = (uint8_t)(i + 1);
    snprintf((char *)r->ssid, sizeof(r->ssid), "%s", NETS[i].ssid);
    r->primary = (uint8_t)NETS[i].channel;
    r->rssi = (int8_t)NETS[i].rssi;
    r->authmode = NETS[i].auth;
}

esp_err_t esp_netif_init(void) { return ESP_OK; }
esp_netif_t *esp_netif_create_default_wifi_sta(void) { return (esp_netif_t *)&s_sta; }
esp_netif_t *esp_netif_create_default_wifi_ap(void) { return (esp_netif_t *)&s_ap; }
esp_netif_t *esp_netif_get_handle_from_ifkey(const char *if_key) { return (esp_netif_t *)&s_sta; }

esp_err_t esp_netif_get_ip_info(esp_netif_t *esp_netif, esp_netif_ip_info_t *ip_info)
{
    static const uint8_t ip[4] = { 192, 168, 50, 23 };
    memset(ip_info, 0, sizeof(*ip_info));
    if (s_connected) memcpy(&ip_info->ip.addr, ip, 4);
    return ESP_OK;
}

char *esp_ip4addr_ntoa(const esp_ip4_addr_t *addr, char *buf, int buflen)
{
    const uint8_t *b = (const uint8_t *)&addr->addr;
    snprintf(buf, buflen, "%d.%d.%d.%d", b[0], b[1], b[2], b[3]);
    return buf;
}

esp_err_t esp_wifi_init(const wifi_init_config_t *config)
{
    sim_start_tasks();
    return ESP_OK;
}

esp_err_t esp_wifi_set_storage(wifi_storage_t storage) { return ESP_OK; }

esp_err_t esp_wifi_set_mode(wifi_mode_t mode)
{
    s_mode = mode;
    return ESP_OK;
}

esp_err_t esp_wifi_start(void)
{
    s_started = 1;
    return ESP_OK;
}

esp_err_t esp_wifi_stop(void)
{
    s_started = s_connected = s_connect_pending = 0;
    s_joined = -1;
    return ESP_OK;
}

esp_err_t esp_wifi_set_config(wifi_interface_t interface, wifi_config_t *conf)
{
    if (interface == WIFI_IF_AP) s_ap = conf->ap;
    else s_sta = conf->sta;
    return ESP_OK;
}

esp_err_t esp_wifi_scan_start(const wifi_scan_config_t *config, bool block)
{
    if (!s_started) return ESP_FAIL;
    vTaskDelay(pdMS_TO_TICKS(150));
    s_scan_count = 0;
    for (int i = 0; i < NET_COUNT && g_sim.wifi_up; i++) {
        if (config && config->ssid && strcmp((const char *)config->ssid, NETS[i].ssid) != 0) continue;
        record_for(i, &s_scan[s_scan_count++]);
    }
    return ESP_OK;
}

esp_err_t esp_wifi_scan_get_ap_records(uint16_t *number, wifi_ap_record_t *ap_records)
{
    if (*number > s_scan_count) *number = (uint16_t)s_scan_count;
    memcpy(ap_records, s_scan, *number * sizeof(*ap_records));
    return ESP_OK;
}

esp_err_t esp_wifi_connect(void)
{
    if (!s_started) return ESP_FAIL;
    s_connect_pending = 4;              /* answered by sim_wifi_tick about 200 ms later */
    return ESP_OK;
}

esp_err_t esp_wifi_disconnect(void)
{
    s_connected = s_connect_pending = 0;
    s_joined = -1;
    return ESP_OK;
}

esp_err_t esp_wifi_sta_get_ap_info(wifi_ap_record_t *ap_info)
{
    if (!s_connected || s_joined < 0) return ESP_FAIL;
    record_for(s_joined, ap_info);
    return ESP_OK;
}

/* Which access point the station settings lead to, or -1 if joining fails. */
static int try_join(void)
{
    if (!g_sim.wifi_up) return -1;
    char ssid[33] = "", pass[65] = "";
    memcpy(ssid, s_sta.ssid, 32);
    memcpy(pass, s_sta.password, 64);
    for (int i = 0; i < NET_COUNT; i++) {
        wifi_ap_record_t r;
        record_for(i, &r);
        if (strcmp(ssid, NETS[i].ssid) != 0) continue;
        if (s_sta.bssid_set && memcmp(s_sta.bssid, r.bssid, 6) != 0) continue;
        if (!NETS[i].password || NETS[i].auth < s_sta.threshold.authmode) continue;
        if (strcmp(pass, NETS[i].password) != 0) continue;
        return i;
    }
    return -1;
}

void sim_wifi_tick(void)
{
    if (s_connected && !g_sim.wifi_up) {                /* the access point went away */
        s_connected = 0;
        s_joined = -1;
        esp_event_post(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, NULL, 0, portMAX_DELAY);
    }
    if (s_connect_pending) {
        s_connect_pending--;
        if (s_connect_pending == 0) {
            s_joined = try_join();
            if (s_joined >= 0) {
                s_connected = 1;
                esp_event_post(IP_EVENT, IP_EVENT_STA_GOT_IP, NULL, 0, portMAX_DELAY);
            } else {
                esp_event_post(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, NULL, 0, portMAX_DELAY);
            }
        }
    }
    /* SNTP keeps trying in the background, as on the chip. */
    if (g_sim.sntp_started && g_sim.clock_up && s_connected && !g_sim.clock_set) g_sim.clock_set = 1;
}

void sim_wifi_state(cJSON *j)
{
    bool ap = s_started && s_mode == WIFI_MODE_APSTA;
    cJSON_AddBoolToObject(j, "sta", s_connected);
    cJSON_AddNumberToObject(j, "channel", s_connected && s_joined >= 0 ? NETS[s_joined].channel : 0);
    cJSON_AddNumberToObject(j, "rssi", s_connected && s_joined >= 0 ? NETS[s_joined].rssi : 0);
    cJSON_AddBoolToObject(j, "ap", ap);
    if (ap) {
        char ssid[33] = "", pass[65] = "";
        memcpy(ssid, s_ap.ssid, 32);
        memcpy(pass, s_ap.password, 64);
        cJSON_AddStringToObject(j, "ap_ssid", ssid);
        cJSON_AddStringToObject(j, "ap_pass", pass);
        cJSON_AddBoolToObject(j, "ap_secure", s_ap.authmode >= WIFI_AUTH_WPA2_PSK && pass[0] != 0);
    }
}
