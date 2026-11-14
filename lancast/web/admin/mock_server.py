#!/usr/bin/env python3
"""Tiny standalone mock of the LanCast admin HTTP API.

This is only a development fixture for lancast/web/admin.  It uses the Python
standard library, serves the static files from this directory, and speaks the
subset of docs/PROTOCOL.md section 5 that the admin page needs.

Run:
    python mock_server.py
    # open http://127.0.0.1:8450/admin#token=test

The mock accepts exactly one admin token: "test".
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ADMIN_DIR = Path(__file__).resolve().parent
ADMIN_TOKEN = "test"
DEFAULT_PORT = 8450
MAX_BODY_BYTES = 64 * 1024
ASSET_EXTENSIONS = {
    ".css",
    ".html",
    ".ico",
    ".js",
    ".json",
    ".mjs",
    ".png",
    ".svg",
    ".webmanifest",
}
CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}


def fingerprint(seed: int) -> str:
    return ":".join(f"{(seed + index * 37) & 0xFF:02X}" for index in range(32))


def qr_placeholder() -> str:
    """Return a small decorative QR-looking SVG as a data URI."""
    size = 25
    random_state = 0x5A17C0DE

    def next_bit() -> bool:
        nonlocal random_state
        random_state = (random_state * 1103515245 + 12345) & 0x7FFFFFFF
        return bool((random_state >> 16) & 1)

    dark: set[tuple[int, int]] = set()

    def finder(left: int, top: int) -> None:
        for y in range(7):
            for x in range(7):
                edge = x in (0, 6) or y in (0, 6)
                center = 2 <= x <= 4 and 2 <= y <= 4
                if edge or center:
                    dark.add((left + x, top + y))

    finder(1, 1)
    finder(size - 8, 1)
    finder(1, size - 8)

    for y in range(size):
        for x in range(size):
            if any(
                (left <= x < left + 8) and (top <= y < top + 8)
                for left, top in ((0, 0), (size - 8, 0), (0, size - 8))
            ):
                continue
            if (x * 7 + y * 11 + (x ^ y)) % 5 < 2 or next_bit():
                dark.add((x, y))

    rects = "".join(
        f'<rect x="{x}" y="{y}" width="1" height="1"/>' for x, y in sorted(dark)
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="260" height="260" '
        f'viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#ffffff"/>'
        f'<g fill="#111111">{rects}</g></svg>'
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


QR_DATA_URI = qr_placeholder()


class MockState:
    """Mutable in-memory fixture; all access goes through the lock."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        now = time.time()
        self.started_at = now - 3600.0
        self.paused = False
        self.pin = "123456"
        self.pin_expires_at = now + 512.0
        self.pin_rotations = 0
        self.qr_url = "https://192.168.1.20:8443/#pair=mock-initial"

        self.settings = {
            "fps": 60,
            "max_width": 1920,
            "max_height": 1080,
            "bitrate_kbps": 10000,
            "jpeg_quality": 70,
            "jpeg_fps": 15,
            "approval": True,
            "control_enabled": True,
            "viewer_quality": True,
            "max_viewers": 8,
        }

        self.displays = [
            {
                "index": 0,
                "name": "Display 1",
                "width": 2880,
                "height": 1800,
                "primary": True,
            },
            {
                "index": 1,
                "name": "Display 2",
                "width": 1920,
                "height": 1080,
                "primary": False,
            },
        ]

        self.pending = [
            {
                "request_id": "r_ab12cd34",
                "name": "iPad",
                "ip": "192.168.3.20",
                "ua": (
                    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "method": "qr",
                "_created_at": now - 3.0,
            }
        ]

        self.viewers = [
            {
                "viewer_id": "v_9f8e7d6c",
                "device_id": "d_1a2b3c4d5e6f",
                "name": "iPad",
                "ip": "192.168.3.20",
                "ua": (
                    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "display": 0,
                "codec": "h264",
                "fps": 59.8,
                "kbps": 8200,
                "rtt_ms": 6.1,
                "latency_ms": 38,
                "drops": 0,
                "control": True,
                "controlling": True,
                "quality": {
                    "preset": "high",
                    "max_width": 2560,
                    "max_height": 1440,
                    "fps": 60,
                    "bitrate_kbps": 20000,
                },
                "_connected_at": now - 120.0,
            },
            {
                "viewer_id": "v_11223344",
                "device_id": None,
                "name": "Pixel 8",
                "ip": "192.168.3.21",
                "ua": (
                    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 "
                    "Mobile Safari/537.36"
                ),
                "display": 1,
                "codec": "jpeg",
                "fps": 15,
                "kbps": 1200,
                "rtt_ms": None,
                "latency_ms": None,
                "drops": None,
                "control": False,
                "controlling": False,
                "quality": {
                    "preset": "smooth",
                    "max_width": 1280,
                    "max_height": 720,
                    "fps": 15,
                    "jpeg_quality": 70,
                },
                "_connected_at": now - 42.0,
            },
            {
                "viewer_id": "v_55667788",
                "device_id": None,
                "name": "Surface Pro",
                "ip": "192.168.3.22",
                "ua": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
                    "Safari/537.36 Edg/124.0.0.0"
                ),
                "display": 0,
                "codec": "h264",
                "fps": 30,
                "kbps": 2400,
                "rtt_ms": 12.4,
                "latency_ms": 55,
                "drops": 2,
                "control": False,
                "controlling": False,
                "quality": None,
                "_connected_at": now - 18.0,
            },
        ]

        self.devices = [
            {
                "device_id": "d_1a2b3c4d5e6f",
                "name": "iPad",
                "created_at": now - 86400 * 3,
                "last_seen_at": now - 180.0,
                "last_ip": "192.168.3.20",
                "online": True,
                "can_control": True,
            },
            {
                "device_id": "d_aabbccddeeff",
                "name": "MacBook Pro",
                "created_at": now - 86400 * 9,
                "last_seen_at": now - 86400 * 1.5,
                "last_ip": "192.168.3.31",
                "online": False,
                "can_control": False,
            },
        ]

        self.log = [
            {
                "t": now - 300.0,
                "level": "info",
                "msg": "LanCast 服务已启动，监听 0.0.0.0:8443。",
            },
            {
                "t": now - 280.0,
                "level": "debug",
                "msg": "捕获后端 dxgi 初始化完成。",
            },
            {
                "t": now - 240.0,
                "level": "info",
                "msg": "H.264 编码器 h264_qsv 已就绪。",
            },
            {
                "t": now - 200.0,
                "level": "info",
                "msg": "设备 MacBook Pro 已离线。",
            },
            {
                "t": now - 150.0,
                "level": "warning",
                "msg": "检测到虚拟网卡 vEthernet (WSL)，二维码仍使用 Wi-Fi 地址。",
            },
            {
                "t": now - 120.0,
                "level": "info",
                "msg": "iPad 开始观看 Display 1（H.264）。",
            },
            {
                "t": now - 75.0,
                "level": "info",
                "msg": "Pixel 8 开始观看 Display 2（JPEG）。",
            },
            {
                "t": now - 3.0,
                "level": "warning",
                "msg": "iPad 正在等待管理员批准。",
            },
        ]

    def _add_log_locked(self, level: str, message: str) -> None:
        self.log.append({"t": time.time(), "level": level, "msg": message})
        if len(self.log) > 100:
            self.log = self.log[-100:]

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            pending = []
            for item in self.pending:
                public = copy.deepcopy(item)
                public["age_s"] = max(0, int(now - public.pop("_created_at")))
                pending.append(public)

            viewers = []
            for item in self.viewers:
                public = copy.deepcopy(item)
                public["connected_s"] = max(
                    0, int(now - public.pop("_connected_at"))
                )
                viewers.append(public)

            return {
                "server": {
                    "name": "DESKTOP-LANCAST",
                    "version": "0.1.0",
                    "started_at": self.started_at,
                    "port": 8443,
                    "admin_port": 8442,
                    "paused": self.paused,
                    "urls": [
                        {
                            "url": "https://192.168.1.20:8443",
                            "interface": "Wi-Fi",
                            "kind": "lan",
                            "primary": True,
                        },
                        {
                            "url": "https://10.20.0.5:8443",
                            "interface": "vEthernet (WSL)",
                            "kind": "virtual",
                            "primary": False,
                        },
                        {
                            "url": "https://100.101.102.103:8443",
                            "interface": "Tailscale",
                            "kind": "vpn",
                            "primary": False,
                        },
                    ],
                    "cert_sha256": fingerprint(0x4A),
                    "ca_sha256": fingerprint(0x91),
                    "encoder": "h264_qsv",
                    "capture_backend": "dxgi",
                    "warnings": [
                        "检测到虚拟网卡，请确认手机与 PC 处于同一网络后再扫码。"
                    ],
                },
                "pairing": {
                    "pin": self.pin,
                    "expires_in_s": max(0, int(self.pin_expires_at - now)),
                    "qr_url": self.qr_url,
                    "qr_svg": QR_DATA_URI,
                },
                "settings": copy.deepcopy(self.settings),
                "displays": copy.deepcopy(self.displays),
                "pending": pending,
                "viewers": viewers,
                "devices": copy.deepcopy(self.devices),
                "log": copy.deepcopy(self.log),
            }

    def approve(self, body: dict) -> None:
        request_id = str(body.get("request_id", ""))
        allow = bool(body.get("allow"))
        control = bool(body.get("control"))
        with self.lock:
            match = next(
                (
                    item
                    for item in self.pending
                    if item["request_id"] == request_id
                ),
                None,
            )
            if match is not None:
                self.pending.remove(match)
                name = match.get("name") or request_id
                if allow:
                    if control:
                        for viewer in self.viewers:
                            if viewer.get("name") == name:
                                viewer["control"] = True
                        self._add_log_locked(
                            "info", f"已允许 {name} 连接并允许远程控制。"
                        )
                    else:
                        self._add_log_locked("info", f"已允许 {name} 连接。")
                else:
                    self._add_log_locked("warning", f"已拒绝 {name} 连接。")

    def set_viewer_control(self, body: dict) -> None:
        viewer_id = str(body.get("viewer_id", ""))
        control = bool(body.get("control"))
        with self.lock:
            match = next(
                (
                    item
                    for item in self.viewers
                    if item["viewer_id"] == viewer_id
                ),
                None,
            )
            if match is None:
                return
            match["control"] = control
            if not control:
                match["controlling"] = False
            device_id = match.get("device_id")
            if device_id:
                device = next(
                    (
                        item
                        for item in self.devices
                        if item["device_id"] == device_id
                    ),
                    None,
                )
                if device is not None:
                    device["can_control"] = control
            self._add_log_locked(
                "info",
                f"{match.get('name') or viewer_id} 的远程控制已"
                f"{'开启' if control else '关闭'}。",
            )

    def set_device_control(self, body: dict) -> None:
        device_id = str(body.get("device_id", ""))
        control = bool(body.get("control"))
        with self.lock:
            match = next(
                (
                    item
                    for item in self.devices
                    if item["device_id"] == device_id
                ),
                None,
            )
            if match is None:
                return
            match["can_control"] = control
            for viewer in self.viewers:
                if viewer.get("device_id") != device_id:
                    continue
                viewer["control"] = control
                if not control:
                    viewer["controlling"] = False
            self._add_log_locked(
                "info",
                f"已{'允许' if control else '禁止'} "
                f"{match.get('name') or device_id} 远程控制。",
            )

    def kick(self, body: dict) -> None:
        viewer_id = str(body.get("viewer_id", ""))
        with self.lock:
            match = next(
                (
                    item
                    for item in self.viewers
                    if item["viewer_id"] == viewer_id
                ),
                None,
            )
            if match is not None:
                self.viewers.remove(match)
                self._add_log_locked(
                    "info", f"已断开 {match.get('name') or viewer_id}。"
                )

    def revoke(self, body: dict) -> None:
        device_id = str(body.get("device_id", ""))
        with self.lock:
            match = next(
                (
                    item
                    for item in self.devices
                    if item["device_id"] == device_id
                ),
                None,
            )
            if match is not None:
                self.devices.remove(match)
                self.viewers = [
                    item
                    for item in self.viewers
                    if item.get("device_id") != device_id
                ]
                self._add_log_locked(
                    "warning", f"已撤销设备 {match.get('name') or device_id}。"
                )

    def rotate_pin(self) -> None:
        with self.lock:
            self.pin_rotations += 1
            self.pin = f"{secrets.randbelow(900000) + 100000:06d}"
            self.pin_expires_at = time.time() + 600.0
            self.qr_url = (
                "https://192.168.1.20:8443/"
                f"#pair=mock-{self.pin_rotations}-{self.pin}"
            )
            self._add_log_locked("info", "配对码和二维码已更换。")

    def set_paused(self, body: dict) -> None:
        with self.lock:
            self.paused = bool(body.get("paused"))
            self._add_log_locked(
                "info", "共享已暂停。" if self.paused else "共享已恢复。"
            )

    def update_settings(self, body: dict) -> None:
        with self.lock:
            for key, value in body.items():
                if key == "max_viewers":
                    continue
                if key in self.settings:
                    self.settings[key] = value
            if not self.settings.get("control_enabled", True):
                for viewer in self.viewers:
                    viewer["controlling"] = False
            self._add_log_locked("info", "共享设置已更新。")


STATE = MockState()


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "LanCastMock/0.1"
    sys_version = ""

    # -- headers -----------------------------------------------------

    def _host(self) -> str:
        host = (self.headers.get("Host") or "127.0.0.1:8450").strip()
        host = host.replace("\r", "").replace("\n", "")
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
            return "127.0.0.1:8450"
        return host

    def _send_common_headers(self) -> None:
        host = self._host()
        csp = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; "
            f"connect-src 'self' wss://{host} ws://{host}; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'none'"
        )
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")

    def _send_bytes(
        self,
        status: HTTPStatus | int,
        content_type: str,
        payload: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: HTTPStatus | int, payload: dict) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._send_bytes(
            status, "application/json; charset=utf-8", encoded
        )

    def _not_found(self) -> None:
        if urlsplit(self.path).path.startswith("/admin/api/"):
            self._json(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}
            )
        else:
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                "text/plain; charset=utf-8",
                b"not found\n",
            )

    def _check_token(self) -> bool:
        if self.headers.get("X-Admin-Token") == ADMIN_TOKEN:
            return True
        self._json(
            HTTPStatus.UNAUTHORIZED,
            {"ok": False, "error": "invalid admin token"},
        )
        return False

    # -- request routing --------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = urlsplit(self.path).path

        if path in ("/admin", "/admin/"):
            self._serve_file(ADMIN_DIR / "index.html", "text/html; charset=utf-8")
            return

        if path.startswith("/static/admin/"):
            name = path[len("/static/admin/") :]
            if (
                not name
                or "/" in name
                or "\\" in name
                or Path(name).name != name
                or Path(name).suffix.lower() not in ASSET_EXTENSIONS
            ):
                self._not_found()
                return
            target = (ADMIN_DIR / name).resolve()
            if (
                target.parent != ADMIN_DIR
                or not target.is_file()
            ):
                self._not_found()
                return
            content_type = CONTENT_TYPES.get(
                target.suffix.lower(), "application/octet-stream"
            )
            self._serve_file(target, content_type)
            return

        if path == "/admin/api/state":
            if self._check_token():
                self._json(HTTPStatus.OK, STATE.snapshot())
            return

        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return

        if path == "/ca.crt":
            self._serve_ca()
            return

        self._not_found()

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        path = urlsplit(self.path).path
        if not path.startswith("/admin/api/"):
            self._not_found()
            return
        if not self._check_token():
            return

        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "body too large"},
            )
            return

        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid JSON body"},
            )
            return
        if not isinstance(body, dict):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "JSON body must be an object"},
            )
            return

        print(
            f"POST {path} {json.dumps(body, ensure_ascii=False)}",
            flush=True,
        )

        if path == "/admin/api/approve":
            STATE.approve(body)
        elif path == "/admin/api/viewer/control":
            STATE.set_viewer_control(body)
        elif path == "/admin/api/device/control":
            STATE.set_device_control(body)
        elif path == "/admin/api/kick":
            STATE.kick(body)
        elif path == "/admin/api/revoke":
            STATE.revoke(body)
        elif path == "/admin/api/pin/rotate":
            STATE.rotate_pin()
        elif path == "/admin/api/pause":
            STATE.set_paused(body)
        elif path == "/admin/api/settings":
            STATE.update_settings(body)
        else:
            self._not_found()
            return

        self._json(HTTPStatus.OK, {"ok": True})

    # -- response helpers -------------------------------------------

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            payload = path.read_bytes()
        except OSError:
            self._not_found()
            return
        self._send_bytes(HTTPStatus.OK, content_type, payload)

    def _serve_ca(self) -> None:
        # A deliberately fake fixture, not a usable certificate.
        payload = (
            "-----BEGIN CERTIFICATE-----\n"
            "LANCAST-MOCK-CA-NOT-A-REAL-CERTIFICATE\n"
            "-----END CERTIFICATE-----\n"
        ).encode("ascii")
        self._send_bytes(
            HTTPStatus.OK,
            "application/x-x509-ca-cert",
            payload,
            {
                "Content-Disposition": 'attachment; filename="LanCast-CA.crt"'
            },
        )

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Keep the fixture useful in a terminal without noisy default banners.
        print(f"{self.address_string()} {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="LanCast admin mock server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), AdminHandler)
    server.daemon_threads = True
    print(
        "LanCast admin mock: "
        f"http://127.0.0.1:{args.port}/admin#token={ADMIN_TOKEN}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
