"""Builds click-through mocks of every screen into docs/mocks/.

The server screens are the real pages rendered with sample data; the button
setup screens are the firmware's real portal.html with its network calls faked.
Run from the server folder:  .venv\\Scripts\\python tools\\make_mocks.py
"""
import io
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
import wave
from datetime import timedelta
from pathlib import Path

ADMIN_PW = secrets.token_urlsafe(18)       # new every run; the sample site exists only for this run
os.environ.update(DATA_DIR=tempfile.mkdtemp(), MQTT_ENABLED="0", ADMIN_EMAIL="morgan@example.com",
                  ADMIN_PASSWORD=ADMIN_PW, DEFAULT_TZ="America/New_York")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

import pyotp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app import chart, mqtt, security
from app.db import Press, SessionLocal, User
from app.main import app

OUT = ROOT / "docs" / "mocks"
CSS = (ROOT / "server" / "app" / "static" / "app.css").read_text(encoding="utf-8")
CHART_JS = (ROOT / "server" / "app" / "static" / "chart.js").read_text(encoding="utf-8")
LINKS = {"/": "overview-by-person.html", "/?by=person": "overview-by-person.html",
         "/?by=location": "overview-by-location.html", "/activity": "activity.html", "/people": "people.html",
         "/locations": "locations.html", "/buttons": "buttons.html", "/updates": "updates.html",
         "/users": "users.html", "/account": "account.html", "/people/1": "person.html",
         "/locations/1": "location.html", "/buttons/1": "button.html", "/buttons/5": "button-new.html"}
screens: list[tuple[str, str, str, str]] = []          # (group, file, title, note)


def save(group: str, name: str, title: str, note: str, html: str) -> None:
    """Make the page self-contained and point its links at the other mock files."""
    html = html.replace('<link rel="stylesheet" href="/static/app.css">', f"<style>{CSS}</style>")
    html = html.replace('<meta http-equiv="refresh" content="30">', "")
    html = html.replace('<script src="/static/chart.js" defer></script>', f"<script>{CHART_JS}</script>")
    html = re.sub(r'href="(/[^"]*)"', lambda m: f'href="{LINKS.get(m.group(1), "#")}"', html)
    html = html.replace("<form ", '<form onsubmit="return false" ')
    (OUT / group).mkdir(parents=True, exist_ok=True)
    (OUT / group / f"{name}.html").write_text(html, encoding="utf-8")
    screens.append((group, name, title, note))


class Repeatable:
    """A repeatable stream of numbers (SHA-256 of a counter) for the sample history, so every run draws the
    same five weeks. It is for sample data only."""

    def __init__(self, seed: int):
        self.seed, self.count = str(seed).encode(), 0

    def random(self) -> float:
        self.count += 1
        digest = hashlib.sha256(self.seed + b":" + str(self.count).encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2 ** 64

    def randrange(self, start: int, stop: int | None = None) -> int:
        if stop is None:
            start, stop = 0, start
        return start + int(self.random() * (stop - start))

    def choice(self, items):
        return items[self.randrange(len(items))]

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self.random()

    def lognormvariate(self, mu: float, sigma: float) -> float:
        z = math.sqrt(-2 * math.log(1 - self.random())) * math.cos(2 * math.pi * self.random())
        return math.exp(mu + sigma * z)


def csr(serial: str) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    req = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, serial)])).sign(key, hashes.SHA256()))
    return req.public_bytes(serialization.Encoding.PEM).decode()


class Browser:
    def __init__(self, email: str, password: str, code: str = ""):
        self.c = TestClient(app)
        self.c.post("/login", data={"email": email, "password": password, "code": code})
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', self.c.get("/account").text).group(1)

    def post(self, url, data=None, files=None):
        return self.c.post(url, data={**(data or {}), "csrf": self.csrf}, files=files)   # follows the redirect

    def get(self, url):
        return self.c.get(url).text


