"""Measure LanCast's capture-to-arrival latency, frame rate and bitrate.

Starts the server in a subprocess, connects one viewer over WSS (verified
against LanCast's CA), and compares each frame's capture timestamp with the
arrival time. Server and client run on the same machine, so both read the same
monotonic clock (QueryPerformanceCounter on Windows) and the numbers are exact.

    python scripts/measure_latency.py                      # real screen (DXGI)
    python scripts/measure_latency.py --synthetic 2880x1800 # moving test pattern, screen-sized
    python scripts/measure_latency.py --encoder libx264 --seconds 15

Decode/render time on the phone is not included (WebCodecs hardware decode
typically adds 2-6 ms plus up to one display refresh).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import ssl
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
HEADER = struct.Struct("!BBHIII")


def start_server(args, data_dir: str) -> subprocess.Popen:
    cli = ["serve", "--no-open", "--no-approval", "--port", str(args.port), "--admin-port", str(args.port - 1),
           "--fps", str(args.fps), "--encoder", args.encoder, "--data-dir", data_dir,
           "--bitrate", args.bitrate, "--max-res", args.max_res]
    prelude = ""
    if args.synthetic:
        w, h = (int(v) for v in args.synthetic.lower().split("x"))
        cli += ["--capture", "synthetic"]
        # Screen-sized synthetic source: patch open_capture inside the server process.
        prelude = (
            "import lancast.capture as c; from lancast.capture import open_synthetic; "
            f"c.open_capture = lambda i, b='auto': open_synthetic({w}, {h}, {args.fps}); "
        )
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); {prelude}from lancast.cli import main; sys.exit(main({cli!r}))"
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")


def wait_for_pin(proc: subprocess.Popen, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("server exited early")
            continue
        m = re.search(r"PIN：(\d{3}) (\d{3})", line)
        if m:
            return m.group(1) + m.group(2)
    raise RuntimeError("server did not print a PIN in time")


async def measure(args, pin: str, ca_pem: str) -> dict:
    ctx = ssl.create_default_context(cadata=ca_pem)
    origin = f"https://127.0.0.1:{args.port}"
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as http:
        ws = await http.ws_connect(f"wss://127.0.0.1:{args.port}/ws", origin=origin)
        await ws.send_json({"type": "auth", "method": "pin", "pin": pin, "name": "latency-probe", "remember": False})
        reply = json.loads((await ws.receive(timeout=15)).data)
        assert reply["type"] == "auth_ok", reply
        await ws.send_json({"type": "watch", "display": args.display, "codec": args.codec})
        lat, sizes, keyframes, stream = [], [], 0, None
        first = None
        end = None
        while True:
            msg = await ws.receive(timeout=30)
            now_us = time.perf_counter_ns() // 1000   # same clock as the server's timestamps
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data["type"] == "stream":
                    stream = data
                elif data["type"] == "error":
                    raise RuntimeError(data)
                continue
            if msg.type != aiohttp.WSMsgType.BINARY:
                raise RuntimeError(f"socket closed: {msg.type}")
            kind, flags, epoch, seq, hi, lo = HEADER.unpack_from(msg.data)
            if first is None:
                first = time.monotonic()
                end = first + args.seconds
                continue                      # the first frame includes encoder start-up
            lat.append((now_us - ((hi << 32) | lo)) / 1000.0)
            sizes.append(len(msg.data))
            keyframes += flags & 1
            if time.monotonic() >= end:
                break
        await ws.close()
    elapsed = time.monotonic() - first
    lat.sort()
    return {
        "stream": {k: stream.get(k) for k in ("codec", "width", "height", "fps", "codec_string")} if stream else None,
        "frames": len(lat),
        "fps": round(len(lat) / elapsed, 1),
        "kbps": round(sum(sizes) * 8 / elapsed / 1000),
        "keyframes": keyframes,
        "latency_ms": {
            "min": round(lat[0], 1),
            "p50": round(statistics.median(lat), 1),
            "p95": round(lat[int(len(lat) * 0.95) - 1], 1),
            "max": round(lat[-1], 1),
        } if lat else None,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--port", type=int, default=18463)
    p.add_argument("--display", type=int, default=0)
    p.add_argument("--codec", default="h264", choices=["h264", "jpeg"])
    p.add_argument("--encoder", default="auto")
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--bitrate", default="10M")
    p.add_argument("--max-res", default="1080p")
    p.add_argument("--synthetic", metavar="WxH", help="use a moving test pattern of this size instead of the screen")
    args = p.parse_args()

    with tempfile.TemporaryDirectory() as data_dir:
        proc = start_server(args, data_dir)
        try:
            pin = wait_for_pin(proc)
            ca_pem = (Path(data_dir) / "ca.crt.pem").read_text()
            result = asyncio.run(measure(args, pin, ca_pem))
        finally:
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out = ""
        enc_lines = [ln.strip() for ln in (out or "").splitlines() if "lancast.pipeline" in ln]
    result["server"] = enc_lines[-1] if enc_lines else None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
