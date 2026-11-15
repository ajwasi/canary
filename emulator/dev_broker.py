"""Small MQTT 3.1.1 broker for local testing. It stands in for Mosquitto and
applies the same rules as server/mosquitto/mosquitto.conf:

  port 1883 (127.0.0.1 only)  plain, anonymous, full access: for the server app
  port 8883                   TLS, client certificate from the device CA required;
                              the certificate's common name is the user and may
                              only use topics under btn/<common name>/

Supported: QoS 0 and 1, retained messages, last will, persistent sessions,
+ and # wildcards, keep-alive. Not supported: QoS 2, redelivery of unacknowledged
messages, persistence across a broker restart. Do not use it in production.

    python dev_broker.py --device-ca ../server/data/ca/device-ca.pem
"""
import argparse
import asyncio
import ipaddress
import logging
import ssl
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

log = logging.getLogger("broker")
CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = \
    1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 14


def ensure_dev_cert(state: Path) -> tuple[Path, Path]:
    """Self-signed server certificate for localhost. Emulated buttons trust it with --cafile."""
    cert_file, key_file = state / "dev-server.pem", state / "dev-server.key"
    if cert_file.exists() and key_file.exists():
        return cert_file, key_file
    state.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file


def matches(topic_filter: str, topic: str) -> bool:
    f, t = topic_filter.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


def packet(kind: int, flags: int, body: bytes) -> bytes:
    n, length = len(body), bytearray()
    while True:
        digit, n = n % 128, n // 128
        length.append(digit | (0x80 if n else 0))
        if not n:
            break
    return bytes([kind << 4 | flags]) + bytes(length) + body


