# Button server

Logs every button press with its date and time, and provides the web UI for people, locations, buttons, updates and users. It implements the server side of [../docs/protocol.md](../docs/protocol.md).

**Status:** the application passes its tests and runs locally. The Docker deployment files (`compose.yml`, `Caddyfile`, `mosquitto/`) have not been run, because Docker was not available where this was written.

## What it does

- **Overview by person or by location:** each person or location with their buttons, online state, last press and 24-hour count.
- **Activity chart and log:** a bar chart of presses per hour, day, week or month, then every press. Both follow the same filters (person, location, button, date range, quick ranges), and the log exports to CSV. Each press is shown in its location's time zone.
- **Buttons:** add a button and get a claim code, assign it to a person and a location, rename, restart, play its sound, revoke.
- **Updates:** upload firmware and press sounds and send them to chosen buttons.
- **Users:** viewer, manager and administrator roles, temporary passwords, optional two-step sign-in, lockout after five failed sign-ins, and a change log.
- **Device CA:** signs each button's 30-day client certificate at enrolment and renews it over MQTT.

A press keeps the person and location the button had when it was pressed, so moving a button does not rewrite history.

## Parts

| Part | Role |
|---|---|
| `app/` | FastAPI application: UI, enrolment API, device CA, MQTT handling |
| PostgreSQL | Data (SQLite when run locally) |
| Mosquitto | MQTT broker; buttons connect on 8883 with TLS and a client certificate |
| Caddy | HTTPS on 443 with an automatically renewed Let's Encrypt certificate |

## Try it locally, without hardware or Docker

```
cd server
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
set MQTT_ENABLED=0
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

`requirements.txt` and `requirements-dev.txt` are hash-locked (every package and its dependencies, pinned with checksums), and the Docker image and CI install them with `--require-hashes --only-binary=:all:`. To change a dependency, edit `requirements.in` (or `requirements-dev.in`) and regenerate:

```
uv pip compile --universal --generate-hashes --python-version 3.11 --no-header requirements.in -o requirements.txt
uv pip compile --universal --generate-hashes --python-version 3.11 --no-header requirements-dev.in -o requirements-dev.txt
```

Create the first administrator, which prompts for a password:

```
.venv\Scripts\python -m app.create_admin
```

Then open http://localhost:8000 and sign in. (Setting `ADMIN_EMAIL` and `ADMIN_PASSWORD` in the environment before the first start creates the account for you instead.)

With `MQTT_ENABLED=0` there is no broker, so no presses arrive; you can explore the UI and test enrolment:

```
.venv\Scripts\python tools\sim_device.py --server http://localhost:8000 --claim XXXX-XXXX --enrol-only
```

## Deploy

You need a Linux host with Docker, a DNS name pointing at it, and ports 80, 443 and 8883 open.

1. Copy `.env.example` to `.env` and fill in every value.
2. Run `docker compose up -d --build`.
3. Run `docker compose exec app python -m app.create_admin` and enter a password for the first administrator.
4. Sign in at `https://<your domain>` and turn on two-step sign-in under Account.
5. Set the same domain as the default server host in the firmware (`idf.py menuconfig`).

To test the whole path before hardware arrives, run the simulator from any computer:

```
python tools/sim_device.py --server https://<your domain> --claim XXXX-XXXX
```

Press Enter to send a press; it appears on the overview within 30 seconds.

## Security

| Area | How |
|---|---|
| Browser to server | HTTPS only (Caddy), HSTS, secure HTTP-only session cookie, CSRF token on every form |
| Button to server | TLS on both channels; MQTT also needs a client certificate; each button can use only its own topics |
| Passwords | Argon2id; 12 characters minimum; lockout for 15 minutes after 5 failures |
| Two-step sign-in | TOTP authenticator codes, per user |
| Secrets at rest | Device CA key and two-step secrets are encrypted with `SECRET_KEY`; claim codes and session tokens are stored only as hashes |
| Database at rest | Not encrypted by the app. Put the Docker volumes on an encrypted disk (LUKS or your cloud provider's volume encryption) |

## Limits to know about

- **Revoking is not instant at the broker.** A revoked button's presses are ignored immediately and its certificate is never renewed, but the broker accepts its connection until the certificate expires (30 days at most).
- **The device CA lasts 10 years** and has no rotation procedure yet.
- **No schema migrations.** Tables are created on first start; a later change to the data model needs a migration tool such as Alembic.
- **One app instance only.** Running two would double-handle MQTT messages.
- **The internal MQTT listener (1883) has no password.** It is reachable only inside the Docker network; never publish that port.
- **Back up** the `pg_data` and `app_data` volumes and the `.env` file together.
