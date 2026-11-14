"""Black-box verification of a running LanCast.exe.

Checks, in order:

1. Wait (up to ``--ready-timeout`` seconds) for the admin listener's /healthz.
2. HTTP 200 for /healthz, /, /static/viewer/app.js and /admin on the local
   (plain HTTP) listener, using only ``urllib.request``.
3. Pair over ``wss://127.0.0.1:<ws-port>/ws`` with the printed PIN (TLS verified
   against ``<data-dir>/ca.crt.pem``), subscribe to display 0 with H.264 and
   collect at least ``--frames`` binary media frames plus the ``stream`` message.

This script intentionally does not import ``lancast``; it validates the shipped
executable exactly like a third-party client would.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

HEADER = struct.Struct("!BBHIII")  # docs/PROTOCOL.md §3.3


# --------------------------------------------------------------------------- HTTP

def _get(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "LanCast-packaging-verify"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read()


def wait_until_ready(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            status, body = _get(base_url + "/healthz", timeout=2.0)
            if status == 200 and json.loads(body) == {"ok": True}:
                print(f"[ready] admin /healthz answered after {attempts} attempt(s)")
                return
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"admin listener not ready after {timeout:.0f}s: {last_error!r}")


def check_http(base_url: str) -> list[tuple[str, int, int]]:
    results: list[tuple[str, int, int]] = []
    for path in ("/healthz", "/", "/static/viewer/app.js", "/admin"):
        status, body = _get(base_url + path)
        if status != 200:
            raise RuntimeError(f"GET {path} returned {status}, expected 200")
        if not body:
            raise RuntimeError(f"GET {path} returned an empty body")
        if path == "/healthz":
            if json.loads(body) != {"ok": True}:
                raise RuntimeError(f"GET /healthz returned unexpected body {body!r}")
        results.append((path, status, len(body)))
        print(f"[http] GET {path} -> 200 ({len(body)} bytes)")
    return results


# --------------------------------------------------------------------------- WebSocket

async def _next_text(ws: aiohttp.ClientWebSocketResponse, timeout: float) -> dict:
    """Return the next non-cursor text message (skipping cursor/pong noise)."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for a text message")
        message = await ws.receive(timeout=remaining)
        if message.type == aiohttp.WSMsgType.TEXT:
            data = json.loads(message.data)
            if data.get("type") == "cursor":
                continue
            return data
        if message.type == aiohttp.WSMsgType.BINARY:
            continue
        raise RuntimeError(
            f"WebSocket closed/received unexpected frame while waiting for text: "
            f"type={message.type!r} data={message.data!r} extra={message.extra!r}"
        )


