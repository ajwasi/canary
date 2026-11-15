/* Press sound: plays /spiffs/press.wav through the MAX98357A, or a built-in
 * two-tone beep if no file is installed. The file can be replaced from the server. */
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/i2s_std.h"
#include "esp_spiffs.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "mbedtls/sha256.h"
#include "board.h"
#include "app.h"

#define WAV_PATH     "/spiffs/press.wav"
#define WAV_TMP_PATH "/spiffs/press.new"
#define WAV_MAX_BYTES (1500 * 1024)
#define BEEP_RATE 16000

static const char *TAG = "audio";
static i2s_chan_handle_t s_tx;
static TaskHandle_t s_task;
static SemaphoreHandle_t s_file_lock;

typedef struct {
    uint32_t rate;
    uint16_t channels;
    uint32_t data_len;
} wav_info_t;

/* Positions f at the first sample. Accepts 16-bit PCM, mono or stereo, 8-48 kHz. */
static bool wav_open(FILE *f, wav_info_t *w)
{
    uint8_t h[12];
    bool have_fmt = false;
    if (fread(h, 1, 12, f) != 12 || memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4)) return false;
    for (;;) {
        uint8_t ch[8];
        if (fread(ch, 1, 8, f) != 8) return false;
        uint32_t size = ch[4] | ch[5] << 8 | ch[6] << 16 | (uint32_t)ch[7] << 24;
        if (!memcmp(ch, "fmt ", 4)) {
            uint8_t fm[16];
            if (size < 16 || fread(fm, 1, 16, f) != 16) return false;
            uint16_t format = fm[0] | fm[1] << 8;
            uint16_t bits = fm[14] | fm[15] << 8;
            w->channels = fm[2] | fm[3] << 8;
            w->rate = fm[4] | fm[5] << 8 | fm[6] << 16 | (uint32_t)fm[7] << 24;
            if (format != 1 || bits != 16 || w->channels < 1 || w->channels > 2 ||
                w->rate < 8000 || w->rate > 48000) return false;
            fseek(f, (long)(size - 16 + (size & 1)), SEEK_CUR);
            have_fmt = true;
        } else if (!memcmp(ch, "data", 4)) {
            w->data_len = size;
            return have_fmt;
        } else {
            fseek(f, (long)(size + (size & 1)), SEEK_CUR);
        }
    }
}

static void amp_start(uint32_t rate)
{
    i2s_std_clk_config_t clk = I2S_STD_CLK_DEFAULT_CONFIG(rate);
    i2s_channel_reconfig_std_clock(s_tx, &clk);
    i2s_channel_enable(s_tx);
    gpio_set_level(PIN_AMP_SD, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
}

static void amp_stop(void)
{
    static const int16_t silence[512] = {0};
    size_t n;
    i2s_channel_write(s_tx, silence, sizeof(silence), &n, portMAX_DELAY);   /* flush the tail */
    vTaskDelay(pdMS_TO_TICKS(40));
    gpio_set_level(PIN_AMP_SD, 0);
    i2s_channel_disable(s_tx);
}

static void write_samples(const int16_t *buf, size_t count)
{
    size_t n;
    i2s_channel_write(s_tx, buf, count * sizeof(int16_t), &n, portMAX_DELAY);
}

static void play_beep(void)
{
    static const struct { float hz; int ms; } notes[] = { { 880, 90 }, { 1320, 140 } };
    int16_t buf[256];
    amp_start(BEEP_RATE);
    for (size_t k = 0; k < sizeof(notes) / sizeof(notes[0]); k++) {
        int total = BEEP_RATE * notes[k].ms / 1000, fade = BEEP_RATE / 100;
        for (int i = 0; i < total;) {
            int n = 0;
            for (; n < 256 && i < total; n++, i++) {
                float env = i < fade ? (float)i / fade : (total - i < fade ? (float)(total - i) / fade : 1.0f);
                buf[n] = (int16_t)(12000.0f * env * sinf(2.0f * (float)M_PI * notes[k].hz * i / BEEP_RATE));
            }
            write_samples(buf, n);
        }
    }
    amp_stop();
}

static bool play_file(void)
{
    static int16_t buf[512];
    wav_info_t w;
    FILE *f = fopen(WAV_PATH, "rb");
    if (!f) return false;
    if (!wav_open(f, &w)) {
        fclose(f);
        return false;
    }
    amp_start(w.rate);
    uint32_t left = w.data_len;
    while (left >= 2) {
        size_t want = left < sizeof(buf) ? left : sizeof(buf);
        size_t got = fread(buf, 1, want, f);
        if (got < 2) break;
        left -= got;
        size_t samples = got / 2;
        if (w.channels == 2) {                       /* keep the left channel */
            samples /= 2;
            for (size_t i = 0; i < samples; i++) buf[i] = buf[2 * i];
        }
        write_samples(buf, samples);
    }
    fclose(f);
    amp_stop();
    return true;
}

static void audio_task(void *arg)
{
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        xSemaphoreTake(s_file_lock, portMAX_DELAY);
        if (!play_file()) play_beep();
        xSemaphoreGive(s_file_lock);
    }
}

