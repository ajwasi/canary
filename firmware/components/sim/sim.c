/* Simulated hardware for the PC test build.
 *
 * The firmware in main/ is compiled unchanged and runs as a Linux program. This file
 * supplies what the chip would: the flash file, the switch inputs, the LED and amplifier
 * outputs, the clock, and a control socket the test harness (emulator/host_firmware.py)
 * uses to press the button and read the unit's state.
 *
 * Environment variables:
 *   SIM_DIR         folder for flash.bin, the sound file and the firmware-slot record
 *   SIM_SERIAL      BTN- plus 12 hex digits (the unit's MAC address)
 *   SIM_CTRL_PORT   UDP port on 127.0.0.1 for the control socket (default 18095)
 *   SIM_BTN, SIM_NC_BROKEN, SIM_WIFI, SIM_CLOCK, SIM_SKEW_S   starting conditions
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_spiffs.h"
#include "esp_netif_sntp.h"
#include "esp_partition.h"
#include "esp_private/partition_linux.h"
#include "esp_timer.h"
#include "esp_mac.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "sdkconfig.h"
#include "board.h"
#include "app.h"
#include "sim_priv.h"

#define FLASH_BYTES (8 * 1024 * 1024)

sim_env_t g_sim;

static struct {
    int led_on;
    int64_t led_on_since;
    const char *led;            /* pattern name worked out from the blink timing */
    int amp;
    int sounds;
    int cur_samples, cur_rate, last_samples, last_rate;
} s_out = { .led = "off", .cur_rate = 16000 };

static char s_storage[240];
static struct timespec s_start;
static volatile int s_tasks_started;

/* ---------------- start-up ---------------- */

static int env_int(const char *name, int def)
{
    const char *v = getenv(name);
    return v && v[0] ? atoi(v) : def;
}

/* Creates the flash file on first use: all 0xFF with the project's partition table in place. */
static void make_flash_file(const char *path)
{
    const char *table = getenv("SIM_PARTITION_TABLE");
    if (!table) table = SIM_BUILD_DIR "/partition_table/partition-table.bin";
    FILE *in = fopen(table, "rb"), *out = fopen(path, "wb");
    if (!in || !out) {
        fprintf(stderr, "sim: cannot create %s from %s: %s\n", path, table, strerror(errno));
        exit(2);
    }
    static uint8_t block[4096];
    memset(block, 0xFF, sizeof(block));
    for (int i = 0; i < FLASH_BYTES / (int)sizeof(block); i++) fwrite(block, 1, sizeof(block), out);
    size_t n = fread(block, 1, sizeof(block), in);
    fseek(out, CONFIG_PARTITION_TABLE_OFFSET, SEEK_SET);
    fwrite(block, 1, n, out);
    fclose(in);
    fclose(out);
}

__attribute__((constructor)) static void sim_ctor(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);           /* the harness reads the log line by line */
    setenv("TZ", "UTC", 1);
    tzset();
    clock_gettime(CLOCK_MONOTONIC, &s_start);

    const char *dir = getenv("SIM_DIR");
    snprintf(g_sim.dir, sizeof(g_sim.dir), "%s", dir && dir[0] ? dir : "sim_state");
    mkdir(g_sim.dir, 0755);
    snprintf(s_storage, sizeof(s_storage), "%s/spiffs", g_sim.dir);

    char flash[240];
    snprintf(flash, sizeof(flash), "%s/flash.bin", g_sim.dir);
    if (access(flash, F_OK) != 0) make_flash_file(flash);
    esp_partition_file_mmap_ctrl_t *ctrl = esp_partition_get_file_mmap_ctrl_input();
    snprintf(ctrl->flash_file_name, sizeof(ctrl->flash_file_name), "%s", flash);

    g_sim.btn = env_int("SIM_BTN", 0);
    g_sim.nc_broken = env_int("SIM_NC_BROKEN", 0);
    g_sim.wifi_up = env_int("SIM_WIFI", 1);
    g_sim.clock_up = env_int("SIM_CLOCK", 1);
    g_sim.skew_ms = (int64_t)env_int("SIM_SKEW_S", 0) * 1000;
    sim_ota_load();
}