def server_mocks() -> None:
    S = "server"
    with TestClient(app) as anon:
        save(S, "sign-in", "Sign in", "Email, password, and the authenticator code if two-step sign-in is on.",
             anon.get("/login").text)
        save(S, "sign-in-refused", "Sign in refused", "One message for every failure, so it never reveals which part was wrong.",
             anon.post("/login", data={"email": "morgan@example.com", "password": "nope"}).text)

        a = Browser("morgan@example.com", ADMIN_PW)
        for name in ("Dana Whitfield", "Sam Ortega", "Priya Raman"):
            a.post("/people", {"name": name})
        a.post("/locations", {"name": "Tulsa warehouse", "address": "410 E Archer St, Tulsa, OK",
                              "timezone_name": "America/Chicago", "notes": "Dock door 3 is the night entrance."})
        a.post("/locations", {"name": "Portland office", "address": "88 SW Oak St, Portland, OR",
                              "timezone_name": "America/Los_Angeles"})
        units = [("Front desk", 1, 1, 44, True), ("Loading dock", 1, 1, 6, False), ("Reception", 2, 2, 149, True),
                 ("Workshop", 3, 2, 11, True)]
        for i, (name, person, loc, channel, online) in enumerate(units):
            page = a.post("/buttons", {"name": name, "person_id": str(person), "location_id": str(loc)}).text
            claim = re.search(r"Claim code: ([A-Z0-9-]+)\.", page).group(1)
            serial = f"BTN-A1B2C3D4E5F{i}"
            assert anon.post("/api/device/enroll", json={"serial": serial, "claim": claim, "csr": csr(serial)}).status_code == 200
            mqtt.handle_message(f"btn/{serial}/status", json.dumps(
                {"online": True, "fw": "0.1.0", "rssi": -52 - 6 * i, "channel": channel, "ip": f"192.168.1.{70 + i}",
                 "nc_fault": i == 3}).encode())
            for s in range(3 + i):
                mqtt.handle_message(f"btn/{serial}/press", json.dumps(
                    {"seq": s, "boot": 1, "age_ms": 60000 * (4 + 9 * i + 171 * s * (i + 1))}).encode())
            if not online:
                mqtt.handle_message(f"btn/{serial}/status", b'{"online":false}')
        mqtt.handle_message("btn/BTN-A1B2C3D4E5F1/press", b'{"seq": 9, "boot": 2}')      # clock unknown
        rng = Repeatable(7)                                                         # five weeks of history
        for n in range(260):
            unit = rng.choice([0, 0, 0, 1, 2, 2, 3])
            day, hour = rng.randrange(1, 36), rng.choice([7, 8, 8, 9, 10, 11, 13, 14, 15, 16, 16, 17, 20])
            if day % 7 in (0, 6) and rng.random() < 0.75:
                continue                                                                # quiet weekends
            mqtt.handle_message(f"btn/BTN-A1B2C3D4E5F{unit}/press", json.dumps(
                {"seq": 100 + n, "boot": 1, "age_ms": 60000 * (day * 1440 - hour * 60 + rng.randrange(60))}).encode())

        with SessionLocal() as db:
            for p in db.query(Press).all():
                slow = p.pressed_at.day in (22, 23) and p.button_id == 3          # a two-day Wi-Fi outage at one site
                seconds = rng.uniform(1800, 30 * 3600) if slow else rng.lognormvariate(-0.9, 0.5)
                p.received_at = p.pressed_at + timedelta(seconds=seconds)
            db.commit()

        save(S, "button-new", "Button: just added", "The claim code is shown once. Type it into the button's setup page.",
             a.post("/buttons", {"name": "Spare unit"}).text)

        wav = io.BytesIO()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\x00\x01" * 24000)
        a.post("/updates/audio", {"name": "Soft chime"}, files={"file": ("chime.wav", wav.getvalue(), "audio/wav")})
        a.post("/updates/firmware", {"version": "0.2.0"}, files={"file": ("fw.bin", b"\xe9" + bytes(1_180_000), "application/octet-stream")})
        a.c.post("/updates/push", data={"csrf": a.csrf, "kind": "firmware", "item": "1", "targets": ["1", "2"]})

        save(S, "overview-by-person", "Overview by person", "The home screen. Each person with their buttons, state, last press and 24-hour count.", a.get("/"))
        save(S, "overview-by-location", "Overview by location", "The same buttons grouped by where they are.", a.get("/?by=location"))
        save(S, "activity", "Activity", "Charts of presses and of delivery delay over time, then every press. Hover a bar for its numbers. Filter by person, location, button and date.", a.get("/activity"))
        week = next(p for p in chart.presets("America/New_York") if p[0] == "7 days")
        save(S, "activity-filtered", "Activity, filtered", "One person, last 7 days. The chart, the table and the CSV download all follow the filters.",
             a.get(f"/activity?person=1&start={week[1]}&end={week[2]}"))
        save(S, "activity-weekly", "Activity, by week", "The same data with one bar per week.", a.get("/activity?group=week"))
        save(S, "people", "People", "List and add form.", a.get("/people"))
        save(S, "person", "Person", "One person's buttons and recent presses, with their details.", a.get("/people/1"))
        save(S, "locations", "Locations", "Locations carry an address and a time zone.", a.get("/locations"))
        save(S, "location", "Location", "One location's buttons and recent presses.", a.get("/locations/1"))
        save(S, "buttons", "Buttons", "All buttons with state, assignment and firmware. 'Updating' shows a rollout in progress.", a.get("/buttons"))
        save(S, "button", "Button", "Live status, recent presses, assignment and actions for one button.", a.get("/buttons/1"))
        a.post("/buttons/4/revoke")
        save(S, "button-revoked", "Button: revoked", "A revoked button's presses are ignored. It can be restored.", a.get("/buttons/4"))
        a.post("/buttons/4/restore")
        mqtt.handle_message("btn/BTN-A1B2C3D4E5F3/status", json.dumps(
            {"online": True, "fw": "0.1.0", "rssi": -70, "channel": 11, "ip": "192.0.2.73", "nc_fault": True}).encode())
        save(S, "updates", "Updates", "Upload firmware and press sounds, then send them to chosen buttons.", a.get("/updates"))

        a.post("/users", {"email": "lee@example.com", "name": "Lee Okafor", "role": "manager"})
        page = a.post("/users", {"email": "vera@example.com", "name": "Vera Lindqvist", "role": "viewer"}).text
        save(S, "users", "Users", "Administrators only. The temporary password is shown once. The change log is at the bottom.", page)
        temp = re.search(r"vera@example.com: (\S+)\. It", page).group(1)

        v = Browser("vera@example.com", temp)
        save(S, "account-must-change", "First sign-in", "A temporary password must be replaced before anything else opens.", v.get("/account"))
        v.post("/account/password", {"current": temp, "new": "a much longer password"})
        save(S, "buttons-viewer", "Buttons, as a viewer", "Viewers see everything but get no forms or actions.", v.get("/buttons"))
        save(S, "not-allowed", "Not allowed", "What a viewer sees on an administrator page.", v.get("/users"))

        save(S, "account", "Account", "Change password; turn two-step sign-in on or off.", a.get("/account"))
        save(S, "account-two-step-setup", "Two-step setup", "Scan the QR code, then confirm with a code.", a.post("/account/totp/start").text)
        with SessionLocal() as db:
            secret = security.decrypt(db.query(User).filter_by(email="morgan@example.com").one().totp_secret)
        save(S, "account-two-step-on", "Two-step on", "Turning it off needs the password.",
             a.post("/account/totp/enable", {"code": pyotp.TOTP(secret).now()}).text)


