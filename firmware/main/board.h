/* Pin map for the Seeed XIAO ESP32-C5 carrier board. See docs/hardware.md. */
#pragma once
#include "driver/gpio.h"

#define PIN_BTN_NO     GPIO_NUM_1   /* D0  - LOW while pressed (optocoupler U1) */
#define PIN_BTN_NC     GPIO_NUM_0   /* D1  - LOW while released (optocoupler U2, optional) */
#define PIN_I2S_BCLK   GPIO_NUM_8   /* D8  */
#define PIN_I2S_LRCLK  GPIO_NUM_9   /* D9  */
#define PIN_I2S_DOUT   GPIO_NUM_10  /* D10 */
#define PIN_AMP_SD     GPIO_NUM_12  /* D7  - HIGH enables the amplifier */
#define PIN_LED        GPIO_NUM_27  /* on-board user LED */
#define LED_ON_LEVEL   0            /* LED is wired to 3V3; verify on your board revision */