int64_t sim_uptime_ms(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)(now.tv_sec - s_start.tv_sec) * 1000 + (now.tv_nsec - s_start.tv_nsec) / 1000000;
}

/* ---------------- port layer (see app.h) ---------------- */

int64_t now_ms(void)
{
    if (!g_sim.clock_set) return sim_uptime_ms();       /* like a chip before SNTP: time since boot */
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000 + g_sim.skew_ms;
}

const char *fw_version(void) { return sim_ota_running(); }

const char *storage_dir(void) { return s_storage; }

/* ---------------- things ESP-IDF does not provide on Linux ---------------- */

__attribute__((weak)) int64_t esp_timer_get_time(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)(now.tv_sec - s_start.tv_sec) * 1000000 + (now.tv_nsec - s_start.tv_nsec) / 1000;
}

__attribute__((weak)) esp_err_t esp_efuse_mac_get_default(uint8_t *mac)
{
    static const uint8_t fallback[6] = { 0x02, 0x12, 0x34, 0x56, 0x78, 0xAB };
    const char *serial = getenv("SIM_SERIAL");
    unsigned v[6];
    if (serial && sscanf(serial, "BTN-%2x%2x%2x%2x%2x%2x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) == 6) {
        for (int i = 0; i < 6; i++) mac[i] = (uint8_t)v[i];
    } else {
        memcpy(mac, fallback, 6);
    }
    return ESP_OK;
}

esp_err_t esp_vfs_spiffs_register(const esp_vfs_spiffs_conf_t *conf)
{
    mkdir(conf->base_path, 0755);
    return ESP_OK;
}

esp_err_t esp_netif_sntp_init(const esp_sntp_config_t *config)
{
    g_sim.sntp_started = 1;
    return ESP_OK;
}

esp_err_t esp_netif_sntp_sync_wait(TickType_t wait)
{
    TickType_t limit = pdMS_TO_TICKS(1500);             /* the test does not sit out the full wait */
    if (wait > limit) wait = limit;
    for (TickType_t t = 0; t < wait && !g_sim.clock_set; t += pdMS_TO_TICKS(50)) vTaskDelay(pdMS_TO_TICKS(50));
    return g_sim.clock_set ? ESP_OK : ESP_ERR_TIMEOUT;
}

/* ---------------- pins ---------------- */

esp_err_t gpio_config(const gpio_config_t *cfg)
{
    sim_start_tasks();
    return ESP_OK;
}

int gpio_get_level(gpio_num_t pin)
{
    if (pin == PIN_BTN_NO) return g_sim.btn ? 0 : 1;                        /* NO closed pulls the input low */
    if (pin == PIN_BTN_NC) return (!g_sim.btn && !g_sim.nc_broken) ? 0 : 1; /* NC closed while released */
    return 1;
}

esp_err_t gpio_set_level(gpio_num_t pin, uint32_t level)
{
    if (pin == PIN_AMP_SD) {
        s_out.amp = level != 0;
    } else if (pin == PIN_LED) {
        int on = (int)level == LED_ON_LEVEL;
        if (on && !s_out.led_on) {
            s_out.led_on_since = sim_uptime_ms();
        } else if (!on && s_out.led_on) {
            int64_t lit = sim_uptime_ms() - s_out.led_on_since;             /* see led_task in app_main.c */
            s_out.led = lit < 65 ? "online" : lit < 300 ? "setup" : lit < 700 ? "connecting" : "fault";
        }
        s_out.led_on = on;
    }
    return ESP_OK;
}

/* ---------------- amplifier ---------------- */

esp_err_t i2s_new_channel(const i2s_chan_config_t *cfg, i2s_chan_handle_t *tx, i2s_chan_handle_t *rx)
{
    if (tx) *tx = (i2s_chan_handle_t)&s_out;
    return ESP_OK;
}

esp_err_t i2s_channel_init_std_mode(i2s_chan_handle_t h, const i2s_std_config_t *cfg) { return ESP_OK; }

esp_err_t i2s_channel_reconfig_std_clock(i2s_chan_handle_t h, const i2s_std_clk_config_t *clk)
{
    s_out.cur_rate = clk->sample_rate_hz ? (int)clk->sample_rate_hz : 16000;
    return ESP_OK;
}

esp_err_t i2s_channel_enable(i2s_chan_handle_t h)
{
    s_out.cur_samples = 0;
    return ESP_OK;
}

esp_err_t i2s_channel_disable(i2s_chan_handle_t h)
{
    s_out.last_samples = s_out.cur_samples;
    s_out.last_rate = s_out.cur_rate;
    s_out.sounds++;
    return ESP_OK;
}

esp_err_t i2s_channel_write(i2s_chan_handle_t h, const void *src, size_t size, size_t *written, TickType_t wait)
{
    int samples = (int)(size / sizeof(int16_t));
    if (s_out.amp) s_out.cur_samples += samples;        /* only what reaches the speaker counts */
    if (written) *written = size;
    vTaskDelay(pdMS_TO_TICKS(samples * 1000 / s_out.cur_rate));     /* a real amplifier plays in real time */
    return ESP_OK;
}

/* ---------------- control socket ---------------- */

static char *nvs_str(nvs_handle_t h, const char *key)
{
    size_t len = 0;
    if (nvs_get_str(h, key, NULL, &len) != ESP_OK || len == 0) return NULL;
    char *buf = malloc(len);
    if (buf && nvs_get_str(h, key, buf, &len) != ESP_OK) {
        free(buf);
        buf = NULL;
    }
    return buf;
}

static void add_str(cJSON *j, nvs_handle_t h, const char *key)
{
    char *v = nvs_str(h, key);
    if (v) cJSON_AddStringToObject(j, key, v);
    free(v);
}

static void add_fingerprint(cJSON *j, nvs_handle_t h, const char *key, const char *as)
{
    char *v = nvs_str(h, key);
    if (!v) return;
    uint8_t digest[32];
    char hex[65];
    mbedtls_sha256((const unsigned char *)v, strlen(v), digest, 0);
    for (int i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", digest[i]);
    cJSON_AddStringToObject(j, as, hex);
    free(v);
}

/* Everything a person with the unit on the bench could see or measure, as JSON. */
static char *state_json(void)
{
    cJSON *j = cJSON_CreateObject();
    uint8_t mac[6];
    char serial[20];
    esp_efuse_mac_get_default(mac);
    snprintf(serial, sizeof(serial), "BTN-%02X%02X%02X%02X%02X%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    cJSON_AddStringToObject(j, "serial", serial);
    cJSON_AddBoolToObject(j, "pressed", g_sim.btn);
    cJSON_AddBoolToObject(j, "nc_broken", g_sim.nc_broken);
    cJSON_AddBoolToObject(j, "wifi_up", g_sim.wifi_up);
    cJSON_AddBoolToObject(j, "clock_up", g_sim.clock_up);
    cJSON_AddBoolToObject(j, "clock_set", g_sim.clock_set);
    cJSON_AddStringToObject(j, "led", s_out.led);
    cJSON_AddNumberToObject(j, "sounds", s_out.sounds);
    cJSON_AddNumberToObject(j, "last_samples", s_out.last_samples);
    cJSON_AddNumberToObject(j, "last_rate", s_out.last_rate);
    cJSON_AddStringToObject(j, "fw", sim_ota_running());
    cJSON_AddBoolToObject(j, "fw_pending", sim_ota_pending());
    cJSON_AddNumberToObject(j, "uptime_ms", (double)sim_uptime_ms());
    sim_wifi_state(j);

    nvs_handle_t h;
    if (nvs_open("cfg", NVS_READONLY, &h) == ESP_OK) {
        uint32_t boot = 0;
        nvs_get_u32(h, "boot", &boot);
        cJSON_AddNumberToObject(j, "boot", boot);
        add_str(j, h, "name");
        add_str(j, h, "ssid");
        add_str(j, h, "host");
        add_str(j, h, "claim");
        add_str(j, h, "audio_sha");
        add_fingerprint(j, h, "cert", "cert_fp");
        add_fingerprint(j, h, "key", "key_fp");
        add_fingerprint(j, h, "key_pend", "key_pend_fp");
        nvs_close(h);
    }

    cJSON *q = cJSON_AddArrayToObject(j, "queue");
    if (nvs_open("evq", NVS_READONLY, &h) == ESP_OK) {
        uint32_t head = 0, tail = 0;
        nvs_get_u32(h, "head", &head);
        nvs_get_u32(h, "tail", &tail);
        cJSON_AddNumberToObject(j, "queued", tail - head);
        for (uint32_t seq = head; seq != tail && seq - head < 40; seq++) {
            char key[16];
            evq_item_t it;
            size_t len = sizeof(it);
            snprintf(key, sizeof(key), "e%08lx", (unsigned long)seq);
            if (nvs_get_blob(h, key, &it, &len) != ESP_OK || len != sizeof(it)) continue;
            cJSON *o = cJSON_CreateObject();
            cJSON_AddNumberToObject(o, "seq", it.seq);
            cJSON_AddNumberToObject(o, "boot", it.boot_id);
            cJSON_AddNumberToObject(o, "ts", (double)it.ts / 1000.0);
            cJSON_AddItemToArray(q, o);
        }
        nvs_close(h);
    } else {
        cJSON_AddNumberToObject(j, "queued", 0);
    }

    char *text = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    return text;
}

static void run_command(const char *cmd)
{
    char word[16] = "";
    long long val = 0;
    if (sscanf(cmd, "%15s %lld", word, &val) < 1) return;
    if (!strcmp(word, "btn")) g_sim.btn = val != 0;
    else if (!strcmp(word, "nc")) g_sim.nc_broken = val != 0;
    else if (!strcmp(word, "wifi")) g_sim.wifi_up = val != 0;
    else if (!strcmp(word, "clock")) g_sim.clock_up = val != 0;
    else if (!strcmp(word, "clockset")) g_sim.clock_set = val != 0;
    else if (!strcmp(word, "skew")) g_sim.skew_ms = val * 1000;
}

static void ctrl_task(void *arg)
{
    struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(env_int("SIM_CTRL_PORT", 18095)) };
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    int sock = socket(AF_INET, SOCK_DGRAM, 0), yes = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    if (sock < 0 || bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "sim: control socket: %s\n", strerror(errno));
        exit(2);
    }
    fcntl(sock, F_SETFL, fcntl(sock, F_GETFL, 0) | O_NONBLOCK);
    int since_wifi = 0;
    for (;;) {
        char cmd[128];
        struct sockaddr_in from;
        socklen_t flen = sizeof(from);
        int n = recvfrom(sock, cmd, sizeof(cmd) - 1, 0, (struct sockaddr *)&from, &flen);
        if (n > 0) {
            cmd[n] = 0;
            run_command(cmd);
            char *reply = state_json();                 /* every command is answered with the state */
            if (reply) sendto(sock, reply, strlen(reply), 0, (struct sockaddr *)&from, flen);
            free(reply);
            continue;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
        if ((since_wifi += 5) >= 50) {
            since_wifi = 0;
            sim_wifi_tick();
        }
    }
}

void sim_start_tasks(void)
{
    if (!__sync_bool_compare_and_swap(&s_tasks_started, 0, 1)) return;
    xTaskCreate(ctrl_task, "sim_ctrl", 16384, NULL, 12, NULL);
}