PORTAL_STUB = """<script>
const STATE = "%s";
const NETWORKS = [
  {ssid: "Warehouse-Staff", rssi: -48, band5: true, secure: true}, {ssid: "Warehouse-Staff", rssi: -44, band5: false, secure: true},
  {ssid: "Scanner-Net", rssi: -61, band5: false, secure: true}, {ssid: "Tulsa-Guest", rssi: -57, band5: false, secure: false},
  {ssid: "NETGEAR-2G", rssi: -79, band5: false, secure: true}];
const reply = data => ({json: async () => data});
window.fetch = async url => {
  if (url === "/info") return reply({serial: "BTN-A1B2C3D4E5F6", fw: "0.1.0", host: "buttons.example.com",
                                     name: STATE === "reclaim" ? "Front desk" : "", enrolled: STATE === "reclaim"});
  if (url === "/scan") return STATE === "scanning" ? new Promise(() => {}) : reply(NETWORKS);
  if (STATE === "lost") throw new Error("offline");
  return reply({ok: true});
};
window.addEventListener("load", () => setTimeout(() => {
  if (STATE === "empty" || STATE === "scanning") return;
  const set = (id, v) => { document.getElementById(id).value = v; };
  set("name", "Front desk"); set("ssid", "Warehouse-Staff"); set("pass", "blue-forklift-42");
  if (STATE !== "reclaim") set("claim", "K7Q2-9XPD");
  if (STATE === "saved" || STATE === "lost") document.getElementById("f").requestSubmit();
}, 150));
</script>
<script>"""


