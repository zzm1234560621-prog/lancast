"""aiohttp application: listeners, admission control, WebSocket sessions, admin API.

See docs/PROTOCOL.md for the wire contract and docs/SECURITY.md for the threat model.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import math
import re
import secrets
import socket
import ssl
import sys
import time
import webbrowser
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

from . import APP_NAME, __version__
from .auth import ApprovalRequest, Approvals, DeviceStore, Pairing, RateLimiter, clean_name, clean_text, new_id
from .certs import CertBundle, ensure_certificates
from .config import Settings
from .control import InputController
from .pipeline import StreamHub, ViewerQueue, monotonic_us

log = logging.getLogger("lancast.server")

WEB_ROOT = Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json",
}
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,255}$")
CLIENT_MSG_LIMIT = 8 * 1024
AUTH_TIMEOUT_S = 10.0
SNDBUF_BYTES = 256 * 1024          # small kernel buffer => backpressure shows up quickly
MSG_BURST, MSG_PER_S = 40, 20.0    # client message token bucket
MAX_BAD_MESSAGES = 20


# --------------------------------------------------------------------------- helpers

def normalize_ip(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        addr = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


def host_for_url(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


def load_static(directory: Path) -> dict[str, tuple[bytes, str]]:
    """Read a flat directory of web assets into memory (no path traversal possible)."""
    files: dict[str, tuple[bytes, str]] = {}
    if not directory.is_dir():
        return files
    for path in sorted(directory.iterdir()):
        ctype = CONTENT_TYPES.get(path.suffix.lower())
        if path.is_file() and ctype and not path.name.startswith(("selftest", "mock_")):
            files[path.name] = (path.read_bytes(), ctype)
    return files


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def tune_socket(transport) -> None:
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None:
        return
    for level, opt, value in (
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        (socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF_BYTES),
    ):
        try:
            sock.setsockopt(level, opt, value)
        except OSError:
            pass


def make_ssl_context(certs: CertBundle) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certs.cert_path, certs.key_path)
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")       # TLS 1.2: forward secrecy + AEAD only
    ctx.options |= ssl.OP_NO_COMPRESSION | ssl.OP_CIPHER_SERVER_PREFERENCE
    ctx.options |= getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


class TokenBucket:
    def __init__(self, burst: float, per_s: float) -> None:
        self.capacity = burst
        self.tokens = burst
        self.per_s = per_s
        self.t = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.per_s)
        self.t = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


# --------------------------------------------------------------------------- state

class AppState:
    def __init__(self, settings: Settings, certs: CertBundle, addresses, displays, warnings, encoders) -> None:
        from .net import parse_cidrs, primary_address

        self.settings = settings
        self.allow_nets = parse_cidrs(settings.allow_cidrs)
        self.started_at = time.time()
        self.server_name = clean_text(socket.gethostname(), 63) or "PC"
        self.admin_token = secrets.token_urlsafe(24)
        self.certs = certs
        self.addresses = list(addresses)
        self.primary_ip = primary_address() or next(
            (a.ip for a in self.addresses if a.kind != "loopback"), "127.0.0.1")
        self.displays = list(displays)
        self.warnings = list(warnings)
        self.encoders = list(encoders)
        self.events: deque[dict] = deque(maxlen=100)
        self.sessions: dict[str, "Session"] = {}
        self.hub: StreamHub | None = None
        self.shutting_down = False
        self.rate = RateLimiter()
        self.devices = DeviceStore(settings.data_dir / "devices.json")
        self.approvals = Approvals(on_change=self._on_approval)
        self.pairing = Pairing(ttl_s=settings.pin_ttl_s, on_rotate=self._on_pin_rotate)
        self.local_hosts = {f"127.0.0.1:{settings.admin_port}", f"localhost:{settings.admin_port}",
                            f"[::1]:{settings.admin_port}"}
        self._qr_cache: tuple[str, str] = ("", "")
        self.ca_der = certs.ca_der
        # remote control, set up by serve(); None means "no input backend available"
        self.input_backend = None
        self.key_codes: frozenset[str] = frozenset()
        self.buttons: tuple[str, ...] = ()

    def update_all_control(self) -> None:
        for session in list(self.sessions.values()):
            session.update_control()

    # -- events ------------------------------------------------------------------
    def event(self, level: str, msg: str) -> None:
        self.events.append({"t": time.time(), "level": level, "msg": msg})
        getattr(log, level if level in ("info", "warning", "error") else "info")(msg)

    def _on_pin_rotate(self, reason: str) -> None:
        if reason == "start":
            return
        text = {"used": "配对码已使用", "expired": "配对码已过期", "too_many_failures": "错误次数过多",
                "manual": "已手动更换配对码"}.get(reason, reason)
        # The PIN itself is shown only on the admin page and the console banner,
        # never written to the event log that remote devices could see later.
        self.event("info", f"{text}，已生成新的配对码")
        if sys.stdout and sys.stdout.isatty():
            print(f"  新配对码 PIN：{self.pairing.pin[:3]} {self.pairing.pin[3:]}（扫码请用管理页面里的新二维码）", flush=True)

    def _on_approval(self, req: ApprovalRequest, what: str) -> None:
        if what == "new":
            self.event("warning", f"新设备请求连接：{req.name}（{req.ip}）—— 请在管理页面点击允许或拒绝")

    # -- URLs --------------------------------------------------------------------
    def lan_url(self, ip: str) -> str:
        return f"https://{host_for_url(ip)}:{self.settings.port}"

    def admin_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.admin_port}/admin#token={self.admin_token}"

    def qr_url(self) -> str:
        _, token = self.pairing.current()
        return f"{self.lan_url(self.primary_ip)}/#pair={token}"

    def qr_svg(self) -> str:
        from .net import qr_svg_data_uri

        url = self.qr_url()
        if self._qr_cache[0] != url:
            self._qr_cache = (url, qr_svg_data_uri(url))
        return self._qr_cache[1]

    def display_dicts(self) -> list[dict]:
        return [{"index": d.index, "name": d.name, "width": d.width, "height": d.height,
                 "primary": d.primary} for d in self.displays]

    # -- admin snapshot ----------------------------------------------------------
    def snapshot(self) -> dict:
        s = self.settings
        hub = self.hub
        pipe = hub.info() if hub else {}
        backends = sorted({p["backend"] for p in pipe.values() if p.get("backend")})
        encoders = sorted({o["encoder"] for p in pipe.values() for o in p["outputs"] if o.get("encoder")})
        urls = [{"url": self.lan_url(a.ip), "interface": a.interface, "kind": a.kind,
                 "primary": a.ip == self.primary_ip} for a in self.addresses if a.kind != "loopback"]
        pin, _ = self.pairing.current()
        now = time.time()
        online = {sess.device_id for sess in self.sessions.values() if sess.device_id}
        mono = time.monotonic()
        viewers = []
        for sess in self.sessions.values():
            cs = sess.client_stats
            out = sess.output
            applied = out.applied if out is not None else None
            viewers.append({
                "viewer_id": sess.viewer_id, "device_id": sess.device_id, "name": sess.name,
                "ip": sess.ip, "ua": sess.ua, "display": sess.display, "codec": sess.codec,
                "connected_s": int(now - sess.connected_at),
                "fps": cs.get("fps"), "kbps": cs.get("kbps"), "rtt_ms": cs.get("rtt_ms"),
                "latency_ms": cs.get("latency_ms"), "drops": sess.queue.dropped + int(cs.get("dropped") or 0),
                "control": sess.control_granted,
                "controlling": bool(sess.controller and sess.control_effective and sess.controller.active(mono)),
                "quality": applied.as_dict(out.codec) if applied is not None else None,
            })
        return {
            "server": {
                "name": self.server_name, "version": __version__, "started_at": self.started_at,
                "port": s.port, "admin_port": s.admin_port, "paused": bool(hub and hub.paused),
                "urls": urls, "cert_sha256": self.certs.cert_sha256, "ca_sha256": self.certs.ca_sha256,
                "encoder": ", ".join(encoders) or (self.encoders[0] if self.encoders else "jpeg"),
                "capture_backend": ", ".join(backends) or s.capture,
                "warnings": self.warnings,
                "pipelines": {str(k): v for k, v in pipe.items()},
            },
            "pairing": {"pin": pin, "expires_in_s": self.pairing.expires_in(),
                        "qr_url": self.qr_url(), "qr_svg": self.qr_svg()},
            "settings": s.public_dict(),
            "displays": self.display_dicts(),
            "pending": [{"request_id": r.request_id, "name": r.name, "ip": r.ip, "ua": r.ua,
                         "method": r.method, "age_s": int(now - r.created)} for r in self.approvals.pending()],
            "viewers": viewers,
            "devices": [{"device_id": d.device_id, "name": d.name, "created_at": d.created_at,
                         "last_seen_at": d.last_seen_at, "last_ip": d.last_ip,
                         "online": d.device_id in online, "can_control": d.can_control}
                        for d in self.devices.list()],
            "log": list(self.events),
        }


# --------------------------------------------------------------------------- viewer session

class Session:
    """One authenticated WebSocket viewer."""

    def __init__(self, state: AppState, ws: web.WebSocketResponse, ip: str) -> None:
        self.state = state
        self.ws = ws
        self.ip = ip
        self.viewer_id = new_id("v_", 4)
        self.device_id: str | None = None
        self.name = "?"
        self.ua = ""
        self.display: int | None = None
        self.codec: str | None = None
        self.output = None                       # set by StreamHub
        self.queue = ViewerQueue(self._need_keyframe)
        self.connected_at = time.time()
        self.client_stats: dict = {}
        self._sender: asyncio.Task | None = None
        self._registered = False
        self._closing = False
        self._bucket = TokenBucket(MSG_BURST, MSG_PER_S)
        self._bad = 0
        self._last_key_request = 0.0
        # remote control (PROTOCOL §3.7)
        self.control_granted = False             # the owner allowed this viewer
        self.control_effective = False           # granted and not suspended (master switch, pause)
        self.controller: InputController | None = None
        self._last_denied = 0.0
        self._control_announced = 0.0

    # -- lifecycle ---------------------------------------------------------------
    async def run(self) -> None:
        if not await self._authenticate():
            return
        self._sender = asyncio.create_task(self._send_loop())
        async for msg in self.ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("type") == "input":
                    self._handle_input(data)          # own, much larger rate budget
                elif not self._bucket.allow():
                    self.close(4001, "too_many_messages")
                    break
                else:
                    self._handle(data)
                if self._closing:
                    break
            elif msg.type == WSMsgType.BINARY:
                self.close(4001, "binary_not_allowed")
                break
            else:
                break

    def close(self, code: int, reason: str, bye: str | None = None) -> None:
        """Send an optional ``bye`` and close, in order, through the sender task."""
        if self._closing:
            return
        self._closing = True
        if bye:
            self.queue.put_control(json.dumps({"type": "bye", "reason": bye}))
        self.queue.finish(code, reason)
        if self._sender is None:
            asyncio.ensure_future(self._close_now(code, reason))

    async def _close_now(self, code: int, reason: str) -> None:
        try:
            await self.ws.close(code=code, message=reason.encode())
        except Exception:  # noqa: BLE001
            pass

    async def cleanup(self) -> None:
        st = self.state
        self._release_input()
        if st.hub is not None:
            st.hub.unsubscribe(self)
        if self._registered:
            st.sessions.pop(self.viewer_id, None)
            st.event("info", f"{self.name}（{self.ip}）已断开")
        if self._sender is not None and not self._sender.done():
            if self._closing:
                try:
                    await asyncio.wait_for(asyncio.shield(self._sender), 2.0)
                except Exception:  # noqa: BLE001
                    pass
            self._sender.cancel()
        self.queue.close()
        if not self.ws.closed:
            await self._close_now(1000, "bye")

    async def _send_loop(self) -> None:
        q = self.queue
        try:
            while True:
                item = await q.next()
                if item is None:
                    return
                if isinstance(item, tuple):
                    _, code, reason = item
                    await self.ws.close(code=code, message=reason.encode())
                    return
                if isinstance(item, str):
                    await self.ws.send_str(item)
                else:
                    await self.ws.send_bytes(item)
                    q.sent_frames += 1
                    q.sent_bytes += len(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - peer went away
            log.debug("sender for %s stopped: %s", self.viewer_id, exc)

    def _need_keyframe(self) -> None:
        if self.output is not None:
            self.output.request_keyframe()

    # -- authentication ----------------------------------------------------------
    async def _fail(self, reason: str, code: int, retry_after: int = 0) -> bool:
        try:
            await self.ws.send_str(json.dumps({"type": "auth_fail", "reason": reason, "retry_after_s": retry_after}))
        except Exception:  # noqa: BLE001
            pass
        await self._close_now(code, reason)
        return False

    def _full(self) -> bool:
        return len(self.state.sessions) >= self.state.settings.max_viewers

    async def _authenticate(self) -> bool:
        st = self.state
        try:
            msg = await self.ws.receive(timeout=AUTH_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self._close_now(4001, "auth_timeout")
            return False
        if msg.type != WSMsgType.TEXT:
            if msg.type == WSMsgType.BINARY:
                await self._close_now(4001, "binary_not_allowed")
            return False
        try:
            data = json.loads(msg.data)
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get("type") != "auth":
            return await self._fail("bad_request", 4001)

        wait = st.rate.retry_after(self.ip)
        if wait:
            return await self._fail("rate_limited", 4008, wait)

        client = data.get("client") if isinstance(data.get("client"), dict) else {}
        self.ua = clean_text(client.get("ua"), 200)
        method = data.get("method")
        token = None

        if method == "device":
            dev = st.devices.verify(data.get("device_id"), data.get("token"))
            if dev is None:
                st.rate.failure(self.ip)
                st.event("warning", f"拒绝了未知或已撤销的设备（{self.ip}）")
                return await self._fail("unknown_device", 4003)
            st.rate.success(self.ip)
            if self._full():
                return await self._fail("server_full", 4009)
            self.device_id, self.name = dev.device_id, dev.name
            self.control_granted = dev.can_control
            st.devices.touch(dev.device_id, self.ip, self.ua)

        elif method in ("pin", "qr"):
            self.name = clean_name(data.get("name"))
            ok = st.pairing.check_pin(data.get("pin")) if method == "pin" else st.pairing.check_qr(data.get("token"))
            if not ok:
                st.rate.failure(self.ip)
                st.event("warning", f"配对失败：{self.name}（{self.ip}，{'PIN' if method == 'pin' else '二维码'}）")
                return await self._fail("bad_credentials" if method == "pin" else "expired", 4003)
            st.rate.success(self.ip)
            if self._full():
                return await self._fail("server_full", 4009)
            if st.settings.approval:
                if st.approvals.busy_for(self.ip):
                    return await self._fail("rate_limited", 4008, 10)
                try:
                    await self.ws.send_str(json.dumps({"type": "auth_pending", "timeout_s": int(st.approvals.timeout_s)}))
                except Exception:  # noqa: BLE001
                    return False
                decision = await self._await_approval(method)
                if decision is None:
                    return False
                if decision not in ("allow", "allow_control"):
                    st.event("info", f"已{'拒绝' if decision == 'deny' else '超时未批准'}：{self.name}（{self.ip}）")
                    return await self._fail("denied" if decision == "deny" else "timeout", 4003)
                self.control_granted = decision == "allow_control"
                if self._full():
                    return await self._fail("server_full", 4009)
            if data.get("remember") is True:
                dev, token = st.devices.issue(self.name, self.ip, self.ua, can_control=self.control_granted)
                self.device_id = dev.device_id
            st.event("info", f"新设备配对成功：{self.name}（{self.ip}）"
                             + ("，已允许远程控制" if self.control_granted else ""))
        else:
            return await self._fail("bad_request", 4001)

        st.sessions[self.viewer_id] = self
        self._registered = True
        self.control_effective = self.effective_control()
        reply = {
            "type": "auth_ok",
            "viewer_id": self.viewer_id,
            "device_id": self.device_id,
            "server": {"name": st.server_name, "version": __version__},
            "displays": st.display_dicts(),
            "paused": bool(st.hub and st.hub.paused),
            "control": self.control_effective,
            "viewer_quality": bool(st.settings.viewer_quality),
        }
        if token:
            reply["device_token"] = token
        try:
            await self.ws.send_str(json.dumps(reply))
        except Exception:  # noqa: BLE001
            return False
        st.event("info", f"{self.name}（{self.ip}）已连接")
        return True

    async def _await_approval(self, method: str) -> str | None:
        """Wait for the owner's decision; None if the client went away meanwhile."""
        st = self.state
        approval = asyncio.ensure_future(st.approvals.wait(self.name, self.ip, self.ua, method))
        receiver = asyncio.ensure_future(self.ws.receive())
        try:
            done, _ = await asyncio.wait({approval, receiver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            pending = [task for task in (approval, receiver) if not task.done()]
            for task in pending:
                task.cancel()
            # Cancellation only takes effect on the next loop turn; wait for it so the
            # WebSocket is no longer "being received from" when run() starts reading.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if approval in done:
            return approval.result()
        return None   # a message or a close arrived while pending

    # -- remote control ------------------------------------------------------------
    def _display_info(self):
        if self.display is None or not 0 <= self.display < len(self.state.displays):
            return None
        return self.state.displays[self.display]

    def effective_control(self) -> bool:
        st = self.state
        return bool(self.control_granted and st.settings.control_enabled and not (st.hub and st.hub.paused))

    def update_control(self) -> None:
        """Re-evaluate permission; tell the viewer and release its input when it is lost."""
        allowed = self.effective_control()
        if allowed == self.control_effective:
            return
        self.control_effective = allowed
        if not allowed:
            self._release_input()
        self.queue.put_control(json.dumps({"type": "control", "allowed": allowed}))

    def _get_controller(self) -> InputController | None:
        st = self.state
        if self.controller is None and st.input_backend is not None:
            self.controller = InputController(st.input_backend, st.key_codes, st.buttons, self._display_info)
        return self.controller

    def _release_input(self) -> None:
        if self.controller is not None:
            self.controller.release_all()

    def _handle_input(self, data: dict) -> None:
        if not self.control_effective:
            now = time.monotonic()
            if now - self._last_denied >= 5.0:
                self._last_denied = now
                self.queue.put_control(json.dumps({"type": "error", "code": "control_denied",
                                                   "message": "电脑端未允许此设备控制"}))
            return
        ctl = self._get_controller()
        if ctl is None:
            return
        if not ctl.allow_message():
            if ctl.flooding():
                self.close(4001, "input_flood")
            return
        before = ctl.last_active
        ctl.handle(data.get("events"))
        # Coarse audit trail only: never log keys or typed text.
        if ctl.last_active != before and ctl.last_active - max(before, self._control_announced) > 60.0:
            self._control_announced = ctl.last_active
            self.state.event("warning", f"{self.name}（{self.ip}）正在远程控制这台电脑")

    # -- streaming phase ---------------------------------------------------------
    def _handle(self, data) -> None:
        if not isinstance(data, dict):
            return self._bad_message()
        kind = data.get("type")
        st = self.state
        if kind == "watch":
            display, codec = data.get("display"), data.get("codec")
            if not isinstance(display, int) or isinstance(display, bool) or not 0 <= display < len(st.displays):
                self.queue.put_control(json.dumps({"type": "error", "code": "bad_display", "message": "无此显示器"}))
                return
            if codec not in ("h264", "jpeg"):
                self.queue.put_control(json.dumps({"type": "error", "code": "bad_codec", "message": "不支持的编码"}))
                return
            if display != self.display:
                self._release_input()
            self.queue.set_extreme(data.get("extreme") is True)
            st.hub.subscribe(self, display, codec, data.get("quality"))
            self.display, self.codec = display, codec
        elif kind == "unwatch":
            self._release_input()
            st.hub.unsubscribe(self)
        elif kind == "keyframe":
            now = time.monotonic()
            if now - self._last_key_request >= 1.0:
                self._last_key_request = now
                self._need_keyframe()
        elif kind == "ping":
            t = data.get("t")
            if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t):
                self.queue.put_control(json.dumps({"type": "pong", "t": t, "server_us": monotonic_us()}))
        elif kind == "stats":
            clean = {}
            for key, hi in (("fps", 1000), ("kbps", 1_000_000), ("rtt_ms", 60_000), ("latency_ms", 60_000),
                            ("decode_ms", 60_000), ("dropped", 10**9)):
                value = data.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    clean[key] = round(min(max(float(value), 0.0), hi), 1)
            self.client_stats = clean
        elif kind == "bye":
            self.close(1000, "bye")
        else:
            self._bad_message()

    def _bad_message(self) -> None:
        self._bad += 1
        if self._bad > MAX_BAD_MESSAGES:
            self.close(4001, "protocol_error")


# --------------------------------------------------------------------------- HTTP layer

STATE = web.AppKey("state", AppState)
LOCAL = web.AppKey("local", bool)
VIEWER_FILES = web.AppKey("viewer_files", dict)
ADMIN_FILES = web.AppKey("admin_files", dict)


@web.middleware
async def admission(request: web.Request, handler):
    state: AppState = request.app[STATE]
    local: bool = request.app[LOCAL]
    ip = normalize_ip(request.remote)
    if local:
        if ip is None or not ipaddress.ip_address(ip).is_loopback or request.host not in state.local_hosts:
            return web.Response(status=403, text="forbidden")
    else:
        from .net import is_allowed_peer

        if ip is None or not is_allowed_peer(ip, state.allow_nets):
            log.warning("refused connection from non-LAN address %s", ip)
            return web.Response(status=403, text="forbidden")
        if not HOST_RE.match(request.host or ""):
            return web.Response(status=400, text="bad host")
    return await handler(request)


async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
    host = request.host if HOST_RE.match(request.host or "") else ""
    connect = "'self'" + (f" wss://{host} ws://{host}" if host else "")
    h = response.headers
    h["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
        f"connect-src {connect}; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["X-Frame-Options"] = "DENY"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    h.setdefault("Cache-Control", "no-store")


def _static_response(files: dict, name: str) -> web.Response:
    item = files.get(name)
    if item is None:
        raise web.HTTPNotFound()
    body, ctype = item
    return web.Response(body=body, headers={"Content-Type": ctype})


async def viewer_index(request: web.Request) -> web.Response:
    return _static_response(request.app[VIEWER_FILES], "index.html")


async def viewer_static(request: web.Request) -> web.Response:
    return _static_response(request.app[VIEWER_FILES], request.match_info["name"])


async def admin_index(request: web.Request) -> web.Response:
    return _static_response(request.app[ADMIN_FILES], "index.html")


async def admin_static(request: web.Request) -> web.Response:
    return _static_response(request.app[ADMIN_FILES], request.match_info["name"])


async def ca_cert(request: web.Request) -> web.Response:
    return web.Response(
        body=request.app[STATE].ca_der,
        headers={"Content-Type": "application/x-x509-ca-cert",
                 "Content-Disposition": 'attachment; filename="LanCast-CA.crt"'},
    )


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def ws_handler(request: web.Request) -> web.StreamResponse:
    state: AppState = request.app[STATE]
    scheme = "http" if request.app[LOCAL] else "https"
    if request.headers.get("Origin") != f"{scheme}://{request.host}":
        return web.Response(status=403, text="bad origin")
    if state.shutting_down:
        return web.Response(status=503, text="shutting down")
    ws = web.WebSocketResponse(heartbeat=15.0, max_msg_size=CLIENT_MSG_LIMIT, compress=False, autoping=True)
    await ws.prepare(request)
    tune_socket(request.transport)
    session = Session(state, ws, normalize_ip(request.remote) or "?")
    try:
        await session.run()
    except Exception:  # noqa: BLE001
        log.exception("session %s crashed", session.viewer_id)
    finally:
        await session.cleanup()
    return ws


# -- admin API (local listener only) ---------------------------------------------

def _check_admin(request: web.Request) -> AppState:
    state: AppState = request.app[STATE]
    token = request.headers.get("X-Admin-Token", "")
    if not (token.isascii() and hmac.compare_digest(token.encode(), state.admin_token.encode())):
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": "bad admin token"}),
                                   content_type="application/json")
    if request.method == "POST":
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text=json.dumps({"ok": False, "error": "use application/json"}),
                                               content_type="application/json")
        origin = request.headers.get("Origin")
        if origin is not None and origin != f"http://{request.host}":
            raise web.HTTPForbidden(text=json.dumps({"ok": False, "error": "bad origin"}),
                                    content_type="application/json")
    return state


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "invalid JSON"}),
                                 content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "expected an object"}),
                                 content_type="application/json")
    return data


