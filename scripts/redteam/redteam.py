#!/usr/bin/env python3
"""LanCast red-team / robustness probe.

Starts one or more LanCast servers on ephemeral loopback ports (synthetic
capture, temporary data directory), drives them with aiohttp and raw sockets,
and prints PASS/FAIL/INFO for every protocol, authentication, HTTP and TLS
check.  Everything touches only 127.0.0.1 and only servers started here.

    .venv\\Scripts\\python.exe scripts/redteam/redteam.py

Exit status: 0 when no check failed, 1 when at least one check failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import aiohttp
from aiohttp import WSMsgType
from yarl import URL

ROOT = Path(__file__).resolve().parents[2]
CLIENT_MSG_LIMIT = 8 * 1024
_AUTO_ORIGIN = object()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


# --------------------------------------------------------------------------- reporting

@dataclass
class Result:
    cid: str
    title: str
    status: str
    evidence: str


class Checker:
    def __init__(self) -> None:
        self.results: list[Result] = []
        self._last = ""

    def add(self, cid: str, title: str, status: str, evidence: str) -> None:
        assert status in ("PASS", "FAIL", "INFO")
        evidence = " ".join(str(evidence).split())
        if len(evidence) > 900:
            evidence = evidence[:897] + "..."
        self.results.append(Result(cid, title, status, evidence))
        print(f"[{status}] {cid} {title}")
        print(f"       {evidence}", flush=True)

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "INFO": 0}
        for r in self.results:
            out[r.status] += 1
        return out


# --------------------------------------------------------------------------- server process

def free_port(avoid: Iterable[int] = ()) -> int:
    avoid = set(avoid)
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in avoid:
            return port
    raise RuntimeError("could not find a free port")


def port_is_closed(port: int, wait: float = 3.0) -> bool:
    """True once nothing accepts connections on 127.0.0.1:port.

    Windows needs a moment after TerminateProcess before the listening sockets
    disappear, so retry briefly instead of reporting a false leftover.
    """
    deadline = time.monotonic() + wait
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


class ServerProcess:
    """A LanCast child process plus its banner data and recent stdout."""

    def __init__(self, label: str, extra_args: list[str]) -> None:
        self.label = label
        self.extra_args = list(extra_args)
        self.tmp = tempfile.TemporaryDirectory(prefix=f"lancast-redteam-{label}-")
        self.data_dir = Path(self.tmp.name)
        self.port = free_port()
        self.admin_port = free_port({self.port})
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.pin: str | None = None
        self.admin_url: str | None = None
        self.admin_token: str | None = None
        self.ca_pem: str = ""
        self._cv = threading.Condition()
        self._eof = False
        self._reader: threading.Thread | None = None

    @property
    def lan_base(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    @property
    def admin_base(self) -> str:
        return f"http://127.0.0.1:{self.admin_port}"

    @property
    def ws_url(self) -> str:
        return f"{self.lan_base}/ws"

    def start(self, timeout: float = 90.0) -> "ServerProcess":
        cli = [
            "serve", "--capture", "synthetic", "--no-open", "--bind", "127.0.0.1",
            "--port", str(self.port), "--admin-port", str(self.admin_port),
            "--data-dir", str(self.data_dir), *self.extra_args,
        ]
        code = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            f"from lancast.cli import main; sys.exit(main({cli!r}))"
        )
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
        self._reader = threading.Thread(target=self._read_stdout, name=f"read-{self.label}", daemon=True)
        self._reader.start()

        deadline = time.monotonic() + timeout
        with self._cv:
            while self.pin is None or self.admin_token is None:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"{self.label}: server exited early\n{self.tail(30)}")
                if self._eof:
                    raise RuntimeError(f"{self.label}: server stdout ended before banner\n{self.tail(30)}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"{self.label}: no banner within {timeout:.0f}s\n{self.tail(30)}")
                self._cv.wait(min(0.25, remaining))
        ca_path = self.data_dir / "ca.crt.pem"
        self.ca_pem = ca_path.read_text(encoding="utf-8")
        print(
            f"--- server[{self.label}] pid={self.proc.pid} "
            f"lan={self.lan_base} admin={self.admin_base} data={self.data_dir}",
            flush=True,
        )
        return self

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for raw in self.proc.stdout:
                line = raw.rstrip("\r\n")
                with self._cv:
                    self.lines.append(line)
                    if self.pin is None:
                        m = re.search(r"PIN[：:]\s*(\d{3})\s+(\d{3})", line)
                        if m:
                            self.pin = m.group(1) + m.group(2)
                    if self.admin_token is None:
                        m = re.search(
                            r"(http://127\.0\.0\.1:(\d+)/admin#token=([A-Za-z0-9_\-]+))", line
                        )
                        if m:
                            self.admin_url = m.group(1)
                            self.admin_token = m.group(3)
                    self._cv.notify_all()
        except Exception as exc:  # pragma: no cover - pipe teardown
            with self._cv:
                self.lines.append(f"<stdout reader stopped: {exc}>")
        finally:
            with self._cv:
                self._eof = True
                self._cv.notify_all()

    def tail(self, n: int = 30) -> str:
        with self._cv:
            return "\n".join(self.lines[-n:])

    def stop(self) -> tuple[bool, str]:
        proc = self.proc
        if proc is None:
            return True, "no process"
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self._reader is not None:
            self._reader.join(timeout=3)
        with contextlib.suppress(Exception):
            if proc.stdout is not None:
                proc.stdout.close()
        gone = proc.poll() is not None
        ports_closed = port_is_closed(self.port) and port_is_closed(self.admin_port)
        with contextlib.suppress(Exception):
            self.tmp.cleanup()
        return gone and ports_closed, (
            f"pid={proc.pid} returncode={proc.poll()} ports_closed={ports_closed}"
        )


class Instance:
    """HTTP/WS clients bound to one running server."""

    def __init__(self, server: ServerProcess) -> None:
        self.srv = server
        self.ssl_ctx: ssl.SSLContext | None = None
        self.http: aiohttp.ClientSession | None = None
        self.admin: aiohttp.ClientSession | None = None
        self.plain: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "Instance":
        self.ssl_ctx = ssl.create_default_context(cadata=self.srv.ca_pem)
        timeout = aiohttp.ClientTimeout(total=30)
        self.http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=self.ssl_ctx, limit=0), timeout=timeout
        )
        self.admin = aiohttp.ClientSession(timeout=timeout)
        self.plain = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        sessions = [s for s in (self.http, self.admin, self.plain) if s is not None]
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)

    async def admin_state(self) -> dict:
        assert self.admin is not None
        r = await self.admin.get(
            f"{self.srv.admin_base}/admin/api/state",
            headers={"X-Admin-Token": self.srv.admin_token or ""},
        )
        text = await r.text()
        if r.status != 200:
            raise RuntimeError(f"admin state HTTP {r.status}: {text[:300]}")
        return json.loads(text)

    async def fresh_pin(self) -> str:
        return str((await self.admin_state())["pairing"]["pin"])

    async def healthz(self) -> tuple[int, str]:
        assert self.http is not None
        r = await self.http.get(f"{self.srv.lan_base}/healthz")
        return r.status, await r.text()

    async def ws_connect(
        self, origin: str | None | object = _AUTO_ORIGIN, timeout: float = 10.0
    ) -> aiohttp.ClientWebSocketResponse:
        """Open /ws.

        origin omitted (the default) means "the correct same-origin header";
        pass ``None`` explicitly to send no Origin header at all.
        """
        assert self.http is not None
        kwargs: dict[str, Any] = {
            "timeout": aiohttp.ClientWSTimeout(ws_close=timeout),
            "max_msg_size": 0,
            "autoping": True,
        }
        if origin is _AUTO_ORIGIN:
            kwargs["origin"] = f"https://127.0.0.1:{self.srv.port}"
        elif origin is not None:
            kwargs["origin"] = origin
        return await self.http.ws_connect(self.srv.ws_url, **kwargs)

    async def authenticate(self, name: str, remember: bool = False, timeout: float = 20.0):
        pin = await self.fresh_pin()
        ws = await self.ws_connect()
        try:
            await ws.send_json({
                "type": "auth", "method": "pin", "pin": pin, "name": name, "remember": remember,
            })
            reply = await recv_json(ws, timeout)
        except Exception:
            await safe_close(ws)
            raise
        if reply.get("type") != "auth_ok":
            await safe_close(ws)
            raise RuntimeError(f"auth did not succeed: {reply}")
        return ws, reply


async def safe_close(ws: aiohttp.ClientWebSocketResponse | None) -> None:
    if ws is None:
        return
    with contextlib.suppress(Exception):
        await ws.close()


# --------------------------------------------------------------------------- WS helpers

class WsClosed(Exception):
    def __init__(self, code: int | None, detail: str = "") -> None:
        super().__init__(f"websocket closed code={code} {detail}")
        self.code = code
        self.detail = detail


async def recv_json(ws: aiohttp.ClientWebSocketResponse, timeout: float = 10.0) -> dict:
    """Next JSON text message; binary frames are skipped."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError("timed out waiting for a JSON message")
        msg = await ws.receive(timeout=remaining)
        if msg.type == WSMsgType.TEXT:
            try:
                value = json.loads(msg.data)
            except ValueError:
                return {"type": "_non_json", "raw": str(msg.data)[:200]}
            return value if isinstance(value, dict) else {"type": "_non_object", "value": value}
        if msg.type == WSMsgType.BINARY:
            continue
        if msg.type == WSMsgType.CLOSE:
            raise WsClosed(msg.data if isinstance(msg.data, int) else None, str(msg.extra))
        if msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise WsClosed(ws.close_code, str(msg.extra))
        if msg.type == WSMsgType.ERROR:
            raise WsClosed(ws.close_code, repr(msg.data))


