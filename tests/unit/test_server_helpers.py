"""Unit tests for lancast.server helpers (no sockets, no real ports)."""

from __future__ import annotations

import ssl

import pytest

from lancast.certs import ensure_certificates
from lancast.server import TokenBucket, load_static, make_ssl_context, normalize_ip


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.2", "192.168.1.2"),
        ("10.0.0.1", "10.0.0.1"),
        ("::ffff:192.168.1.2", "192.168.1.2"),
        ("::ffff:10.1.2.3", "10.1.2.3"),
        ("fe80::1%eth0", "fe80::1"),
        ("fe80::1%12", "fe80::1"),
        ("::1", "::1"),
        ("2001:db8::1", "2001:db8::1"),
        (None, None),
        ("", None),
        ("not-an-ip", None),
        ("999.1.1.1", None),
        ("192.168.1.2/24", None),
        ("fe80::1%", "fe80::1"),
    ],
)
def test_normalize_ip(raw, expected):
    assert normalize_ip(raw) == expected


def test_load_static_only_whitelisted_flat_files(tmp_path):
    files = {
        "index.html": b"<html></html>",
        "app.js": b"console.log(1)",
        "style.css": b"body {}",
        "logo.svg": b"<svg/>",
        "photo.png": b"\x89PNG",
        "favicon.ico": b"\x00\x00\x01\x00",
        "site.webmanifest": b"{}",
        "data.json": b"{}",
        "notes.txt": b"ignored",
        "server.py": b"ignored",
        "README": b"ignored",
        "archive.tar.gz": b"ignored",
        "selftest.js": b"ignored",
        "selftest_notes.html": b"ignored",
        "mock_server.py": b"ignored",
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)

    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "inner.html").write_bytes(b"ignored")
    (nested / "inner.js").write_bytes(b"ignored")

    loaded = load_static(tmp_path)

    assert set(loaded) == {
        "index.html",
        "app.js",
        "style.css",
        "logo.svg",
        "photo.png",
        "favicon.ico",
        "site.webmanifest",
        "data.json",
    }
    assert loaded["index.html"] == (b"<html></html>", "text/html; charset=utf-8")
    assert loaded["app.js"] == (b"console.log(1)", "text/javascript; charset=utf-8")
    assert loaded["style.css"] == (b"body {}", "text/css; charset=utf-8")
    assert loaded["logo.svg"] == (b"<svg/>", "image/svg+xml")
    assert loaded["site.webmanifest"] == (b"{}", "application/manifest+json")
    assert loaded["data.json"] == (b"{}", "application/json")


def test_load_static_missing_directory_is_empty(tmp_path):
    assert load_static(tmp_path / "does-not-exist") == {}


def test_mjs_files_are_served_as_javascript(tmp_path):
    # docs/PROTOCOL.md §2 lists .mjs; selftest*.mjs is still excluded by name.
    (tmp_path / "module.mjs").write_bytes(b"export const answer = 42;")

    loaded = load_static(tmp_path)

    assert "module.mjs" in loaded
    assert loaded["module.mjs"][1].startswith("text/javascript")


def test_token_bucket_burst_and_refill(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("lancast.server.time.monotonic", lambda: now[0])

    bucket = TokenBucket(burst=5, per_s=2.0)
    assert all(bucket.allow() for _ in range(5))
    assert bucket.allow() is False

    now[0] += 0.25  # +0.5 tokens: not enough
    assert bucket.allow() is False

    now[0] += 0.25  # +0.5 tokens: exactly one token
    assert bucket.allow() is True
    assert bucket.allow() is False

    now[0] += 10.0  # refill is capped at the burst size
    assert all(bucket.allow() for _ in range(5))
    assert bucket.allow() is False


def test_make_ssl_context_uses_generated_chain_and_tls12_floor(tmp_path):
    bundle = ensure_certificates(tmp_path / "certs", ["127.0.0.1"], hostname="testpc")

    ctx = make_ssl_context(bundle)

    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.protocol == ssl.PROTOCOL_TLS_SERVER
    assert ctx.get_ciphers()  # cipher suites were configured successfully
