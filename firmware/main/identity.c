/* Device identity: an ECDSA P-256 key made on the device and a short-lived
 * client certificate from the server's device CA. See docs/protocol.md.
 * NVS keys: "key"/"cert" (active), "key_pend"/"cert_pend" (renewal in progress). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "mbedtls/pk.h"
#include "mbedtls/ecp.h"
#include "mbedtls/x509_crt.h"
#include "mbedtls/x509_csr.h"
#include "cJSON.h"
#include "sdkconfig.h"
#include "app.h"

static const char *TAG = "identity";

static int rng(void *ctx, unsigned char *buf, size_t len)
{
    esp_fill_random(buf, len);      /* true random while Wi-Fi is running */
    return 0;
}

const char *identity_serial(void)
{
    static char serial[20];
    if (!serial[0]) {
        uint8_t mac[6];
        esp_efuse_mac_get_default(mac);
        snprintf(serial, sizeof(serial), "BTN-%02X%02X%02X%02X%02X%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }
    return serial;
}

/* Moves a renewed key + certificate pair into place. Safe to repeat: if power
 * fails part-way, the next boot runs it again and finishes the job. */
static void commit_pending(void)
{
    char *key = cfg_get_str("key_pend"), *cert = cfg_get_str("cert_pend");
    if (key && cert && cfg_set_str("key", key) == ESP_OK && cfg_set_str("cert", cert) == ESP_OK) {
        cfg_erase("cert_pend");
        cfg_erase("key_pend");
    }
    free(key);
    free(cert);
}

bool identity_has_cert(void)
{
    commit_pending();
    char *cert = cfg_get_str("cert"), *key = cfg_get_str("key");
    bool ok = cert && key;
    free(cert);
    free(key);
    return ok;
}

static char *new_key_pem(void)
{
    char *pem = calloc(1, 1024);
    mbedtls_pk_context pk;
    mbedtls_pk_init(&pk);
    int rc = mbedtls_pk_setup(&pk, mbedtls_pk_info_from_type(MBEDTLS_PK_ECKEY));
    if (rc == 0) rc = mbedtls_ecp_gen_key(MBEDTLS_ECP_DP_SECP256R1, mbedtls_pk_ec(pk), rng, NULL);
    if (rc == 0 && pem) rc = mbedtls_pk_write_key_pem(&pk, (unsigned char *)pem, 1024);
    mbedtls_pk_free(&pk);
    if (rc != 0 || !pem) {
        ESP_LOGE(TAG, "key generation failed (-0x%04x)", -rc);
        free(pem);
        return NULL;
    }
    return pem;
}

static char *make_csr(const char *key_pem)
{
    char subject[40];
    char *pem = calloc(1, 2048);
    mbedtls_pk_context pk;
    mbedtls_x509write_csr req;
    mbedtls_pk_init(&pk);
    mbedtls_x509write_csr_init(&req);
    snprintf(subject, sizeof(subject), "CN=%s", identity_serial());
    int rc = mbedtls_pk_parse_key(&pk, (const unsigned char *)key_pem, strlen(key_pem) + 1, NULL, 0, rng, NULL);
    if (rc == 0) {
        mbedtls_x509write_csr_set_md_alg(&req, MBEDTLS_MD_SHA256);
        mbedtls_x509write_csr_set_key(&req, &pk);
        rc = mbedtls_x509write_csr_set_subject_name(&req, subject);
    }
    if (rc == 0 && pem) rc = mbedtls_x509write_csr_pem(&req, (unsigned char *)pem, 2048, rng, NULL);
    mbedtls_x509write_csr_free(&req);
    mbedtls_pk_free(&pk);
    if (rc != 0 || !pem) {
        ESP_LOGE(TAG, "CSR creation failed (-0x%04x)", -rc);
        free(pem);
        return NULL;
    }
    return pem;
}

/* True if the certificate's public key belongs to the given private key. */
static bool cert_matches_key(const char *cert_pem, const char *key_pem)
{
    mbedtls_x509_crt crt;
    mbedtls_pk_context pk;
    mbedtls_x509_crt_init(&crt);
    mbedtls_pk_init(&pk);
    bool ok = mbedtls_x509_crt_parse(&crt, (const unsigned char *)cert_pem, strlen(cert_pem) + 1) == 0 &&
              mbedtls_pk_parse_key(&pk, (const unsigned char *)key_pem, strlen(key_pem) + 1, NULL, 0, rng, NULL) == 0 &&
              mbedtls_pk_check_pair(&crt.pk, &pk, rng, NULL) == 0;
    mbedtls_pk_free(&pk);
    mbedtls_x509_crt_free(&crt);
    return ok;
}

static time_t to_time(const mbedtls_x509_time *t)
{
    struct tm tm = { .tm_year = t->year - 1900, .tm_mon = t->mon - 1, .tm_mday = t->day,
                     .tm_hour = t->hour, .tm_min = t->min, .tm_sec = t->sec };
    return mktime(&tm);             /* the system time zone is UTC */
}

static bool cert_times(time_t *from, time_t *to)
{
    char *cert = cfg_get_str("cert");
    if (!cert) return false;
    mbedtls_x509_crt crt;
    mbedtls_x509_crt_init(&crt);
    bool ok = mbedtls_x509_crt_parse(&crt, (const unsigned char *)cert, strlen(cert) + 1) == 0;
    if (ok) {
        *from = to_time(&crt.valid_from);
        *to = to_time(&crt.valid_to);
    }
    mbedtls_x509_crt_free(&crt);
    free(cert);
    return ok;
}

time_t identity_cert_not_after(void)
{
    time_t from, to;
    return cert_times(&from, &to) ? to : 0;
}

bool identity_renew_due(void)
{
    time_t from, to;
    if (!time_is_valid() || !cert_times(&from, &to)) return false;
    return now_ms() / 1000 > from + (to - from) / 3 * 2;
}

int identity_enroll(void)
{
    int result = -1;
    char *host = cfg_get_str("host"), *claim = cfg_get_str("claim"), *name = cfg_get_str("name");
    char *key = NULL, *csr = NULL, *body = NULL, *resp = NULL, url[200];
    esp_http_client_handle_t http = NULL;

    if (!host || !claim) {
        ESP_LOGE(TAG, "no server or claim code configured");
        result = 1;
        goto done;
    }
    if (!(key = new_key_pem()) || !(csr = make_csr(key))) goto done;

    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "serial", identity_serial());
    cJSON_AddStringToObject(j, "name", name ? name : "");
    cJSON_AddStringToObject(j, "claim", claim);
    cJSON_AddStringToObject(j, "csr", csr);
    body = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    if (!body) goto done;

    snprintf(url, sizeof(url), "https://%s:%d/api/device/enroll", host, CONFIG_BTN_HTTPS_PORT);
    esp_http_client_config_t hc = {
        .url = url, .method = HTTP_METHOD_POST,
        .crt_bundle_attach = esp_crt_bundle_attach, .timeout_ms = 15000,
    };
    if (!(http = esp_http_client_init(&hc))) goto done;
    esp_http_client_set_header(http, "Content-Type", "application/json");
    int blen = strlen(body);
    if (esp_http_client_open(http, blen) != ESP_OK || esp_http_client_write(http, body, blen) != blen) {
        ESP_LOGW(TAG, "cannot reach %s", host);
        goto done;
    }
    esp_http_client_fetch_headers(http);
    int status = esp_http_client_get_status_code(http);
    if (status == 401 || status == 403) {
        ESP_LOGE(TAG, "claim code rejected");
        result = 1;
        goto done;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "enrolment returned HTTP %d", status);
        goto done;
    }
    if (!(resp = calloc(1, 8192))) goto done;
    if (esp_http_client_read_response(http, resp, 8191) <= 0) goto done;

    cJSON *r = cJSON_Parse(resp);
    cJSON *cert = r ? cJSON_GetObjectItem(r, "cert") : NULL;
    if (cJSON_IsString(cert) && strlen(cert->valuestring) < 3900 && cert_matches_key(cert->valuestring, key)) {
        if (cfg_set_str("key", key) == ESP_OK && cfg_set_str("cert", cert->valuestring) == ESP_OK) {
            cfg_erase("claim");
            ESP_LOGI(TAG, "enrolled as %s", identity_serial());
            result = 0;
        }
    } else {
        ESP_LOGE(TAG, "server returned an unusable certificate");
    }
    cJSON_Delete(r);

done:
    if (http) {
        esp_http_client_close(http);
        esp_http_client_cleanup(http);
    }
    free(resp);
    free(body);
    free(csr);
    free(key);
    free(host);
    free(claim);
    free(name);
    return result;
}

char *identity_begin_renew(void)
{
    char *key = new_key_pem();
    if (!key) return NULL;
    char *csr = make_csr(key);
    cfg_erase("cert_pend");
    if (csr && cfg_set_str("key_pend", key) != ESP_OK) {
        free(csr);
        csr = NULL;
    }
    free(key);
    return csr;
}

esp_err_t identity_install_renewed(const char *cert_pem)
{
    char *pend = cfg_get_str("key_pend");
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (pend && strlen(cert_pem) < 3900 && cert_matches_key(cert_pem, pend)) {
        err = cfg_set_str("cert_pend", cert_pem);
        if (err == ESP_OK) commit_pending();
    } else {
        ESP_LOGW(TAG, "renewed certificate does not match the pending key");
    }
    free(pend);
    return err;
}
