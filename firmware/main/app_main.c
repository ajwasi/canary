/* Button notifier firmware entry point.
 *
 * Boot sequence:
 *   1. Load settings; start the button and the sound so presses work at once.
 *   2. No Wi-Fi settings, or button held for 10 s from power-up: run setup mode.
 *   3. Join Wi-Fi, set the clock, enrol for a client certificate if needed.
 *   4. Hold the MQTT link open; send presses; renew the certificate when due.
 */
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_netif_sntp.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_log.h"
#include "sdkconfig.h"
#include "board.h"
#include "app.h"

#define SETUP_HOLD_MS CONFIG_BTN_SETUP_HOLD_MS
#define ROLLBACK_AFTER_MS (CONFIG_BTN_ROLLBACK_AFTER_S * 1000LL)
#define STATUS_EVERY_S CONFIG_BTN_STATUS_EVERY_S
#define CSR_RETRY_S (6 * 3600)

static const char *TAG = "app";

/* ---- status LED (inside the box; useful on the bench) ---- */
typedef enum { LED_CONNECTING, LED_SETUP, LED_ONLINE, LED_FAULT } led_mode_t;
static volatile led_mode_t s_led = LED_CONNECTING;

static void led_task(void *arg)
{
    gpio_config_t io = { .pin_bit_mask = 1ULL << PIN_LED, .mode = GPIO_MODE_OUTPUT };
    gpio_config(&io);
    for (;;) {
        int on_ms, off_ms;
        switch (s_led) {
        case LED_SETUP:  on_ms = 100; off_ms = 100;  break;   /* fast blink */
        case LED_ONLINE: on_ms = 30;  off_ms = 4970; break;   /* brief flash every 5 s */
        case LED_FAULT:  on_ms = 900; off_ms = 100;  break;   /* mostly on */
        default:         on_ms = 500; off_ms = 500;  break;   /* slow blink */
        }
        gpio_set_level(PIN_LED, LED_ON_LEVEL);
        vTaskDelay(pdMS_TO_TICKS(on_ms));
        gpio_set_level(PIN_LED, !LED_ON_LEVEL);
        vTaskDelay(pdMS_TO_TICKS(off_ms));
    }
}

static void on_press(void)
{
    audio_play();
    evq_push();
    mqtt_link_notify_press();
}

/* True if the button is down now and stays down for the whole hold time. */
static bool setup_requested(void)
{
    if (!button_is_pressed()) return false;
    s_led = LED_SETUP;
    for (int waited = 0; waited < SETUP_HOLD_MS; waited += 50) {
        vTaskDelay(pdMS_TO_TICKS(50));
        if (!button_is_pressed()) {
            s_led = LED_CONNECTING;
            return false;
        }
    }
    return true;
}

static bool pending_verify(void)
{
    esp_ota_img_states_t st;
    return esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK &&
           st == ESP_OTA_IMG_PENDING_VERIFY;
}

/* A freshly updated firmware that cannot reach the server is rolled back. */
static void rollback_if_stuck(void)
{
    if (pending_verify() && !mqtt_link_ever_connected() && esp_timer_get_time() / 1000 > ROLLBACK_AFTER_MS) {
        ESP_LOGE(TAG, "new firmware never reached the server; rolling back");
        esp_ota_mark_app_invalid_rollback_and_reboot();
    }
}

static void enter_setup(void)
{
    s_led = LED_SETUP;
    provisioning_run();
}

void app_main(void)
{
    ESP_ERROR_CHECK(cfg_init());
    ESP_LOGI(TAG, "%s firmware %s, boot %lu", identity_serial(), fw_version(),
             (unsigned long)g_boot_id);
    xTaskCreate(led_task, "led", 2048, NULL, 2, NULL);
    evq_init();
    audio_init();
    button_init(on_press);
    wifi_mgr_init();

    char *ssid = cfg_get_str("ssid");
    bool have_wifi = ssid != NULL;
    free(ssid);
    if (!have_wifi || setup_requested()) enter_setup();

    /* ---- Wi-Fi ---- */
    wifi_mgr_start_sta();
    while (!wifi_mgr_wait_connected(5000)) {
        rollback_if_stuck();
        /* Settings that have never worked (for example a mistyped password) lead back to setup. */
        if (!cfg_get_u32("verified", 0) && wifi_mgr_fail_count() >= 6) {
            ESP_LOGE(TAG, "could not join the configured network; returning to setup mode");
            cfg_erase("ssid");
            cfg_erase("pass");
            esp_restart();
        }
    }

    /* ---- clock ---- */
    esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_netif_sntp_init(&sntp);
    if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(20000)) != ESP_OK) ESP_LOGW(TAG, "clock not set yet; continuing");

    /* ---- identity ---- */
    int rejected = 0;
    while (!identity_has_cert()) {
        int r = identity_enroll();
        if (r == 0) break;
        s_led = LED_FAULT;
        if (r == 1 && ++rejected >= 5) {
            ESP_LOGE(TAG, "claim code rejected; returning to setup mode");
            cfg_erase("claim");
            cfg_erase("ssid");
            cfg_erase("pass");
            esp_restart();
        }
        rollback_if_stuck();
        vTaskDelay(pdMS_TO_TICKS(CONFIG_BTN_ENROLL_RETRY_S * 1000));
    }

    /* ---- server link ---- */
    mqtt_link_start();
    int since_status = 0, since_csr = CSR_RETRY_S;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        since_status++;
        since_csr++;
        bool online = mqtt_link_connected();
        s_led = online ? LED_ONLINE : (wifi_mgr_connected() ? LED_FAULT : LED_CONNECTING);
        rollback_if_stuck();

        if (online && since_status >= STATUS_EVERY_S) {
            since_status = 0;
            mqtt_link_publish_status();
        }
        if (online && since_csr >= CSR_RETRY_S && identity_renew_due()) {
            char *csr = identity_begin_renew();
            if (csr) {
                ESP_LOGI(TAG, "certificate due for renewal; sending CSR");
                mqtt_link_send_csr(csr);
                free(csr);
                since_csr = 0;
            }
        }
    }
}
