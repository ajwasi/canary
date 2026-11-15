"""Starts a complete local test rig: the server, the dev broker and emulated buttons.

    ..\\server\\.venv\\Scripts\\python run_rig.py            (one button)
    ..\\server\\.venv\\Scripts\\python run_rig.py --buttons 3

Everything it stores goes in emulator/state. Delete that folder to start fresh.
Press Ctrl+C to stop.
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_DIR = HERE.parent / "server"
ADMIN_EMAIL, ADMIN_PASSWORD = "admin@example.com", "local-test-rig-only"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--buttons", type=int, default=1, help="how many emulated buttons to start")
    ap.add_argument("--real-timing", action="store_true", help="use the firmware's real timings (10 s hold, 5 min status)")
    a = ap.parse_args()

    state = HERE / "state"
    state.mkdir(exist_ok=True)
    env = {**os.environ, "DATA_DIR": str(state / "server"), "MQTT_HOST": "127.0.0.1", "MQTT_PORT": "1883",
           "PUBLIC_URL": "http://localhost:8000", "ADMIN_EMAIL": ADMIN_EMAIL, "ADMIN_PASSWORD": ADMIN_PASSWORD}
    py = sys.executable
    procs = [subprocess.Popen([py, "-m", "uvicorn", "app.main:app", "--port", "8000", "--log-level", "warning"],
                              cwd=SERVER_DIR, env=env)]
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://localhost:8000/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            sys.exit("The server did not start. Is port 8000 already in use?")
        procs.append(subprocess.Popen([py, "dev_broker.py", "--device-ca", str(state / "server" / "ca" / "device-ca.pem"),
                                       "--state", str(state)], cwd=HERE))
        time.sleep(1.5)
        print(f"\nServer UI:   http://localhost:8000   sign in as {ADMIN_EMAIL} (test password: see emulator/README.md)")
        for i in range(a.buttons):
            panel = 8090 + 2 * i
            cmd = [py, "button_emulator.py", "--dev", "--state", str(state), "--serial", f"BTN-E0E0E0E0{i + 1:04X}",
                   "--panel-port", str(panel), "--ap-port", str(panel + 1)]
            procs.append(subprocess.Popen(cmd + ([] if a.real_timing else ["--fast"]), cwd=HERE,
                                          stdout=subprocess.DEVNULL))
            print(f"Button {i + 1}:    http://localhost:{panel}   (its setup page: http://localhost:{panel + 1})", flush=True)
        print("\nTo set a button up: add it on the server's Buttons page to get a claim code, open the button's\n"
              "setup page, choose Warehouse-Staff (password blue-forklift-42), server address localhost, and the code.\n", flush=True)
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
