"""Device certificate authority: signs the CSRs that buttons send.

The CA key is stored encrypted with SECRET_KEY. The CA certificate
(device-ca.pem) is what the MQTT broker uses to verify buttons.
"""
import re
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import settings

SERIAL_RE = re.compile(r"^BTN-[0-9A-F]{12}$")
CA_DIR = settings.data_dir / "ca"
CA_CERT = CA_DIR / "device-ca.pem"
CA_KEY = CA_DIR / "device-ca.key"


class CsrError(ValueError):
    pass


def ensure_ca() -> None:
    if CA_CERT.exists() and CA_KEY.exists():
        return
    CA_DIR.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Button Notifier Device CA")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    CA_KEY.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(settings.secret_key.encode())))
    CA_CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _load():
    key = serialization.load_pem_private_key(CA_KEY.read_bytes(), settings.secret_key.encode())
    cert = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
    return key, cert


def sign_csr(csr_pem: str, serial: str) -> tuple[str, datetime]:
    """Returns (certificate PEM, expiry as naive UTC). Raises CsrError if the CSR is unacceptable."""
    if not SERIAL_RE.match(serial):
        raise CsrError("bad serial")
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except ValueError as exc:
        raise CsrError("unreadable CSR") from exc
    if not csr.is_signature_valid:
        raise CsrError("CSR signature invalid")
    names = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(names) != 1 or names[0].value != serial:
        raise CsrError("CSR name does not match serial")
    pub = csr.public_key()
    if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve, ec.SECP256R1):
        raise CsrError("key must be ECDSA P-256")

    ca_key, ca_cert = _load()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=settings.cert_days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, serial)]))
        .issuer_name(ca_cert.subject).public_key(pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode(), expires.replace(tzinfo=None)
