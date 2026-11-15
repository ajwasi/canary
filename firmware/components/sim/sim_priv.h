/* Shared between the simulated-hardware sources. PC test build only. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"

typedef struct {
    /* The world around the unit; set from the environment at start and over the control socket. */
    volatile int btn;           /* actuator held in */
    volatile int nc_broken;     /* NC monitor wire broken */
    volatile int wifi_up;       /* site Wi-Fi reachable */
    volatile int clock_up;      /* a time server can be reached */
    volatile int clock_set;     /* the unit's clock has been set since boot */
    volatile int sntp_started;
    volatile int64_t skew_ms;   /* added to the unit's clock, to make a certificate look old */
    char dir[200];              /* folder that stands in for the unit's flash */
} sim_env_t;

extern sim_env_t g_sim;

void sim_start_tasks(void);             /* safe to call many times */
int64_t sim_uptime_ms(void);

void sim_wifi_tick(void);               /* called every 50 ms */
void sim_wifi_state(cJSON *j);

void sim_ota_load(void);
const char *sim_ota_running(void);
bool sim_ota_pending(void);
