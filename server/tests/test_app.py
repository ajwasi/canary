"""End-to-end tests. The MQTT broker is not needed: messages are fed straight
to the handler, and commands the server would publish are captured in mqtt.outbox."""
import io
import json
import os
import html
import re
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

os.environ.update(DATA_DIR=tempfile.mkdtemp(), MQTT_ENABLED="0", ADMIN_EMAIL="admin@test.local",
                  ADMIN_PASSWORD="correct horse battery", DEFAULT_TZ="UTC")

import pyotp
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app import ca, mqtt, security
from app.db import Button, Press, SessionLocal, User
from app.main import app

SERIAL = "BTN-A1B2C3D4E5F6"


def make_csr(serial: str = SERIAL, key=None) -> str:
    key = key or ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, serial)])).sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode()


class Browser:
    """A signed-in browser: keeps cookies and adds the CSRF token to every form."""

    def __init__(self, client: TestClient, email: str, password: str, code: str = ""):
        self.c = client
        self.c.cookies.clear()
        r = self.c.post("/login", data={"email": email, "password": password, "code": code}, follow_redirects=False)
        self.signed_in = r.status_code == 303
        self.csrf = ""
        if self.signed_in:
            page = self.c.get("/account").text
            self.csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def post(self, url, data=None, files=None):
        r = self.c.post(url, data={**(data or {}), "csrf": self.csrf}, files=files, follow_redirects=False)
        r.flash = ""
        if r.status_code == 303:                            # the message is shown once, on the next page
            page = self.c.get(r.headers["location"]).text
            found = re.search(r'<div class="flash">(.*?)</div>', page, re.S)
            r.flash = html.unescape(found.group(1)) if found else ""
        return r

    def get(self, url):
        return self.c.get(url)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    b = Browser(client, "admin@test.local", "correct horse battery")
    assert b.signed_in
    return b


def press(serial=SERIAL, **data):
    mqtt.handle_message(f"btn/{serial}/press", json.dumps(data).encode())


