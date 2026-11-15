/* Setup mode: the unit becomes a WPA2 access point "Button-XXXX" with a
 * per-device password, and serves a setup page at http://192.168.4.1/.
 * A tiny DNS responder sends every host name there so phones open the page. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_http_server.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_log.h"
#include <sys/socket.h>
#include <netinet/in.h>
#include "cJSON.h"
#include "sdkconfig.h"
#include "app.h"

#define SETUP_TIMEOUT_MS (10 * 60 * 1000)

static const char *TAG = "setup";
extern const char portal_html_start[] asm("_binary_portal_html_start");
extern const char portal_html_end[] asm("_binary_portal_html_end");
static volatile bool s_saved;

/* Per-device AP password, created on first use and printed on the unit's label. */
static char *ap_password(void)
{
    char *pw = cfg_get_str("ap_pass");
    if (pw) return pw;
    static const char alphabet[] = "abcdefghjkmnpqrstuvwxyz23456789";   /* no look-alikes */
    pw = malloc(11);
    for (int i = 0; i < 10; i++) pw[i] = alphabet[esp_random() % (sizeof(alphabet) - 1)];
    pw[10] = 0;
    cfg_set_str("ap_pass", pw);
    return pw;
}

static esp_err_t send_json(httpd_req_t *req, cJSON *json)
{
    char *text = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t err = httpd_resp_sendstr(req, text ? text : "{}");
    free(text);
    return err;
}

static esp_err_t h_root(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    /* EMBED_TXTFILES appends a NUL; do not send it. */
    return httpd_resp_send(req, portal_html_start, portal_html_end - portal_html_start - 1);
}

static esp_err_t h_info(httpd_req_t *req)
{
    char *name = cfg_get_str("name"), *host = cfg_get_str("host");
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "serial", identity_serial());
    cJSON_AddStringToObject(j, "fw", fw_version());
    cJSON_AddStringToObject(j, "name", name ? name : "");
    cJSON_AddStringToObject(j, "host", host ? host : CONFIG_BTN_DEFAULT_SERVER_HOST);
    cJSON_AddBoolToObject(j, "enrolled", identity_has_cert());
    free(name);
    free(host);
    return send_json(req, j);
}

static esp_err_t h_scan(httpd_req_t *req)
{
    uint16_t n = 30;
    wifi_ap_record_t *recs = calloc(n, sizeof(*recs));
    cJSON *arr = cJSON_CreateArray();
    if (recs && esp_wifi_scan_start(NULL, true) == ESP_OK && esp_wifi_scan_get_ap_records(&n, recs) == ESP_OK) {
        for (int i = 0; i < n; i++) {
            cJSON *o = cJSON_CreateObject();
            cJSON_AddStringToObject(o, "ssid", (const char *)recs[i].ssid);
            cJSON_AddNumberToObject(o, "rssi", recs[i].rssi);
            cJSON_AddBoolToObject(o, "band5", recs[i].primary > 14);
            cJSON_AddBoolToObject(o, "secure", wifi_auth_ok(recs[i].authmode));
            cJSON_AddItemToArray(arr, o);
        }
    }
    free(recs);
    return send_json(req, arr);
}

static const char *field(cJSON *j, const char *key, size_t min, size_t max)
{
    cJSON *v = cJSON_GetObjectItem(j, key);
    if (!cJSON_IsString(v)) return NULL;
    size_t len = strlen(v->valuestring);
    return (len >= min && len <= max) ? v->valuestring : NULL;
}

static esp_err_t fail(httpd_req_t *req, cJSON *body, const char *why)
{
    cJSON_Delete(body);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "ok", false);
    cJSON_AddStringToObject(j, "error", why);
    return send_json(req, j);
}

static esp_err_t h_save(httpd_req_t *req)
{
    char buf[768];
    if (req->content_len == 0 || req->content_len >= sizeof(buf)) return fail(req, NULL, "Request too large.");
    int got = 0;
    while (got < (int)req->content_len) {
        int r = httpd_req_recv(req, buf + got, req->content_len - got);
        if (r <= 0) return ESP_FAIL;
        got += r;
    }
    buf[got] = 0;
    cJSON *j = cJSON_Parse(buf);
    if (!j) return fail(req, NULL, "Bad request.");

    const char *name = field(j, "name", 1, 32);
    const char *ssid = field(j, "ssid", 1, 32);
    const char *pass = field(j, "pass", 8, 63);
    const char *host = field(j, "host", 1, 128);
    const char *claim = field(j, "claim", 0, 64);
    if (!name) return fail(req, j, "Enter a button name (up to 32 characters).");
    if (!ssid) return fail(req, j, "Choose a Wi-Fi network.");
    if (!pass) return fail(req, j, "The Wi-Fi password must be 8 to 63 characters. Open networks are not allowed.");
    if (!host || strchr(host, '/') || strchr(host, ' ')) return fail(req, j, "Enter the server address as a host name only.");
    bool has_claim = claim && claim[0];
    if (!has_claim && !identity_has_cert()) return fail(req, j, "Enter the claim code from the server.");

    cfg_set_str("name", name);
    cfg_set_str("ssid", ssid);
    cfg_set_str("pass", pass);
    cfg_set_str("host", host);
    if (has_claim) {
        cfg_set_str("claim", claim);
        cfg_erase("cert");                  /* a new claim replaces the old identity */
        cfg_erase("key");
        cfg_erase("key_pend");
        cfg_erase("cert_pend");
    }
    cfg_set_u32("verified", 0);
    cJSON_Delete(j);

    cJSON *ok = cJSON_CreateObject();
    cJSON_AddBoolToObject(ok, "ok", true);
    esp_err_t err = send_json(req, ok);
    s_saved = true;
    return err;
}

