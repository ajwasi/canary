"""Software emulator of one button unit (docs/hardware.md) running the behaviour
of the firmware in firmware/main. Use it to test the server without hardware.

It emulates the parts you can touch and the conditions around the unit:

  switch (NO and NC contacts), power, the Wi-Fi around it, the internet clock,
  the status LED, the speaker, the settings and press queue kept in flash,
  setup mode (serving the firmware's real setup page), enrolment, the TLS/MQTT
  link, certificate renewal, firmware and sound updates with rollback.

Run it, then open the control panel:

    python button_emulator.py                 (against a real server)
    python button_emulator.py --dev           (against a local server + dev_broker.py)

Control panel: http://localhost:8090     Setup page (in setup mode): http://localhost:8091
"""
import argparse
import asyncio
import collections
import hashlib
import io
import json
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as paho
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from state_path import safe_state_root

HERE = Path(__file__).resolve().parent
PORTAL_HTML = HERE.parent / "firmware" / "main" / "portal.html"      # the page the real unit serves

# Values taken from the firmware sources.
DEBOUNCE_S = 0.030          # CONFIG_BTN_DEBOUNCE_MS
NC_FAULT_S = 1.0            # button.c NC_FAULT_MS
EVQ_MAX = 200               # evq.c
ACK_TIMEOUT_S = 10          # mqtt_link.c
WAV_MAX_BYTES = 1500 * 1024  # audio.c
FW_MAX_BYTES = 0x300000     # partitions.csv ota slot
WIFI_FAILS_TO_SETUP = 6     # app_main.c
CLAIM_REJECTS_TO_SETUP = 5  # app_main.c
FACTORY_FW = "0.1.0"        # firmware/version.txt
FAKE_IP = "192.0.2.73"      # the simulated unit's LAN address (a documentation address, RFC 5737)


class Timing:
    """Real firmware timings. Tests shorten them."""
    setup_hold_s = 10.0         # hold the button this long from power-up to enter setup mode
    setup_timeout_s = 600.0     # setup mode gives up after this
    status_every_s = 300.0
    csr_retry_s = 6 * 3600.0
    rollback_after_s = 600.0    # new firmware must reach the server within this
    enroll_retry_s = 30.0
    wifi_retry_max_s = 30.0     # longest wait between Wi-Fi join attempts
    boot_s = 0.5


SERIAL_RE = re.compile(r"^BTN-[0-9A-F]{12}$")      # the shape the server accepts


def unit_dir(state_dir, serial: str) -> Path:
    """The unit's own folder inside state_dir. A serial that is not BTN- plus 12 hex digits, or that would
    land anywhere else, is refused."""
    if not SERIAL_RE.fullmatch(serial):
        raise ValueError(f"bad serial {serial!r}: expected BTN- plus 12 hex digits")
    base = Path(state_dir).resolve()
    path = (base / serial).resolve()
    if path.parent != base:
        raise ValueError("serial points outside the state folder")
    return path


class Environment:
    """The world around the unit. Change these from the control panel or a test."""

    def __init__(self):
        self.networks = [   # ssid, psk (the network's key; None = open network), channel, rssi
            {"ssid": "Warehouse-Staff", "psk": "blue-forklift-42", "channel": 44, "rssi": -48},
            {"ssid": "Warehouse-Staff", "psk": "blue-forklift-42", "channel": 6, "rssi": -44},
            {"ssid": "Scanner-Net", "psk": "scan-scan-scan", "channel": 11, "rssi": -61},
            {"ssid": "Tulsa-Guest", "psk": None, "channel": 1, "rssi": -57},
        ]
        self.wifi_up = True         # is the site Wi-Fi (and internet) reachable?
        self.clock_up = True        # can the unit reach a time server?


