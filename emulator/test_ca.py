"""Makes a throw-away root certificate and a localhost server certificate for the test rig.

    python test_ca.py ../firmware/host/test-ca

The PC build of the firmware (firmware/sdkconfig.defaults.linux) adds root.pem to its list
of trusted roots, so the test server and broker must present localhost.pem from the same
folder. Never use these files for anything real; the keys are not protected.
"""
import ipaddress
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _write(folder: Path, name: str, key, cert) -> None:
    (folder / f"{name}.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (folder / f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def make(folder: Path) -> Path:
    """Creates the files if they are missing and returns the folder."""
    folder.mkdir(parents=True, exist_ok=True)
    if all((folder / n).exists() for n in ("root.pem", "localhost.pem", "localhost.key")):
        return folder
    now = datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Canary test rig"),
                           x509.NameAttribute(NameOID.COMMON_NAME, "Canary test root (not for real use)")])
    root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(root_key, hashes.SHA256()))
    _write(folder, "root", root_key, root)

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(root_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(root_key, hashes.SHA256()))
    _write(folder, "localhost", key, cert)
    return folder


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(f"test certificates are in {make(Path(sys.argv[1])).resolve()}")
