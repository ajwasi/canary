# Button firmware

Firmware for the button unit described in [../docs/hardware.md](../docs/hardware.md). It targets the Seeed XIAO ESP32-C5 and talks to the server as described in [../docs/protocol.md](../docs/protocol.md).

**Status: builds and passes its tests on a PC; not yet run on hardware.** On every push, GitHub Actions ([../.github/workflows/ci.yml](../.github/workflows/ci.yml)):

- builds the firmware for the ESP32-C5 with ESP-IDF 5.5 and keeps the image as a download on the run's page;
- builds the same source as a Linux program and runs it through the end-to-end test against the real server (see [PC test build](#pc-test-build)).

Not yet tested, because they need a real board: the Wi-Fi radio, the pins, the amplifier, real firmware slots, secure boot and flash encryption.

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
| `main/port_esp.c` | Clock, firmware version and sound folder on the chip |
| `components/sim/` | PC test build only: simulated radio, pins, amplifier, clock and firmware slots |
| `sdkconfig.defaults` | Settings for every build |
| `sdkconfig.defaults.esp32c5` | Settings for the real unit |
| `sdkconfig.defaults.linux` | Settings for the PC test build: local ports, short timings, test root certificate |

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

To skip installing ESP-IDF, download `firmware-esp32c5` from the latest run on the repository's Actions page. It holds the bootloader, partition table and application images, and `flasher_args.json` lists the flash offsets for `esptool.py`.

## PC test build

The firmware also builds as an ordinary Linux program, using ESP-IDF's Linux target. Everything in `main/` is compiled unchanged; `components/sim/` replaces the ESP-IDF parts that need a chip.

| Real in the PC build | Simulated |
|---|---|
| Boot sequence, setup mode and its web server | Wi-Fi radio and the access points around the unit |
| Settings and the press queue in NVS, in a flash image file | Switch inputs, LED and amplifier pins |
| Key and CSR creation, enrolment over HTTPS | I2S output (samples are counted, not played) |
| TLS with the certificate bundle, MQTT, commands | Firmware slots and the bootloader's rollback rules |
| Sound download, checksum and WAV checks | The clock source (SNTP) |
| Renewal and rollback timers (shortened) | The sound file store (a folder, not SPIFFS) |

Build and test on Linux, macOS or WSL (not native Windows), from an ESP-IDF 5.5 shell at the repository root:

```
python -m venv ~/canary-venv && ~/canary-venv/bin/pip install -r server/requirements-dev.txt
~/canary-venv/bin/python emulator/test_ca.py firmware/host/test-ca
cd firmware
idf.py -B build-host -D SDKCONFIG=build-host/sdkconfig --preview set-target linux
idf.py -B build-host -D SDKCONFIG=build-host/sdkconfig build
cd ../emulator
CANARY_TARGET=firmware CANARY_FIRMWARE_ELF=../firmware/build-host/button_notifier.elf \
CANARY_TEST_CA=../firmware/host/test-ca ~/canary-venv/bin/python -m pytest test_e2e.py -q
```

- `test_ca.py` makes a throw-away root certificate. The PC build trusts it in addition to the public roots, so the local test server can present a certificate the firmware accepts. Real builds never include it.
- The separate build folder keeps the PC build from disturbing the ESP32-C5 build in `build/`. GitHub Actions uses the plain `build/` folder; only that form has been run.
- The test takes about a minute. The harness that plays the part of the bench is [../emulator/host_firmware.py](../emulator/host_firmware.py).

Changes made to `main/` so that both builds share one source: timings and ports became settings (menuconfig, "Button notifier"), and the clock, firmware version and sound folder go through three small functions declared in `app.h`.

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
