"""Unit tests for :class:`lancast.auth.Pairing` (docs/PROTOCOL.md section 4)."""

from __future__ import annotations

import base64
import itertools

import pytest

from lancast import auth
from lancast.auth import Pairing


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


@pytest.fixture
def fixed_secrets(monkeypatch):
    """Deterministic PINs/tokens so rotations never collide by chance."""
    pins = itertools.count(123456)
    tokens = itertools.count(1)
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: next(pins) % n)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda n: f"tok-{next(tokens):018d}")


def test_pin_is_six_ascii_digits(fixed_secrets):
    pairing = Pairing(clock=FakeClock())
    for _ in range(50):
        assert len(pairing.pin) == 6
        assert pairing.pin.isascii()
        assert pairing.pin.isdigit()
        pairing.rotate("start")


def test_pin_is_zero_padded(monkeypatch):
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: 42)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda n: "x" * 22)
    assert Pairing(clock=FakeClock()).pin == "000042"


def test_success_rotates_and_old_pin_is_dead(fixed_secrets):
    reasons: list[str] = []
    pairing = Pairing(clock=FakeClock(), on_rotate=reasons.append)
    old_pin, old_qr = pairing.pin, pairing.qr_token

    assert pairing.check_pin(old_pin) is True
    assert pairing.pin != old_pin
    assert pairing.qr_token != old_qr
    assert pairing.check_pin(old_pin) is False
    assert reasons[:2] == ["start", "used"]
    # the freshly rotated PIN still works
    assert pairing.check_pin(pairing.pin) is True


def test_failures_rotate_at_max_failures(fixed_secrets):
    reasons: list[str] = []
    pairing = Pairing(max_failures=3, clock=FakeClock(), on_rotate=reasons.append)
    first = pairing.pin
    wrong = "000000" if first != "000000" else "111111"

    assert pairing.check_pin(wrong) is False
    assert pairing.check_pin(wrong) is False
    assert pairing.pin == first
    assert pairing.failures == 2

    assert pairing.check_pin(wrong) is False
    assert pairing.pin != first
    assert pairing.failures == 0
    assert reasons == ["start", "too_many_failures"]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        123456,
        123456.0,
        b"123456",
        "１２３４５６",          # full-width digits: isdigit() but not ASCII
        "١٢٣٤٥٦",              # Arabic-Indic digits
        "123 456",
        " 123456",
        "123456 ",
        "1234567",
        "12345",
        "",
        "12345a",
    ],
)
def test_malformed_pins_return_false_without_raising(bad, fixed_secrets):
    pairing = Pairing(max_failures=100, clock=FakeClock())
    pin_before = pairing.pin
    assert pairing.check_pin(bad) is False
    assert pairing.failures == 1
    assert pairing.pin == pin_before


def test_ttl_expiry_rotates(fixed_secrets):
    clock = FakeClock(500.0)
    reasons: list[str] = []
    pairing = Pairing(ttl_s=10.0, clock=clock, on_rotate=reasons.append)
    old_pin, old_qr = pairing.pin, pairing.qr_token

    clock.advance(9.0)
    assert pairing.current() == (old_pin, old_qr)
    assert pairing.expires_in() == 1

    clock.advance(2.0)  # past expires_at
    new_pin, new_qr = pairing.current()
    assert (new_pin, new_qr) != (old_pin, old_qr)
    assert reasons == ["start", "expired"]
    assert pairing.expires_in() == 10


def test_qr_token_is_single_use_and_long_enough(fixed_secrets):
    pairing = Pairing(clock=FakeClock())
    token = pairing.qr_token

    assert len(token) >= 22  # 16 random bytes -> 22 URL-safe characters
    padded = token + "=" * (-len(token) % 4)
    assert len(base64.urlsafe_b64decode(padded)) >= 16  # >= 128 bits

    assert pairing.check_qr("wrong-token") is False
    assert pairing.check_qr(token) is True
    assert pairing.check_qr(token) is False  # single use
    assert pairing.qr_token != token


def test_on_rotate_receives_each_reason(fixed_secrets):
    clock = FakeClock()
    reasons: list[str] = []
    pairing = Pairing(ttl_s=5.0, max_failures=2, clock=clock, on_rotate=reasons.append)

    assert reasons == ["start"]
    assert pairing.check_pin(pairing.pin) is True
    assert reasons[-1] == "used"
    assert pairing.check_qr(pairing.qr_token) is True
    assert reasons[-1] == "used"

    wrong = "000000" if pairing.pin != "000000" else "111111"
    pairing.check_pin(wrong)
    pairing.check_pin(wrong)
    assert reasons[-1] == "too_many_failures"

    clock.advance(5.0)
    pairing.current()
    assert reasons[-1] == "expired"
    assert reasons == ["start", "used", "used", "too_many_failures", "expired"]