async def api_state(request: web.Request) -> web.Response:
    return web.json_response(_check_admin(request).snapshot())


async def api_approve(request: web.Request) -> web.Response:
    state = _check_admin(request)
    data = await _body(request)
    allow = data.get("allow") is True
    control = allow and data.get("control") is True
    if not state.approvals.resolve(str(data.get("request_id", "")), allow, control):
        return json_error(404, "request not found or already answered")
    return web.json_response({"ok": True})


def _set_session_control(state: AppState, session: "Session", allowed: bool) -> None:
    session.control_granted = allowed
    session.update_control()


async def api_device_control(request: web.Request) -> web.Response:
    state = _check_admin(request)
    data = await _body(request)
    device_id = str(data.get("device_id", ""))
    allowed = data.get("control") is True
    dev = state.devices.get(device_id)
    if dev is None or not state.devices.set_control(device_id, allowed):
        return json_error(404, "device not found")
    for session in list(state.sessions.values()):
        if session.device_id == device_id:
            _set_session_control(state, session, allowed)
    state.event("warning" if allowed else "info", f"已{'允许' if allowed else '禁止'}「{dev.name}」远程控制")
    return web.json_response({"ok": True})


async def api_viewer_control(request: web.Request) -> web.Response:
    state = _check_admin(request)
    data = await _body(request)
    session = state.sessions.get(str(data.get("viewer_id", "")))
    if session is None:
        return json_error(404, "viewer not found")
    allowed = data.get("control") is True
    if session.device_id:
        state.devices.set_control(session.device_id, allowed)
        for other in list(state.sessions.values()):
            if other.device_id == session.device_id:
                _set_session_control(state, other, allowed)
    else:
        _set_session_control(state, session, allowed)
    state.event("warning" if allowed else "info", f"已{'允许' if allowed else '禁止'}「{session.name}」远程控制")
    return web.json_response({"ok": True})


