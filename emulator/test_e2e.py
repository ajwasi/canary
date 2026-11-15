"""End-to-end test: the real server, the dev broker and a button, talking over real
HTTP, TLS and MQTT on localhost. The button is one of two things:

  the Python emulator (default; any operating system)

      ..\\server\\.venv\\Scripts\\python -m pytest test_e2e.py -q

  the real firmware built as a Linux program (see ../firmware/README.md, "PC test build")

      CANARY_TARGET=firmware CANARY_FIRMWARE_ELF=../firmware/build/button_notifier.elf \\
      CANARY_TEST_CA=../firmware/host/test-ca python -m pytest test_e2e.py -q
"""
import asyncio
import io
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

import dev_broker
import test_ca
from button_emulator import Button, Timing, run_servers

TARGET = os.environ.get("CANARY_TARGET", "emulator")        # "emulator" or "firmware"
HTTP, PLAIN, TLS, PANEL, AP = 18000, 18830, 18831, 18090, 18091
HTTPS, HTTPS_LINK, TLS_LINK, CTRL = 18443, 18444, 18832, 18095      # firmware target; see sdkconfig.defaults.linux
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
    def __init__(self, base, verify=True):
        self.c = httpx.Client(base_url=base, timeout=20, verify=verify)
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


def start_server(tmp, public_url, extra_args, check_url, verify):
    env = {**os.environ, "DATA_DIR": str(tmp / "server"), "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(PLAIN),
           "MQTT_ENABLED": "1", "PUBLIC_URL": public_url, "ADMIN_EMAIL": "admin@test.local",
           "ADMIN_PASSWORD": "correct horse battery", "DEFAULT_TZ": "UTC"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", *extra_args],
                              cwd=ROOT / "server", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def healthy():
        try:
            return httpx.get(check_url + "/healthz", timeout=1, verify=verify).status_code == 200
        except httpx.HTTPError:
            return False
    try:
        wait(healthy, 30, "server start")
    except BaseException:
        server.terminate()
        raise
    return server


def start_broker(tmp, state):
    threading.Thread(target=lambda: asyncio.run(dev_broker.serve(
        tmp / "server" / "ca" / "device-ca.pem", state, TLS, PLAIN)), daemon=True).start()

    def listening():
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", TLS)) == 0
    wait(listening, 15, "broker start")


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rig")
    state = tmp / "emu"
    state.mkdir()
    if TARGET == "firmware":
        # The firmware trusts only public roots plus the test root it was built with, and speaks
        # only HTTPS and MQTT over TLS, so the server and broker present a certificate from that root.
        from host_firmware import HostFirmware
        ca = test_ca.make(Path(os.environ["CANARY_TEST_CA"]).resolve())
        elf = Path(os.environ["CANARY_FIRMWARE_ELF"]).resolve()
        root = ssl.create_default_context(cafile=str(ca / "root.pem"))
        shutil.copy(ca / "localhost.pem", state / "dev-server.pem")
        shutil.copy(ca / "localhost.key", state / "dev-server.key")
        server = start_server(tmp, f"https://127.0.0.1:{HTTPS_LINK}", ["--port", str(HTTPS), "--ssl-keyfile",
                              str(ca / "localhost.key"), "--ssl-certfile", str(ca / "localhost.pem")],
                              f"https://127.0.0.1:{HTTPS}", root)
        try:
            start_broker(tmp, state)
            table = elf.parent / "partition_table" / "partition-table.bin"
            button = HostFirmware(SERIAL, state, elf, ctrl_port=CTRL, partition_table=table if table.exists() else None,
                                  links=((HTTPS_LINK, HTTPS), (TLS_LINK, TLS)))
            yield SimpleNamespace(button=button, admin=Admin(f"https://127.0.0.1:{HTTPS}", root), firmware=True,
                                  host="127.0.0.1", broker_ca=str(ca / "root.pem"))
            button.power_off()
        finally:
            server.terminate()
            if "button" in locals():                    # keep the unit's log for a failed run
                log = Path(os.environ.get("CANARY_LOG", tmp / "firmware.log"))
                log.write_text(chr(10).join(text for _, text in reversed(button.events)), encoding="utf-8")
        return

    server = start_server(tmp, f"http://localhost:{HTTP}", ["--port", str(HTTP)], f"http://localhost:{HTTP}", True)
    try:
        start_broker(tmp, state)
        t = Timing()
        t.setup_hold_s, t.status_every_s, t.rollback_after_s, t.enroll_retry_s = 1.0, 2.0, 6.0, 1.0
        t.wifi_retry_max_s, t.boot_s = 0.5, 0.1
        button = Button(SERIAL, state, http_base=f"http://localhost:{HTTP}", mqtt_port=TLS,
                        cafile=str(state / "dev-server.pem"), allow_http=True, timing=t)
        threading.Thread(target=lambda: asyncio.run(run_servers(button, PANEL, AP)), daemon=True).start()
        yield SimpleNamespace(button=button, admin=Admin(f"http://localhost:{HTTP}"), firmware=False,
                              host="localhost", broker_ca=str(state / "dev-server.pem"))
        button.power_off()
    finally:
        server.terminate()


def portal(path, body=None):
    """The unit's own setup page. Status 0 means nothing is listening."""
    url = f"http://127.0.0.1:{AP}{path}"
    try:
        return httpx.post(url, json=body, timeout=5) if body is not None else httpx.get(url, timeout=5)
    except httpx.HTTPError:
        return SimpleNamespace(status_code=0, text="", json=dict)


def firmware_image(version: str) -> bytes:
    """The start of an ESP application image: header, first segment header, app description."""
    desc = struct.pack("<I I 8x 32s 32s", 0xABCD5432, 0, version.encode(), b"button_notifier").ljust(256, b"\0")
    return b"\xe9" + bytes(23) + bytes(8) + desc + bytes(4096)


def test_everything(rig):
    button, admin = rig.button, rig.admin
    good = {"name": "Front desk", "ssid": "Warehouse-Staff", "pass": "blue-forklift-42", "host": rig.host}

    # ---- a new unit starts in setup mode and serves the firmware's own setup page ----
    button.power_on()
    wait(lambda: button.mode == "setup", what="setup mode")
    wait(lambda: portal("/").status_code == 200, what="setup page")
    assert "Button setup" in portal("/").text
    nets = portal("/scan").json()
    assert {"ssid": "Tulsa-Guest", "rssi": -57, "band5": False, "secure": False} in nets
    assert portal("/save", {**good, "pass": "short", "claim": "x"}).json()["ok"] is False
    assert "claim code" in portal("/save", good).json()["error"]
    if rig.firmware:                                                # the setup network is never open
        ap = button.access_point
        assert ap["secure"] and ap["ssid"] == "Button-0001" and len(ap["password"]) == 10

    admin.post("/people", {"name": "Dana Whitfield"})
    admin.post("/locations", {"name": "Tulsa warehouse", "timezone_name": "America/Chicago"})
    flash = admin.post("/buttons", {"name": "Front desk", "person_id": "1", "location_id": "1"})
    claim = re.search(r"Claim code: ([A-Z0-9-]+)\.", flash).group(1)
    assert portal("/save", {**good, "claim": claim}).json() == {"ok": True}

    # ---- it restarts, joins the 5 GHz access point, enrols and connects ----
    wait(lambda: button.mode == "online", what="online")
    assert button.wifi["channel"] == 44 and button.has_cert() and "claim" not in button.cfg
    assert portal("/").status_code in (0, 503)                    # setup page is gone outside setup mode
    wait(lambda: "Online" in admin.page("/buttons/1"), what="server shows online")
    assert SERIAL in admin.page("/buttons/1") and "5 GHz" in admin.page("/buttons/1")

    # ---- presses arrive and leave the unit's queue ----
    button.press(); button.press()
    wait(lambda: len(admin.presses()) == 2 and not button.queue(), what="two presses logged")
    wait(lambda: button.sounds_played == 2, what="two sounds")
    assert all("Dana Whitfield" in row for row in admin.presses())
    if rig.firmware:                                                # the built-in beep: 230 ms at 16 kHz
        assert button.last_sound == (3680 + 512, 16000)     # plus the 512 samples of silence that end every sound

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
    wait(lambda: button.sound_file() is not None, what="sound installed")
    assert button.sound_file() == buf.getvalue()
    wait(lambda: "Custom" in admin.page("/buttons/1"), what="server sees the new sound")

    # ---- firmware update: applied, restarted, confirmed ----
    admin.post("/updates/firmware", {"version": "0.2.0"},
               files={"file": ("fw.bin", firmware_image("0.2.0"), "application/octet-stream")})
    admin.post("/updates/push", {"kind": "firmware", "item": "1", "targets": "1"})
    wait(lambda: button.cfg.get("fw") == "0.2.0" and not button.cfg.get("fw_pending") and button.mode == "online",
         what="firmware 0.2.0 confirmed")
    wait(lambda: "updating to" not in admin.page("/buttons/1") and "0.2.0" in admin.page("/buttons/1"))

    # ---- firmware that cannot reach the server is rolled back; the retained command then retries it ----
    admin.post("/updates/firmware", {"version": "0.3.0"},
               files={"file": ("fw.bin", firmware_image("0.3.0"), "application/octet-stream")})
    admin.post("/updates/push", {"kind": "firmware", "item": "2", "targets": "1"})
    wait(lambda: button.cfg.get("fw") == "0.3.0", what="0.3.0 flashed")
    button.env.wifi_up = False
    wait(lambda: button.cfg.get("fw") == "0.2.0", 20, "rollback to 0.2.0")
    assert any("rolling back" in text for _, text in button.events)
    button.env.wifi_up = True
    wait(lambda: button.cfg.get("fw") == "0.3.0" and not button.cfg.get("fw_pending") and button.mode == "online",
         30, "0.3.0 retried and confirmed")

    # ---- certificate renewal over MQTT, with a new key ----
    old_cert, old_key, _ = button.identity()
    boots = button.cfg["boot"]
    button.request_renewal()
    wait(lambda: button.identity()[0] != old_cert and button.cfg["boot"] > boots and button.mode == "online",
         what="renewed certificate in use")
    assert button.identity()[1] != old_key and not button.identity()[2]

    # ---- commands and status ----
    admin.post("/buttons/1", {"name": "Dock door 3", "person_id": "1", "location_id": "1"})
    wait(lambda: button.cfg.get("name") == "Dock door 3", what="rename")
    played = button.sounds_played
    admin.post("/buttons/1/identify")
    wait(lambda: button.sounds_played == played + 1, what="identify sound")
    if rig.firmware:                                                # the installed sound, not the beep
        assert button.last_sound == (800 + 512, 16000)
    button.nc_wire_broken = True
    wait(lambda: "wiring fault" in admin.page("/buttons/1"), what="wiring fault reported")
    button.nc_wire_broken = False
    wait(lambda: "wiring fault" not in admin.page("/buttons/1"), what="wiring fault cleared")

    # ---- the broker refuses a client without a certificate ----
    ctx = ssl.create_default_context(cafile=rig.broker_ca)
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
    wait(lambda: portal("/info").status_code == 200, what="setup page")
    assert portal("/info").json()["enrolled"] is True
    assert portal("/save", {**good, "pass": "wrong-password"}).json() == {"ok": True}      # no claim needed
    wait(lambda: any("returning to setup mode" in text for _, text in button.events), 30, "gave up on bad password")
    wait(lambda: button.mode == "setup")
    wait(lambda: portal("/info").status_code == 200, what="setup page")
    assert portal("/save", good).json() == {"ok": True}
    wait(lambda: button.mode == "online", what="online again")
    button.press()
    wait(lambda: len(admin.presses()) == count + 1, what="press after reconfiguration")

    if rig.firmware:
        assert button.crashes == 0, [text for _, text in button.events if "crash" in text]
