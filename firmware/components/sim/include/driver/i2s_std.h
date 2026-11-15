/* PC test build: stand-in for driver/i2s_std.h. Samples are counted, not played. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"
#include "esp_err.h"
#include "driver/gpio.h"

typedef struct sim_i2s_chan *i2s_chan_handle_t;
typedef enum { I2S_NUM_0 = 0 } i2s_port_t;
typedef enum { I2S_ROLE_MASTER = 0 } i2s_role_t;
typedef enum { I2S_DATA_BIT_WIDTH_16BIT = 16 } i2s_data_bit_width_t;
typedef enum { I2S_SLOT_MODE_MONO = 1, I2S_SLOT_MODE_STEREO = 2 } i2s_slot_mode_t;
typedef enum { I2S_STD_SLOT_LEFT = 1, I2S_STD_SLOT_RIGHT = 2, I2S_STD_SLOT_BOTH = 3 } i2s_std_slot_mask_t;
#define I2S_GPIO_UNUSED GPIO_NUM_NC

typedef struct { i2s_port_t id; i2s_role_t role; bool auto_clear; } i2s_chan_config_t;
#define I2S_CHANNEL_DEFAULT_CONFIG(num, r) { .id = (num), .role = (r) }

typedef struct { uint32_t sample_rate_hz; } i2s_std_clk_config_t;
#define I2S_STD_CLK_DEFAULT_CONFIG(rate) { .sample_rate_hz = (rate) }

typedef struct {
    i2s_data_bit_width_t data_bit_width;
    i2s_slot_mode_t slot_mode;
    i2s_std_slot_mask_t slot_mask;
} i2s_std_slot_config_t;
#define I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(bits, mode) { .data_bit_width = (bits), .slot_mode = (mode) }

typedef struct { gpio_num_t mclk, bclk, ws, dout, din; } i2s_std_gpio_config_t;
typedef struct {
    i2s_std_clk_config_t clk_cfg;
    i2s_std_slot_config_t slot_cfg;
    i2s_std_gpio_config_t gpio_cfg;
} i2s_std_config_t;

esp_err_t i2s_new_channel(const i2s_chan_config_t *cfg, i2s_chan_handle_t *tx, i2s_chan_handle_t *rx);
esp_err_t i2s_channel_init_std_mode(i2s_chan_handle_t h, const i2s_std_config_t *cfg);
esp_err_t i2s_channel_reconfig_std_clock(i2s_chan_handle_t h, const i2s_std_clk_config_t *clk);
esp_err_t i2s_channel_enable(i2s_chan_handle_t h);
esp_err_t i2s_channel_disable(i2s_chan_handle_t h);
esp_err_t i2s_channel_write(i2s_chan_handle_t h, const void *src, size_t size, size_t *written, TickType_t wait);
