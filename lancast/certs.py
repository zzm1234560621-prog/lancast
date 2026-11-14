"""Local certificate authority and TLS server certificate.

A phone cannot verify a server on a bare LAN IP against a public CA, so LanCast
creates a tiny PKI of its own on first run:

* a root CA (ECDSA P-256, 10 years) whose critical NameConstraints extension
  only permits private / link-local / loopback / CGNAT addresses and the names
  ``localhost`` and ``*.local``. Even if its key leaked it could not be used to
  impersonate a public website on a phone that trusts it;
* a server certificate (397 days) signed by that CA. It is re-issued
  automatically when the machine's LAN addresses change or expiry is near.

Devices that install and trust the CA (optional, via ``/ca.crt``) get a
warning-free and MITM-proof connection; everyone else still gets TLS
encryption after clicking through the self-signed warning once.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("lancast.certs")

PERMITTED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "100.64.0.0/10",   # CGNAT, used by Tailscale
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)
PERMITTED_DNS = ("localhost", "local")

CA_DAYS = 3650
LEAF_DAYS = 397                       # Apple caps TLS server certificates at 825 days
RENEW_BEFORE = dt.timedelta(days=30)

CA_KEY, CA_CERT = "ca.key.pem", "ca.crt.pem"
SERVER_KEY, SERVER_CERT = "server.key.pem", "server.crt.pem"


@dataclass(frozen=True)
class CertBundle:
    cert_path: Path          # server certificate followed by the CA certificate (PEM chain)
    key_path: Path           # server private key (PEM, PKCS#8)
    ca_der: bytes            # CA certificate in DER form, served at /ca.crt
    cert_sha256: str         # "AB:CD:..." fingerprint of the server certificate
    ca_sha256: str
    sans: tuple[str, ...]    # names and addresses the server certificate covers


def fingerprint(cert: x509.Certificate) -> str:
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _atomic_write(path: Path, data: bytes, private: bool) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _is_permitted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr.version == net.version and addr in net for net in PERMITTED_NETWORKS)


def server_names(ips, hostname: str) -> tuple[list, list[str]]:
    """Addresses and DNS names for the server certificate.

    Addresses outside the CA's name constraints are left out: one such entry
    would make strict clients reject the whole certificate.
    """
    addrs = {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
    for raw in ips:
        try:
            addr = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            continue
        if _is_permitted(addr):
            addrs.add(addr)
        else:
            log.info("not adding %s to the certificate (not a private address)", addr)
    dns = ["localhost"]
    host = (hostname or "").strip().lower()
    if re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", host):
        dns.append(f"{host}.local")
    return sorted(addrs, key=lambda a: (a.version, int(a))), dns


def _san_strings(ips, dns) -> list[str]:
    return sorted([*dns, *(str(a) for a in ips)])


def _load_ca(data_dir: Path):
    key_p, crt_p = data_dir / CA_KEY, data_dir / CA_CERT
    if not (key_p.exists() and crt_p.exists()):
        return None
    try:
        key = serialization.load_pem_private_key(key_p.read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(crt_p.read_bytes())
    except Exception as exc:  # noqa: BLE001 - any damage means "make a new CA"
        log.warning("existing CA is unreadable (%s); creating a new one", exc)
        return None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        return None
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        log.warning("CA key does not match its certificate; creating a new CA")
        return None
    if cert.not_valid_after_utc - _now() < RENEW_BEFORE:
        log.info("CA certificate is about to expire; creating a new one")
        return None
    return key, cert


def _create_ca(data_dir: Path, hostname: str):
    key = ec.generate_private_key(ec.SECP256R1())
    label = f"LanCast Local CA ({hostname})" if hostname else "LanCast Local CA"
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, label[:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LanCast"),
        ]
    )
    now = _now()
    constraints = x509.NameConstraints(
        permitted_subtrees=[x509.IPAddress(n) for n in PERMITTED_NETWORKS]
        + [x509.DNSName(d) for d in PERMITTED_DNS],
        excluded_subtrees=None,
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(constraints, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _atomic_write(data_dir / CA_KEY, _key_pem(key), private=True)
    _atomic_write(data_dir / CA_CERT, cert.public_bytes(serialization.Encoding.PEM), private=False)
    # A new CA invalidates any server certificate signed by the old one.
    for stale in (SERVER_KEY, SERVER_CERT):
        try:
            (data_dir / stale).unlink()
        except FileNotFoundError:
            pass
    log.info("created local CA %s", fingerprint(cert))
    return key, cert


def _load_leaf(data_dir: Path, ca_cert: x509.Certificate, want_sans: list[str]):
    key_p, crt_p = data_dir / SERVER_KEY, data_dir / SERVER_CERT
    if not (key_p.exists() and crt_p.exists()):
        return None
    try:
        key = serialization.load_pem_private_key(key_p.read_bytes(), password=None)
        chain = x509.load_pem_x509_certificates(crt_p.read_bytes())
        leaf = chain[0]
        leaf.verify_directly_issued_by(ca_cert)
    except Exception as exc:  # noqa: BLE001
        log.info("server certificate needs re-issuing (%s)", exc)
        return None
    if leaf.public_key().public_numbers() != key.public_key().public_numbers():
        return None
    if leaf.not_valid_after_utc - _now() < RENEW_BEFORE:
        return None
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    have = sorted([*san.get_values_for_type(x509.DNSName), *(str(a) for a in san.get_values_for_type(x509.IPAddress))])
    if have != want_sans:
        log.info("LAN addresses changed; re-issuing the server certificate")
        return None
    return key, leaf


def _issue_leaf(data_dir: Path, ca_key, ca_cert: x509.Certificate, ips, dns):
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LanCast server")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([*(x509.DNSName(d) for d in dns), *(x509.IPAddress(a) for a in ips)]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    chain = leaf.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    _atomic_write(data_dir / SERVER_KEY, _key_pem(key), private=True)
    _atomic_write(data_dir / SERVER_CERT, chain, private=False)
    log.info("issued server certificate %s", fingerprint(leaf))
    return key, leaf


def ensure_certificates(data_dir: Path, ips, hostname: str | None = None) -> CertBundle:
    """Load or create the CA and a server certificate covering ``ips``."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    hostname = hostname if hostname is not None else socket.gethostname()

    ca = _load_ca(data_dir) or _create_ca(data_dir, hostname)
    ca_key, ca_cert = ca
    want_ips, want_dns = server_names(ips, hostname)
    want_sans = _san_strings(want_ips, want_dns)
    leaf = _load_leaf(data_dir, ca_cert, want_sans) or _issue_leaf(data_dir, ca_key, ca_cert, want_ips, want_dns)
    _, leaf_cert = leaf
    return CertBundle(
        cert_path=data_dir / SERVER_CERT,
        key_path=data_dir / SERVER_KEY,
        ca_der=ca_cert.public_bytes(serialization.Encoding.DER),
        cert_sha256=fingerprint(leaf_cert),
        ca_sha256=fingerprint(ca_cert),
        sans=tuple(want_sans),
    )
