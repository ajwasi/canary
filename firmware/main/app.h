/* Shared declarations for all firmware modules. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <time.h>
#include "esp_err.h"

/* ---- cfg.c: settings in NVS ---- */
esp_err_t cfg_init(void);
char *cfg_get_str(const char *key);                 /* malloc'd, or NULL if unset */
esp_err_t cfg_set_str(const char *key, const char *val);
void cfg_erase(const char *key);
uint32_t cfg_get_u32(const char *key, uint32_t def);
void cfg_set_u32(const char *key, uint32_t val);
extern uint32_t g_boot_id;                          /* increments every boot */

/* ---- port: the few things that differ between the chip and the PC test build.
 * On the chip they are in port_esp.c; on the PC in components/sim. ---- */
int64_t now_ms(void);                               /* unix time in ms; time since boot until the clock is set */
const char *fw_version(void);                       /* version of the running firmware */
const char *storage_dir(void);                      /* folder that holds the press sound */
static inline bool time_is_valid(void) { return now_ms() > 1700000000000LL; }

/* ---- button.c ---- */
typedef void (*button_cb_t)(void);
void button_init(button_cb_t on_press);
bool button_is_pressed(void);
bool button_nc_fault(void);

/* ---- evq.c: press queue that survives reboots ---- */
typedef struct {
    uint32_t seq;
    uint32_t boot_id;
    int64_t ts;       /* unix time in milliseconds, 0 if clock was not set */
    int64_t up_ms;    /* uptime at press */
} evq_item_t;
void evq_init(void);
void evq_push(void);
bool evq_peek(evq_item_t *out);
void evq_pop(void);
uint32_t evq_pending(void);

/* ---- audio.c ---- */
void audio_init(void);
void audio_play(void);                               /* non-blocking */
esp_err_t audio_update(const char *url, const char *sha256_hex);

/* ---- wifi_mgr.c ---- */
void wifi_mgr_init(void);
bool wifi_auth_ok(int authmode);                     /* WPA2/WPA3 personal only */
void wifi_mgr_start_sta(void);
bool wifi_mgr_wait_connected(uint32_t timeout_ms);
bool wifi_mgr_connected(void);
int wifi_mgr_fail_count(void);

/* ---- provisioning.c ---- */
void provisioning_run(void);                         /* never returns */

/* ---- identity.c ---- */
const char *identity_serial(void);
bool identity_has_cert(void);
int identity_enroll(void);                           /* 0 ok, 1 claim rejected, -1 retry later */
time_t identity_cert_not_after(void);
bool identity_renew_due(void);
char *identity_begin_renew(void);                    /* returns CSR PEM (malloc'd) */
esp_err_t identity_install_renewed(const char *cert_pem);

/* ---- mqtt_link.c ---- */
void mqtt_link_start(void);
bool mqtt_link_connected(void);
bool mqtt_link_ever_connected(void);
void mqtt_link_notify_press(void);
void mqtt_link_publish_status(void);
void mqtt_link_send_csr(const char *csr_pem);