void audio_init(void)
{
    gpio_config_t io = { .pin_bit_mask = 1ULL << PIN_AMP_SD, .mode = GPIO_MODE_OUTPUT };
    gpio_config(&io);
    gpio_set_level(PIN_AMP_SD, 0);

    esp_vfs_spiffs_conf_t fs = {
        .base_path = "/spiffs", .partition_label = "storage",
        .max_files = 3, .format_if_mount_failed = true,
    };
    if (esp_vfs_spiffs_register(&fs) != ESP_OK) ESP_LOGW(TAG, "no audio storage; using built-in beep");

    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    cc.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&cc, &s_tx, NULL));
    i2s_std_config_t sc = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(BEEP_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED, .bclk = PIN_I2S_BCLK, .ws = PIN_I2S_LRCLK,
            .dout = PIN_I2S_DOUT, .din = I2S_GPIO_UNUSED,
        },
    };
    sc.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;       /* same sample on left and right */
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &sc));

    s_file_lock = xSemaphoreCreateMutex();
    xTaskCreate(audio_task, "audio", 4096, NULL, 8, &s_task);
}

void audio_play(void)
{
    if (s_task) xTaskNotifyGive(s_task);
}

esp_err_t audio_update(const char *url, const char *sha256_hex)
{
    if (strncmp(url, "https://", 8) != 0 || strlen(sha256_hex) != 64) return ESP_ERR_INVALID_ARG;

    esp_http_client_config_t hc = { .url = url, .crt_bundle_attach = esp_crt_bundle_attach, .timeout_ms = 15000 };
    esp_http_client_handle_t http = esp_http_client_init(&hc);
    if (!http) return ESP_FAIL;

    esp_err_t err = ESP_FAIL;
    FILE *f = NULL;
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);

    if (esp_http_client_open(http, 0) != ESP_OK) goto done;
    esp_http_client_fetch_headers(http);
    if (esp_http_client_get_status_code(http) != 200) goto done;
    if (!(f = fopen(WAV_TMP_PATH, "wb"))) goto done;

    static uint8_t buf[1024];
    int total = 0, n;
    while ((n = esp_http_client_read(http, (char *)buf, sizeof(buf))) > 0) {
        total += n;
        if (total > WAV_MAX_BYTES || fwrite(buf, 1, n, f) != (size_t)n) goto done;
        mbedtls_sha256_update(&sha, buf, n);
    }
    if (n < 0) goto done;
    fclose(f);
    f = NULL;

    uint8_t digest[32];
    char hex[65];
    mbedtls_sha256_finish(&sha, digest);
    for (int i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", digest[i]);
    if (strcasecmp(hex, sha256_hex) != 0) {
        ESP_LOGW(TAG, "audio checksum mismatch");
        goto done;
    }

    wav_info_t w;
    FILE *chk = fopen(WAV_TMP_PATH, "rb");
    bool valid = chk && wav_open(chk, &w);
    if (chk) fclose(chk);
    if (!valid) {
        ESP_LOGW(TAG, "audio file is not 16-bit PCM WAV");
        goto done;
    }

    xSemaphoreTake(s_file_lock, portMAX_DELAY);
    unlink(WAV_PATH);
    bool renamed = rename(WAV_TMP_PATH, WAV_PATH) == 0;
    xSemaphoreGive(s_file_lock);
    if (renamed) {
        cfg_set_str("audio_sha", hex);
        ESP_LOGI(TAG, "press sound updated (%d bytes)", total);
        err = ESP_OK;
    }

done:
    if (f) fclose(f);
    if (err != ESP_OK) unlink(WAV_TMP_PATH);
    mbedtls_sha256_free(&sha);
    esp_http_client_close(http);
    esp_http_client_cleanup(http);
    return err;
}