async def api_kick(request: web.Request) -> web.Response:
    state = _check_admin(request)
    data = await _body(request)
    session = state.sessions.get(str(data.get("viewer_id", "")))
    if session is None:
        return json_error(404, "viewer not found")
    state.event("info", f"已断开 {session.name}（{session.ip}）")
    session.close(4004, "kicked", bye="kicked")
    return web.json_response({"ok": True})


async def api_revoke(request: web.Request) -> web.Response:
    state = _check_admin(request)
    data = await _body(request)
    device_id = str(data.get("device_id", ""))
    dev = state.devices.get(device_id)
    if dev is None or not state.devices.revoke(device_id):
        return json_error(404, "device not found")
    for session in list(state.sessions.values()):
        if session.device_id == device_id:
            session.close(4004, "revoked", bye="revoked")
    state.event("info", f"已撤销设备：{dev.name}")
    return web.json_response({"ok": True})


async def api_rotate(request: web.Request) -> web.Response:
    state = _check_admin(request)
    await _body(request)
    state.pairing.rotate("manual")
    return web.json_response({"ok": True})


async def api_pause(request: web.Request) -> web.Response:
    state = _check_admin(request)
    data = await _body(request)
    paused = data.get("paused") is True
    state.hub.set_paused(paused)
    msg = json.dumps({"type": "paused", "paused": paused})
    for session in state.sessions.values():
        session.queue.put_control(msg)
    state.update_all_control()          # pausing also suspends remote control
    state.event("info", "已暂停共享" if paused else "已恢复共享")
    return web.json_response({"ok": True})


