"""Runs the real firmware, built as a Linux program, as if it were a button on the bench.

The firmware in firmware/main is compiled unchanged with ESP-IDF's Linux target
(see firmware/README.md, "PC test build"). firmware/components/sim stands in for the
chip's radio, pins, amplifier and firmware slots. This class is the bench around it:

  power        starting and killing the program; a restart request starts it again
  switch       commands on the simulator's control socket
  site Wi-Fi   the simulator is told, and the unit's TCP connections are cut
  observation  the unit's log, and the state it reports on the control socket

It offers the same controls as button_emulator.Button, so test_e2e.py can drive either.
Linux only.
"""
import collections
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

DEBOUNCE_S = 0.030
RENEW_SKEW_S = 21 * 86400       # past two thirds of a 30-day certificate


class Link:
    """One TCP path from the unit to the outside (a port forward). Cut when the site Wi-Fi is down."""

    def __init__(self, listen_port: int, target_port: int):
        self.listen_port, self.target_port = listen_port, target_port
        self._server: socket.socket | None = None
        self._conns: set[socket.socket] = set()
        self._lock = threading.Lock()

    def up(self) -> None:
        with self._lock:
            if self._server:
                return
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", self.listen_port))
            srv.listen(16)
            self._server = srv
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def down(self) -> None:
        with self._lock:
            srv, self._server = self._server, None
            conns, self._conns = self._conns, set()
        for s in ([srv] if srv else []) + list(conns):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def _accept(self, srv: socket.socket) -> None:
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                return
            try:
                target = socket.create_connection(("127.0.0.1", self.target_port), timeout=5)
            except OSError:
                client.close()
                continue
            target.settimeout(None)
            with self._lock:
                if self._server is not srv:             # cut while connecting
                    client.close(), target.close()
                    return
                self._conns.update((client, target))
            for a, b in ((client, target), (target, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        with self._lock:
            self._conns.difference_update((src, dst))


class _Env:
    """The world around the unit (same names as button_emulator.Environment)."""

    def __init__(self, unit: "HostFirmware"):
        self._unit, self._wifi_up, self._clock_up = unit, True, True

    @property
    def wifi_up(self) -> bool:
        return self._wifi_up

    @wifi_up.setter
    def wifi_up(self, value: bool) -> None:
        self._wifi_up = bool(value)
        self._unit._send(f"wifi {int(self._wifi_up)}")
        for link in self._unit.links:
            link.up() if self._wifi_up else link.down()

    @property
    def clock_up(self) -> bool:
        return self._clock_up

    @clock_up.setter
    def clock_up(self, value: bool) -> None:
        self._clock_up = bool(value)
        self._unit._send(f"clock {int(self._clock_up)}")


class HostFirmware:
    def __init__(self, serial: str, state_dir: Path, elf: Path, *, ctrl_port: int = 18095,
                 links: tuple[tuple[int, int], ...] = (), partition_table: Path | None = None):
        self.serial = serial
        self.dir = Path(state_dir) / serial
        self.dir.mkdir(parents=True, exist_ok=True)
        self.elf, self.ctrl_port, self.partition_table = Path(elf), ctrl_port, partition_table
        self.links = [Link(a, b) for a, b in links]
        for link in self.links:
            link.up()
        self.env = _Env(self)
        self.events: collections.deque = collections.deque(maxlen=20000)
        self.crashes = 0
        self.powered = False
        self._actuator_down = self._nc_wire_broken = False
        self._skew_s = 0
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._mqtt_up = False
        self._last: dict = {}           # last state the unit reported
        self._sound_base = 0            # sounds played by earlier runs of the program

    # ---------------- power ----------------

    def log(self, text: str) -> None:
        self.events.appendleft((time.time(), text))

    def power_on(self, holding: bool = False) -> None:
        if self.powered:
            return
        self.powered, self._actuator_down = True, holding
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def power_off(self) -> None:
        """Cuts the power: the program is killed with no warning."""
        if not self.powered:
            return
        self._state()                   # one last look, as you would note the display before pulling the plug
        self.powered = False
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        if self._thread:
            self._thread.join(timeout=10)
        self.log("power off")

    def _run(self) -> None:
        while self.powered:
            env = {**os.environ, "SIM_DIR": str(self.dir), "SIM_SERIAL": self.serial,
                   "SIM_CTRL_PORT": str(self.ctrl_port), "SIM_BTN": str(int(self._actuator_down)),
                   "SIM_NC_BROKEN": str(int(self._nc_wire_broken)), "SIM_WIFI": str(int(self.env.wifi_up)),
                   "SIM_CLOCK": str(int(self.env.clock_up)), "SIM_SKEW_S": str(self._skew_s)}
            if self.partition_table:
                env["SIM_PARTITION_TABLE"] = str(self.partition_table)
            self._mqtt_up = False
            self._proc = proc = subprocess.Popen([str(self.elf)], cwd=self.dir, env=env, text=True, errors="replace",
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in proc.stdout:
                self._on_log(line.rstrip())
            code = proc.wait()
            self._sound_base += self._last.get("sounds", 0)
            self._last = {**self._last, "sounds": 0, "sta": False, "ap": False}
            self._mqtt_up = False
            if self.powered and code != 0:      # exit code 0 is esp_restart(); anything else is a crash
                self.crashes += 1
                self.log(f"firmware crashed (exit code {code}); restarting")
            time.sleep(0.1)

    def _on_log(self, line: str) -> None:
        self.log(line)
        if re.search(r"\bmqtt: connected\b", line):
            self._mqtt_up = True
        elif re.search(r"\bmqtt: disconnected\b", line):
            self._mqtt_up = False
        elif "sending CSR" in line and self._skew_s:
            self._skew_s = 0                    # the clock trick has done its job
            self._send("skew 0")

    # ---------------- control socket ----------------

    def _send(self, command: str) -> dict | None:
        """Sends one command to the simulator. Returns the unit's state, or None if it is not running."""
        if not self.powered or not self._proc or self._proc.poll() is not None:
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.3)
            for _ in range(3):
                try:
                    s.sendto(command.encode(), ("127.0.0.1", self.ctrl_port))
                    self._last = json.loads(s.recvfrom(65535)[0])
                    return self._last
                except (OSError, ValueError):
                    continue
        return None

    def _state(self) -> dict:
        return self._send("state") or self._last

    # ---------------- what a person can do to the hardware ----------------

    @property
    def actuator_down(self) -> bool:
        return self._actuator_down

    @actuator_down.setter
    def actuator_down(self, value: bool) -> None:
        self._actuator_down = bool(value)
        self._send(f"btn {int(self._actuator_down)}")

    @property
    def nc_wire_broken(self) -> bool:
        return self._nc_wire_broken

    @nc_wire_broken.setter
    def nc_wire_broken(self, value: bool) -> None:
        self._nc_wire_broken = bool(value)
        self._send(f"nc {int(self._nc_wire_broken)}")

    def press(self, hold_s: float = 0.15) -> None:
        self.actuator_down = True
        time.sleep(max(hold_s, DEBOUNCE_S * 2))
        self.actuator_down = False
        time.sleep(DEBOUNCE_S * 2)

    def request_renewal(self) -> None:
        """Moves the unit's clock three weeks ahead so it decides its certificate is due."""
        self._skew_s = RENEW_SKEW_S
        self._send(f"skew {RENEW_SKEW_S}")

    # ---------------- what can be observed ----------------

    @property
    def clock_set(self) -> bool:
        return bool(self._state().get("clock_set"))

    @clock_set.setter
    def clock_set(self, value: bool) -> None:
        self._send(f"clockset {int(bool(value))}")

    @property
    def mode(self) -> str:
        if not self.powered:
            return "off"
        st = self._send("state")
        if st is None:
            return "boot"
        if st.get("ap"):
            return "setup"
        if not st.get("sta"):
            return "wifi"
        if self._mqtt_up:
            return "online"
        return "server-unreachable" if st.get("cert_fp") else "enrol"

    @property
    def led(self) -> str:
        return self._state().get("led", "off")

    @property
    def wifi(self) -> dict | None:
        st = self._state()
        return {"channel": st["channel"], "rssi": st["rssi"]} if st.get("sta") else None

    @property
    def access_point(self) -> dict | None:
        st = self._state()
        return {"ssid": st["ap_ssid"], "password": st["ap_pass"], "secure": st["ap_secure"]} if st.get("ap") else None

    @property
    def cfg(self) -> dict:
        st = self._state()
        out = {k: st[k] for k in ("name", "ssid", "host", "claim", "boot", "fw") if k in st}
        if st.get("fw_pending"):
            out["fw_pending"] = True
        return out

    def has_cert(self) -> bool:
        st = self._state()
        return "cert_fp" in st and "key_fp" in st

    def identity(self) -> tuple:
        """Fingerprints of the certificate and key in use, and whether a renewal is half done."""
        st = self._state()
        return st.get("cert_fp"), st.get("key_fp"), "key_pend_fp" in st

    def queue(self) -> list:
        return self._state().get("queue", [])

    @property
    def sounds_played(self) -> int:
        return self._sound_base + self._state().get("sounds", 0)

    @property
    def last_sound(self) -> tuple:
        """(samples, sample rate) of the last sound sent to the amplifier."""
        st = self._state()
        return st.get("last_samples", 0), st.get("last_rate", 0)

    def sound_file(self) -> bytes | None:
        path = self.dir / "spiffs" / "press.wav"
        return path.read_bytes() if path.exists() else None
