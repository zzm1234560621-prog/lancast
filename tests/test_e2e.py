"""End-to-end: real server (synthetic capture) + protocol-speaking clients.

Covers the TLS chain (verified against LanCast's own CA), PIN pairing with
owner approval, H.264 decoding of the stream, remembered-device login, JPEG
mode, kicking, and the main refusal paths.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import ssl
import struct
import tempfile
import time
from pathlib import Path

import aiohttp
import av
import cv2
import numpy as np
import pytest

from lancast.config import Settings
from lancast.encode.h264util import avcc_to_annexb, parse_avcc_record
from lancast.server import serve

HEADER = struct.Struct("!BBHIII")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _settings(tmp: Path, **kw) -> Settings:
    return Settings(port=_free_port(), admin_port=_free_port(), bind="127.0.0.1", capture="synthetic",
                    data_dir=tmp, open_admin=False, fps=30, **kw)


class Harness:
    def __init__(self, tmp: Path, **kw) -> None:
        self.settings = _settings(tmp, **kw)
        self.task: asyncio.Task | None = None
        self.state = None

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        self.task = asyncio.create_task(serve(self.settings, ready))
        self.state = await asyncio.wait_for(ready, 60)
        ctx = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(self.state.ca_der))
        self.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        self.port, self.admin_port = self.settings.port, self.settings.admin_port
        self.lan = f"https://127.0.0.1:{self.port}"
        self.admin = f"http://127.0.0.1:{self.admin_port}"
        return self

    async def __aexit__(self, *exc):
        await self.http.close()
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    async def ws(self):
        return await self.http.ws_connect(f"wss://127.0.0.1:{self.port}/ws", origin=self.lan)

    async def admin_post(self, path: str, body: dict, token: str | None = None):
        headers = {"X-Admin-Token": token or self.state.admin_token}
        async with self.http.post(self.admin + path, json=body, headers=headers) as resp:
            return resp.status, await resp.json()


async def _recv_json(ws, timeout=10.0) -> dict:
    while True:
        msg = await ws.receive(timeout=timeout)
        if msg.type == aiohttp.WSMsgType.TEXT:
            data = json.loads(msg.data)
            if data.get("type") != "cursor":
                return data
        elif msg.type == aiohttp.WSMsgType.BINARY:
            continue
        else:
            raise AssertionError(f"socket closed: {msg.type} {msg.data} {msg.extra}")


async def _collect(ws, n_frames: int, timeout=20.0):
    """Return (stream message, [(header tuple, payload)])."""
    stream, frames = None, []
    deadline = time.monotonic() + timeout
    while len(frames) < n_frames:
        msg = await ws.receive(timeout=max(0.1, deadline - time.monotonic()))
        if msg.type == aiohttp.WSMsgType.TEXT:
            data = json.loads(msg.data)
            if data["type"] == "stream":
                stream = data
        elif msg.type == aiohttp.WSMsgType.BINARY:
            assert stream is not None, "binary frame before stream message"
            head = HEADER.unpack_from(msg.data)
            frames.append((head, msg.data[HEADER.size:]))
        else:
            raise AssertionError(f"socket closed: {msg.type}")
    return stream, frames


async def _pair_with_pin(h: Harness, name="Test iPad", remember=True):
    ws = await h.ws()
    await ws.send_json({"type": "auth", "method": "pin", "pin": h.state.pairing.pin, "name": name,
                        "remember": remember, "client": {"ua": "pytest"}})
    first = await _recv_json(ws)
    assert first["type"] == "auth_pending", first
    pending = h.state.approvals.pending()
    assert len(pending) == 1 and pending[0].name == name
    status, body = await h.admin_post("/admin/api/approve", {"request_id": pending[0].request_id, "allow": True})
    assert status == 200 and body["ok"]
    ok = await _recv_json(ws)
    assert ok["type"] == "auth_ok", ok
    return ws, ok


def test_full_session_flow():
    async def scenario(tmp: Path):
        async with Harness(tmp) as h:
            # 1. PIN pairing + approval; PIN is single use.
            used_pin = h.state.pairing.pin
            ws, ok = await _pair_with_pin(h)
            assert ok["device_token"] and ok["device_id"].startswith("d_")
            assert h.state.pairing.pin != used_pin
            assert ok["displays"][0]["width"] == 1280

            # 2. H.264 stream decodes.
            await ws.send_json({"type": "watch", "display": 0, "codec": "h264"})
            stream, frames = await _collect(ws, 45)
            assert stream["codec"] == "h264" and stream["codec_string"].startswith("avc1.")
            kinds = {f[0][0] for f in frames}
            assert kinds == {1}
            assert frames[0][0][1] & 1, "first frame must be a keyframe"
            assert all(f[0][2] == stream["epoch"] for f in frames)
            sps, pps = parse_avcc_record(base64.b64decode(stream["description"]))
            prefix = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
            dec = av.CodecContext.create("h264", "r")
            decoded = 0
            for (kind, flags, *_rest), payload in frames:
                data = (prefix if flags & 1 else b"") + avcc_to_annexb(payload)
                for frame in dec.decode(av.Packet(data)):
                    assert (frame.width, frame.height) == (stream["width"], stream["height"])
                    decoded += 1
            assert decoded >= 44

            # 3. ping/pong and stats reach the admin view.
            await ws.send_json({"type": "ping", "t": 123.5})
            await ws.send_json({"type": "stats", "fps": 30, "rtt_ms": 4.2, "latency_ms": 40})
            while True:
                data = await _recv_json(ws)
                if data["type"] == "pong":
                    break
            assert data["t"] == 123.5 and data["server_us"] > 0

            # 4. Remembered device logs in without PIN/approval, JPEG mode works.
            ws2 = await h.ws()
            await ws2.send_json({"type": "auth", "method": "device", "device_id": ok["device_id"],
                                 "token": ok["device_token"], "client": {}})
            ok2 = await _recv_json(ws2)
            assert ok2["type"] == "auth_ok" and "device_token" not in ok2
            await ws2.send_json({"type": "watch", "display": 0, "codec": "jpeg"})
            jstream, jframes = await _collect(ws2, 3)
            assert jstream["codec"] == "jpeg"
            img = cv2.imdecode(np.frombuffer(jframes[-1][1], np.uint8), cv2.IMREAD_COLOR)
            assert img.shape[:2] == (jstream["height"], jstream["width"])

            # 5. Admin state lists both viewers; the PIN is never in the event log.
            async with h.http.get(h.admin + "/admin/api/state",
                                  headers={"X-Admin-Token": h.state.admin_token}) as resp:
                snap = await resp.json()
            assert len(snap["viewers"]) == 2 and snap["pairing"]["qr_svg"].startswith("data:image/svg+xml")
            assert snap["viewers"][0]["rtt_ms"] == 4.2 or snap["viewers"][1]["rtt_ms"] == 4.2
            assert used_pin not in json.dumps(snap["log"])

            # 6. Kick the second viewer: it gets bye + close 4004.
            status, _ = await h.admin_post("/admin/api/kick", {"viewer_id": ok2["viewer_id"]})
            assert status == 200
            bye = None
            while True:
                msg = await ws2.receive(timeout=5)
                if msg.type == aiohttp.WSMsgType.TEXT and json.loads(msg.data)["type"] == "bye":
                    bye = json.loads(msg.data)
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
            assert bye == {"type": "bye", "reason": "kicked"} and ws2.close_code == 4004

            # 7. Revoke the device: its token stops working.
            status, _ = await h.admin_post("/admin/api/revoke", {"device_id": ok["device_id"]})
            assert status == 200
            ws3 = await h.ws()
            await ws3.send_json({"type": "auth", "method": "device", "device_id": ok["device_id"],
                                 "token": ok["device_token"]})
            fail = await _recv_json(ws3)
            assert fail == {"type": "auth_fail", "reason": "unknown_device", "retry_after_s": 0}
            await ws.close()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_refusals_and_rate_limit():
    async def scenario(tmp: Path):
        async with Harness(tmp) as h:
            # Cross-origin WebSocket is refused before upgrade.
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await h.http.ws_connect(f"wss://127.0.0.1:{h.port}/ws", origin="https://evil.example")
            assert err.value.status == 403

            # Admin API: no token -> 401; wrong Host (DNS rebinding) -> 403; LAN port has no admin.
            async with h.http.get(h.admin + "/admin/api/state") as resp:
                assert resp.status == 401
            async with h.http.get(h.admin + "/admin/api/state",
                                  headers={"X-Admin-Token": h.state.admin_token, "Host": f"evil.example:{h.admin_port}"}) as resp:
                assert resp.status == 403
            async with h.http.get(h.lan + "/admin/api/state",
                                  headers={"X-Admin-Token": h.state.admin_token}) as resp:
                assert resp.status == 404
            # Form-encoded POST (CSRF shape) is refused.
            async with h.http.post(h.admin + "/admin/api/pause", data="paused=true",
                                   headers={"X-Admin-Token": h.state.admin_token,
                                            "Content-Type": "application/x-www-form-urlencoded"}) as resp:
                assert resp.status == 415

            # Security headers are present.
            async with h.http.get(h.lan + "/") as resp:
                assert resp.status == 200
                assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
                assert resp.headers["X-Content-Type-Options"] == "nosniff"
            async with h.http.get(h.lan + "/static/viewer/..%5Cserver.py") as resp:
                assert resp.status == 404

            # First message must be auth; garbage closes with 4001.
            ws = await h.ws()
            await ws.send_str("not json")
            msg = await _recv_json(ws)
            assert msg["reason"] == "bad_request"

            # Wrong PINs: 5 failures from one IP -> rate limited; PIN rotates after 5 failures.
            pin_before = h.state.pairing.pin
            wrong = "000000" if pin_before != "000000" else "111111"
            for _ in range(5):
                ws = await h.ws()
                await ws.send_json({"type": "auth", "method": "pin", "pin": wrong, "name": "x"})
                assert (await _recv_json(ws))["reason"] == "bad_credentials"
                await ws.close()
            assert h.state.pairing.pin != pin_before
            ws = await h.ws()
            await ws.send_json({"type": "auth", "method": "pin", "pin": h.state.pairing.pin, "name": "x"})
            fail = await _recv_json(ws)
            assert fail["reason"] == "rate_limited" and fail["retry_after_s"] > 0
            await ws.close()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_qr_pairing_without_approval_and_denial():
    async def scenario(tmp: Path):
        async with Harness(tmp, approval=False) as h:
            token = h.state.pairing.qr_token
            ws = await h.ws()
            await ws.send_json({"type": "auth", "method": "qr", "token": token, "name": "Phone", "remember": False})
            ok = await _recv_json(ws)
            assert ok["type"] == "auth_ok" and ok["device_id"] is None and "device_token" not in ok
            # The QR token is single use.
            ws2 = await h.ws()
            await ws2.send_json({"type": "auth", "method": "qr", "token": token, "name": "Phone 2"})
            assert (await _recv_json(ws2))["reason"] == "expired"
            await ws.close()

        async with Harness(tmp) as h:
            ws = await h.ws()
            await ws.send_json({"type": "auth", "method": "pin", "pin": h.state.pairing.pin, "name": "Intruder"})
            assert (await _recv_json(ws))["type"] == "auth_pending"
            req = h.state.approvals.pending()[0]
            await h.admin_post("/admin/api/approve", {"request_id": req.request_id, "allow": False})
            assert (await _recv_json(ws))["reason"] == "denied"

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


# --------------------------------------------------------------------------- remote control & quality

async def _sync(ws, marker: float = 7.5) -> list:
    """Ping and wait for the pong; returns the other text messages seen meanwhile.

    The server handles messages in order, so once the pong arrives every input
    sent before the ping has been applied."""
    await ws.send_json({"type": "ping", "t": marker})
    seen = []
    while True:
        data = await _recv_json(ws)
        if data["type"] == "pong" and data["t"] == marker:
            return seen
        seen.append(data)


async def _pair_control(h: Harness, name: str, control: bool):
    ws = await h.ws()
    await ws.send_json({"type": "auth", "method": "pin", "pin": h.state.pairing.pin, "name": name, "remember": True})
    assert (await _recv_json(ws))["type"] == "auth_pending"
    req = h.state.approvals.pending()[0]
    await h.admin_post("/admin/api/approve", {"request_id": req.request_id, "allow": True, "control": control})
    ok = await _recv_json(ws)
    assert ok["type"] == "auth_ok", ok
    return ws, ok


def test_remote_control_permissions_and_events():
    async def scenario(tmp: Path):
        async with Harness(tmp) as h:
            backend = h.state.input_backend
            assert backend.name == "fake"          # synthetic capture never injects real input

            # View-only device: input is refused, nothing reaches the backend.
            ws_view, ok_view = await _pair_control(h, "Viewer", control=False)
            assert ok_view["control"] is False and ok_view["viewer_quality"] is True
            await ws_view.send_json({"type": "input", "events": [{"t": "kd", "k": "KeyA"}]})
            seen = await _sync(ws_view)
            assert any(m.get("code") == "control_denied" for m in seen)
            assert backend.calls == []

            # Controlling device.
            ws, ok = await _pair_control(h, "Phone", control=True)
            assert ok["control"] is True
            await ws.send_json({"type": "watch", "display": 0, "codec": "h264"})
            await ws.send_json({"type": "input", "events": [
                {"t": "mm", "dx": 10.6, "dy": -3.2},
                {"t": "mm", "dx": 0.6, "dy": 0.4},          # sub-pixel remainders carry over
                {"t": "ma", "x": 0.5, "y": 0.5},
                {"t": "mb", "b": "left", "d": True},
                {"t": "mb", "b": "left", "d": False},
                {"t": "mw", "dy": 120},
                {"t": "kd", "k": "ControlLeft"}, {"t": "kd", "k": "KeyC"},
                {"t": "kd", "k": "KeyC"},                    # auto-repeat is ignored
                {"t": "ku", "k": "KeyC"}, {"t": "ku", "k": "ControlLeft"},
                {"t": "tx", "s": "你好\r\nok\x07"},           # CRLF -> LF, control chars dropped
                {"t": "kd", "k": "NotAKey"}, {"t": "mb", "b": "bogus", "d": True},
                {"t": "mm", "dx": "1", "dy": 1}, "junk",
            ]})
            await _sync(ws)
            assert backend.calls == [
                ("move_relative", 10, -3),
                ("move_relative", 1, 0),
                ("move_absolute", 640, 360),
                ("button", "left", True), ("button", "left", False),
                ("wheel", 120, 0),
                ("key", "ControlLeft", True), ("key", "KeyC", True),
                ("key", "KeyC", False), ("key", "ControlLeft", False),
                ("text", "你好\nok"),
            ]

            # Revoking control releases held keys and tells the viewer.
            backend.calls.clear()
            await ws.send_json({"type": "input", "events": [{"t": "kd", "k": "KeyW"}, {"t": "mb", "b": "right", "d": True}]})
            await _sync(ws)
            status, _ = await h.admin_post("/admin/api/viewer/control", {"viewer_id": ok["viewer_id"], "control": False})
            assert status == 200
            seen = await _sync(ws)
            assert {"type": "control", "allowed": False} in seen
            assert backend.calls[-2:] == [("key", "KeyW", False), ("button", "right", False)]
            assert h.state.devices.get(ok["device_id"]).can_control is False   # persisted for the device

            # Grant again per device; the master switch and pause both suspend control.
            await h.admin_post("/admin/api/device/control", {"device_id": ok["device_id"], "control": True})
            assert {"type": "control", "allowed": True} in await _sync(ws)
            await h.admin_post("/admin/api/settings", {"control_enabled": False})
            assert {"type": "control", "allowed": False} in await _sync(ws)
            await h.admin_post("/admin/api/settings", {"control_enabled": True})
            assert {"type": "control", "allowed": True} in await _sync(ws)
            await h.admin_post("/admin/api/pause", {"paused": True})
            assert {"type": "control", "allowed": False} in await _sync(ws)
            await h.admin_post("/admin/api/pause", {"paused": False})
            assert {"type": "control", "allowed": True} in await _sync(ws)

            # Watchdog: a held key is released after ~3 s of silence.
            backend.calls.clear()
            await ws.send_json({"type": "input", "events": [{"t": "kd", "k": "ShiftLeft"}]})
            await _sync(ws)
            await asyncio.sleep(4.5)
            assert ("key", "ShiftLeft", False) in backend.calls

            # The admin page sees the controller.
            async with h.http.get(h.admin + "/admin/api/state", headers={"X-Admin-Token": h.state.admin_token}) as resp:
                snap = await resp.json()
            mine = next(v for v in snap["viewers"] if v["viewer_id"] == ok["viewer_id"])
            assert mine["control"] is True and mine["quality"]["preset"] == "standard"
            assert next(d for d in snap["devices"] if d["device_id"] == ok["device_id"])["can_control"] is True

            # Disconnecting releases everything too.
            backend.calls.clear()
            await ws.send_json({"type": "input", "events": [{"t": "kd", "k": "Space"}]})
            await _sync(ws)
            await ws.close()
            await asyncio.sleep(0.3)
            assert ("key", "Space", False) in backend.calls
            await ws_view.close()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


async def _qr_viewer(h: Harness, name: str):
    ws = await h.ws()
    await ws.send_json({"type": "auth", "method": "qr", "token": h.state.pairing.qr_token, "name": name})
    ok = await _recv_json(ws)
    assert ok["type"] == "auth_ok", ok
    return ws


async def _stream_quality(ws, watch: dict) -> dict:
    await ws.send_json({"type": "watch", "display": 0, **watch})
    stream, _ = await _collect(ws, 2)
    return stream


def test_quality_presets_custom_clamping_and_cap():
    async def scenario(tmp: Path):
        async with Harness(tmp, approval=False) as h:
            saver = await _qr_viewer(h, "saver")
            s = await _stream_quality(saver, {"codec": "h264", "quality": {"preset": "saver"}})
            assert s["quality"] == {"preset": "saver", "max_width": 960, "max_height": 540, "fps": 30, "bitrate_kbps": 2000}
            assert (s["width"], s["height"]) == (960, 540)

            std = await _qr_viewer(h, "standard")
            s = await _stream_quality(std, {"codec": "h264"})
            assert s["quality"]["preset"] == "standard" and (s["width"], s["height"]) == (1280, 720)

            custom = await _qr_viewer(h, "custom")
            s = await _stream_quality(custom, {"codec": "h264", "quality": {
                "preset": "custom", "max_width": 100, "max_height": 100, "fps": 999, "bitrate_kbps": 1}})
            # fps is capped at max(PC setting = 30 in this harness, 60): viewers cannot overload the PC
            assert s["quality"] == {"preset": "custom", "max_width": 320, "max_height": 240, "fps": 60, "bitrate_kbps": 500}
            assert (s["width"], s["height"]) == (320, 180)

            jpeg = await _qr_viewer(h, "jpeg")
            s = await _stream_quality(jpeg, {"codec": "jpeg", "quality": {"preset": "high"}})
            assert s["quality"] == {"preset": "high", "max_width": 2560, "max_height": 1440, "fps": 20, "jpeg_quality": 80}

            # Display 0 now runs 4 encoders: a 5th distinct request reuses the closest one.
            extra = await _qr_viewer(h, "extra")
            s = await _stream_quality(extra, {"codec": "h264", "quality": {
                "preset": "custom", "max_width": 1000, "max_height": 560, "fps": 30, "bitrate_kbps": 2200}})
            assert s["quality"]["preset"] == "saver"
            assert len(h.state.hub.displays[0].outputs) == 4

            # The owner switches viewer-chosen quality off: everyone falls back to standard.
            await h.admin_post("/admin/api/settings", {"viewer_quality": False})
            # Frames of the old stream may still be in flight; wait for the new "stream" message.
            while True:
                msg = await saver.receive(timeout=10)
                if msg.type == aiohttp.WSMsgType.TEXT and json.loads(msg.data)["type"] == "stream":
                    stream = json.loads(msg.data)
                    break
            assert stream["quality"]["preset"] == "standard"
            for ws in (saver, std, custom, jpeg, extra):
                await ws.close()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
