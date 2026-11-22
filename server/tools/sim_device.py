"""Button simulator: behaves like the firmware so the server can be tried without hardware.

Enrol once with a claim code from the UI, then press Enter to send presses:

    python tools/sim_device.py --server https://buttons.example.com --claim K7Q2-9XPD

State (key, certificate, counters) is kept in --state so later runs reconnect
as the same button. Use --enrol-only to test enrolment without an MQTT broker.
"""
import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import paho.mqtt.client as paho
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def new_key_and_csr(serial: str):
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, serial)])).sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return key_pem, csr.public_bytes(serialization.Encoding.PEM).decode()


def state_root(arg: str) -> Path:
    """--state, resolved. It has to be inside the current folder, your home folder or the temp folder."""
    path = os.path.realpath(arg)
    for allowed in (os.getcwd(), os.path.expanduser("~"), tempfile.gettempdir()):
        root = os.path.realpath(allowed)
        try:
            if os.path.commonpath([path, root]) == root:
                return Path(path)
        except ValueError:                       # different drives on Windows
            continue
    sys.exit(f"--state {arg!r} must be inside the current folder, your home folder or the temp folder")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True, help="Server URL, e.g. https://buttons.example.com")
    ap.add_argument("--claim", help="Claim code (needed the first time only)")
    ap.add_argument("--serial", default="BTN-5100000000A1")
    ap.add_argument("--name", default="Simulated button")
    ap.add_argument("--mqtt-port", type=int, default=8883)
    ap.add_argument("--state", default="./sim_state")
    ap.add_argument("--enrol-only", action="store_true")
    args = ap.parse_args()

    if not re.fullmatch(r"BTN-[0-9A-F]{12}", args.serial):      # the shape the server accepts
        sys.exit("--serial must be BTN- plus 12 hex digits, for example BTN-5100000000A1")
    base = state_root(args.state)
    state = (base / args.serial).resolve()
    if state.parent != base:
        sys.exit("--serial points outside the state folder")
    state.mkdir(parents=True, exist_ok=True)
    key_file, cert_file, meta_file = state / "key.pem", state / "cert.pem", state / "meta.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {"seq": 0, "boot": 0, "fw": "0.1.0", "audio_sha": ""}
    meta["boot"] += 1

    def save_meta():
        meta_file.write_text(json.dumps(meta))

    if not cert_file.exists():
        if not args.claim:
            sys.exit("Not enrolled yet: pass --claim with a code from the server's Buttons page.")
        key_pem, csr = new_key_and_csr(args.serial)
        body = json.dumps({"serial": args.serial, "name": args.name, "claim": args.claim, "csr": csr}).encode()
        req = urllib.request.Request(args.server.rstrip("/") + "/api/device/enroll", body,
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                cert = json.load(resp)["cert"]
        except urllib.error.HTTPError as exc:
            sys.exit(f"Enrolment refused: HTTP {exc.code} {exc.read().decode(errors='replace')}")
        key_file.write_bytes(key_pem)
        cert_file.write_text(cert)
        print(f"Enrolled as {args.serial}.")
    save_meta()
    if args.enrol_only:
        return

    base = f"btn/{args.serial}"
    pending: dict = {}
    started = time.time()

    def status(client):
        expiry = x509.load_pem_x509_certificate(cert_file.read_bytes()).not_valid_after_utc.timestamp()
        client.publish(f"{base}/status", json.dumps({
            "online": True, "serial": args.serial, "name": args.name, "fw": meta["fw"], "rssi": -55, "channel": 44,
            "ip": "192.0.2.10", "uptime_s": int(time.time() - started), "pending": 0, "cert_not_after": int(expiry),
            "audio_sha": meta["audio_sha"], "nc_fault": False}), qos=1, retain=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"MQTT: {reason_code}")
        client.subscribe(f"{base}/cmd/#", qos=1)
        status(client)

    def on_message(client, userdata, msg):
        cmd = json.loads(msg.payload)
        kind = cmd.get("type")
        if kind == "ota" and cmd.get("version") != meta["fw"]:
            size = len(urllib.request.urlopen(cmd["url"], timeout=30).read())
            print(f"Firmware update: downloaded {size} bytes, now 'running' {cmd['version']}")
            meta["fw"] = cmd["version"]
        elif kind == "audio" and cmd.get("sha256") != meta["audio_sha"]:
            data = urllib.request.urlopen(cmd["url"], timeout=30).read()
            if hashlib.sha256(data).hexdigest() != cmd["sha256"]:
                print("Sound update: checksum mismatch, ignored")
                return
            print(f"Sound update: {len(data)} bytes installed")
            meta["audio_sha"] = cmd["sha256"]
        elif kind == "cert" and pending.get("key"):
            key_file.write_bytes(pending.pop("key"))
            cert_file.write_text(cmd["cert"])
            print("Certificate renewed (used from the next start).")
        elif kind in ("identify", "reboot", "name"):
            print(f"Command: {cmd}")
        else:
            return
        save_meta()
        status(client)

    tls = ssl.create_default_context()
    tls.load_cert_chain(cert_file, key_file)
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=args.serial, clean_session=False)
    client.tls_set_context(tls)
    client.will_set(f"{base}/status", '{"online":false}', qos=1, retain=True)
    client.on_connect, client.on_message = on_connect, on_message
    client.connect(urlparse(args.server).hostname, args.mqtt_port, keepalive=30)
    client.loop_start()

    print("Enter = press, r = request certificate renewal, q = quit")
    try:
        for line in sys.stdin:
            word = line.strip().lower()
            if word == "q":
                break
            if word == "r":
                pending["key"], csr = new_key_and_csr(args.serial)
                client.publish(f"{base}/csr", json.dumps({"csr": csr}), qos=1)
                continue
            client.publish(f"{base}/press", json.dumps(
                {"seq": meta["seq"], "boot": meta["boot"], "ts": int(time.time()), "age_ms": 0}), qos=1)
            print(f"Pressed (seq {meta['seq']})")
            meta["seq"] += 1
            save_meta()
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