async def api_settings(request: web.Request) -> web.Response:
    state = _check_admin(request)
    data = await _body(request)
    try:
        applied = state.settings.apply_live(data)
    except ValueError as exc:
        return json_error(400, str(exc))
    if applied:
        state.settings.save_live()
        if applied.keys() & {"fps", "max_width", "max_height", "bitrate_kbps", "jpeg_quality", "jpeg_fps"}:
            state.hub.settings_changed()     # only video settings need the encoders re-opened
        if "control_enabled" in applied:
            state.update_all_control()
        if applied.get("viewer_quality") is False:
            # Viewers that picked their own quality fall back to the PC's standard settings.
            for session in list(state.sessions.values()):
                out = session.output
                if out is not None and out.qkey != ("standard",):
                    state.hub.subscribe(session, session.display, session.codec, None)
        state.event("info", "设置已更新：" + ", ".join(f"{k}={v}" for k, v in applied.items()))
    return web.json_response({"ok": True})


def build_app(state: AppState, local: bool) -> web.Application:
    app = web.Application(middlewares=[admission], client_max_size=64 * 1024)
    app[STATE] = state
    app[LOCAL] = local
    app[VIEWER_FILES] = load_static(WEB_ROOT / "viewer")
    app.on_response_prepare.append(security_headers)
    r = app.router
    r.add_get("/", viewer_index)
    r.add_get("/static/viewer/{name}", viewer_static)
    r.add_get("/ws", ws_handler)
    r.add_get("/ca.crt", ca_cert)
    r.add_get("/healthz", healthz)
    if local:
        app[ADMIN_FILES] = load_static(WEB_ROOT / "admin")
        r.add_get("/admin", admin_index)
        r.add_get("/static/admin/{name}", admin_static)
        r.add_get("/admin/api/state", api_state)
        r.add_post("/admin/api/approve", api_approve)
        r.add_post("/admin/api/device/control", api_device_control)
        r.add_post("/admin/api/viewer/control", api_viewer_control)
        r.add_post("/admin/api/kick", api_kick)
        r.add_post("/admin/api/revoke", api_revoke)
        r.add_post("/admin/api/pin/rotate", api_rotate)
        r.add_post("/admin/api/pause", api_pause)
        r.add_post("/admin/api/settings", api_settings)
    return app


