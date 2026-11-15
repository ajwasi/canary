/* Chip side of the port layer declared in app.h. The PC test build uses components/sim instead. */
#include <sys/time.h>
#include "esp_app_desc.h"
#include "app.h"

int64_t now_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);        /* counts from zero at boot until SNTP sets it */
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

const char *fw_version(void) { return esp_app_get_description()->version; }

const char *storage_dir(void) { return "/spiffs"; }
