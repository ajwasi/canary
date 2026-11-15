# Button notifier

A Wi-Fi push button built into an Omron A22NZ-A-B01Y control box. Each press is logged by a server with its date and time.

| Part | Where | State |
|---|---|---|
| Hardware design, wiring, assembly | [docs/hardware.md](docs/hardware.md) | Designed, not yet built |
| Datasheets | [docs/datasheets/](docs/datasheets/) | Downloaded |
| Device–server protocol | [docs/protocol.md](docs/protocol.md) | Defined |
| Button firmware (ESP-IDF, ESP32-C5) | [firmware/](firmware/) | Written, not yet compiled |
| Server and web UI | [server/](server/) | Tested locally; Docker deployment not yet run |
| Button simulator (no hardware needed) | [server/tools/sim_device.py](server/tools/sim_device.py) | Enrolment tested; MQTT part not yet run |
| Screen mocks (button setup page and every server screen) | [docs/mocks/index.html](docs/mocks/index.html) | Generated from the real pages with sample data |
| Button emulator and local test rig | [emulator/](emulator/) | End-to-end test passes against the real server |
