"""A self-signed HTTPS certificate for the phone remote (REMOTE_HTTPS).

Phones only allow the microphone and notifications on https pages. The certificate is made on
this PC for its home-network address and kept in the data folder; the phone shows a warning
the first time ("not private"): choose Advanced -> Proceed. The fingerprint shown in Jarvis lets
you check it is really your PC.
"""

from __future__ import annotations

import hashlib
import ipaddress
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

CERT, KEY, PASS = "remote-cert.pem", "remote-key.pem", "remote-key.pass"


def _passphrase(key_path: Path) -> bytes:
    """The random passphrase of the encrypted key; kept next to it, protected for this Windows account (DPAPI) like the pairing file."""
    from app.security.protect import read_bytes, write_bytes

    path = Path(key_path).with_name(PASS)
    if path.is_file():
        return read_bytes(path)
    import secrets

    value = secrets.token_urlsafe(32).encode("ascii")
    write_bytes(path, value)
    return value


def _protect_existing_key(key_path: Path) -> None:
    """A key written by an older version is plain text on disk: encrypt it in place (same key, same certificate, the phone's pin still matches)."""
    from cryptography.hazmat.primitives import serialization

    raw = Path(key_path).read_bytes()
    if b"ENCRYPTED" in raw[:60]:
        return
    key = serialization.load_pem_private_key(raw, password=None)
    Path(key_path).write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.BestAvailableEncryption(_passphrase(key_path))))


def ensure_certificate(data_dir: Path, address: str) -> tuple[Path, Path]:
    """The certificate and key for ``address`` (made again if missing, expired or for another address)."""
    cert_path, key_path = Path(data_dir) / CERT, Path(data_dir) / KEY
    if cert_path.is_file() and key_path.is_file() and _valid_for(cert_path, address):
        try:
            _protect_existing_key(key_path)
        except Exception:
            pass                                     # still usable; it is tried again next start
        return cert_path, key_path
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jarvis on this PC")])
    now = datetime.now(timezone.utc)
    names: list = [x509.DNSName("localhost")]
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(address)))
    except ValueError:
        pass
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.BestAvailableEncryption(_passphrase(key_path))))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _valid_for(cert_path: Path, address: str) -> bool:
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if cert.not_valid_after_utc < datetime.now(timezone.utc) + timedelta(days=7):
            return False
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return ipaddress.ip_address(address) in names.get_values_for_type(x509.IPAddress)
    except Exception:
        return False


def fingerprint(cert_path: Path) -> str:
    """SHA-256 of the certificate, as the phone's browser shows it (AB:CD:...)."""
    der = ssl.PEM_cert_to_DER_cert(Path(cert_path).read_text(encoding="ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def server_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    encrypted = b"ENCRYPTED" in Path(key_path).read_bytes()[:60]
    context.load_cert_chain(str(cert_path), str(key_path), password=_passphrase(key_path) if encrypted else None)
    return context