async def recv_until(
    ws: aiohttp.ClientWebSocketResponse,
    predicate: Callable[[dict], bool],
    timeout: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError("timed out waiting for matching message")
        msg = await recv_json(ws, remaining)
        if predicate(msg):
            return msg


async def drain_close(
    ws: aiohttp.ClientWebSocketResponse, timeout: float = 10.0
) -> tuple[int | None, list[str], int, str | None]:
    """Read until the socket closes; returns (close code, text frames, binary count, error)."""
    code: int | None = None
    texts: list[str] = []
    binary = 0
    error: str | None = None
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await ws.receive(timeout=remaining)
        except asyncio.TimeoutError:
            break
        except (aiohttp.ClientError, ConnectionError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
        if msg.type == WSMsgType.TEXT:
            texts.append(msg.data)
        elif msg.type == WSMsgType.BINARY:
            binary += 1
        elif msg.type == WSMsgType.CLOSE:
            code = msg.data if isinstance(msg.data, int) else None
            break
        elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING):
            break
        elif msg.type == WSMsgType.ERROR:
            error = repr(msg.data)
            break
    if code is None:
        code = ws.close_code
    return code, texts, binary, error


async def pump(
    ws: aiohttp.ClientWebSocketResponse, wait: float = 0.3
) -> tuple[bool, int | None, list[str], int]:
    """Consume whatever arrives for `wait` seconds; report whether the socket is still open."""
    code: int | None = None
    texts: list[str] = []
    binary = 0
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            msg = await ws.receive(timeout=max(0.01, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            return True, None, texts, binary
        except (aiohttp.ClientError, ConnectionError, OSError):
            return False, ws.close_code, texts, binary
        if msg.type == WSMsgType.TEXT:
            texts.append(msg.data)
        elif msg.type == WSMsgType.BINARY:
            binary += 1
        elif msg.type == WSMsgType.CLOSE:
            return False, msg.data if isinstance(msg.data, int) else None, texts, binary
        elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
            return False, ws.close_code, texts, binary
    return True, None, texts, binary


def auth_fail(texts: list[str]) -> dict | None:
    for text in texts:
        with contextlib.suppress(ValueError):
            data = json.loads(text)
            if isinstance(data, dict) and data.get("type") == "auth_fail":
                return data
    return None


async def attempt_auth(inst: Instance, payload: dict, timeout: float = 8.0):
    ws = await inst.ws_connect()
    try:
        await ws.send_json(payload)
        code, texts, binary, error = await drain_close(ws, timeout)
    finally:
        await safe_close(ws)
    return code, texts, binary, error, auth_fail(texts)


async def wait_for_media(ws: aiohttp.ClientWebSocketResponse, timeout: float = 25.0):
    """Wait for the first `stream` control message and first binary frame."""
    deadline = time.monotonic() + timeout
    stream: dict | None = None
    while time.monotonic() < deadline:
        msg = await ws.receive(timeout=max(0.1, deadline - time.monotonic()))
        if msg.type == WSMsgType.TEXT:
            data = json.loads(msg.data)
            if data.get("type") == "stream":
                stream = data
            elif data.get("type") == "error":
                raise RuntimeError(f"server error before media: {data}")
        elif msg.type == WSMsgType.BINARY:
            return stream, msg.data
        elif msg.type == WSMsgType.CLOSE:
            raise WsClosed(msg.data if isinstance(msg.data, int) else None, "before media")
        elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
            raise WsClosed(ws.close_code, "before media")
    raise asyncio.TimeoutError("no media frame arrived")


async def count_frames(ws: aiohttp.ClientWebSocketResponse, duration: float):
    end = time.monotonic() + duration
    count = 0
    code: int | None = None
    while time.monotonic() < end:
        try:
            msg = await ws.receive(timeout=max(0.01, end - time.monotonic()))
        except asyncio.TimeoutError:
            break
        except (aiohttp.ClientError, ConnectionError, OSError):
            code = ws.close_code
            break
        if msg.type == WSMsgType.BINARY:
            count += 1
        elif msg.type == WSMsgType.CLOSE:
            code = msg.data if isinstance(msg.data, int) else None
            break
        elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
            code = ws.close_code
            break
    return count, code


async def flood_pings(ws: aiohttp.ClientWebSocketResponse, duration: float):
    """Send `ping` as fast as possible for `duration` seconds (or until the socket dies)."""
    end = time.monotonic() + duration
    sent = 0
    errors = 0
    first_error: str | None = None
    while time.monotonic() < end:
        if ws.closed:
            await asyncio.sleep(0.005)
            continue
        try:
            await ws.send_json({"type": "ping", "t": sent})
            sent += 1
        except Exception as exc:  # noqa: BLE001 - expected once the server closes us
            errors += 1
            if first_error is None:
                first_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.002)
        if sent % 64 == 0:
            await asyncio.sleep(0)
    return sent, errors, first_error


# --------------------------------------------------------------------------- TLS helpers

def tls_probe(
    port: int,
    ca_pem: str,
    min_version: ssl.TLSVersion,
    max_version: ssl.TLSVersion,
    ciphers: str | None = None,
    timeout: float = 6.0,
) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            ctx = ssl.create_default_context(cadata=ca_pem)
            ctx.minimum_version = min_version
            ctx.maximum_version = max_version
            if ciphers is not None:
                ctx.set_ciphers(ciphers)
            cipher_count = len(ctx.get_ciphers())
            if ciphers is not None and cipher_count == 0:
                return {"ok": False, "error": "no usable client ciphers", "cipher_count": 0}
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname="127.0.0.1") as ssock:
                    cipher = ssock.cipher()
                    return {
                        "ok": True,
                        "version": ssock.version(),
                        "cipher": cipher[0] if cipher else None,
                        "bits": cipher[2] if cipher else None,
                        "alpn": ssock.selected_alpn_protocol(),
                        "cipher_count": cipher_count,
                    }
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "cipher_count": locals().get("cipher_count", 0),
            }