def test_pages_need_sign_in(client):
    client.cookies.clear()
    for url in ("/", "/activity", "/people", "/buttons", "/users", "/activity.csv"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", url


def test_full_flow(client, admin):
    # People, locations and a button.
    assert admin.post("/people", {"name": "Dana Whitfield"}).status_code == 303
    assert admin.post("/people", {"name": "Sam Ortega"}).status_code == 303
    assert admin.post("/locations", {"name": "Tulsa warehouse", "timezone_name": "America/Chicago"}).status_code == 303
    r = admin.post("/buttons", {"name": "Front desk", "person_id": "1", "location_id": "1"})
    claim = re.search(r"Claim code: ([A-Z0-9-]+)\.", r.flash).group(1)

    # Enrolment: wrong code refused, right code works once, in any letter case.
    key = ec.generate_private_key(ec.SECP256R1())
    body = {"serial": SERIAL, "name": "x", "claim": "AAAA-AAAA", "csr": make_csr(key=key)}
    assert client.post("/api/device/enroll", json=body).status_code == 403
    body["claim"] = claim.lower()
    r = client.post("/api/device/enroll", json=body)
    assert r.status_code == 200
    cert = x509.load_pem_x509_certificate(r.json()["cert"].encode())
    ca_cert = x509.load_pem_x509_certificate(ca.CA_CERT.read_bytes())
    ca_cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == SERIAL
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    assert timedelta(days=29) < cert.not_valid_after_utc - datetime.now(timezone.utc) <= timedelta(days=30)
    assert client.post("/api/device/enroll", json=body).status_code == 403

    # A CSR whose name is not the serial is refused.
    r2 = admin.post("/buttons", {"name": "Spare"})
    claim2 = re.search(r"Claim code: ([A-Z0-9-]+)\.", r2.flash).group(1)
    bad = {"serial": "BTN-000000000001", "claim": claim2, "csr": make_csr("BTN-FFFFFFFFFFFF")}
    assert client.post("/api/device/enroll", json=bad).status_code == 403

    # Presses: exact time, duplicate, age-based, and unknown time.
    t0 = datetime(2026, 11, 10, 18, 30, 5, tzinfo=timezone.utc)
    press(seq=0, boot=1, ts=int(t0.timestamp()), age_ms=120)
    press(seq=0, boot=1, ts=int(t0.timestamp()), age_ms=9000)       # resend
    press(seq=1, boot=1, age_ms=60000)
    press(seq=2, boot=1)
    press("BTN-000000000099", seq=0, boot=1)                         # unknown unit
    with SessionLocal() as db:
        rows = db.query(Press).order_by(Press.seq).all()
        assert [p.seq for p in rows] == [0, 1, 2]
        assert rows[0].pressed_at == t0.replace(tzinfo=None) and not rows[0].approximate
        assert 55 < (rows[1].received_at - rows[1].pressed_at).total_seconds() < 65 and not rows[1].approximate
        assert rows[2].approximate
        assert all(p.person_id == 1 and p.location_id == 1 for p in rows)

    # The press is shown in the location's time zone (Chicago is UTC-6 in November).
    page = admin.get("/activity").text
    assert "2026-11-10 12:30:05 CST" in page and "Dana Whitfield" in page and "Tulsa warehouse" in page
    assert "3 presses" in page
    assert "1 press<" in admin.get("/activity?start=2026-11-10&end=2026-11-10").text.replace("\n", "")
    csv_text = admin.get("/activity.csv?person=1").text
    assert csv_text.count("Front desk") == 3 and "2026-11-10 18:30:05" in csv_text

    # Status and last will.
    mqtt.handle_message(f"btn/{SERIAL}/status", json.dumps(
        {"online": True, "fw": "0.1.0", "rssi": -58, "channel": 44, "ip": "192.168.1.73", "pending": 0}).encode())
    page = admin.get("/buttons/1").text
    assert "Online" in page and "5 GHz" in page and "0.1.0" in page
    assert "Online" in admin.get("/?by=location").text
    mqtt.handle_message(f"btn/{SERIAL}/status", b'{"online":false}')
    assert "Offline" in admin.get("/buttons/1").text

    # Reassigning changes new presses only.
    assert admin.post("/buttons/1", {"name": "Front desk", "person_id": "2", "location_id": "1"}).status_code == 303
    press(seq=3, boot=1, age_ms=0)
    with SessionLocal() as db:
        assert [p.person_id for p in db.query(Press).order_by(Press.seq)] == [1, 1, 1, 2]
    assert "Front desk" in admin.get("/people/2").text

    # Certificate renewal over MQTT.
    mqtt.outbox.clear()
    mqtt.handle_message(f"btn/{SERIAL}/csr", json.dumps({"csr": make_csr()}).encode())
    topic, cmd, retain = mqtt.outbox[-1]
    assert topic == f"btn/{SERIAL}/cmd/cert" and cmd["type"] == "cert" and "BEGIN CERTIFICATE" in cmd["cert"]
    mqtt.outbox.clear()
    mqtt.handle_message(f"btn/{SERIAL}/csr", json.dumps({"csr": make_csr("BTN-000000000002")}).encode())
    assert not mqtt.outbox

    # Revoked buttons are ignored.
    assert admin.post("/buttons/1/revoke").status_code == 303
    press(seq=4, boot=1, age_ms=0)
    mqtt.handle_message(f"btn/{SERIAL}/csr", json.dumps({"csr": make_csr()}).encode())
    with SessionLocal() as db:
        assert db.query(Press).count() == 4
    assert not mqtt.outbox
    assert admin.post("/buttons/1/restore").status_code == 303


def test_updates(client, admin):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 1600)
    r = admin.post("/updates/audio", {"name": "Chime"}, files={"file": ("chime.wav", buf.getvalue(), "audio/wav")})
    assert r.status_code == 303, r.text
    assert admin.post("/updates/audio", files={"file": ("x.wav", b"not a wav", "audio/wav")}).status_code == 400
    assert admin.post("/updates/firmware", {"version": "0.2.0"},
                      files={"file": ("fw.bin", b"\x00" * 64, "application/octet-stream")}).status_code == 400
    assert admin.post("/updates/firmware", {"version": "0.2.0"},
                      files={"file": ("fw.bin", b"\xe9" + b"\x00" * 64, "application/octet-stream")}).status_code == 303

    mqtt.outbox.clear()
    assert admin.post("/updates/push", {"kind": "audio", "item": "1", "targets": "1"}).status_code == 303
    assert admin.post("/updates/push", {"kind": "firmware", "item": "1", "targets": "1"}).status_code == 303
    (t1, audio, r1), (t2, ota, r2) = mqtt.outbox
    assert t1 == f"btn/{SERIAL}/cmd/audio" and r1 and len(audio["sha256"]) == 64
    assert t2 == f"btn/{SERIAL}/cmd/ota" and r2 and ota["version"] == "0.2.0"
    client.cookies.clear()                                           # devices download without signing in
    path = audio["url"].split("localhost:8000")[1]
    assert client.get(path).content == buf.getvalue()
    assert client.get("/files/audio/nope.wav").status_code == 404
    assert client.get(ota["url"].split("localhost:8000")[1]).content[0] == 0xE9


