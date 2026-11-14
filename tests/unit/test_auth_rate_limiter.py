"""Unit tests for :class:`lancast.auth.RateLimiter`."""

from __future__ import annotations

from lancast.auth import RateLimiter


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def test_blocks_after_max_failures_and_expires():
    clock = FakeClock()
    limiter = RateLimiter(max_failures=3, window_s=60.0, block_s=30.0, clock=clock)

    assert limiter.retry_after("10.0.0.1") == 0
    for _ in range(2):
        limiter.failure("10.0.0.1")
        assert limiter.retry_after("10.0.0.1") == 0

    limiter.failure("10.0.0.1")
    assert limiter.retry_after("10.0.0.1") == 30

    clock.advance(29.0)
    assert limiter.retry_after("10.0.0.1") == 1

    clock.advance(1.0)  # block has just expired
    assert limiter.retry_after("10.0.0.1") == 0


def test_failures_outside_the_window_do_not_count():
    clock = FakeClock()
    limiter = RateLimiter(max_failures=3, window_s=10.0, block_s=10.0, clock=clock)

    limiter.failure("k")           # t=1000
    clock.advance(6.0)
    limiter.failure("k")           # t=1006
    clock.advance(6.0)
    limiter.failure("k")           # t=1012: the t=1000 entry is outside the window

    assert limiter.retry_after("k") == 0

    limiter.failure("k")           # t=1012 again: 1006 + 1012 + 1012 -> block
    assert limiter.retry_after("k") > 0


def test_success_clears_the_failure_count():
    clock = FakeClock()
    limiter = RateLimiter(max_failures=2, window_s=300.0, block_s=300.0, clock=clock)

    limiter.failure("k")
    limiter.success("k")
    assert "k" not in limiter._fails

    limiter.failure("k")
    assert limiter.retry_after("k") == 0  # only one failure since the success

    limiter.failure("k")
    assert limiter.retry_after("k") > 0


def test_internal_maps_do_not_grow_past_max_entries():
    clock = FakeClock()
    limiter = RateLimiter(max_failures=5, clock=clock, max_entries=128)

    for i in range(5000):
        limiter.failure(f"192.0.{i // 256}.{i % 256}")

    total = len(limiter._fails) + len(limiter._blocked)
    assert 0 < total <= 128
