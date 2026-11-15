# Device–server protocol

This is the contract between the button firmware and the server. The firmware in [../firmware/](../firmware/) implements the device side and [../server/](../server/) implements the server side.

One host name serves both HTTPS (443) and MQTT over TLS (8883). Its certificate must chain to a public root, because devices verify it against the root list compiled into the firmware.

## Identity

- **Serial:** `BTN-` plus the 12 hex digits of the chip's base MAC, for example `BTN-A1B2C3D4E5F6`.
- **Key:** ECDSA P-256, generated on the device. The private key never leaves it.
- **Client certificate:** issued by the server's device CA with `CN=<serial>`. The broker must require a client certificate and restrict each client to `btn/<its CN>/#`.

## Enrolment (HTTPS, once per claim)

`POST https://<host>/api/device/enroll`

```json
{ "serial": "BTN-A1B2C3D4E5F6", "name": "Front desk", "claim": "K7Q2-9XPD", "csr": "-----BEGIN CERTIFICATE REQUEST-----\n..." }
```

| Response | Meaning | Device behaviour |
|---|---|---|
| `200 {"cert": "<PEM>"}` | Claim accepted | Stores the certificate, deletes the claim code, connects to MQTT |
| `401` or `403` | Claim code wrong, used or expired | After 5 rejections, returns to setup mode |
| anything else | Temporary failure | Retries every 30 s |

The claim code is single-use and is created in the server UI when an operator adds a button. The certificate's public key must match the CSR.

## MQTT session

- URI `mqtts://<host>:8883`, client id = serial, keep-alive 30 s, persistent session.
- Last will: `btn/<serial>/status`, retained, QoS 1, `{"online":false}`.

### Device publishes

| Topic | QoS | Retained | Payload |
|---|---|---|---|
| `btn/<serial>/press` | 1 | no | One press, see below |
| `btn/<serial>/status` | 1 | yes | State report, on connect and every 5 minutes |
| `btn/<serial>/csr` | 1 | no | `{"csr":"<PEM>"}` when the certificate is due for renewal |

Press payload:

```json
{ "seq": 412, "boot": 37, "ts": 1794668405.312, "age_ms": 180 }
```

| Field | Meaning |
|---|---|
| `seq` | Counter that never repeats for a device. The server must ignore a `(serial, seq)` it has already stored; the device resends until acknowledged. |
| `boot` | Boot counter. |
| `ts` | Unix time of the press by the device clock, in seconds with a millisecond fraction. Omitted if the clock was not set. |
| `age_ms` | Milliseconds between the press and this send. Omitted if the press was made before the last reboot. |

Server time rule: use `ts` if present; otherwise receive time minus `age_ms`; otherwise receive time, flagged as approximate.

The server also stores when it received each press. Delivery delay is receive time minus press time; it is not calculated for approximate presses.

Status payload:

```json
{ "online": true, "serial": "BTN-A1B2C3D4E5F6", "name": "Front desk", "fw": "0.1.0",
  "rssi": -58, "channel": 44, "ip": "192.168.1.73", "uptime_s": 86400, "pending": 0,
  "cert_not_after": 1797260405, "audio_sha": "9f2c…", "nc_fault": false }
```

`channel` above 14 means the unit is on 5 GHz. `nc_fault` is true when the NO and NC contacts agree for more than one second, which indicates a broken wire or a missing contact block.

### Device subscribes

`btn/<serial>/cmd/#`, QoS 1. The server publishes each command on `btn/<serial>/cmd/<type>`; each message is one JSON object that repeats the type.

| `type` | Other fields | Effect |
|---|---|---|
| `ota` | `version`, `url` | If `version` differs from the running version, download the signed image from `url` (HTTPS only), switch to it and reboot. The new image rolls back unless it reaches the broker within 10 minutes. |
| `audio` | `url`, `sha256` | Download a WAV file (HTTPS only, 1.5 MB maximum, 16-bit PCM, mono or stereo, 8–48 kHz), check its SHA-256 and make it the press sound. |
| `cert` | `cert` | Install a renewed client certificate. It must match the key of the last CSR the device sent. The device then reboots. |
| `name` | `name` | Change the display name. |
| `identify` | — | Play the press sound once. |
| `reboot` | — | Reboot. |

Publish `cmd/ota` and `cmd/audio` retained so the unit applies them again after any reconnect; both are no-ops when the version or checksum already matches. Do not retain the other types.

## Certificate renewal

1. When two thirds of the certificate lifetime has passed, the device generates a new key, keeps it as pending and publishes a CSR on `btn/<serial>/csr`.
2. The server signs it and publishes `{"type":"cert","cert":"<PEM>"}` on `btn/<serial>/cmd/cert`.
3. The device checks that the certificate matches the pending key, stores both and reboots onto them.
4. If no certificate arrives, the device sends a fresh CSR every 6 hours.

A device whose certificate has fully expired cannot connect. It needs a new claim code, entered through setup mode.