# --------------------------------------------------------------------------- startup

def _discover(settings: Settings):
    """Blocking start-up work: addresses, displays, encoders, certificates, diagnostics."""
    from .capture import DisplayInfo, list_displays
    from .encode import preferred_h264_backends
    from .net import lan_addresses, windows_network_report

    addresses = lan_addresses()
    if settings.capture == "synthetic":
        displays = [DisplayInfo(0, "synthetic", "Synthetic", 0, 0, 1280, 720, True)]
    else:
        displays = list_displays()
    encoders = preferred_h264_backends()
    if settings.encoder == "auto" and settings.capture != "synthetic":
        from .encode.autotune import choose_encoder
        chosen, _report = choose_encoder(
            settings, displays[0] if displays else None, encoders,
            force=settings.retune,
        )
        if chosen:
            settings.encoder = chosen
            encoders = [chosen] + [name for name in encoders if name != chosen]
    elif settings.encoder != "auto":
        encoders = [settings.encoder] + [e for e in encoders if e != settings.encoder]
    certs = ensure_certificates(settings.data_dir, [a.ip for a in addresses])
    warnings = []
    if not encoders:
        warnings.append("没有可用的 H.264 编码器，只能使用兼容 JPEG 模式。")
    if not displays:
        warnings.append("没有检测到显示器。")
    try:
        warnings.extend(windows_network_report(settings.port))
    except Exception as exc:  # noqa: BLE001
        log.debug("network report failed: %s", exc)
    return addresses, displays, encoders, certs, warnings