async def _verify_ws(port: int, ca_file: Path, pin: str, frames_needed: int) -> dict:
    origin = f"https://127.0.0.1:{port}"
    url = f"wss://127.0.0.1:{port}/ws"
    context = ssl.create_default_context(cafile=str(ca_file))
    timeout = aiohttp.ClientTimeout(total=60.0, sock_connect=10.0)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(
            url,
            origin=origin,
            ssl=context,
            heartbeat=None,
            compress=0,
            max_msg_size=0,
        ) as ws:
            print(f"[ws] connected {url} (TLS verified with {ca_file})")

            await ws.send_json(
                {
                    "type": "auth",
                    "method": "pin",
                    "pin": pin,
                    "name": "packaging-verify",
                    "remember": False,
                    "client": {"ua": "lancast-packaging-verify"},
                }
            )
            auth = await _next_text(ws, 15.0)
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"pairing failed: {auth!r}")
            print(
                f"[ws] auth_ok viewer={auth.get('viewer_id')} "
                f"server={auth.get('server', {}).get('name')} "
                f"displays={len(auth.get('displays') or [])}"
            )

            await ws.send_json({"type": "watch", "display": 0, "codec": "h264"})

            stream: dict | None = None
            frame_count = 0
            keyframe_count = 0
            payload_bytes = 0
            first_header: tuple[int, int, int, int, int, int] | None = None
            deadline = time.monotonic() + 45.0

            while frame_count < frames_needed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"only {frame_count}/{frames_needed} binary frames within 45s"
                    )
                message = await ws.receive(timeout=remaining)

                if message.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    kind = data.get("type")
                    if kind == "stream":
                        stream = data
                        print(
                            f"[ws] stream display={data.get('display')} codec={data.get('codec')} "
                            f"epoch={data.get('epoch')} {data.get('width')}x{data.get('height')} "
                            f"@ {data.get('fps')} fps codec_string={data.get('codec_string')}"
                        )
                    elif kind in ("error", "bye"):
                        raise RuntimeError(f"server sent {kind}: {data!r}")
                    continue

                if message.type == aiohttp.WSMsgType.BINARY:
                    if stream is None:
                        raise RuntimeError("binary frame arrived before the stream message")
                    payload = message.data
                    if len(payload) <= HEADER.size:
                        raise RuntimeError(f"binary frame too short: {len(payload)} bytes")
                    header = HEADER.unpack_from(payload)
                    kind, flags, epoch, seq, ts_hi, ts_lo = header
                    if kind != 1:
                        raise RuntimeError(f"expected H.264 kind 1, got {kind}")
                    if epoch != (int(stream["epoch"]) & 0xFFFF):
                        raise RuntimeError(
                            f"frame epoch {epoch} != stream epoch {stream['epoch']}"
                        )
                    if frame_count == 0:
                        if not (flags & 1):
                            raise RuntimeError("first H.264 frame is not a keyframe")
                        first_header = header
                    if flags & 1:
                        keyframe_count += 1
                    payload_bytes += len(payload) - HEADER.size
                    frame_count += 1
                    continue

                raise RuntimeError(
                    f"WebSocket closed/received unexpected frame: "
                    f"type={message.type!r} data={message.data!r} extra={message.extra!r}"
                )

            await ws.send_json({"type": "bye"})
            await ws.close()
            print(
                f"[ws] received stream message and {frame_count} binary frames "
                f"({keyframe_count} keyframe(s), {payload_bytes} payload bytes)"
            )
            if first_header is None or stream is None:
                raise RuntimeError("no stream/frame captured")
            return {
                "stream": {
                    key: stream.get(key)
                    for key in ("display", "codec", "epoch", "width", "height", "fps", "codec_string")
                },
                "frames": frame_count,
                "keyframes": keyframe_count,
                "payload_bytes": payload_bytes,
                "first_header": {
                    "kind": first_header[0],
                    "flags": first_header[1],
                    "epoch": first_header[2],
                    "seq": first_header[3],
                },
            }


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-port", type=int, default=18742)
    parser.add_argument("--ws-port", type=int, default=18743)
    parser.add_argument("--data-dir", type=Path, required=True, help="server --data-dir (CA lives here)")
    parser.add_argument("--pin", required=True, help="6-digit pairing PIN, spaces allowed")
    parser.add_argument("--frames", type=int, default=30, help="minimum binary frames to collect")
    parser.add_argument("--ready-timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    pin = "".join(args.pin.split())
    if not (len(pin) == 6 and pin.isdigit()):
        print(f"invalid PIN {args.pin!r}", file=sys.stderr)
        return 2

    ca_file = args.data_dir / "ca.crt.pem"
    if not ca_file.is_file():
        print(f"CA certificate not found: {ca_file}", file=sys.stderr)
        return 2

    admin_url = f"http://127.0.0.1:{args.admin_port}"
    try:
        wait_until_ready(admin_url, args.ready_timeout)
        check_http(admin_url)
        summary = asyncio.run(_verify_ws(args.ws_port, ca_file, pin, args.frames))
    except Exception as exc:  # noqa: BLE001 - report any failure as a verification failure
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("[PASS] HTTP and WebSocket checks succeeded")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