def mqtt_str(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("!H", len(raw)) + raw


class Session:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.subs: dict[str, int] = {}
        self.queue: list[bytes] = []            # QoS 1 messages held while a persistent client is offline
        self.writer: asyncio.StreamWriter | None = None
        self.clean = True
        self.next_id = 0

    def send(self, data: bytes) -> None:
        if self.writer:
            self.writer.write(data)

    def deliver(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        body = mqtt_str(topic)
        if qos:
            self.next_id = self.next_id % 65535 + 1
            body += struct.pack("!H", self.next_id)
        data = packet(PUBLISH, (qos << 1) | (1 if retain else 0), body + payload)
        if self.writer:
            self.writer.write(data)
        elif qos and not self.clean:
            self.queue.append(data)


class Broker:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.retained: dict[str, tuple[bytes, int]] = {}

    def route(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        if retain:
            if payload:
                self.retained[topic] = (payload, qos)
            else:
                self.retained.pop(topic, None)
        for s in self.sessions.values():
            granted = [q for f, q in s.subs.items() if matches(f, topic)]
            if granted:
                s.deliver(topic, payload, min(qos, max(granted)), False)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tls: bool) -> None:
        owner = None                              # None = unrestricted (internal listener)
        if tls:
            cert = writer.get_extra_info("peercert") or {}
            owner = next((v for rdn in cert.get("subject", ()) for k, v in rdn if k == "commonName"), "")
        allowed = (lambda topic: True) if owner is None else (lambda topic: topic.startswith(f"btn/{owner}/"))
        session, will, keepalive, clean_exit = None, None, 0, False

        async def read_packet():
            head = await reader.readexactly(1)
            length, shift = 0, 0
            while True:
                b = (await reader.readexactly(1))[0]
                length |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            return head[0] >> 4, head[0] & 0x0F, await reader.readexactly(length)

        try:
            kind, _, body = await asyncio.wait_for(read_packet(), 10)
            if kind != CONNECT:
                return
            pos = 2 + struct.unpack("!H", body[:2])[0]
            level, flags, keepalive = body[pos], body[pos + 1], struct.unpack("!H", body[pos + 2:pos + 4])[0]
            pos += 4

            def take() -> bytes:
                nonlocal pos
                n = struct.unpack("!H", body[pos:pos + 2])[0]
                pos += 2 + n
                return body[pos - n:pos]

            client_id = take().decode() or f"anon-{id(writer)}"
            if flags & 0x04:
                will_topic, will_payload = take().decode(), take()
                will = (will_topic, will_payload, (flags >> 3) & 3, bool(flags & 0x20))
            if level != 4:
                writer.write(packet(CONNACK, 0, bytes([0, 1])))       # unacceptable protocol version
                return

            clean = bool(flags & 0x02)
            old = self.sessions.get(client_id)
            if old and old.writer:
                old.writer.close()                                    # a new connection takes over
            present = bool(old) and not clean
            session = old if present else Session(client_id)
            session.clean, session.writer = clean, writer
            self.sessions[client_id] = session
            writer.write(packet(CONNACK, 0, bytes([1 if present else 0, 0])))
            for data in session.queue:
                writer.write(data)
            session.queue.clear()
            log.info("connected %s%s", client_id, f" (certificate {owner})" if owner is not None else "")

            while True:
                timeout = keepalive * 1.5 if keepalive else None
                kind, flags, body = await asyncio.wait_for(read_packet(), timeout)
                if kind == PUBLISH:
                    qos, retain = (flags >> 1) & 3, bool(flags & 1)
                    n = struct.unpack("!H", body[:2])[0]
                    topic, rest = body[2:2 + n].decode(), body[2 + n:]
                    if qos:
                        session.send(packet(PUBACK, 0, rest[:2]))
                        rest = rest[2:]
                    if allowed(topic):
                        self.route(topic, rest, min(qos, 1), retain)
                    else:
                        log.warning("%s may not publish to %s; dropped", client_id, topic)
                elif kind == SUBSCRIBE:
                    pid, p, codes = body[:2], 2, bytearray()
                    while p < len(body):
                        n = struct.unpack("!H", body[p:p + 2])[0]
                        topic_filter, qos = body[p + 2:p + 2 + n].decode(), min(body[p + 2 + n], 1)
                        p += 3 + n
                        if allowed(topic_filter):
                            session.subs[topic_filter] = qos
                            codes.append(qos)
                        else:
                            codes.append(0x80)
                            log.warning("%s may not subscribe to %s", client_id, topic_filter)
                    session.send(packet(SUBACK, 0, pid + bytes(codes)))
                    for topic, (payload, qos) in list(self.retained.items()):
                        granted = [q for f, q in session.subs.items() if matches(f, topic)]
                        if granted:
                            session.deliver(topic, payload, min(qos, max(granted)), True)
                elif kind == UNSUBSCRIBE:
                    p = 2
                    while p < len(body):
                        n = struct.unpack("!H", body[p:p + 2])[0]
                        session.subs.pop(body[p + 2:p + 2 + n].decode(), None)
                        p += 2 + n
                    session.send(packet(UNSUBACK, 0, body[:2]))
                elif kind == PINGREQ:
                    session.send(packet(PINGRESP, 0, b""))
                elif kind == DISCONNECT:
                    clean_exit = True
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ssl.SSLError, OSError):
            pass
        except Exception:
            log.exception("connection error")
        finally:
            if session and session.writer is writer:
                session.writer = None
                if session.clean:
                    self.sessions.pop(session.client_id, None)
                log.info("disconnected %s%s", session.client_id, "" if clean_exit else " (connection lost)")
                if will and not clean_exit and allowed(will[0]):
                    self.route(*will)                                 # last will: marks the button offline
            writer.close()


async def serve(device_ca: Path, state: Path, tls_port: int = 8883, plain_port: int = 1883,
                ready: asyncio.Event | None = None) -> None:
    broker = Broker()
    cert_file, key_file = ensure_dev_cert(state)
    while not device_ca.exists():
        log.info("waiting for the server to create %s", device_ca)
        await asyncio.sleep(1)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    ctx.load_verify_locations(device_ca)
    ctx.verify_mode = ssl.CERT_REQUIRED
    plain = await asyncio.start_server(lambda r, w: broker.handle(r, w, False), "127.0.0.1", plain_port)
    secure = await asyncio.start_server(lambda r, w: broker.handle(r, w, True), "0.0.0.0", tls_port, ssl=ctx)
    log.info("server app: 127.0.0.1:%d   buttons (TLS + client certificate): port %d   trust file: %s",
             plain_port, tls_port, cert_file)
    if ready:
        ready.set()
    async with plain, secure:
        await asyncio.gather(plain.serve_forever(), secure.serve_forever())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device-ca", default="../server/data/ca/device-ca.pem", help="CA the server signs buttons with")
    ap.add_argument("--state", default="./state", help="where the broker keeps its own localhost certificate")
    ap.add_argument("--tls-port", type=int, default=8883)
    ap.add_argument("--plain-port", type=int, default=1883)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s broker: %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(serve(Path(a.device_ca), Path(a.state), a.tls_port, a.plain_port))
    except KeyboardInterrupt:
        pass