/* Any other URL (captive-portal probes) redirects to the setup page. */
static esp_err_t h_redirect(httpd_req_t *req, httpd_err_code_t code)
{
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
    return httpd_resp_send(req, NULL, 0);
}

/* Answers every DNS A query with 192.168.4.1. */
static void dns_task(void *arg)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(53), .sin_addr.s_addr = htonl(INADDR_ANY) };
    if (sock < 0 || bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGW(TAG, "DNS responder not started");
        vTaskDelete(NULL);
    }
    static const uint8_t answer[] = { 0xC0, 0x0C, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4, 192, 168, 4, 1 };
    uint8_t pkt[512];
    for (;;) {
        struct sockaddr_in from;
        socklen_t flen = sizeof(from);
        int len = recvfrom(sock, pkt, sizeof(pkt) - sizeof(answer), 0, (struct sockaddr *)&from, &flen);
        if (len < 17 || (pkt[2] & 0x80) || pkt[4] != 0 || pkt[5] != 1) continue;   /* one question only */
        int p = 12;
        while (p < len && pkt[p] != 0) p += pkt[p] + 1;                           /* skip the name */
        p += 5;                                                                   /* zero byte, type, class */
        if (p > len) continue;
        pkt[2] = 0x81; pkt[3] = 0x80;                                             /* response, no error */
        pkt[6] = 0; pkt[7] = 1;                                                   /* one answer */
        pkt[8] = pkt[9] = pkt[10] = pkt[11] = 0;
        memcpy(pkt + p, answer, sizeof(answer));
        sendto(sock, pkt, p + sizeof(answer), 0, (struct sockaddr *)&from, flen);
    }
}

void provisioning_run(void)
{
    /* Start the radio first: the password generator needs it for true random numbers. */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    char *pw = ap_password();
    ESP_ERROR_CHECK(esp_wifi_stop());                        /* the AP must never start unconfigured (open) */
    const char *serial = identity_serial();
    wifi_config_t ap = { 0 };
    snprintf((char *)ap.ap.ssid, sizeof(ap.ap.ssid), "Button-%s", serial + strlen(serial) - 4);
    strlcpy((char *)ap.ap.password, pw, sizeof(ap.ap.password));
    ap.ap.channel = 1;
    ap.ap.max_connection = 2;
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));     /* STA side is only used for scanning */
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());
#if CONFIG_SOC_WIFI_SUPPORT_5G
    esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO);             /* list 5 GHz networks too */
#endif

    /* Shown on the console only; copy both lines to the unit's label at the bench. */
    printf("\n*** SETUP MODE ***\n  Wi-Fi name:     %s\n  Wi-Fi password: %s\n  Then open http://192.168.4.1/\n\n",
           (char *)ap.ap.ssid, pw);

    httpd_config_t hc = HTTPD_DEFAULT_CONFIG();
    hc.stack_size = 8192;
    hc.server_port = CONFIG_BTN_SETUP_HTTP_PORT;
    hc.lru_purge_enable = true;
    httpd_handle_t srv = NULL;
    ESP_ERROR_CHECK(httpd_start(&srv, &hc));
    static const httpd_uri_t routes[] = {
        { .uri = "/", .method = HTTP_GET, .handler = h_root },
        { .uri = "/info", .method = HTTP_GET, .handler = h_info },
        { .uri = "/scan", .method = HTTP_GET, .handler = h_scan },
        { .uri = "/save", .method = HTTP_POST, .handler = h_save },
    };
    for (size_t i = 0; i < sizeof(routes) / sizeof(routes[0]); i++) httpd_register_uri_handler(srv, &routes[i]);
    httpd_register_err_handler(srv, HTTPD_404_NOT_FOUND, h_redirect);
    xTaskCreate(dns_task, "dns", 4096, NULL, 4, NULL);

    for (uint32_t waited = 0; !s_saved && waited < SETUP_TIMEOUT_MS; waited += 200) vTaskDelay(pdMS_TO_TICKS(200));
    vTaskDelay(pdMS_TO_TICKS(1500));                         /* let the reply reach the browser */
    ESP_LOGI(TAG, "%s; restarting", s_saved ? "settings saved" : "setup timed out");
    esp_restart();
    for (;;) vTaskDelay(portMAX_DELAY);
}
