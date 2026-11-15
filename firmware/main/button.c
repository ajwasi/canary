/* Debounced switch input. Polls every 5 ms; the unit is mains powered. */
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "sdkconfig.h"
#include "board.h"
#include "app.h"

#define POLL_MS 5
#define NC_FAULT_MS 1000

static const char *TAG = "button";
static button_cb_t s_cb;
static volatile bool s_pressed;
static volatile bool s_nc_fault;

static bool raw_pressed(void) { return gpio_get_level(PIN_BTN_NO) == 0; }

static void button_task(void *arg)
{
    int stable_ms = 0, disagree_ms = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
        bool raw = raw_pressed();
        if (raw == s_pressed) {
            stable_ms = 0;
        } else if ((stable_ms += POLL_MS) >= CONFIG_BTN_DEBOUNCE_MS) {
            s_pressed = raw;
            stable_ms = 0;
            if (raw && s_cb) s_cb();
        }
#if CONFIG_BTN_MONITOR_NC
        /* Healthy wiring: exactly one of NO / NC is closed. NC closed reads LOW. */
        bool nc_closed = gpio_get_level(PIN_BTN_NC) == 0;
        if (nc_closed == raw) {
            if (disagree_ms < NC_FAULT_MS && (disagree_ms += POLL_MS) >= NC_FAULT_MS) {
                s_nc_fault = true;
                ESP_LOGW(TAG, "NO/NC contacts agree: wiring fault");
            }
        } else {
            disagree_ms = 0;
            s_nc_fault = false;
        }
#else
        (void)disagree_ms;
#endif
    }
}

void button_init(button_cb_t on_press)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << PIN_BTN_NO) | (1ULL << PIN_BTN_NC),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,   /* backs up the external 10 k */
    };
    gpio_config(&io);
    vTaskDelay(pdMS_TO_TICKS(20));
    s_pressed = raw_pressed();              /* a button held at power-up is not a press */
    s_cb = on_press;
    xTaskCreate(button_task, "button", 3072, NULL, 10, NULL);
}

bool button_is_pressed(void) { return s_pressed; }
bool button_nc_fault(void) { return s_nc_fault; }
