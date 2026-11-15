/* Persistent MQTT-over-TLS link with client-certificate authentication.
 * Sends queued presses, publishes status, and runs server commands
 * (firmware update, sound update, certificate renewal). See docs/protocol.md. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "mqtt_client.h"
#include "esp_crt_bundle.h"
#include "esp_https_ota.h"
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_log.h"
#include "cJSON.h"
#include "sdkconfig.h"
#include "app.h"

#define BIT_CONNECTED BIT0
#define ACK_TIMEOUT_MS 10000

typedef enum { JOB_OTA, JOB_AUDIO, JOB_CERT, JOB_REBOOT } job_type_t;
typedef struct {
    job_type_t type;
    char *a;     /* url or certificate (malloc'd) */
    char *b;     /* version or sha256 (malloc'd) */
} job_t;

static const char *TAG = "mqtt";
static esp_mqtt_client_handle_t s_client;
static EventGroupHandle_t s_eg;
static QueueHandle_t s_acks, s_jobs;
static TaskHandle_t s_sender;
static bool s_ever_connected;
static char s_t_press[48], s_t_status[48], s_t_cmd[48], s_t_csr[48];

static void publish_json(const char *topic, cJSON *j, int qos, int retain)
{
    char *text = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    if (text) esp_mqtt_client_enqueue(s_client, topic, text, 0, qos, retain, true);
    free(text);
}

void mqtt_link_publish_status(void)
{
    if (!mqtt_link_connected()) return;
    char *name = cfg_get_str("name"), *sha = cfg_get_str("audio_sha");
    wifi_ap_record_t ap = { 0 };
    esp_netif_ip_info_t ip = { 0 };
    char ipstr[16];
    esp_wifi_sta_get_ap_info(&ap);
    esp_netif_get_ip_info(esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"), &ip);
    esp_ip4addr_ntoa(&ip.ip, ipstr, sizeof(ipstr));

    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "online", true);
    cJSON_AddStringToObject(j, "serial", identity_serial());
    cJSON_AddStringToObject(j, "name", name ? name : "");
    cJSON_AddStringToObject(j, "fw", esp_app_get_description()->version);
    cJSON_AddNumberToObject(j, "rssi", ap.rssi);
    cJSON_AddNumberToObject(j, "channel", ap.primary);
    cJSON_AddStringToObject(j, "ip", ipstr);
    cJSON_AddNumberToObject(j, "uptime_s", (double)(esp_timer_get_time() / 1000000));
    cJSON_AddNumberToObject(j, "pending", evq_pending());
    cJSON_AddNumberToObject(j, "cert_not_after", (double)identity_cert_not_after());
    cJSON_AddStringToObject(j, "audio_sha", sha ? sha : "");
    cJSON_AddBoolToObject(j, "nc_fault", button_nc_fault());
    publish_json(s_t_status, j, 1, 1);
    free(name);
    free(sha);
}

void mqtt_link_send_csr(const char *csr_pem)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "csr", csr_pem);
    publish_json(s_t_csr, j, 1, 0);
}

/* ---- presses: one at a time, removed from the queue only when the broker acknowledges ---- */

static void sender_task(void *arg)
{
    for (;;) {
        xEventGroupWaitBits(s_eg, BIT_CONNECTED, pdFALSE, pdTRUE, portMAX_DELAY);
        evq_item_t it;
        if (!evq_peek(&it)) {
            ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000));
            continue;
        }
        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "seq", it.seq);
        cJSON_AddNumberToObject(j, "boot", it.boot_id);
        int64_t age_ms = -1;
        if (it.boot_id == g_boot_id) {
            age_ms = esp_timer_get_time() / 1000 - it.up_ms;
            cJSON_AddNumberToObject(j, "age_ms", (double)age_ms);
        }
        /* Seconds with a millisecond fraction, so the server can measure delivery delay. */
        if (it.ts > 0) cJSON_AddNumberToObject(j, "ts", (double)it.ts / 1000.0);
        else if (age_ms >= 0 && time_is_valid()) cJSON_AddNumberToObject(j, "ts", (double)(now_ms() - age_ms) / 1000.0);
        char *text = cJSON_PrintUnformatted(j);
        cJSON_Delete(j);
        if (!text) continue;

        xQueueReset(s_acks);
        int msg_id = esp_mqtt_client_publish(s_client, s_t_press, text, 0, 1, 0);
        free(text);
        if (msg_id < 0) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(ACK_TIMEOUT_MS);
        int acked;
        for (;;) {
            TickType_t now = xTaskGetTickCount();
            if ((int32_t)(deadline - now) <= 0 || xQueueReceive(s_acks, &acked, deadline - now) != pdTRUE) break;
            if (acked == msg_id) {
                evq_pop();
                break;
            }
        }
        /* No acknowledgement: the same press is sent again; the server ignores repeats by seq. */
    }
}

void mqtt_link_notify_press(void)
{
    if (s_sender) xTaskNotifyGive(s_sender);
}

/* ---- commands ---- */

static void queue_job(job_type_t type, const char *a, const char *b)
{
    job_t job = { .type = type, .a = a ? strdup(a) : NULL, .b = b ? strdup(b) : NULL };
    if (xQueueSend(s_jobs, &job, 0) != pdTRUE) {
        free(job.a);
        free(job.b);
    }
}

static const char *str_field(cJSON *j, const char *key)
{
    cJSON *v = cJSON_GetObjectItem(j, key);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}