def print_banner(state: AppState) -> None:
    from .net import qr_terminal

    s = state.settings
    pin, _ = state.pairing.current()
    lines = [
        "",
        f"  {APP_NAME} {__version__} · 内网投屏",
        "  " + "─" * 58,
        "  手机/平板连接同一 Wi-Fi 后扫码，或在浏览器打开：",
        f"      {state.lan_url(state.primary_ip)}",
        f"  配对码 PIN：{pin[:3]} {pin[3:]}   （单次有效，{s.pin_ttl_s // 60} 分钟后自动更换）",
        f"  管理页面（仅本机）：{state.admin_url()}",
        f"  证书指纹 SHA-256：{state.certs.cert_sha256[:47]}…",
        f"  H.264 编码器：{state.encoders[0] if state.encoders else '无（仅 JPEG）'}"
        f"{'（可选：' + ', '.join(state.encoders[1:]) + '）' if len(state.encoders) > 1 else ''}"
        f"   新设备需批准：{'是' if s.approval else '否'}",
    ]
    for warning in state.warnings:
        lines.append(f"  [!] {warning}")
    print("\n".join(lines), flush=True)
    try:
        print(qr_terminal(state.qr_url()), flush=True)
    except Exception:  # noqa: BLE001 - the QR is a convenience only
        pass
    print("  首次打开会提示证书不受信任，属正常现象，选择“继续访问”即可。按 Ctrl+C 退出。\n", flush=True)