def local_legacy_capable(cert: Path, key: Path, version: ssl.TLSVersion) -> tuple[bool, str]:
    """Can this machine's OpenSSL still negotiate `version` with the LanCast cert?

    Used only to distinguish "server refuses TLS 1.0" from "local OpenSSL has
    TLS 1.0 disabled", which would make the client-side failure meaningless.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            sctx.minimum_version = version
            sctx.maximum_version = version
            sctx.load_cert_chain(str(cert), str(key))
            sctx.set_ciphers("ALL:@SECLEVEL=0")
        except Exception as exc:  # noqa: BLE001
            return False, f"local server context: {type(exc).__name__}: {exc}"

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(6.0)
        port = listener.getsockname()[1]
        server_error: list[str] = []

        def serve() -> None:
            try:
                conn, _ = listener.accept()
                with sctx.wrap_socket(conn, server_side=True) as ssock:
                    ssock.recv(1)
            except Exception as exc:  # noqa: BLE001
                server_error.append(f"{type(exc).__name__}: {exc}")
            finally:
                with contextlib.suppress(OSError):
                    listener.close()

        thread = threading.Thread(target=serve, name="tls-local-probe", daemon=True)
        thread.start()
        try:
            cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            cctx.check_hostname = False
            cctx.verify_mode = ssl.CERT_NONE
            cctx.minimum_version = version
            cctx.maximum_version = version
            cctx.set_ciphers("ALL:@SECLEVEL=0")
            with socket.create_connection(("127.0.0.1", port), timeout=6.0) as sock:
                with cctx.wrap_socket(sock, server_hostname="127.0.0.1") as ssock:
                    ssock.sendall(b"x")
            thread.join(timeout=2.0)
            return True, "local control handshake succeeded"
        except Exception as exc:  # noqa: BLE001
            thread.join(timeout=2.0)
            detail = f"local control failed: {type(exc).__name__}: {exc}"
            if server_error:
                detail += f" / server side: {server_error[0]}"
            return False, detail


# --------------------------------------------------------------------------- checks: protocol/auth

async def check_1_unauth(inst: Instance, ck: Checker) -> None:
    cases = [
        ("1a", "未认证发送 watch", "watch"),
        ("1b", "未认证发送 ping", "ping"),
        ("1c", "未认证发送二进制帧", "binary"),
    ]
    for cid, title, kind in cases:
        ws = None
        try:
            ws = await inst.ws_connect()
            if kind == "watch":
                await ws.send_json({"type": "watch", "display": 0, "codec": "h264"})
            elif kind == "ping":
                await ws.send_json({"type": "ping", "t": 1.0})
            else:
                await ws.send_bytes(b"\x00\x01\x02\x03" * 8)
            code, texts, binary, error = await drain_close(ws, 8.0)
            fail = auth_fail(texts)
            ok = code == 4001 and binary == 0
            evidence = (
                f"close_code={code} binary_frames={binary} "
                f"auth_fail={fail.get('reason') if fail else None} "
                f"text_frames={len(texts)} error={error}"
            )
            ck.add(cid, title + " → 关闭且无媒体", "PASS" if ok else "FAIL", evidence)
        except Exception as exc:  # noqa: BLE001
            ck.add(cid, title + " → 关闭且无媒体", "FAIL", f"harness error: {type(exc).__name__}: {exc}")
        finally:
            await safe_close(ws)


async def check_2_auth_timeout(inst: Instance, ck: Checker) -> None:
    ws = None
    started = time.monotonic()
    try:
        ws = await inst.ws_connect()
        code, texts, binary, error = await drain_close(ws, 16.0)
        elapsed = time.monotonic() - started
        ok = code == 4001 and 8.0 <= elapsed <= 14.0 and binary == 0
        ck.add(
            "2", "连上后不发送 auth → 约 10 秒关闭(4001)",
            "PASS" if ok else "FAIL",
            f"elapsed={elapsed:.1f}s close_code={code} binary_frames={binary} "
            f"text_frames={len(texts)} error={error}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("2", "认证超时", "FAIL", f"harness error: {type(exc).__name__}: {exc}")
    finally:
        await safe_close(ws)


async def check_3_oversize(inst: Instance, ck: Checker) -> None:
    ws = None
    try:
        ws = await inst.ws_connect()
        await ws.send_str("A" * (CLIENT_MSG_LIMIT + 1024))
        code, texts, binary, error = await drain_close(ws, 8.0)
        health_status, _ = await inst.healthz()
        alive = inst.srv.proc is not None and inst.srv.proc.poll() is None
        ok = code is not None and health_status == 200 and alive
        ck.add(
            "3", f"超大文本消息 (>{CLIENT_MSG_LIMIT} B) → 关闭且服务不崩",
            "PASS" if ok else "FAIL",
            f"close_code={code} healthz={health_status} server_alive={alive} "
            f"binary_frames={binary} error={error}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("3", "超大文本消息", "FAIL", f"harness error: {type(exc).__name__}: {exc}")
    finally:
        await safe_close(ws)


async def check_4_malformed(inst: Instance, ck: Checker) -> None:
    payloads = [
        ("4a", "畸形 JSON", "{not-json"),
        ("4b", "JSON 数组", "[1,2,3]"),
        ("4c", "深层嵌套 JSON (~4000 层)", "[" * 4000 + "]" * 4000),
        ("4d", "超长字符串字段 (~7.6 KiB)", '{"type":"stats","pad":"' + "A" * 7600 + '"}'),
    ]
    ws = None
    try:
        ws, _ = await inst.authenticate("malformed-probe")
        for cid, label, payload in payloads:
            if ws is None or ws.closed:
                ws, _ = await inst.authenticate("malformed-probe-2")
            assert len(payload.encode("utf-8")) <= CLIENT_MSG_LIMIT
            await ws.send_str(payload)
            alive, close_code, texts, binary = await pump(ws, 0.35)
            health_status, _ = await inst.healthz()
            server_alive = inst.srv.proc is not None and inst.srv.proc.poll() is None
            ok = health_status == 200 and server_alive
            note = "connection stayed open" if alive else f"connection closed code={close_code}"
            ck.add(
                cid, f"{label} → 服务不崩",
                "PASS" if ok else "FAIL",
                f"payload_bytes={len(payload.encode('utf-8'))} {note} "
                f"healthz={health_status} server_alive={server_alive}",
            )
            if not alive:
                ws = None
        try:
            ws2, _ = await inst.authenticate("post-malformed")
            await safe_close(ws2)
            new_auth = "auth_ok"
        except Exception as exc:  # noqa: BLE001
            new_auth = f"failed: {type(exc).__name__}: {exc}"
        health_status, _ = await inst.healthz()
        ok = new_auth == "auth_ok" and health_status == 200
        ck.add(
            "4e", "畸形输入后新的合法连接仍能认证",
            "PASS" if ok else "FAIL",
            f"new_auth={new_auth} healthz={health_status}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("4", "畸形输入健壮性", "FAIL", f"harness error: {type(exc).__name__}: {exc}")
    finally:
        await safe_close(ws)


async def check_6_forged_device(inst: Instance, ck: Checker) -> None:
    payload = {
        "type": "auth", "method": "device",
        "device_id": "d_ffffffffffff", "token": "forged-token-" + "x" * 32,
        "client": {"ua": "redteam"},
    }
    try:
        code, texts, binary, error, fail = await attempt_auth(inst, payload)
        reason = fail.get("reason") if fail else None
        ok = reason == "unknown_device" and code == 4003 and binary == 0
        ck.add(
            "6", "伪造 device_id/token → unknown_device",
            "PASS" if ok else "FAIL",
            f"reason={reason} close_code={code} binary_frames={binary} error={error}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("6", "伪造设备令牌", "FAIL", f"harness error: {type(exc).__name__}: {exc}")


async def check_7_flood(inst: Instance, ck: Checker) -> None:
    a = b = None
    try:
        a, _ = await inst.authenticate("flooder")
        b, _ = await inst.authenticate("bystander")
        await b.send_json({"type": "watch", "display": 0, "codec": "h264"})
        stream, first = await wait_for_media(b, timeout=30.0)
        close_task = asyncio.create_task(drain_close(a, 8.0))
        count_task = asyncio.create_task(count_frames(b, 2.0))
        started = time.monotonic()
        sent, errors, first_error = await flood_pings(a, 2.0)
        elapsed = time.monotonic() - started
        close_code, close_texts, close_binary, close_error = await close_task
        b_frames, b_close = await count_task
        pongs = 0
        for text in close_texts:
            with contextlib.suppress(ValueError):
                if json.loads(text).get("type") == "pong":
                    pongs += 1
        a_ok = close_code == 4001
        ck.add(
            "7a", "认证后 ping 洪泛 → 服务关闭该连接(4001)",
            "PASS" if a_ok else "FAIL",
            f"flood_window={elapsed:.2f}s sent={sent} send_errors={errors} "
            f"close_code={close_code} pongs_received={pongs} send_error={first_error} "
            f"close_error={close_error}",
        )
        b_ok = b_frames > 0 and b_close is None and not b.closed
        if b_ok:
            try:
                await b.send_json({"type": "ping", "t": 987.5})
                pong = await recv_until(b, lambda m: m.get("type") == "pong", 5.0)
                b_ok = pong.get("t") == 987.5
            except Exception:  # noqa: BLE001
                b_ok = False
        ck.add(
            "7b", "洪泛期间另一个已认证 watch 连接不受影响",
            "PASS" if b_ok else "FAIL",
            f"bystander_frames={b_frames} close_code={b_close} alive={not b.closed} "
            f"stream={{{', '.join(f'{k}={stream.get(k)}' for k in ('codec', 'width', 'height', 'fps') if stream)}}}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("7", "认证后消息洪泛", "FAIL", f"harness error: {type(exc).__name__}: {exc}")
    finally:
        await safe_close(a)
        await safe_close(b)


async def check_8_bad_watch(inst: Instance, ck: Checker) -> None:
    cases = [
        ("8a", "display=-1", {"display": -1, "codec": "h264"}, "bad_display"),
        ("8b", "display=99", {"display": 99, "codec": "h264"}, "bad_display"),
        ("8c", 'display="0"', {"display": "0", "codec": "h264"}, "bad_display"),
        ("8d", "display=true", {"display": True, "codec": "h264"}, "bad_display"),
        ("8e", 'codec="hevc"', {"display": 0, "codec": "hevc"}, "bad_codec"),
    ]
    ws = None
    try:
        ws, _ = await inst.authenticate("bad-watch-probe")
        for cid, label, payload, expected in cases:
            await ws.send_json({"type": "watch", **payload})
            try:
                err = await recv_until(ws, lambda m: m.get("type") == "error", 5.0)
                await ws.send_json({"type": "ping", "t": 123.25})
                pong = await recv_until(ws, lambda m: m.get("type") == "pong", 5.0)
                ok = err.get("code") == expected and pong.get("t") == 123.25
                evidence = (
                    f"watch={json.dumps(payload)} error_code={err.get('code')} "
                    f"expected={expected} pong={pong.get('t')} usable={True}"
                )
            except Exception as exc:  # noqa: BLE001
                ok = False
                evidence = f"watch={json.dumps(payload)} failed after error: {type(exc).__name__}: {exc}"
            ck.add(cid, f"watch 非法参数 {label} → error 且连接可用", "PASS" if ok else "FAIL", evidence)
    except Exception as exc:  # noqa: BLE001
        ck.add("8", "watch 非法参数", "FAIL", f"harness error: {type(exc).__name__}: {exc}")
    finally:
        await safe_close(ws)


# --------------------------------------------------------------------------- checks: HTTP

async def check_10_origin(inst: Instance, ck: Checker) -> None:
    cases = [
        ("10a", "无 Origin", None),
        ("10b", "Origin=https://evil.example", "https://evil.example"),
        ("10c", "Origin scheme 错误 (http://127.0.0.1:<lan>)", f"http://127.0.0.1:{inst.srv.port}"),
    ]
    for cid, label, origin in cases:
        status: int | None = None
        detail = ""
        ws = None
        try:
            ws = await inst.ws_connect(origin=origin, timeout=8.0)
            status = 101
            detail = "upgrade accepted"
        except aiohttp.WSServerHandshakeError as exc:
            status = exc.status
            detail = exc.message or ""
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
        finally:
            await safe_close(ws)
        ok = status == 403
        ck.add(
            cid, f"WebSocket {label} → 握手 403",
            "PASS" if ok else "FAIL",
            f"http_status={status} detail={detail}",
        )


async def check_11_admin(inst: Instance, ck: Checker) -> None:
    assert inst.admin is not None and inst.http is not None
    state_url = f"{inst.srv.admin_base}/admin/api/state"
    rotate_url = f"{inst.srv.admin_base}/admin/api/pin/rotate"
    token = inst.srv.admin_token or ""

    async def get(url: str, headers: dict | None = None):
        r = await inst.admin.get(url, headers=headers or {})
        body = await r.text()
        return r.status, body

    try:
        status, body = await get(state_url)
        ck.add("11a", "管理 API 无令牌 → 401", "PASS" if status == 401 else "FAIL",
               f"status={status} body={body[:160]}")

        status, body = await get(state_url, {"X-Admin-Token": "wrong-token"})
        ck.add("11b", "管理 API 错误令牌 → 401", "PASS" if status == 401 else "FAIL",
               f"status={status} body={body[:160]}")

        status, body = await get(
            state_url,
            {"X-Admin-Token": token, "Host": f"evil.example:{inst.srv.admin_port}"},
        )
        ck.add("11c", "正确令牌但 Host=evil.example:<admin端口> → 403",
               "PASS" if status == 403 else "FAIL", f"status={status} body={body[:160]}")

        r = await inst.admin.post(
            rotate_url, headers={"X-Admin-Token": token}, data="{}",
        )
        body = await r.text()
        ck.add("11d", "表单 Content-Type 的 POST → 415", "PASS" if r.status == 415 else "FAIL",
               f"status={r.status} content_type=application/x-www-form-urlencoded body={body[:160]}")

        r = await inst.admin.post(
            rotate_url,
            headers={"X-Admin-Token": token, "Origin": "http://evil.example"},
            json={},
        )
        body = await r.text()
        ck.add("11e", "带 Origin: http://evil.example 的 JSON POST → 403",
               "PASS" if r.status == 403 else "FAIL", f"status={r.status} body={body[:160]}")

        r = await inst.http.get(f"{inst.srv.lan_base}/admin")
        body = await r.text()
        ck.add("11f", "LAN 端口 (https) 访问 /admin → 404", "PASS" if r.status == 404 else "FAIL",
               f"status={r.status} body={body[:160]}")

        r = await inst.http.get(
            f"{inst.srv.lan_base}/admin/api/state", headers={"X-Admin-Token": token}
        )
        body = await r.text()
        ck.add("11g", "LAN 端口 (https) 访问 /admin/api/state → 404",
               "PASS" if r.status == 404 else "FAIL", f"status={r.status} body={body[:160]}")
    except Exception as exc:  # noqa: BLE001
        ck.add("11", "管理 API 访问控制", "FAIL", f"harness error: {type(exc).__name__}: {exc}")


async def check_12_traversal(inst: Instance, ck: Checker) -> None:
    assert inst.http is not None and inst.admin is not None
    lan_paths = [
        ("12a", "/static/viewer/../server.py"),
        ("12b", "/static/viewer/%2e%2e%2fserver.py"),
        ("12c", "/static/viewer/..%5cserver.py"),
        ("12d", "/static/viewer/selftest.mjs"),
    ]
    markers = (b"from __future__", b"class AppState", b"def build_app", b"import aiohttp")
    for cid, path in lan_paths:
        try:
            url = URL(inst.srv.lan_base + path, encoded=True)
            r = await inst.http.get(url, timeout=aiohttp.ClientTimeout(total=8))
            body = await r.read()
            leaked = next((m.decode() for m in markers if m in body), None)
            ok = r.status in (403, 404) and leaked is None
            ck.add(
                cid, f"路径穿越 {path} → 404/403 且无文件内容",
                "PASS" if ok else "FAIL",
                f"status={r.status} bytes={len(body)} leaked_marker={leaked} "
                f"body={body[:100]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            ck.add(cid, f"路径穿越 {path}", "FAIL", f"harness error: {type(exc).__name__}: {exc}")

    try:
        url = URL(inst.srv.admin_base + "/static/admin/mock_server.py", encoded=True)
        r = await inst.admin.get(url, timeout=aiohttp.ClientTimeout(total=8))
        body = await r.read()
        leaked = next((m.decode() for m in markers if m in body), None)
        ok = r.status in (403, 404) and leaked is None
        ck.add(
            "12e", "本机端口 /static/admin/mock_server.py → 404/403 且无文件内容",
            "PASS" if ok else "FAIL",
            f"status={r.status} bytes={len(body)} leaked_marker={leaked} body={body[:100]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("12e", "本机端口静态测试文件", "FAIL", f"harness error: {type(exc).__name__}: {exc}")


async def check_13_headers(inst: Instance, ck: Checker) -> None:
    assert inst.http is not None and inst.admin is not None
    cases = [
        ("13a", "LAN /", inst.srv.lan_base + "/", False),
        ("13b", "LAN /static/viewer/app.js", inst.srv.lan_base + "/static/viewer/app.js", True),
        ("13c", "本机 /admin", inst.srv.admin_base + "/admin", False),
        ("13d", "LAN /ca.crt", inst.srv.lan_base + "/ca.crt", False),
    ]
    for cid, label, url, check_js_type in cases:
        try:
            client = inst.admin if "127.0.0.1" in url and url.startswith("http://") else inst.http
            r = await client.get(url, timeout=aiohttp.ClientTimeout(total=8))
            await r.read()
            csp = r.headers.get("Content-Security-Policy", "")
            xcto = r.headers.get("X-Content-Type-Options", "")
            referrer = r.headers.get("Referrer-Policy", "")
            ctype = r.headers.get("Content-Type", "")
            checks = {
                "csp": "frame-ancestors 'none'" in csp,
                "nosniff": xcto.lower() == "nosniff",
                "referrer": referrer.lower() == "no-referrer",
                "js_type": ("text/javascript" in ctype.lower()) if check_js_type else True,
            }
            ok = all(checks.values())
            ck.add(
                cid, f"响应头 {label}",
                "PASS" if ok else "FAIL",
                f"status={r.status} csp_frame_ancestors={checks['csp']} "
                f"x_content_type_options={xcto!r} referrer_policy={referrer!r} "
                f"content_type={ctype!r} js_type_ok={checks['js_type']}",
            )
        except Exception as exc:  # noqa: BLE001
            ck.add(cid, f"响应头 {label}", "FAIL", f"harness error: {type(exc).__name__}: {exc}")


async def check_14_tls(inst: Instance, ck: Checker) -> None:
    srv = inst.srv
    port = srv.port
    cert = srv.data_dir / "server.crt.pem"
    key = srv.data_dir / "server.key.pem"

    for cid, label, version in [
        ("14a", "TLS 1.0", ssl.TLSVersion.TLSv1),
        ("14b", "TLS 1.1", ssl.TLSVersion.TLSv1_1),
    ]:
        result = tls_probe(port, srv.ca_pem, version, version, ciphers="ALL:@SECLEVEL=0")
        if result["ok"]:
            ck.add(cid, f"{label} 握手", "FAIL",
                   f"服务端接受了旧协议: version={result.get('version')} cipher={result.get('cipher')}")
            continue
        local_ok, local_detail = local_legacy_capable(cert, key, version)
        if local_ok:
            ck.add(cid, f"{label} 握手失败（本机 OpenSSL 仍支持该版本）", "PASS",
                   f"server_error={result.get('error')} local_probe=ok")
        else:
            ck.add(cid, f"{label} 握手失败（本机 OpenSSL 已禁用旧版本）", "INFO",
                   f"server_error={result.get('error')} local_probe={local_detail}")

    for cid, label, version in [
        ("14c", "TLS 1.2", ssl.TLSVersion.TLSv1_2),
        ("14d", "TLS 1.3", ssl.TLSVersion.TLSv1_3),
    ]:
        result = tls_probe(port, srv.ca_pem, version, version)
        if result["ok"]:
            cipher = str(result.get("cipher") or "")
            pfs = result.get("version") == "TLSv1.3" or cipher.startswith(("ECDHE", "DHE"))
            ck.add(
                cid, f"{label} 握手成功且使用前向保密套件",
                "PASS" if pfs else "FAIL",
                f"version={result.get('version')} cipher={cipher} bits={result.get('bits')} "
                f"alpn={result.get('alpn')} forward_secret={pfs}",
            )
        else:
            ck.add(cid, f"{label} 握手", "FAIL", f"error={result.get('error')}")

    for cid, cipher in [
        ("14e", "AES128-SHA"),
        ("14f", "AES256-GCM-SHA384"),
    ]:
        result = tls_probe(
            port, srv.ca_pem, ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_2,
            ciphers=f"{cipher}:@SECLEVEL=0",
        )
        if result.get("cipher_count") == 0:
            ck.add(cid, f"TLS 1.2 非前向保密套件 {cipher}", "INFO",
                   f"本机 OpenSSL 无法启用该套件，无法测试: {result.get('error')}")
        elif result["ok"]:
            ck.add(cid, f"TLS 1.2 非前向保密套件 {cipher}", "FAIL",
                   f"服务端接受了非 PFS 套件: cipher={result.get('cipher')} version={result.get('version')}")
        else:
            ck.add(cid, f"TLS 1.2 非前向保密套件 {cipher} 被拒绝", "PASS",
                   f"error={result.get('error')} client_ciphers={result.get('cipher_count')}")


async def check_15_slowloris(inst: Instance, ck: Checker) -> None:
    writers: list[asyncio.StreamWriter] = []
    latencies: list[float] = []
    failures = 0
    try:
        partial = (
            f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{inst.srv.admin_port}\r\n"
            f"X-Partial-Header: "
        ).encode("ascii")
        for _ in range(50):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", inst.srv.admin_port)
                writer.write(partial)
                await writer.drain()
                writers.append(writer)
                del reader
            except Exception:  # noqa: BLE001
                failures += 1
        start = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while time.monotonic() - start < 10.0:
                t0 = time.perf_counter()
                try:
                    async with session.get(f"{inst.srv.admin_base}/healthz") as r:
                        await r.read()
                        if r.status == 200:
                            latencies.append((time.perf_counter() - t0) * 1000.0)
                        else:
                            failures += 1
                except Exception:  # noqa: BLE001
                    failures += 1
                await asyncio.sleep(0.35)
        if latencies:
            latencies.sort()
            p50 = statistics.median(latencies)
            p95 = latencies[int(len(latencies) * 0.95) - 1]
            stats = (
                f"min={latencies[0]:.1f}ms p50={p50:.1f}ms p95={p95:.1f}ms "
                f"max={latencies[-1]:.1f}ms samples={len(latencies)}"
            )
        else:
            stats = "no successful /healthz sample"
        ck.add(
            "15", "50 个半截请求头慢连接保持 10s 时的 /healthz 延迟（INFO）",
            "INFO",
            f"slow_connections={len(writers)} open_failures={failures} {stats} "
            f"server_alive={inst.srv.proc is not None and inst.srv.proc.poll() is None}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("15", "慢连接对合法请求的影响", "INFO",
               f"probe incomplete: {type(exc).__name__}: {exc} samples={len(latencies)}")
    finally:
        for writer in writers:
            with contextlib.suppress(Exception):
                writer.close()
        for writer in writers:
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# --------------------------------------------------------------------------- checks: dedicated instances

async def check_5_rate_limit(inst: Instance, ck: Checker) -> None:
    try:
        pin0 = await inst.fresh_pin()
        wrong = "000000" if pin0 != "000000" else "111111"
        reasons: list[str | None] = []
        codes: list[int | None] = []
        for _ in range(5):
            code, texts, binary, error, fail = await attempt_auth(
                inst, {"type": "auth", "method": "pin", "pin": wrong, "name": "brute"}
            )
            reasons.append(fail.get("reason") if fail else None)
            codes.append(code)
        pin1 = await inst.fresh_pin()
        rotated = pin1 != pin0
        ck.add(
            "5a", "5 次错误 PIN 后管理 API 显示 PIN 已轮换",
            "PASS" if rotated else "FAIL",
            f"pin_before={pin0} pin_after={pin1} rotated={rotated} "
            f"fail_reasons={reasons} close_codes={codes}",
        )

        code, texts, binary, error, fail = await attempt_auth(
            inst, {"type": "auth", "method": "pin", "pin": wrong, "name": "brute-6"}
        )
        reason = fail.get("reason") if fail else None
        retry = fail.get("retry_after_s") if fail else None
        ok = reason == "rate_limited" and code == 4008 and isinstance(retry, (int, float)) and retry > 0
        ck.add(
            "5b", "同一来源第 6 次错误 PIN → rate_limited (4008)",
            "PASS" if ok else "FAIL",
            f"reason={reason} retry_after_s={retry} close_code={code} error={error}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("5", "PIN 暴力与限速", "FAIL", f"harness error: {type(exc).__name__}: {exc}")


async def check_9_max_viewers(inst: Instance, ck: Checker) -> None:
    v1 = v2 = v3 = None
    try:
        v1, _ = await inst.authenticate("viewer-1")
        v2, _ = await inst.authenticate("viewer-2")
        pin = await inst.fresh_pin()
        v3 = await inst.ws_connect()
        await v3.send_json({"type": "auth", "method": "pin", "pin": pin, "name": "viewer-3"})
        code, texts, binary, error = await drain_close(v3, 8.0)
        fail = auth_fail(texts)
        reason = fail.get("reason") if fail else None
        ok = reason == "server_full" and code == 4009
        ck.add(
            "9a", "第 3 个认证连接 (max-viewers=2) → server_full / 4009",
            "PASS" if ok else "FAIL",
            f"reason={reason} close_code={code} binary_frames={binary} error={error}",
        )

        alive = True
        for viewer in (v1, v2):
            try:
                await viewer.send_json({"type": "ping", "t": 55.5})
                await recv_until(viewer, lambda m: m.get("type") == "pong", 4.0)
            except Exception:  # noqa: BLE001
                alive = False
        state = await inst.admin_state()
        viewers = len(state.get("viewers", []))
        ok2 = alive and viewers == 2
        ck.add(
            "9b", "并发上限触发后前两个观看者仍在线",
            "PASS" if ok2 else "FAIL",
            f"first_two_alive={alive} admin_viewers={viewers}",
        )
    except Exception as exc:  # noqa: BLE001
        ck.add("9", "并发上限", "FAIL", f"harness error: {type(exc).__name__}: {exc}")
    finally:
        await safe_close(v1)
        await safe_close(v2)
        await safe_close(v3)


# --------------------------------------------------------------------------- orchestration

async def run_instance(label: str, extra_args: list[str], body: Callable[[Instance, Checker], Any],
                       ck: Checker, stops: list[tuple[str, bool, str]]) -> None:
    server = ServerProcess(label, extra_args)
    try:
        server.start()
        async with Instance(server) as inst:
            await body(inst, ck)
    finally:
        ok, detail = server.stop()
        stops.append((label, ok, detail))
        print(f"--- stop server[{label}] {'OK' if ok else 'LEFTOVER?'} ({detail})", flush=True)


async def amain() -> int:
    print("LanCast red-team — 安全与健壮性检测")
    print(f"repo={ROOT}  python={sys.executable}  ssl={ssl.OPENSSL_VERSION}")
    print("所有目标均为本脚本启动的 127.0.0.1 临时实例；开始运行检查。\n", flush=True)
    ck = Checker()
    started = time.monotonic()
    stops: list[tuple[str, bool, str]] = []

    async def main_body(inst: Instance, c: Checker) -> None:
        for fn in (
            check_1_unauth,
            check_2_auth_timeout,
            check_3_oversize,
            check_4_malformed,
            check_7_flood,
            check_8_bad_watch,
            check_10_origin,
            check_11_admin,
            check_12_traversal,
            check_13_headers,
            check_14_tls,
            check_15_slowloris,
        ):
            await fn(inst, c)
        # Device forgery counts as one auth failure; run it last on this instance.
        await check_6_forged_device(inst, c)

    try:
        await run_instance("main", ["--no-approval", "--max-viewers", "8"], main_body, ck, stops)
        await run_instance("rate-limit", ["--no-approval"], check_5_rate_limit, ck, stops)
        await run_instance("max-viewers", ["--no-approval", "--max-viewers", "2"],
                           check_9_max_viewers, ck, stops)
    except Exception as exc:  # noqa: BLE001
        ck.add("harness", "检测环境/服务启动", "FAIL", f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()

    leftovers = [f"{label}:{detail}" for label, ok, detail in stops if not ok]
    ck.add(
        "cleanup", "清理：所有服务进程已停止且端口关闭",
        "PASS" if not leftovers else "FAIL",
        f"instances={len(stops)} " + (
            "; ".join(f"{label} {detail}" for label, _, detail in stops)
            if not leftovers else "leftovers=" + "; ".join(leftovers)
        ),
    )

    elapsed = time.monotonic() - started
    counts = ck.counts()
    print("\n" + "=" * 78)
    print(f"汇总: PASS={counts['PASS']}  FAIL={counts['FAIL']}  INFO={counts['INFO']}  "
          f"总计={len(ck.results)}  用时={elapsed:.1f}s")
    if counts["FAIL"]:
        print("失败项:")
        for result in ck.results:
            if result.status == "FAIL":
                print(f"  - [{result.cid}] {result.title}: {result.evidence}")
    else:
        print("没有 FAIL。")
    print("=" * 78)
    return 0 if counts["FAIL"] == 0 else 1


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
