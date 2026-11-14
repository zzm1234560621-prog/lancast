"""Unit tests for :class:`lancast.auth.Approvals` (asyncio.run, no pytest-asyncio)."""

from __future__ import annotations

import asyncio

import pytest

from lancast.auth import Approvals


async def _pending(a: Approvals, n: int = 1) -> list:
    for _ in range(200):
        requests = a.pending()
        if len(requests) >= n:
            return requests
        await asyncio.sleep(0)
    raise AssertionError("approval request never became pending")


def test_resolve_allow_and_deny():
    async def scenario():
        approvals = Approvals(timeout_s=5.0)

        task = asyncio.create_task(approvals.wait("iPad", "10.0.0.5", "ua", "pin"))
        [req] = await _pending(approvals)
        assert req.name == "iPad"
        assert req.ip == "10.0.0.5"
        assert req.method == "pin"
        assert approvals.resolve(req.request_id, True) is True
        assert await task == "allow"
        assert approvals.pending() == []

        task = asyncio.create_task(approvals.wait("Phone", "10.0.0.6", "ua", "qr"))
        [req] = await _pending(approvals)
        assert approvals.resolve(req.request_id, False) is True
        assert await task == "deny"
        assert approvals.pending() == []

    asyncio.run(scenario())


def test_timeout_returns_timeout_and_removes_the_request():
    async def scenario():
        approvals = Approvals(timeout_s=0.01)
        result = await asyncio.wait_for(approvals.wait("iPad", "10.0.0.5", "ua", "pin"), 2.0)
        assert result == "timeout"
        assert approvals.pending() == []

    asyncio.run(scenario())


def test_cancelled_caller_withdraws_the_request():
    async def scenario():
        approvals = Approvals(timeout_s=5.0)
        task = asyncio.create_task(approvals.wait("iPad", "10.0.0.5", "ua", "pin"))
        await _pending(approvals)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert approvals.pending() == []

    asyncio.run(scenario())


def test_busy_for_the_same_ip():
    async def scenario():
        approvals = Approvals(timeout_s=5.0)
        task = asyncio.create_task(approvals.wait("iPad", "192.168.1.9", "ua", "pin"))
        [req] = await _pending(approvals)

        assert approvals.busy_for("192.168.1.9") is True
        assert approvals.busy_for("192.168.1.10") is False

        approvals.resolve(req.request_id, False)
        assert await task == "deny"
        assert approvals.busy_for("192.168.1.9") is False

    asyncio.run(scenario())


def test_busy_for_when_max_pending_is_reached():
    async def scenario():
        approvals = Approvals(timeout_s=5.0, max_pending=1)
        task = asyncio.create_task(approvals.wait("iPad", "192.168.1.9", "ua", "pin"))
        [req] = await _pending(approvals)

        assert approvals.busy_for("192.168.1.10") is True  # queue is full
        approvals.resolve(req.request_id, True)
        assert await task == "allow"
        assert approvals.busy_for("192.168.1.10") is False

    asyncio.run(scenario())


def test_resolve_after_completion_returns_false():
    async def scenario():
        approvals = Approvals(timeout_s=5.0)
        task = asyncio.create_task(approvals.wait("iPad", "10.0.0.5", "ua", "pin"))
        [req] = await _pending(approvals)

        assert approvals.resolve(req.request_id, True) is True
        assert approvals.resolve(req.request_id, False) is False
        assert approvals.resolve("r_doesnotexist", True) is False
        assert await task == "allow"

    asyncio.run(scenario())
