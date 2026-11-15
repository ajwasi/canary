"""MQTT link to the broker: stores presses and status, answers renewal CSRs,
and publishes commands to buttons. Message formats are in docs/protocol.md."""
import json
import logging
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as paho
from sqlalchemy.exc import IntegrityError

from . import ca
from .config import settings
from .db import Button, Press, SessionLocal, audit, utcnow

log = logging.getLogger("mqtt")
MAX_FUTURE = timedelta(minutes=5)

_client: paho.Client | None = None
outbox: list[tuple[str, dict, bool]] = []       # filled instead of publishing when MQTT is disabled (tests)


def publish(serial: str, kind: str, payload: dict, retain: bool = False) -> None:
    topic = f"btn/{serial}/cmd/{kind}"
    body = {"type": kind, **payload}
    if _client is None:
        outbox.append((topic, body, retain))
        return
    _client.publish(topic, json.dumps(body), qos=1, retain=retain)


def _from_epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _on_press(db, button: Button, data: dict) -> None:
    seq = data.get("seq")
    if not isinstance(seq, int):
        return
    now = utcnow()
    pressed, approximate = _from_epoch(data.get("ts")) if "ts" in data else None, False
    if pressed is None or pressed > now + MAX_FUTURE:
        age = data.get("age_ms")
        if isinstance(age, (int, float)) and age >= 0:
            pressed = now - timedelta(milliseconds=age)
        else:
            pressed, approximate = now, True
    db.add(Press(button_id=button.id, epoch=button.epoch, seq=seq, pressed_at=pressed, received_at=now,
                 approximate=approximate, person_id=button.person_id, location_id=button.location_id))
    if not button.last_press_at or pressed > button.last_press_at:
        button.last_press_at = pressed
    button.last_seen = now
    try:
        db.commit()
    except IntegrityError:
        db.rollback()                              # the device resent a press we already have


def _on_status(db, button: Button, data: dict) -> None:
    button.online = bool(data.get("online"))
    if button.online:
        button.last_seen = utcnow()
        button.fw = str(data.get("fw", button.fw))[:40]
        button.rssi = data.get("rssi") if isinstance(data.get("rssi"), int) else None
        button.channel = data.get("channel") if isinstance(data.get("channel"), int) else None
        button.ip = str(data.get("ip", ""))[:45]
        button.audio_sha = str(data.get("audio_sha", ""))[:64].lower()
        button.nc_fault = bool(data.get("nc_fault"))
        button.pending = data.get("pending") if isinstance(data.get("pending"), int) else 0
        expiry = _from_epoch(data.get("cert_not_after"))
        if expiry:
            button.cert_not_after = expiry
    db.commit()


def _on_csr(db, button: Button, data: dict) -> None:
    try:
        cert, expires = ca.sign_csr(str(data.get("csr", "")), button.serial)
    except ca.CsrError as exc:
        log.warning("renewal refused for %s: %s", button.serial, exc)
        return
    button.cert_not_after = expires
    audit(db, button.serial, "certificate renewed")
    db.commit()
    publish(button.serial, "cert", {"cert": cert})


HANDLERS = {"press": _on_press, "status": _on_status, "csr": _on_csr}


def handle_message(topic: str, payload: bytes) -> None:
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "btn" or parts[2] not in HANDLERS:
        return
    try:
        data = json.loads(payload)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    with SessionLocal() as db:
        button = db.query(Button).filter_by(serial=parts[1]).first()
        if not button or button.revoked:
            return
        HANDLERS[parts[2]](db, button, data)


def _on_connect(client, userdata, flags, reason_code, properties):
    log.info("broker connected: %s", reason_code)
    client.subscribe([("btn/+/press", 1), ("btn/+/status", 1), ("btn/+/csr", 1)])


def _on_message(client, userdata, msg):
    try:
        handle_message(msg.topic, msg.payload)
    except Exception:
        log.exception("failed to handle %s", msg.topic)


def start() -> None:
    """Connect in the background. A fixed client id and a persistent session make
    the broker hold presses for us while this app is restarting."""
    global _client
    if not settings.mqtt_enabled:
        return
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="button-server", clean_session=False)
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.reconnect_delay_set(1, 30)
    client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=30)
    client.loop_start()
    _client = client


def stop() -> None:
    global _client
    if _client:
        _client.loop_stop()
        _client.disconnect()
        _client = None
