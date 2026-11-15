/* PC test build: the small part of esp_netif.h the firmware uses, backed by sim_wifi.c. */
#pragma once
#include <stdint.h>
#include "esp_err.h"
#include "esp_event.h"

typedef struct esp_netif_obj esp_netif_t;
typedef struct { uint32_t addr; } esp_ip4_addr_t;
typedef struct { esp_ip4_addr_t ip, netmask, gw; } esp_netif_ip_info_t;

ESP_EVENT_DECLARE_BASE(IP_EVENT);
typedef enum { IP_EVENT_STA_GOT_IP = 0, IP_EVENT_STA_LOST_IP } ip_event_t;

esp_err_t esp_netif_init(void);
esp_netif_t *esp_netif_create_default_wifi_sta(void);
esp_netif_t *esp_netif_create_default_wifi_ap(void);
esp_netif_t *esp_netif_get_handle_from_ifkey(const char *if_key);
esp_err_t esp_netif_get_ip_info(esp_netif_t *esp_netif, esp_netif_ip_info_t *ip_info);
char *esp_ip4addr_ntoa(const esp_ip4_addr_t *addr, char *buf, int buflen);
