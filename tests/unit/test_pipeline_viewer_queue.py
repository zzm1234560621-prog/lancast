"""Unit tests for ViewerQueue priority/backpressure and the media HEADER."""

from __future__ import annotations

import asyncio

import pytest

from lancast.pipeline import HEADER, KIND, ViewerQueue


async def _drain(queue: ViewerQueue, n: int) -> list:
    return [await asyncio.wait_for(queue.next(), 1.0) for _ in range(n)]


async def _assert_idle(queue: ViewerQueue, timeout: float = 0.02) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.next(), timeout)


def test_header_matches_protocol_3_3():
    assert HEADER.size == 16
    assert KIND == {"h264": 1, "jpeg": 2}

    packed = HEADER.pack(KIND["h264"], 1, 3, 0x01020304, 0x11223344, 0x55667788)
    assert packed == bytes.fromhex("01010003010203041122334455667788")
    assert HEADER.unpack(packed) == (1, 1, 3, 0x01020304, 0x11223344, 0x55667788)


def test_priority_control_then_cursor_then_media():
    async def scenario():
        queue = ViewerQueue(lambda: None)
        queue.reset("h264")
        queue.put_frame(b"frame", True, 1, "stream")
        queue.put_cursor("cursor")
        queue.put_control("control")

        assert await _drain(queue, 4) == ["control", "cursor", "stream", b"frame"]

    asyncio.run(scenario())


def test_cursor_only_keeps_the_latest_position():
    async def scenario():
        queue = ViewerQueue(lambda: None)
        queue.put_cursor("c1")
        queue.put_cursor("c2")
        queue.put_cursor("c3")

        assert await queue.next() == "c3"
        await _assert_idle(queue)

    asyncio.run(scenario())


def test_epoch_change_emits_stream_message_before_frames():
    async def scenario():
        queue = ViewerQueue(lambda: None)
        queue.reset("h264")
        queue.put_frame(b"k1", True, 1, "s1")
        assert await _drain(queue, 2) == ["s1", b"k1"]

        # A delta on a new epoch is dropped until a keyframe, but the stream
        # configuration message must still be delivered first.
        queue.put_frame(b"ignored-delta", False, 2, "s2")
        assert await queue.next() == "s2"
        await _assert_idle(queue)

        queue.put_frame(b"k2", True, 2, "s2")
        assert await queue.next() == b"k2"

    asyncio.run(scenario())


def test_reset_drops_deltas_during_waiting_key():
    async def scenario():
        queue = ViewerQueue(lambda: None)
        queue.reset("h264")
        queue.put_frame(b"k1", True, 1, "s1")
        await _drain(queue, 2)

        queue.reset("h264")
        assert queue._waiting_key is True

        queue.put_frame(b"delta", False, 1, "s1")   # dropped, stream message only
        assert await queue.next() == "s1"
        await _assert_idle(queue)

        queue.put_frame(b"delta", False, 1, "s1")   # same epoch: silently ignored
        await _assert_idle(queue)

        queue.put_frame(b"k2", True, 1, "s1")
        assert await queue.next() == b"k2"

    asyncio.run(scenario())


def test_keyframe_discards_queued_old_frames():
    async def scenario():
        queue = ViewerQueue(lambda: None)
        queue.reset("h264")
        queue.put_frame(b"k1", True, 1, "s1")
        await _drain(queue, 2)

        queue.put_frame(b"d1", False, 1, "s1")
        queue.put_frame(b"d2", False, 1, "s1")
        assert queue.dropped == 0

        queue.put_frame(b"k2", True, 1, "s1")
        assert queue.dropped == 2
        assert await queue.next() == b"k2"
        await _assert_idle(queue)

    asyncio.run(scenario())


def test_backlog_at_max_frames_drops_and_requests_a_keyframe():
    async def scenario():
        requests: list[int] = []
        queue = ViewerQueue(lambda: requests.append(1))
        queue.reset("h264")
        queue.put_frame(b"k1", True, 1, "s1")
        await _drain(queue, 2)

        for i in range(ViewerQueue.MAX_FRAMES):
            queue.put_frame(f"d{i}".encode(), False, 1, "s1")
        assert queue._frames == ViewerQueue.MAX_FRAMES

        queue.put_frame(b"overflow", False, 1, "s1")
        assert requests == [1]
        assert queue.dropped == ViewerQueue.MAX_FRAMES
        await _assert_idle(queue)

        # After the request the queue waits for a keyframe again.
        queue.put_frame(b"k2", True, 1, "s1")
        assert await queue.next() == b"k2"

    asyncio.run(scenario())


def test_jpeg_mode_keeps_only_the_newest_frame():
    async def scenario():
        queue = ViewerQueue(lambda: None)
        queue.reset("jpeg")
        queue.put_frame(b"j1", True, 1, "s1")
        queue.put_frame(b"j2", True, 1, "s1")
        queue.put_frame(b"j3", True, 1, "s1")

        assert queue.dropped == 2
        assert await queue.next() == "s1"
        assert await queue.next() == b"j3"
        await _assert_idle(queue)

    asyncio.run(scenario())


def test_finish_queues_close_after_pending_control_messages():
    async def scenario():
        queue = ViewerQueue(lambda: None)
        queue.put_control("notice-1")
        queue.put_control("notice-2")
        queue.finish(4004, "kicked")

        assert await queue.next() == "notice-1"
        assert await queue.next() == "notice-2"
        assert await queue.next() == ("close", 4004, "kicked")

        queue.close()
        assert await queue.next() is None

    asyncio.run(scenario())


def test_close_makes_next_return_none():
    async def scenario():
        queue = ViewerQueue(lambda: None)
        queue.put_control("still queued")
        queue.close()
        assert await queue.next() is None

    asyncio.run(scenario())


def test_frames_are_ignored_while_unsubscribed():
    async def scenario():
        requests: list[int] = []
        queue = ViewerQueue(lambda: requests.append(1))
        queue.reset(None)

        queue.put_frame(b"frame", True, 1, "s1")

        assert list(queue._media) == []
        assert queue._frames == 0
        assert queue.dropped == 0
        assert requests == []
        await _assert_idle(queue)

    asyncio.run(scenario())