class Button:
    def __init__(self, serial: str, state_dir: Path, *, http_base: str | None = None, mqtt_port: int = 8883,
                 cafile: str | None = None, allow_http: bool = False, timing: Timing | None = None):
        self.serial = serial
        self.dir = unit_dir(state_dir, serial)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.http_base, self.mqtt_port, self.cafile, self.allow_http = http_base, mqtt_port, cafile, allow_http
        self.t = timing or Timing()
        self.env = Environment()
        self.events: collections.deque = collections.deque(maxlen=200)
        self.lock = threading.RLock()

        # Things a person can do to the hardware.
        self.powered = False
        self.actuator_down = False      # is the button held in?
        self.nc_wire_broken = False

        # Volatile state (lost at power-off).
        self.mode = "off"               # off | boot | setup | wifi | enrol | online | server-unreachable
        self.led = "off"
        self.sounds_played = 0
        self.nc_fault = False
        self.clock_set = False
        self.wifi = None                # the access point we are joined to
        self.mqtt_connected = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client: paho.Client | None = None
        self._jobs: collections.deque = collections.deque()
        self._acked: set[int] = set()
        self._pressed_debounced = False
        self._edge_at = None
        self._disagree_since = None
        self._boot_time = 0.0
        self._ever_connected = False
        self._setup_saved = False

    # ---------------- flash storage (survives power-off) ----------------

    def _load(self, name: str, default):
        with self.lock:
            path = self.dir / name
            return json.loads(path.read_text()) if path.exists() else default

    def _store(self, name: str, value) -> None:
        with self.lock:
            (self.dir / name).write_text(json.dumps(value))

    @property
    def cfg(self) -> dict:
        return self._load("nvs.json", {})

    def set_cfg(self, **changes) -> None:
        with self.lock:
            cfg = self.cfg
            for k, v in changes.items():
                if v is None:
                    cfg.pop(k, None)
                else:
                    cfg[k] = v
            self._store("nvs.json", cfg)

    def has_cert(self) -> bool:
        return (self.dir / "cert.pem").exists() and (self.dir / "key.pem").exists()

    def cert_times(self):
        cert = x509.load_pem_x509_certificate((self.dir / "cert.pem").read_bytes())
        return cert.not_valid_before_utc.timestamp(), cert.not_valid_after_utc.timestamp()

    def queue(self) -> list:
        """The unsent presses, rebuilt field by field so only numbers come back from the file."""
        return [{"seq": int(e["seq"]), "boot": int(e["boot"]), "ts": float(e["ts"]), "up": float(e["up"])}
                for e in self._load("evq.json", [])]

    def identity(self) -> tuple:
        """Fingerprints of the certificate and key in use, and whether a renewal is half done."""
        def fp(name):
            path = self.dir / name
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        return fp("cert.pem"), fp("key.pem"), (self.dir / "key_pend.pem").exists()

    def sound_file(self) -> bytes | None:
        path = self.dir / "press.wav"
        return path.read_bytes() if path.exists() else None

    def factory_reset(self) -> None:
        """Erase the flash: settings, identity, queue, sound and firmware version."""
        self.power_off()
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log("flash erased (factory reset)")

    # ---------------- hardware controls ----------------

    def log(self, text: str) -> None:
        self.events.appendleft((time.time(), text))

    def power_on(self, holding: bool = False) -> None:
        with self.lock:
            if self.powered:
                return
            self.powered, self.actuator_down = True, holding
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._firmware, args=(self._stop,), daemon=True)
            self._thread.start()

    def power_off(self) -> None:
        with self.lock:
            if not self.powered:
                return
            self.powered = False
            self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=15)
        self._drop_link()
        self.mode = self.led = "off"
        self.wifi, self.clock_set, self.nc_fault, self.mqtt_connected = None, False, False, False
        self.log("power off")

    def press(self, hold_s: float = 0.15) -> None:
        """Push and release the button."""
        self.actuator_down = True
        time.sleep(max(hold_s, DEBOUNCE_S * 2))
        self.actuator_down = False
        time.sleep(DEBOUNCE_S * 2)

    # ---------------- firmware ----------------

    def _sleep(self, stop: threading.Event, seconds: float) -> bool:
        """Sleep while still reading the switch. Returns False when power is cut."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if stop.wait(0.005):
                return False
            self._poll_switch()
        return True

    def _poll_switch(self) -> None:
        """button.c: debounce the NO contact; compare it with the NC contact."""
        now = time.monotonic()
        raw = self.actuator_down                                # NO contact closed
        if raw == self._pressed_debounced:
            self._edge_at = None
        else:
            self._edge_at = self._edge_at or now
            if now - self._edge_at >= DEBOUNCE_S:
                self._pressed_debounced, self._edge_at = raw, None
                if raw:
                    self._on_press()
        nc_closed = (not self.actuator_down) and not self.nc_wire_broken
        if nc_closed == raw:                                    # healthy wiring: exactly one is closed
            self._disagree_since = self._disagree_since or now
            if not self.nc_fault and now - self._disagree_since >= NC_FAULT_S:
                self.nc_fault = True
                self.log("NO/NC contacts agree: wiring fault")
        else:
            self._disagree_since, self.nc_fault = None, False

    def _on_press(self) -> None:
        self._play_sound()
        q = self.queue()
        tail = int(self._load("evq_meta.json", {"tail": 0})["tail"])
        item = {"seq": tail, "boot": int(self.cfg.get("boot", 0)),
                "ts": round(time.time(), 3) if self.clock_set else 0, "up": time.monotonic() - self._boot_time}
        q.append(item)
        self._store("evq.json", q[-EVQ_MAX:])
        self._store("evq_meta.json", {"tail": tail + 1})
        self.log(f"pressed (seq {item['seq']}, {len(q)} waiting to send)")

    def _play_sound(self) -> None:
        self.sounds_played += 1
        custom = (self.dir / "press.wav").exists()
        self.log("speaker: custom sound" if custom else "speaker: built-in beep")

    def _firmware(self, stop: threading.Event) -> None:
        """app_main.c. Returning means 'restart'; the loop below boots again while power is on."""
        while not stop.is_set():
            try:
                self._boot(stop)
            except Exception as exc:                              # a crash restarts the unit, as a watchdog would
                self.log(f"firmware crashed: {exc!r}; restarting")
            self._drop_link()
            self.wifi, self.clock_set, self.mqtt_connected = None, False, False

    def _boot(self, stop: threading.Event) -> None:
        self.mode, self.led = "boot", "slow"
        self._boot_time = time.monotonic()
        self._ever_connected, self._setup_saved = False, False
        self._jobs.clear()
        self._pressed_debounced = self.actuator_down             # held at power-up is not a press
        self.set_cfg(boot=self.cfg.get("boot", 0) + 1)
        self._commit_pending_cert()
        self.log(f"boot {self.cfg['boot']}, firmware {self.cfg.get('fw', FACTORY_FW)}")
        if not self._sleep(stop, self.t.boot_s):
            return

        held = self._pressed_debounced
        if held:
            self.led = "fast"
            end = time.monotonic() + self.t.setup_hold_s
            while held and time.monotonic() < end:
                if not self._sleep(stop, 0.05):
                    return
                held = self._pressed_debounced
        if not self.cfg.get("ssid") or held:
            return self._setup_mode(stop)

        # ---- Wi-Fi (wifi_mgr.c) ----
        self.mode, self.led = "wifi", "slow"
        fails = 0
        while not self._join_wifi():
            fails += 1
            if self._rollback_if_stuck():
                return
            if not self.cfg.get("verified") and fails >= WIFI_FAILS_TO_SETUP:
                self.log("could not join the configured network; returning to setup mode")
                self.set_cfg(ssid=None, **{"pass": None})
                return
            if not self._sleep(stop, min(self.t.wifi_retry_max_s, 2 << min(fails, 4))):
                return
        if not self.cfg.get("verified"):
            self.set_cfg(verified=1)

        # ---- clock ----
        self.clock_set = self.env.clock_up
        if not self.clock_set:
            self.log("clock not set yet; continuing")

        # ---- identity (identity.c) ----
        self.mode = "enrol"
        rejected = 0
        while not self.has_cert():
            result = self._enroll()
            if result == 0:
                break
            self.led = "fault"
            if result == 1:
                rejected += 1
                if rejected >= CLAIM_REJECTS_TO_SETUP:
                    self.log("claim code rejected; returning to setup mode")
                    self.set_cfg(claim=None, ssid=None, **{"pass": None})
                    return
            if self._rollback_if_stuck() or not self._sleep(stop, self.t.enroll_retry_s):
                return

        # ---- server link (mqtt_link.c) ----
        self._connect_mqtt()
        last_status, last_csr, inflight = time.monotonic(), -1e9, None
        while True:
            if not self._sleep(stop, 0.05):
                return
            now = time.monotonic()

            # Wi-Fi coming and going.
            if self.wifi and not self._network_ok():
                self.log("Wi-Fi connection lost")
                self._drop_link()
                self.wifi, inflight = None, None
            elif not self.wifi and self._join_wifi():
                self._connect_mqtt()
            if self.env.clock_up and self.wifi:
                self.clock_set = True
            online = self.mqtt_connected
            self.mode = "online" if online else ("server-unreachable" if self.wifi else "wifi")
            self.led = "online" if online else ("fault" if self.wifi else "slow")
            if self._rollback_if_stuck():
                return

            # Commands from the server.
            while self._jobs:
                if self._run_job(self._jobs.popleft()):
                    self._sleep(stop, 1.0)
                    return                                        # restart

            if not online:
                inflight = None
                continue

            # Presses: one at a time, removed only when the broker acknowledges.
            if inflight and inflight[0] in self._acked:
                q = self.queue()
                if q and q[0]["seq"] == inflight[1]:
                    self._store("evq.json", q[1:])
                inflight = None
            if inflight and now - inflight[2] > ACK_TIMEOUT_S:
                inflight = None                                   # resend; the server ignores repeats
            if not inflight:
                q = self.queue()
                if q:
                    item, body = q[0], {"seq": q[0]["seq"], "boot": q[0]["boot"]}
                    same_boot = item["boot"] == self.cfg["boot"]
                    age_ms = int((now - self._boot_time - item["up"]) * 1000) if same_boot else None
                    if age_ms is not None:
                        body["age_ms"] = age_ms
                    if item["ts"]:
                        body["ts"] = item["ts"]
                    elif age_ms is not None and self.clock_set:
                        body["ts"] = round(time.time() - age_ms / 1000, 3)
                    info = self._client.publish(f"btn/{self.serial}/press", json.dumps(body), qos=1)
                    inflight = (info.mid, item["seq"], now)

            if now - last_status >= self.t.status_every_s:
                last_status = now
                self._publish_status()
            if now - last_csr >= self.t.csr_retry_s and self._renew_due():
                last_csr = now
                self.request_renewal()

    # ---- setup mode (provisioning.c) ----

    def ap_name(self) -> str:
        return f"Button-{self.serial[-4:]}"

    def ap_password(self) -> str:
        if not self.cfg.get("ap_pass"):
            import secrets
            self.set_cfg(ap_pass="".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10)))
        return self.cfg["ap_pass"]

    def _setup_mode(self, stop: threading.Event) -> None:
        self.mode, self.led = "setup", "fast"
        self.log(f"setup mode: Wi-Fi '{self.ap_name()}', password '{self.ap_password()}'")
        end = time.monotonic() + self.t.setup_timeout_s
        while not self._setup_saved and time.monotonic() < end:
            if not self._sleep(stop, 0.05):
                return
        self.log("settings saved; restarting" if self._setup_saved else "setup timed out; restarting")
        self._sleep(stop, 0.3)

    def portal_info(self) -> dict:
        return {"serial": self.serial, "fw": self.cfg.get("fw", FACTORY_FW), "name": self.cfg.get("name", ""),
                "host": self.cfg.get("host", "buttons.example.com"), "enrolled": self.has_cert()}

    def portal_scan(self) -> list:
        return [{"ssid": n["ssid"], "rssi": n["rssi"], "band5": n["channel"] > 14, "secure": n["psk"] is not None}
                for n in self.env.networks] if self.env.wifi_up else []

    def portal_save(self, body: dict) -> dict:
        """The same checks as h_save() in provisioning.c."""
        def field(key, lo, hi):
            v = body.get(key)
            return v if isinstance(v, str) and lo <= len(v) <= hi else None
        name, ssid, pw, host = field("name", 1, 32), field("ssid", 1, 32), field("pass", 8, 63), field("host", 1, 128)
        claim = field("claim", 0, 64)
        if not name:
            return {"ok": False, "error": "Enter a button name (up to 32 characters)."}
        if not ssid:
            return {"ok": False, "error": "Choose a Wi-Fi network."}
        if not pw:
            return {"ok": False, "error": "The Wi-Fi password must be 8 to 63 characters. Open networks are not allowed."}
        if not host or "/" in host or " " in host:
            return {"ok": False, "error": "Enter the server address as a host name only."}
        if not claim and not self.has_cert():
            return {"ok": False, "error": "Enter the claim code from the server."}
        self.set_cfg(name=name, ssid=ssid, host=host, verified=None, **{"pass": pw})
        if claim:
            self.set_cfg(claim=claim)
            for f in ("cert.pem", "key.pem", "key_pend.pem", "cert_pend.pem"):
                (self.dir / f).unlink(missing_ok=True)
        self._setup_saved = True
        return {"ok": True}

    # ---- Wi-Fi ----

    def _network_ok(self) -> bool:
        return self.env.wifi_up and any(n["ssid"] == self.cfg.get("ssid") for n in self.env.networks)

    def _join_wifi(self) -> bool:
        """Secure networks only; prefer 5 GHz when its signal is at least -78 dBm (wifi_mgr.c)."""
        if not self.env.wifi_up:
            return False
        cfg = self.cfg
        usable = [n for n in self.env.networks if n["ssid"] == cfg.get("ssid") and n["psk"] is not None
                  and not (n["channel"] > 14 and n["rssi"] < -78)]
        if not usable:
            self.log("no WPA2/WPA3 access point found for the configured network")
            return False
        best = max(usable, key=lambda n: n["rssi"] + (15 if n["channel"] > 14 else 0))
        if best["psk"] != cfg.get("pass"):
            self.log(f"joining {best['ssid']} failed: wrong password")
            return False
        self.wifi = best
        self.log(f"joined {best['ssid']} on channel {best['channel']} ({best['rssi']} dBm)")
        return True

    # ---- identity ----

    def _url(self, path: str) -> str:
        return (self.http_base or f"https://{self.cfg['host']}") + path

    def _tls(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=self.cafile)

    def _new_key_and_csr(self):
        key = ec.generate_private_key(ec.SECP256R1())
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.serial)]))
               .sign(key, hashes.SHA256()))
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        return pem, csr.public_bytes(serialization.Encoding.PEM).decode()

    @staticmethod
    def _cert_matches(cert_pem: bytes, key_pem: bytes) -> bool:
        try:
            cert = x509.load_pem_x509_certificate(cert_pem)
            key = serialization.load_pem_private_key(key_pem, None)
            return cert.public_key().public_numbers() == key.public_key().public_numbers()
        except Exception:
            return False

    def _enroll(self) -> int:
        """0 = enrolled, 1 = claim rejected, -1 = try again later."""
        cfg = self.cfg
        if not cfg.get("claim"):
            self.log("no claim code configured")
            return 1
        if not self._network_ok():
            self.log("cannot reach the server: no Wi-Fi")
            return -1
        key_pem, csr = self._new_key_and_csr()
        body = json.dumps({"serial": self.serial, "name": cfg.get("name", ""), "claim": cfg["claim"], "csr": csr}).encode()
        req = urllib.request.Request(self._url("/api/device/enroll"), body, {"Content-Type": "application/json"})
        try:
            ctx = self._tls() if req.full_url.startswith("https") else None
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                cert = json.load(resp)["cert"].encode()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                self.log("claim code rejected")
                return 1
            self.log(f"enrolment returned HTTP {exc.code}")
            return -1
        except Exception as exc:
            self.log(f"cannot reach the server: {exc}")
            return -1
        if not self._cert_matches(cert, key_pem):
            self.log("server returned an unusable certificate")
            return -1
        (self.dir / "key.pem").write_bytes(key_pem)
        (self.dir / "cert.pem").write_bytes(cert)
        self.set_cfg(claim=None)
        self.log(f"enrolled as {self.serial}")
        return 0

    def _renew_due(self) -> bool:
        if not self.clock_set:
            return False
        start, end = self.cert_times()
        return time.time() > start + (end - start) * 2 / 3

    def request_renewal(self) -> None:
        """Make a new key, keep it as pending and send its CSR (identity_begin_renew)."""
        if not self.mqtt_connected:
            return
        key_pem, csr = self._new_key_and_csr()
        (self.dir / "cert_pend.pem").unlink(missing_ok=True)
        (self.dir / "key_pend.pem").write_bytes(key_pem)
        self._client.publish(f"btn/{self.serial}/csr", json.dumps({"csr": csr}), qos=1)
        self.log("certificate due for renewal; CSR sent")

    def _commit_pending_cert(self) -> None:
        key, cert = self.dir / "key_pend.pem", self.dir / "cert_pend.pem"
        if key.exists() and cert.exists():
            (self.dir / "key.pem").write_bytes(key.read_bytes())
            (self.dir / "cert.pem").write_bytes(cert.read_bytes())
            key.unlink()
            cert.unlink()

    # ---- MQTT ----

    def _connect_mqtt(self) -> None:
        self._drop_link()
        tls = self._tls()
        tls.load_cert_chain(self.dir / "cert.pem", self.dir / "key.pem")
        c = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=self.serial, clean_session=False)
        c.tls_set_context(tls)
        c.will_set(f"btn/{self.serial}/status", '{"online":false}', qos=1, retain=True)
        c.reconnect_delay_set(1, 5)
        c.on_connect, c.on_disconnect, c.on_message, c.on_publish = \
            self._mq_connect, self._mq_disconnect, self._mq_message, self._mq_published
        self._acked = set()
        self._client = c
        c.connect_async(self.cfg["host"], self.mqtt_port, keepalive=30)
        c.loop_start()

    def _drop_link(self) -> None:
        """Lose the connection the way a power cut or Wi-Fi drop does: no goodbye, so the last will fires."""
        c, self._client, self.mqtt_connected = self._client, None, False
        if c:
            sock = c.socket()
            c.loop_stop()
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _mq_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            self.log(f"server refused the connection: {reason_code}")
            return
        self.mqtt_connected = True
        if not self._ever_connected:
            self._ever_connected = True
            if self.cfg.get("fw_pending"):                       # new firmware proved itself: keep it
                self.set_cfg(fw_pending=None, fw_prev=None)
                self.log("new firmware confirmed")
        client.subscribe(f"btn/{self.serial}/cmd/#", qos=1)
        self._publish_status()
        self.log("connected to the server")

    def _mq_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.mqtt_connected:
            self.log("server connection lost")
        self.mqtt_connected = False

    def _mq_published(self, client, userdata, mid, reason_code, properties):
        self._acked.add(mid)

    def _mq_message(self, client, userdata, msg):
        try:
            cmd = json.loads(msg.payload)
        except ValueError:
            return
        if isinstance(cmd, dict) and isinstance(cmd.get("type"), str):
            self._jobs.append(cmd)

    def _publish_status(self) -> None:
        if not (self._client and self.mqtt_connected):
            return
        cfg = self.cfg
        self._client.publish(f"btn/{self.serial}/status", json.dumps({
            "online": True, "serial": self.serial, "name": cfg.get("name", ""), "fw": cfg.get("fw", FACTORY_FW),
            "rssi": self.wifi["rssi"] if self.wifi else 0, "channel": self.wifi["channel"] if self.wifi else 0,
            "ip": FAKE_IP, "uptime_s": int(time.monotonic() - self._boot_time), "pending": len(self.queue()),
            "cert_not_after": int(self.cert_times()[1]), "audio_sha": cfg.get("audio_sha", ""),
            "nc_fault": self.nc_fault}), qos=1, retain=True)

    # ---- commands (handle_cmd / job_task in mqtt_link.c) ----

    def _download(self, url: str, limit: int) -> bytes | None:
        if not (url.startswith("https://") or (self.allow_http and url.startswith("http://"))):
            self.log("download refused: the address is not https")
            return None
        if not self._network_ok():
            self.log("download failed: no Wi-Fi")
            return None
        try:
            ctx = self._tls() if url.startswith("https") else None
            with urllib.request.urlopen(url, timeout=30, context=ctx) as resp:
                data = resp.read(limit + 1)
        except Exception as exc:
            self.log(f"download failed: {exc}")
            return None
        if len(data) > limit:
            self.log("download refused: file too large")
            return None
        return data

    def _run_job(self, cmd: dict) -> bool:
        """Returns True if the unit must restart."""
        kind, cfg = cmd["type"], self.cfg
        if kind == "ota":
            version, url = cmd.get("version"), cmd.get("url")
            if not (isinstance(version, str) and isinstance(url, str)) or version == cfg.get("fw", FACTORY_FW):
                return False
            self.log(f"updating firmware to {version}")
            image = self._download(url, FW_MAX_BYTES)
            if not image or image[0] != 0xE9:
                self.log("firmware update failed; staying on current version")
                return False
            self.set_cfg(fw_prev=cfg.get("fw", FACTORY_FW), fw=version, fw_pending=1)
            return True
        if kind == "audio":
            sha, url = cmd.get("sha256"), cmd.get("url")
            if not (isinstance(sha, str) and isinstance(url, str)) or sha.lower() == cfg.get("audio_sha"):
                return False
            data = self._download(url, WAV_MAX_BYTES)
            if not data:
                return False
            if hashlib.sha256(data).hexdigest() != sha.lower():
                self.log("audio checksum mismatch")
                return False
            try:
                with wave.open(io.BytesIO(data)) as w:
                    ok = w.getsampwidth() == 2 and w.getnchannels() in (1, 2) and 8000 <= w.getframerate() <= 48000
            except (wave.Error, EOFError):
                ok = False
            if not ok:
                self.log("audio file is not 16-bit PCM WAV")
                return False
            (self.dir / "press.wav").write_bytes(data)
            self.set_cfg(audio_sha=sha.lower())
            self.log(f"press sound updated ({len(data)} bytes)")
            self._publish_status()
        elif kind == "cert":
            pend, cert = self.dir / "key_pend.pem", str(cmd.get("cert", "")).encode()
            if pend.exists() and self._cert_matches(cert, pend.read_bytes()):
                (self.dir / "cert_pend.pem").write_bytes(cert)
                self._commit_pending_cert()
                self.log("client certificate renewed")
                return True
            self.log("renewed certificate does not match the pending key")
        elif kind == "name":
            name = cmd.get("name")
            if isinstance(name, str) and 0 < len(name) <= 32:
                self.set_cfg(name=name)
                self.log(f"renamed to {name}")
                self._publish_status()
        elif kind == "identify":
            self._play_sound()
        elif kind == "reboot":
            self.log("restart requested by the server")
            return True
        return False

    def _rollback_if_stuck(self) -> bool:
        cfg = self.cfg
        if cfg.get("fw_pending") and not self._ever_connected and \
                time.monotonic() - self._boot_time > self.t.rollback_after_s:
            self.log(f"new firmware never reached the server; rolling back to {cfg.get('fw_prev')}")
            self.set_cfg(fw=cfg.get("fw_prev", FACTORY_FW), fw_pending=None, fw_prev=None)
            return True
        return False

    # ---------------- snapshot for the control panel ----------------

    def snapshot(self) -> dict:
        cfg = self.cfg
        expiry = datetime.fromtimestamp(self.cert_times()[1], timezone.utc).isoformat() if self.has_cert() else None
        return {
            "serial": self.serial, "powered": self.powered, "mode": self.mode, "led": self.led,
            "pressed": self.actuator_down, "nc_wire_broken": self.nc_wire_broken, "nc_fault": self.nc_fault,
            "wifi_up": self.env.wifi_up, "clock_up": self.env.clock_up, "clock_set": self.clock_set,
            "sounds_played": self.sounds_played, "custom_sound": (self.dir / "press.wav").exists(),
            "name": cfg.get("name", ""), "ssid": cfg.get("ssid", ""), "host": cfg.get("host", ""),
            "fw": cfg.get("fw", FACTORY_FW), "fw_pending": bool(cfg.get("fw_pending")), "boot": cfg.get("boot", 0),
            "joined": f"{self.wifi['ssid']} ch {self.wifi['channel']}, {self.wifi['rssi']} dBm" if self.wifi else "",
            "enrolled": self.has_cert(), "cert_expires": expiry, "queue": len(self.queue()),
            "ap_name": self.ap_name(), "ap_password": self.ap_password() if self.mode == "setup" else "",
            "events": [[datetime.fromtimestamp(t).strftime("%H:%M:%S"), text] for t, text in list(self.events)[:60]],
        }


# ---------------------------------------------------------------------------
# Web front ends: the control panel, and the unit's own setup page.
# ---------------------------------------------------------------------------

def make_apps(button: Button):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

    panel = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @panel.get("/")
    def panel_page():
        return FileResponse(HERE / "panel.html")

    @panel.get("/api/state")
    def state():
        return button.snapshot()

    @panel.get("/api/sound")
    def sound():
        path = button.dir / "press.wav"
        return FileResponse(path, media_type="audio/wav") if path.exists() else Response(status_code=404)

    @panel.post("/api/{action}")
    async def act(action: str, request: Request):
        body = await request.json() if await request.body() else {}
        if action == "actuator":
            button.actuator_down = bool(body.get("down"))
        elif action == "power":
            (button.power_on(holding=bool(body.get("holding"))) if body.get("on") else button.power_off())
        elif action == "wifi":
            button.env.wifi_up = bool(body.get("up"))
        elif action == "clock":
            button.env.clock_up = bool(body.get("up"))
        elif action == "nc_wire":
            button.nc_wire_broken = bool(body.get("broken"))
        elif action == "renew":
            button.request_renewal()
        elif action == "factory_reset":
            button.factory_reset()
        else:
            return JSONResponse({"error": "unknown action"}, 404)
        return {"ok": True}

    ap = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @ap.middleware("http")
    async def only_in_setup_mode(request: Request, call_next):
        if button.mode != "setup":
            return HTMLResponse("<h1>Not reachable</h1><p>The button's setup Wi-Fi is only on while it is in "
                                "setup mode.</p>", 503)
        return await call_next(request)

    @ap.get("/")
    def portal():
        return FileResponse(PORTAL_HTML, media_type="text/html")

    @ap.get("/info")
    def info():
        return button.portal_info()

    @ap.get("/scan")
    def scan():
        return button.portal_scan()

    @ap.post("/save")
    async def save(request: Request):
        try:
            body = json.loads(await request.body())
        except ValueError:
            return {"ok": False, "error": "Bad request."}
        return button.portal_save(body if isinstance(body, dict) else {})

    return panel, ap


async def run_servers(button: Button, panel_port: int, ap_port: int) -> None:
    import uvicorn
    panel, ap = make_apps(button)
    servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
               for app, port in ((panel, panel_port), (ap, ap_port))]
    await asyncio.gather(*(s.serve() for s in servers))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--serial", default="BTN-E0E0E0E00001", help="BTN- plus 12 hex digits")
    p.add_argument("--state", default="./state", help="folder that stands in for the unit's flash memory")
    p.add_argument("--panel-port", type=int, default=8090)
    p.add_argument("--ap-port", type=int, default=8091)
    p.add_argument("--mqtt-port", type=int, default=8883)
    p.add_argument("--cafile", help="extra CA or certificate to trust for the server (testing only)")
    p.add_argument("--http-base", help="use this base URL for enrolment instead of https://<host>")
    p.add_argument("--allow-http", action="store_true", help="accept http:// download links (testing only)")
    p.add_argument("--dev", action="store_true",
                   help="local test rig: server at http://localhost:8000, dev_broker.py on 8883")
    p.add_argument("--fast", action="store_true", help="short timings: 3 s setup hold, 20 s status, 60 s rollback")
    a = p.parse_args()

    timing = Timing()
    if a.fast:
        timing.setup_hold_s, timing.status_every_s, timing.rollback_after_s, timing.enroll_retry_s = 3, 20, 60, 5
        timing.wifi_retry_max_s = 4
    if a.dev:
        a.http_base = a.http_base or "http://localhost:8000"
        a.cafile = a.cafile or str(Path(a.state) / "dev-server.pem")
        a.allow_http = True
    button = Button(a.serial, safe_state_root(a.state), http_base=a.http_base, mqtt_port=a.mqtt_port, cafile=a.cafile,
                    allow_http=a.allow_http, timing=timing)
    button.power_on()
    print(f"Emulating {a.serial}\n  Control panel: http://localhost:{a.panel_port}\n"
          f"  Setup page (while in setup mode): http://localhost:{a.ap_port}")
    try:
        asyncio.run(run_servers(button, a.panel_port, a.ap_port))
    except KeyboardInterrupt:
        pass
    finally:
        button.power_off()


if __name__ == "__main__":
    main()
