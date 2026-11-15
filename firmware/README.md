# Button firmware

Firmware for the button unit described in [../docs/hardware.md](../docs/hardware.md). It targets the Seeed XIAO ESP32-C5 and talks to the server as described in [../docs/protocol.md](../docs/protocol.md).

**Status: written but not yet compiled or run.** ESP-IDF was not installed on the machine where this was written, so expect to fix build errors on the first `idf.py build`.

## What it does

- Detects presses with a 30 ms debounce and plays a sound at once.
- Stores every press in flash until the server acknowledges it, so presses made while offline are delivered later.
- Joins WPA2/WPA3-personal Wi-Fi only, and prefers a 5 GHz access point when its signal is at least −78 dBm.
- Offers a setup access point and web page for the name, Wi-Fi details, server address and claim code.
- Creates its own key, enrols for a client certificate and renews it at two thirds of its lifetime.
- Holds an MQTT-over-TLS connection open, verified against the public root list in the firmware.
- Updates its firmware and its press sound on command from the server, with automatic rollback.

## Source layout

| File | Purpose |
|---|---|
| `main/app_main.c` | Boot sequence, status LED, renewal and rollback timers |
| `main/board.h` | Pin map |
| `main/button.c` | Debounce, wire-break monitor |
| `main/evq.c` | Press queue in flash |
| `main/audio.c` | I2S playback, sound download |
| `main/wifi_mgr.c` | Wi-Fi station, security filter, band choice |
| `main/provisioning.c`, `main/portal.html` | Setup access point and page |
| `main/identity.c` | Key, CSR, enrolment, renewal |
| `main/mqtt_link.c` | Server link, commands, firmware update |
| `main/cfg.c` | Settings storage |

## Build and flash

Requires ESP-IDF 5.5 (the first release line with ESP32-C5 support). ESP-IDF 6 changes the MQTT, JSON and crypto components this code uses and will need porting.

```
cd firmware
idf.py set-target esp32c5
idf.py menuconfig        # "Button notifier": set the default server host
idf.py build
idf.py -p COMx flash monitor
```

Flash with only the USB-C cable attached. Never connect USB-C while the unit is powered through J1.

## First setup

1. Power the unit. With no Wi-Fi settings it starts setup mode and prints the access point name and password on the USB console. Copy both to the unit's label.
2. On the server, add a button and note its claim code.
3. From a phone or laptop, join the `Button-XXXX` network with the label password. The setup page opens by itself, or browse to `http://192.168.4.1/`.
4. Enter the name, choose the Wi-Fi network, enter its password, the server address and the claim code, then save.
5. The unit restarts, joins the network, enrols and connects. The on-board LED flashes briefly every 5 seconds when it is online.

To change settings later, hold the button down, apply power, and keep it held for 10 seconds. Setup mode ends after 10 minutes without a save.

If the Wi-Fi details have never worked, the unit returns to setup mode after six failed attempts. Once they have worked, it keeps retrying indefinitely.

| LED pattern | Meaning |
|---|---|
| Fast blink | Setup mode |
| Slow blink | Joining Wi-Fi |
| Mostly on | Wi-Fi up, server unreachable or enrolment failing |
| Brief flash every 5 s | Online |

## Production build

`sdkconfig.prod.defaults` turns on secure boot, flash encryption and encrypted settings storage. These burn one-time fuses in the chip on first boot; a mistake makes that module unusable, so try it on one spare first.

```
espsecure.py generate_signing_key --version 2 secure_boot_signing_key.pem
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.prod.defaults" build
idf.py -p COMx flash monitor
```

- Keep `secure_boot_signing_key.pem` offline and backed up. Without it you can never update the fleet; with it anyone can.
- Sign firmware update images with the same key. Units reject unsigned or wrongly signed images.
- The press sound is stored unencrypted; it is not sensitive.

## Known limits

- Enterprise (username and password) Wi-Fi is not supported.
- The setup page is plain HTTP inside the WPA2-protected setup network.
- A unit whose certificate has fully expired needs a new claim code through setup mode.
- Sound and firmware URLs must answer directly; redirects are not followed for sound downloads.
