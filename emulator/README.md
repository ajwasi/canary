# Button emulator

A software stand-in for one button unit, for testing the server before any hardware exists. It follows the hardware in [../docs/hardware.md](../docs/hardware.md), the firmware behaviour in [../firmware/main/](../firmware/main/) and the protocol in [../docs/protocol.md](../docs/protocol.md).

It is a re-implementation of the firmware's behaviour in Python, not the firmware itself. It tests the server and the protocol; it cannot find bugs in the C code.

## What is emulated

| Hardware or condition | In the emulator |
|---|---|
| Push button (NO contact) | Click or hold the red button; 30 ms debounce |
| NC contact and wire-break monitor | "Break the NC contact wire" switch; fault reported after 1 s |
| Power supply | Power off / on; "Power on, holding the button" |
| Flash memory | A folder per unit: settings, key and certificate, unsent presses, press sound, firmware version |
| Speaker | The browser plays the built-in beep or the installed WAV |
| Status LED | Same blink patterns as the firmware |
| Site Wi-Fi | A fixed set of networks (5 GHz, 2.4 GHz, one open); an on/off switch |
| Time server | An on/off switch |
| Setup access point | The firmware's real setup page, served on a second port while in setup mode |

Firmware behaviour that is reproduced:

- **Setup mode:** entered when there are no Wi-Fi settings, or when the button is held from power-up; the same validation as the firmware.
- **Wi-Fi:** secure networks only; prefers 5 GHz; returns to setup mode after six failed joins if the settings have never worked.
- **Identity:** makes its own key, enrols with the claim code, returns to setup mode after five rejections.
- **Presses:** queued in flash, sent one at a time, removed only when acknowledged, kept across outages and power cuts (200 at most).
- **Server link:** MQTT over TLS with the client certificate, last will, status reports.
- **Commands:** firmware update (with rollback if the new version cannot reach the server), sound update with checksum, certificate renewal with a new key, rename, play sound, restart.

Not emulated: radio behaviour, real audio hardware, secure boot and flash encryption, and exact timing below about 5 ms.

## Run a complete local rig

This starts the server, a small test broker and one emulated button on this computer. It uses the server's Python environment (see [../server/README.md](../server/README.md) for creating it).

```
cd emulator
..\server\.venv\Scripts\python run_rig.py
```

1. Open http://localhost:8000 and sign in with the address and password the rig prints.
2. On the Buttons page, add a button and note the claim code.
3. Open the button's control panel at http://localhost:8090 and follow its link to the setup page.
4. Enter a name, choose `Warehouse-Staff`, password `blue-forklift-42`, server address `localhost`, and the claim code.
5. The unit restarts and goes online. Click the red button; the press appears on the server.

Things to try from the control panel: switch the Wi-Fi off, press a few times, switch it back on; power off with presses waiting; break the NC wire; send a firmware or sound update from the server.

`run_rig.py --buttons 3` starts three buttons (panels on 8090, 8092, 8094). By default the rig uses short timings (3 s setup hold, status every 20 s); add `--real-timing` for the firmware's own.

## Run against a deployed server

```
..\server\.venv\Scripts\python button_emulator.py --serial BTN-E0E0E0E00001
```

Without `--dev` the emulator behaves like a real unit: HTTPS for enrolment, TLS on port 8883, and the server's certificate must chain to a public root. In the setup page, enter your server's host name.

## Automated end-to-end test

```
..\server\.venv\Scripts\python -m pytest test_e2e.py -q
```

It starts the real server, the test broker and an emulated button on spare ports and checks, over real HTTP, TLS and MQTT: setup and enrolment, presses, an outage with queued presses, a power cut, sound and firmware updates, rollback, certificate renewal, commands, the wiring fault, revocation and a wrong Wi-Fi password. It takes about 80 seconds.

## Files

| File | Purpose |
|---|---|
| `button_emulator.py` | The emulated unit, its control panel and its setup page |
| `panel.html` | The control panel page |
| `dev_broker.py` | Small MQTT broker for local testing, with the same access rules as the production Mosquitto configuration. Not for production |
| `run_rig.py` | Starts server, broker and buttons together |
| `test_e2e.py` | The end-to-end test |