async def _refresh_displays(state: AppState) -> None:
    """Notice monitors being plugged in or removed."""
    from .capture import list_displays

    if state.settings.capture == "synthetic":
        return
    while True:
        await asyncio.sleep(5.0)
        try:
            current = await asyncio.get_running_loop().run_in_executor(None, list_displays)
        except Exception:  # noqa: BLE001
            continue
        if current and [(d.id, d.width, d.height, d.x, d.y) for d in current] != \
                [(d.id, d.width, d.height, d.x, d.y) for d in state.displays]:
            state.displays = current
            state.event("info", f"显示器变化：现在有 {len(current)} 个")
            msg = json.dumps({"type": "displays", "displays": state.display_dicts()})
            for session in state.sessions.values():
                session.queue.put_control(msg)


async def _input_watchdog(state: AppState) -> None:
    """Release keys/buttons of viewers that stopped sending input (PROTOCOL §3.7)."""
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()
        for session in list(state.sessions.values()):
            ctl = session.controller
            if ctl is not None and ctl.watchdog(now):
                log.info("released stuck input of %s", session.viewer_id)


async def serve(settings: Settings, ready: asyncio.Future | None = None) -> None:
    """Run LanCast until cancelled. ``ready`` (optional) receives the AppState."""
    loop = asyncio.get_running_loop()
    addresses, displays, encoders, certs, warnings = await loop.run_in_executor(None, _discover, settings)
    state = AppState(settings, certs, addresses, displays, warnings, encoders)
    state.hub = StreamHub(settings, loop, on_event=state.event)
    try:
        from .input import BUTTONS, KEY_CODES, create_input_backend

        state.input_backend = create_input_backend(fake=settings.fake_input or settings.capture == "synthetic")
        state.key_codes, state.buttons = frozenset(KEY_CODES), tuple(BUTTONS)
    except Exception as exc:  # noqa: BLE001 - viewing still works without input injection
        log.warning("remote control unavailable: %s", exc)
        state.warnings.append(f"远程控制不可用：{exc}")

    runners: list[web.AppRunner] = []
    try:
        lan = web.AppRunner(build_app(state, local=False), access_log=None, handle_signals=False, shutdown_timeout=1.0)
        local = web.AppRunner(build_app(state, local=True), access_log=None, handle_signals=False, shutdown_timeout=1.0)
        for runner in (lan, local):
            await runner.setup()
            runners.append(runner)
        try:
            await web.TCPSite(lan, settings.bind, settings.port, ssl_context=make_ssl_context(certs)).start()
            await web.TCPSite(local, "127.0.0.1", settings.admin_port).start()
        except OSError as exc:
            raise SystemExit(f"无法监听端口 {settings.port}/{settings.admin_port}：{exc}（端口被占用？可用 --port/--admin-port 更换）")

        print_banner(state)
        state.event("info", f"服务已启动：{state.lan_url(state.primary_ip)}")
        if settings.open_admin:
            try:
                webbrowser.open(state.admin_url())
            except Exception:  # noqa: BLE001
                pass
        if ready is not None and not ready.done():
            ready.set_result(state)
        refresher = asyncio.ensure_future(_refresh_displays(state))
        watchdog = asyncio.ensure_future(_input_watchdog(state))
        try:
            await asyncio.Event().wait()
        finally:
            refresher.cancel()
            watchdog.cancel()
    finally:
        state.shutting_down = True
        for session in list(state.sessions.values()):
            session._release_input()
            session.close(4010, "shutdown", bye="shutdown")
        await asyncio.sleep(0.2)
        threads = state.hub.stop_all() if state.hub else []
        for runner in runners:
            await runner.cleanup()
        await loop.run_in_executor(None, lambda: [t.join(2.0) for t in threads if t.is_alive()])
