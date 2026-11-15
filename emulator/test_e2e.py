"""End-to-end test: the real server, the dev broker and an emulated button,
talking over real HTTP, TLS and MQTT on localhost.

    ..\\server\\.venv\\Scripts\\python -m pytest test_e2e.py -q
"""
import asyncio
import io
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

import dev_broker
from button_emulator import Button, Timing, run_servers

HTTP, PLAIN, TLS, PANEL, AP = 18000, 18830, 18831, 18090, 18091
SERVER = f"http://localhost:{HTTP}"
SERIAL = "BTN-E0E0E0E00001"
ROOT = Path(__file__).resolve().parents[1]


def wait(cond, timeout=20, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class Admin:
    def __init__(self):
        self.c = httpx.Client(base_url=SERVER, timeout=20)
        r = self.c.post("/login", data={"email": "admin@test.local", "password": "correct horse battery"})
        assert r.status_code == 303
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', self.c.get("/account").text).group(1)

    def post(self, url, data=None, files=None):
        r = self.c.post(url, data={**(data or {}), "csrf": self.csrf}, files=files)
        assert r.status_code == 303, r.text
        flash = unquote(r.cookies.get("flash", ""))
        self.c.cookies.delete("flash")
        return flash

    def page(self, url):
        return self.c.get(url).text

    def presses(self):
        return self.page("/activity.csv").strip().splitlines()[1:]


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rig")
    env = {**os.environ, "DATA_DIR": str(tmp / "server"), "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(PLAIN),
           "MQTT_ENABLED": "1", "PUBLIC_URL": SERVER, "ADMIN_EMAIL": "admin@test.local",
           "ADMIN_PASSWORD": "correct horse battery", "DEFAULT_TZ": "UTC"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(HTTP)],
                              cwd=ROOT / "server", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def healthy():
            try:
                return httpx.get(SERVER + "/healthz", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False
        wait(healthy, 30, "server start")

        state = tmp / "emu"
        threading.Thread(target=lambda: asyncio.run(dev_broker.serve(
            tmp / "server" / "ca" / "device-ca.pem", state, TLS, PLAIN)), daemon=True).start()
        wait(lambda: (state / "dev-server.pem").exists(), 10, "broker certificate")

        t = Timing()
        t.setup_hold_s, t.status_every_s, t.rollback_after_s, t.enroll_retry_s = 1.0, 2.0, 6.0, 1.0
        t.wifi_retry_max_s, t.boot_s = 0.5, 0.1
        button = Button(SERIAL, state, http_base=SERVER, mqtt_port=TLS, cafile=str(state / "dev-server.pem"),
                        allow_http=True, timing=t)
        threading.Thread(target=lambda: asyncio.run(run_servers(button, PANEL, AP)), daemon=True).start()
        yield button, Admin()
        button.power_off()
    finally:
        server.terminate()


def portal(path, body=None):
    url = f"http://127.0.0.1:{AP}{path}"
    return httpx.post(url, json=body, timeout=5) if body is not None else httpx.get(url, timeout=5)


def test_everything(rig):
    button, admin = rig
    good = {"name": "Front desk", "ssid": "Warehouse-Staff", "pass": "blue-forklift-42", "host": "localhost"}

    # ---- a new unit starts in setup mode and serves the firmware's own setup page ----
    button.power_on()
    wait(lambda: button.mode == "setup", what="setup mode")
    wait(lambda: portal("/").status_code == 200, what="setup page")
    assert "Button setup" in portal("/").text
    nets = portal("/scan").json()
    assert {"ssid": "Tulsa-Guest", "rssi": -57, "band5": False, "secure": False} in nets
    assert portal("/save", {**good, "pass": "short", "claim": "x"}).json()["ok"] is False
    assert "claim code" in portal("/save", good).json()["error"]

    admin.post("/people", {"name": "Dana Whitfield"})
    admin.post("/locations", {"name": "Tulsa warehouse", "timezone_name": "America/Chicago"})
    flash = admin.post("/buttons", {"name": "Front desk", "person_id": "1", "location_id": "1"})
    claim = re.search(r"Claim code: ([A-Z0-9-]+)\.", flash).group(1)
    assert portal("/save", {**good, "claim": claim}).json() == {"ok": True}

    # ---- it restarts, joins the 5 GHz access point, enrols and connects ----
    wait(lambda: button.mode == "online", what="online")
    assert button.wifi["channel"] == 44 and button.has_cert() and "claim" not in button.cfg
    assert portal("/").status_code == 503                         # setup page is gone outside setup mode
    wait(lambda: "Online" in admin.page("/buttons/1"), what="server shows online")
    assert SERIAL in admin.page("/buttons/1") and "5 GHz" in admin.page("/buttons/1")

    # ---- presses arrive and leave the unit's queue ----
    button.press(); button.press()
    wait(lambda: len(admin.presses()) == 2 and not button.queue(), what="two presses logged")
    assert button.sounds_played == 2 and all("Dana Whitfield" in row for row in admin.presses())

    # ---- Wi-Fi outage: the server sees the unit go offline; presses are kept and delivered later ----
    button.env.wifi_up = False
    wait(lambda: "Offline" in admin.page("/buttons/1"), what="last will marks it offline")
    for _ in range(3):
        button.press()
    assert len(button.queue()) == 3 and len(admin.presses()) == 2
    stamp = button.queue()[0]["ts"]
    button.env.wifi_up = True
    wait(lambda: len(admin.presses()) == 5 and not button.queue(), what="queued presses delivered")
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(stamp)) in "\n".join(admin.presses())

    # ---- a press made before a power cut, with no clock afterwards, is logged as approximate ----
    button.env.wifi_up = False
    wait(lambda: button.mode == "wifi")
    button.clock_set = False
    button.press()
    button.power_off()
    assert len(button.queue()) == 1
    button.env.clock_up, button.env.wifi_up = False, True
    button.power_on()
    wait(lambda: len(admin.presses()) == 6, what="press from before the power cut")
    assert sum(row.endswith(",yes") for row in admin.presses()) == 1
    button.env.clock_up = True

    # ---- press sound update ----
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\x00\x01" * 800)
    admin.post("/updates/audio", {"name": "Chime"}, files={"file": ("c.wav", buf.getvalue(), "audio/wav")})
    admin.post("/updates/push", {"kind": "audio", "item": "1", "targets": "1"})
    wait(lambda: (button.dir / "press.wav").exists(), what="sound installed")
    assert (button.dir / "press.wav").read_bytes() == buf.getvalue()
    wait(lambda: "Custom" in admin.page("/buttons/1"), what="server sees the new sound")

    # ---- firmware update: applied, restarted, confirmed ----
    image = b"\xe9" + bytes(4096)
    admin.post("/updates/firmware", {"version": "0.2.0"}, files={"file": ("fw.bin", image, "application/octet-stream")})
    admin.post("/updates/push", {"kind": "firmware", "item": "1", "targets": "1"})
    wait(lambda: button.cfg.get("fw") == "0.2.0" and not button.cfg.get("fw_pending") and button.mode == "online",
         what="firmware 0.2.0 confirmed")
    wait(lambda: "updating to" not in admin.page("/buttons/1") and "0.2.0" in admin.page("/buttons/1"))

    # ---- firmware that cannot reach the server is rolled back; the retained command then retries it ----
    admin.post("/updates/firmware", {"version": "0.3.0"}, files={"file": ("fw.bin", image, "application/octet-stream")})
    admin.post("/updates/push", {"kind": "firmware", "item": "2", "targets": "1"})
    wait(lambda: button.cfg.get("fw") == "0.3.0", what="0.3.0 flashed")
    button.env.wifi_up = False
    wait(lambda: button.cfg.get("fw") == "0.2.0", 20, "rollback to 0.2.0")
    assert any("rolling back" in text for _, text in button.events)
    button.env.wifi_up = True
    wait(lambda: button.cfg.get("fw") == "0.3.0" and not button.cfg.get("fw_pending") and button.mode == "online",
         30, "0.3.0 retried and confirmed")

    # ---- certificate renewal over MQTT, with a new key ----
    old_cert, old_key = (button.dir / "cert.pem").read_bytes(), (button.dir / "key.pem").read_bytes()
    boots = button.cfg["boot"]
    button.request_renewal()
    wait(lambda: (button.dir / "cert.pem").read_bytes() != old_cert and button.cfg["boot"] > boots
         and button.mode == "online", what="renewed certificate in use")
    assert (button.dir / "key.pem").read_bytes() != old_key and not (button.dir / "key_pend.pem").exists()

    # ---- commands and status ----
    admin.post("/buttons/1", {"name": "Dock door 3", "person_id": "1", "location_id": "1"})
    wait(lambda: button.cfg.get("name") == "Dock door 3", what="rename")
    played = button.sounds_played
    admin.post("/buttons/1/identify")
    wait(lambda: button.sounds_played == played + 1, what="identify sound")
    button.nc_wire_broken = True
    wait(lambda: "wiring fault" in admin.page("/buttons/1"), what="wiring fault reported")
    button.nc_wire_broken = False
    wait(lambda: "wiring fault" not in admin.page("/buttons/1"), what="wiring fault cleared")

    # ---- the broker refuses a client without a certificate ----
    ctx = ssl.create_default_context(cafile=str(button.dir.parent / "dev-server.pem"))
    with pytest.raises((ssl.SSLError, ConnectionError, AssertionError)):
        with socket.create_connection(("localhost", TLS), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname="localhost") as s:
            s.sendall(b"\x10\x10\x00\x04MQTT\x04\x02\x00\x1e\x00\x04evil")
            assert s.recv(4)[:1] == b"\x20", "broker closed the connection"

    # ---- a revoked unit is ignored ----
    count = len(admin.presses())
    admin.post("/buttons/1/revoke")
    button.press()
    wait(lambda: not button.queue(), what="press acknowledged by the broker")
    time.sleep(1)
    assert len(admin.presses()) == count
    admin.post("/buttons/1/restore")

    # ---- holding the button from power-up opens setup mode; a wrong Wi-Fi password leads back to it ----
    button.power_off()
    button.power_on(holding=True)
    wait(lambda: button.mode == "setup", what="setup mode by holding")
    button.actuator_down = False
    assert portal("/info").json()["enrolled"] is True
    assert portal("/save", {**good, "pass": "wrong-password"}).json() == {"ok": True}      # no claim needed
    wait(lambda: any("returning to setup mode" in text for _, text in button.events), 30, "gave up on bad password")
    wait(lambda: button.mode == "setup")
    assert portal("/save", good).json() == {"ok": True}
    wait(lambda: button.mode == "online", what="online again")
    button.press()
    wait(lambda: len(admin.presses()) == count + 1, what="press after reconfiguration")