def firmware_mocks() -> None:
    portal = (ROOT / "firmware" / "main" / "portal.html").read_text(encoding="utf-8")
    states = [
        ("scanning", "1-scanning", "Looking for networks", "What opens when a phone joins the button's own Wi-Fi."),
        ("empty", "2-empty", "Ready to fill in", "Networks found; the server address is pre-filled."),
        ("filled", "3-filled", "Filled in", "5 GHz networks are labelled. Open networks are listed but cannot be chosen."),
        ("saved", "4-saved", "Saved", "The button restarts and joins the chosen network."),
        ("lost", "5-connection-lost", "Connection lost", "Shown if the phone dropped off the button's Wi-Fi before saving."),
        ("reclaim", "6-already-claimed", "Changing Wi-Fi later", "A button that is already claimed needs no claim code."),
    ]
    (OUT / "firmware").mkdir(parents=True, exist_ok=True)
    for state, name, title, note in states:
        (OUT / "firmware" / f"{name}.html").write_text(portal.replace("<script>", PORTAL_STUB % state, 1), encoding="utf-8")
        screens.append(("firmware", name, title, note))


def gallery() -> None:
    def cards(group: str, cls: str) -> str:
        return "".join(
            f'<figure class="{cls}"><a href="{g}/{n}.html"><img src="{g}/{n}.png" alt="{t}" loading="lazy"></a>'
            f"<figcaption><b>{t}</b><span>{note}</span></figcaption></figure>"
            for g, n, t, note in screens if g == group)
    (OUT / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Button Notifier screens</title>
<style>
body {{ margin: 0; font: 15px/1.45 system-ui, "Segoe UI", sans-serif; background: #ecebe6; color: #1d1f23; }}
main {{ max-width: 1280px; margin: 0 auto; padding: 24px 16px 60px; }}
h1 {{ margin: 0 0 4px; }} h2 {{ margin: 36px 0 4px; }} p {{ margin: 0 0 16px; color: #555b63; max-width: 70ch; }}
.row {{ display: grid; gap: 20px; }}
.phones {{ grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }}
.desktops {{ grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); }}
figure {{ margin: 0; background: #fff; border: 1px solid #d6d4cc; border-radius: 10px; overflow: hidden; }}
figure img {{ display: block; width: 100%; border-bottom: 1px solid #d6d4cc; }}
figure.desktop img {{ aspect-ratio: 4 / 3; object-fit: cover; object-position: top; }}
figcaption {{ padding: 10px 12px; }} figcaption b {{ display: block; }} figcaption span {{ color: #555b63; font-size: 13px; }}
</style></head><body><main>
<h1>Button Notifier screens</h1>
<p>Click any screen to open a click-through copy. The navigation works; forms and actions are switched off.</p>
<h2>Button setup page</h2>
<p>Served by the button itself at http://192.168.4.1/ while it is in setup mode. Built for a phone.</p>
<div class="row phones">{cards("firmware", "phone")}</div>
<h2>Server</h2>
<p>The web application, shown with sample data.</p>
<div class="row desktops">{cards("server", "desktop")}</div>
</main></body></html>""", encoding="utf-8")
    (OUT / "screens.json").write_text(json.dumps([[g, n] for g, n, _, _ in screens]), encoding="utf-8")


if __name__ == "__main__":
    firmware_mocks()
    server_mocks()
    gallery()
    print(f"{len(screens)} screens written to {OUT}")