static void handle_cmd(const char *data, int len)
{
    cJSON *j = cJSON_ParseWithLength(data, len);
    const char *type = j ? str_field(j, "type") : NULL;
    if (!type) {
        cJSON_Delete(j);
        return;
    }
    const char *url = str_field(j, "url");
    if (!strcmp(type, "ota")) {
        const char *ver = str_field(j, "version");
        if (url && ver && strcmp(ver, esp_app_get_description()->version) != 0) queue_job(JOB_OTA, url, ver);
    } else if (!strcmp(type, "audio")) {
        const char *sha = str_field(j, "sha256");
        char *have = cfg_get_str("audio_sha");
        if (url && sha && (!have || strcasecmp(have, sha) != 0)) queue_job(JOB_AUDIO, url, sha);
        free(have);
    } else if (!strcmp(type, "cert")) {
        const char *cert = str_field(j, "cert");
        if (cert) queue_job(JOB_CERT, cert, NULL);
    } else if (!strcmp(type, "name")) {
        const char *name = str_field(j, "name");
        if (name && name[0] && strlen(name) <= 32) {
            cfg_set_str("name", name);
            mqtt_link_publish_status();
        }
    } else if (!strcmp(type, "identify")) {
        audio_play();
    } else if (!strcmp(type, "reboot")) {
        queue_job(JOB_REBOOT, NULL, NULL);
    }
    cJSON_Delete(j);
}

/* Long-running commands run here, not in the MQTT task. */
static void job_task(void *arg)
{
    job_t job;
    for (;;) {
        xQueueReceive(s_jobs, &job, portMAX_DELAY);
        bool reboot = false;
        switch (job.type) {
        case JOB_OTA: {
            if (strncmp(job.a, "https://", 8) != 0) break;
            ESP_LOGI(TAG, "updating firmware to %s", job.b);
            esp_http_client_config_t hc = {
                .url = job.a, .crt_bundle_attach = esp_crt_bundle_attach,
                .timeout_ms = 15000, .keep_alive_enable = true,
            };
            esp_https_ota_config_t oc = { .http_config = &hc };
            if (esp_https_ota(&oc) == ESP_OK) reboot = true;
            else ESP_LOGE(TAG, "firmware update failed; staying on current version");
            break;
        }
        case JOB_AUDIO:
            if (audio_update(job.a, job.b) == ESP_OK) mqtt_link_publish_status();
            break;
        case JOB_CERT:
            if (identity_install_renewed(job.a) == ESP_OK) {
                ESP_LOGI(TAG, "client certificate renewed");
                reboot = true;
            }
            break;
        case JOB_REBOOT:
            reboot = true;
            break;
        }
        free(job.a);
        free(job.b);
        if (reboot) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            esp_restart();
        }
    }
}

/* ---- connection ---- */

static void on_mqtt(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_mqtt_event_handle_t ev = data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "connected");
        xEventGroupSetBits(s_eg, BIT_CONNECTED);
        if (!s_ever_connected) {
            s_ever_connected = true;
            esp_ota_mark_app_valid_cancel_rollback();    /* this firmware can reach the server: keep it */
        }
        esp_mqtt_client_subscribe(s_client, s_t_cmd, 1);
        mqtt_link_publish_status();
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "disconnected");
        xEventGroupClearBits(s_eg, BIT_CONNECTED);
        break;
    case MQTT_EVENT_PUBLISHED: {
        int msg_id = ev->msg_id;
        xQueueSend(s_acks, &msg_id, 0);
        break;
    }
    case MQTT_EVENT_DATA:
        /* Commands fit in one buffer; fragmented messages are ignored. */
        if (ev->current_data_offset == 0 && ev->data_len == ev->total_data_len) handle_cmd(ev->data, ev->data_len);
        break;
    default:
        break;
    }
}

void mqtt_link_start(void)
{
    static char uri[180];
    const char *serial = identity_serial();
    char *host = cfg_get_str("host");
    char *cert = cfg_get_str("cert"), *key = cfg_get_str("key");   /* kept for the life of the client */
    snprintf(uri, sizeof(uri), "mqtts://%s:%d", host ? host : "", CONFIG_BTN_MQTT_PORT);
    snprintf(s_t_press, sizeof(s_t_press), "btn/%s/press", serial);
    snprintf(s_t_status, sizeof(s_t_status), "btn/%s/status", serial);
    snprintf(s_t_cmd, sizeof(s_t_cmd), "btn/%s/cmd/#", serial);
    snprintf(s_t_csr, sizeof(s_t_csr), "btn/%s/csr", serial);
    free(host);

    s_eg = xEventGroupCreate();
    s_acks = xQueueCreate(8, sizeof(int));
    s_jobs = xQueueCreate(4, sizeof(job_t));

    esp_mqtt_client_config_t mc = {
        .broker.address.uri = uri,
        .broker.verification.crt_bundle_attach = esp_crt_bundle_attach,
        .credentials.client_id = serial,
        .credentials.authentication.certificate = cert,
        .credentials.authentication.key = key,
        .session.keepalive = 30,
        .session.disable_clean_session = true,
        .session.last_will = { .topic = s_t_status, .msg = "{\"online\":false}", .qos = 1, .retain = 1 },
        .network.reconnect_timeout_ms = 5000,
        .buffer.size = 6144,
        .task.stack_size = 8192,
    };
    s_client = esp_mqtt_client_init(&mc);
    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, on_mqtt, NULL);
    esp_mqtt_client_start(s_client);

    xTaskCreate(sender_task, "press_tx", 4096, NULL, 6, &s_sender);
    xTaskCreate(job_task, "jobs", 8192, NULL, 4, NULL);
}

bool mqtt_link_connected(void) { return s_eg && (xEventGroupGetBits(s_eg) & BIT_CONNECTED); }
bool mqtt_link_ever_connected(void) { return s_ever_connected; }