def test_users_roles_and_csrf(client, admin):
    admin = Browser(client, "admin@test.local", "correct horse battery")
    r = admin.post("/users", {"email": "viewer@test.local", "name": "Vera", "role": "viewer"})
    temp = re.search(r"viewer@test.local: (\S+)\. It", r.flash).group(1)
    assert admin.post("/users/1", {"role": "viewer", "active": "1"}).status_code == 400   # cannot demote self

    viewer = Browser(client, "viewer@test.local", temp)
    assert viewer.signed_in
    # A temporary password must be changed before anything else.
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert viewer.post("/account/password", {"current": temp, "new": "short"}).status_code == 400
    assert viewer.post("/account/password", {"current": temp, "new": "a much longer password"}).status_code == 303
    assert client.get("/").status_code == 200
    # Viewers can read but not change; forms without the CSRF token are refused.
    assert viewer.get("/people").status_code == 200
    assert viewer.post("/people", {"name": "Nope"}).status_code == 403
    assert viewer.get("/users").status_code == 403
    assert client.post("/account/password", data={"current": "x", "new": "y" * 20}).status_code == 403

    # Disabling the user ends their session.
    admin2 = Browser(TestClient(app), "admin@test.local", "correct horse battery")
    assert admin2.post("/users/2", {"role": "viewer"}).status_code == 303                # "active" unchecked
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_two_step_and_lockout(client):
    me = Browser(client, "admin@test.local", "correct horse battery")
    assert me.post("/account/totp/start").status_code == 303
    with SessionLocal() as db:
        secret = security.decrypt(db.query(User).filter_by(email="admin@test.local").one().totp_secret)
    assert "<svg" in me.get("/account").text
    assert me.post("/account/totp/enable", {"code": "000000"}).status_code == 400
    assert me.post("/account/totp/enable", {"code": pyotp.TOTP(secret).now()}).status_code == 303

    assert not Browser(client, "admin@test.local", "correct horse battery").signed_in           # code missing
    assert Browser(client, "admin@test.local", "correct horse battery", pyotp.TOTP(secret).now()).signed_in

    for _ in range(security.MAX_FAILED):
        assert not Browser(client, "admin@test.local", "wrong password").signed_in
    # Locked: even the right password and code are refused.
    assert not Browser(client, "admin@test.local", "correct horse battery", pyotp.TOTP(secret).now()).signed_in
    with SessionLocal() as db:
        assert db.query(User).filter_by(email="admin@test.local").one().locked_until is not None
        assert db.query(Button).count() == 2


