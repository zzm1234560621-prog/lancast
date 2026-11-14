"""Unit tests for the local CA / server certificate issuance (lancast.certs)."""

from __future__ import annotations

import datetime as dt
import ipaddress

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

from lancast.certs import CA_CERT, CA_KEY, ensure_certificates


def _load_ca(data_dir) -> x509.Certificate:
    return x509.load_pem_x509_certificate((data_dir / CA_CERT).read_bytes())


def _load_leaf(bundle) -> x509.Certificate:
    return x509.load_pem_x509_certificates(bundle.cert_path.read_bytes())[0]


def _verify(ca, subject, leaf) -> None:
    PolicyBuilder().store(Store([ca])).build_server_verifier(subject).verify(leaf, [])


def _ip(value: str) -> x509.IPAddress:
    return x509.IPAddress(ipaddress.ip_address(value))


def _sign_leaf(ca_key, ca_cert, ca_ski, san: x509.GeneralName) -> x509.Certificate:
    """Hand-sign a leaf with the CA key; only the SAN differs from the real leaf."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LanCast test leaf")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
    )
    return builder.sign(ca_key, hashes.SHA256())


def test_bundle_files_and_private_names_verify(tmp_path):
    bundle = ensure_certificates(tmp_path, ["192.168.1.55"], hostname="testpc")
    ca = _load_ca(tmp_path)
    leaf = _load_leaf(bundle)

    assert bundle.cert_path.is_file()
    assert bundle.key_path.is_file()
    assert bundle.cert_path == tmp_path / "server.crt.pem"
    assert bundle.key_path == tmp_path / "server.key.pem"
    assert bundle.ca_der == ca.public_bytes(serialization.Encoding.DER)
    assert set(bundle.sans) == {"127.0.0.1", "::1", "192.168.1.55", "localhost", "testpc.local"}

    for subject in (
        _ip("127.0.0.1"),
        _ip("192.168.1.55"),
        x509.DNSName("testpc.local"),
        x509.DNSName("localhost"),
    ):
        _verify(ca, subject, leaf)  # must not raise


def test_ca_has_critical_name_constraints(tmp_path):
    ensure_certificates(tmp_path, ["192.168.1.55"], hostname="testpc")
    ca = _load_ca(tmp_path)

    ext = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert ext.critical is True

    permitted = ext.value.permitted_subtrees
    networks = {str(item.value) for item in permitted if isinstance(item, x509.IPAddress)}
    dns_names = {str(item.value) for item in permitted if isinstance(item, x509.DNSName)}
    assert {"192.168.0.0/16", "10.0.0.0/8", "127.0.0.0/8"} <= networks
    assert {"localhost", "local"} <= dns_names


def test_name_constraints_reject_public_sans(tmp_path):
    ensure_certificates(tmp_path, ["192.168.1.55"], hostname="testpc")
    ca = _load_ca(tmp_path)
    ca_key = serialization.load_pem_private_key((tmp_path / CA_KEY).read_bytes(), password=None)
    ca_ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value

    evil_ip = _sign_leaf(ca_key, ca, ca_ski, _ip("8.8.8.8"))
    evil_dns = _sign_leaf(ca_key, ca, ca_ski, x509.DNSName("example.com"))

    with pytest.raises(VerificationError, match="name constraints"):
        _verify(ca, _ip("8.8.8.8"), evil_ip)

    with pytest.raises(VerificationError, match="name constraints"):
        _verify(ca, x509.DNSName("example.com"), evil_dns)


def test_public_ips_are_not_put_in_the_san(tmp_path):
    bundle = ensure_certificates(tmp_path, ["8.8.8.8", "192.168.9.9"], hostname="testpc")
    leaf = _load_leaf(bundle)
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = {str(item) for item in san.get_values_for_type(x509.IPAddress)}

    assert "8.8.8.8" not in ips
    assert "192.168.9.9" in ips
    assert "8.8.8.8" not in bundle.sans
    _verify(_load_ca(tmp_path), _ip("192.168.9.9"), leaf)


def test_ip_change_reissues_but_reuses_the_ca(tmp_path):
    first = ensure_certificates(tmp_path, ["192.168.1.10"], hostname="testpc")
    ca_sha = first.ca_sha256
    leaf_sha = first.cert_sha256

    unchanged = ensure_certificates(tmp_path, ["192.168.1.10"], hostname="testpc")
    assert unchanged.cert_sha256 == leaf_sha
    assert unchanged.ca_sha256 == ca_sha

    changed = ensure_certificates(tmp_path, ["192.168.1.11"], hostname="testpc")
    assert changed.cert_sha256 != leaf_sha
    assert changed.ca_sha256 == ca_sha  # re-signing must not replace the CA

    leaf = _load_leaf(changed)
    _verify(_load_ca(tmp_path), _ip("192.168.1.11"), leaf)
    assert "192.168.1.11" in changed.sans


def test_leaf_validity_and_extensions(tmp_path):
    bundle = ensure_certificates(tmp_path, ["127.0.0.1"], hostname="testpc")
    leaf = _load_leaf(bundle)

    span = leaf.not_valid_after_utc - leaf.not_valid_before_utc
    assert dt.timedelta(0) < span <= dt.timedelta(days=825)

    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku

    basic = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic.ca is False

    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    assert "testpc.local" in san.get_values_for_type(x509.DNSName)


def test_corrupt_ca_key_rebuilds_the_ca(tmp_path):
    first = ensure_certificates(tmp_path, ["192.168.1.10"], hostname="testpc")

    (tmp_path / CA_KEY).write_bytes(b"this is not a private key")

    second = ensure_certificates(tmp_path, ["192.168.1.10"], hostname="testpc")
    assert second.ca_sha256 != first.ca_sha256
    assert second.cert_sha256 != first.cert_sha256  # old leaf was signed by the old CA

    ca = _load_ca(tmp_path)
    _verify(ca, _ip("192.168.1.10"), _load_leaf(second))