def test_chart_buckets():
    from app import chart

    def row(d, h, m, delay_s=0.5, approx=False):
        pressed = datetime(2026, 11, d, h, m)
        return pressed, pressed + timedelta(seconds=delay_s), approx
    rows = [row(10, 18, 30, 0.4), row(10, 19, 5, 0.6), row(12, 3, 0, 5400), row(12, 3, 1, 0, approx=True)]   # UTC
    c = chart.build(rows, "2026-11-09", "2026-11-13", "", "America/Chicago")
    assert c["group"] == "day" and c["total"] == 4 and len(c["counts"]["bars"]) == 5
    assert [v for _, v in c["counts"]["table"]] == ["0", "2", "2", "0", "0"]    # 03:00 UTC on the 12th is the 11th in Chicago
    assert c["counts"]["bars"][1]["label"] == "Tue Nov 10, 2026" and c["peak"] == 2
    # Delay: the approximate press is left out; medians per day; the outage shows up.
    st = c["delay_stats"]
    assert (st["timed"], st["untimed"], st["late"], st["slowest"], st["typical"]) == (3, 1, 1, "1 h 30 min", "600 ms")
    assert c["delay"]["table"] == [("Tue Nov 10, 2026", "typical 500 ms, slowest 600 ms"),
                                   ("Wed Nov 11, 2026", "typical 1 h 30 min, slowest 1 h 30 min")]
    assert c["delay"]["y_ticks"][-1]["text"].endswith(" min")
    hourly = chart.build(rows, "2026-11-10", "2026-11-10", "", "UTC")
    assert hourly["group"] == "hour" and len(hourly["counts"]["bars"]) == 24 and hourly["total"] == 2
    assert hourly["delay"]["y_ticks"][-1]["text"].endswith(" ms")
    assert chart.build(rows, "2026-11-01", "2026-11-30", "week", "UTC")["total"] == 4
    assert chart.build(rows, "2020-01-01", "2026-11-30", "hour", "UTC")["group"] != "hour"   # too many bars: coarsened
    assert chart.build([], "", "", "", "UTC") is None
    assert "delay" not in chart.build([row(10, 1, 0, approx=True)], "", "", "", "UTC")
    assert [chart.fmt_delay(v) for v in (None, 0.042, 12.34, 75, 11520, 200000)] == \
        ["unknown", "42 ms", "12.3 s", "1 min 15 s", "3 h 12 min", "2 d 7 h"]


def test_activity_page_has_chart(client):
    with SessionLocal() as db:                                         # earlier tests left this account locked
        u = db.query(User).filter_by(email="admin@test.local").one()
        u.locked_until, u.totp_enabled = None, False
        db.commit()
    me = Browser(client, "admin@test.local", "correct horse battery")
    page = me.get("/activity?start=2026-11-10&end=2026-11-10").text
    assert "Presses per hour" in page and "Delivery delay per hour" in page and 'class="hit"' in page
    assert "delay_seconds" in me.get("/activity.csv").text
    assert "Presses per month" in me.get("/activity?group=month").text
    assert "No presses to chart" in me.get("/activity?button=2").text


def test_redirects_stay_on_this_site_and_messages_stay_off_the_cookie(client):
    from app.main import local_path
    assert local_path("/buttons/1") == "/buttons/1"
    for bad in ("https://evil.example/", "//evil.example", "/\evil.example", "javascript:alert(1)", "evil", "/a\r\nb"):
        assert local_path(bad) == "/"
    admin = Browser(client, "admin@test.local", "correct horse battery")
    r = admin.post("/people", {"name": "Cookie Check"})
    assert r.status_code == 303 and "flash" in r.cookies and "Cookie Check" not in r.cookies["flash"]
    assert "Cookie Check" in r.flash
