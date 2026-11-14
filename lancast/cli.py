"""Command line entry point: ``python -m lancast`` or ``lancast``."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import platform
import re
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from . import APP_NAME, __version__
from .config import Settings, default_data_dir

RES_PRESETS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k": (3840, 2160),
    "native": (7680, 4320),
}


def _bitrate(text: str) -> int:
    """'10M', '8000k' or '8000' (kbps) -> kbps."""
    t = text.strip().lower().removesuffix("bps")
    mult = 1.0
    if t.endswith("m"):
        mult, t = 1000.0, t[:-1]
    elif t.endswith("k"):
        t = t[:-1]
    try:
        kbps = float(t) * mult
    except ValueError:
        raise argparse.ArgumentTypeError(f"无法识别的码率：{text}") from None
    if not 500 <= kbps <= 100_000:
        raise argparse.ArgumentTypeError("码率需在 500k 到 100M 之间")
    return int(kbps)


def _resolution(text: str) -> tuple[int, int]:
    t = text.strip().lower()
    if t in RES_PRESETS:
        return RES_PRESETS[t]
    m = re.fullmatch(r"(\d{3,4})\s*[x×*]\s*(\d{3,4})", t)
    if not m:
        raise argparse.ArgumentTypeError("分辨率写成 1920x1080，或 720p/1080p/1440p/4k/native")
    return int(m.group(1)), int(m.group(2))


def _port(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("端口需在 1–65535 之间")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lancast",
        description=f"{APP_NAME} —— 把电脑屏幕低延迟、加密地投到局域网里的手机/平板浏览器。",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="启动投屏服务（默认命令）")
    s.add_argument("--port", type=_port, default=8443, help="局域网 HTTPS 端口（默认 8443）")
    s.add_argument("--admin-port", type=_port, default=8442, help="本机管理页面端口（默认 8442）")
    s.add_argument("--bind", default="0.0.0.0", help="监听地址（默认所有网卡；来源仍限私网地址）")
    # These five also live in the admin page. default=None means "not given on the
    # command line", so a value saved from the admin page may take over (SERVE_DEFAULTS).
    s.add_argument("--fps", type=int, default=None, help="H.264 帧率上限（默认 60）")
    s.add_argument("--max-res", type=_resolution, default=None,
                   help="输出分辨率上限，如 1920x1080、1440p、native（默认 1080p）")
    s.add_argument("--bitrate", type=_bitrate, default=None, help="H.264 码率，如 10M、6000k（默认 10M）")
    s.add_argument("--jpeg-quality", type=int, default=None, help="兼容模式 JPEG 质量 20–95（默认 70）")
    s.add_argument("--jpeg-fps", type=int, default=None, help="兼容模式帧率（默认 15）")
    s.add_argument("--encoder", default="auto", help="H.264 编码器：auto、h264_qsv、libx264 …")
    s.add_argument("--retune", action="store_true", help="启动时重新测速，重新选择最优 H.264 编码器")
    s.add_argument("--capture", default="auto", choices=["auto", "dxgi", "mss", "synthetic"],
                   help="采集方式（synthetic 为测试图案）")
    s.add_argument("--no-approval", action="store_true", help="新设备配对成功后无需在电脑上点允许")
    s.add_argument("--no-control", action="store_true",
                   help="启动时关闭远程控制总开关（之后可在管理页打开）")
    s.add_argument("--fake-input", action="store_true",
                   help="测试用：远程输入只记录不执行（--capture synthetic 时自动启用）")
    s.add_argument("--max-viewers", type=int, default=8, help="同时观看的设备上限（默认 8）")
    s.add_argument("--allow-cidr", action="append", default=[], metavar="CIDR",
                   help="额外允许的来源网段，如 100.64.0.0/10（Tailscale）；可重复")
    s.add_argument("--pin-ttl", type=int, default=600, help="配对码有效期，秒（默认 600）")
    s.add_argument("--no-open", action="store_true", help="启动时不自动打开管理页面")
    s.add_argument("--data-dir", type=Path, default=default_data_dir(), help="证书与设备列表的存放目录")
    s.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    d = sub.add_parser("doctor", help="检查本机环境：显示器、采集、编码器、网络、证书")
    d.add_argument("--port", type=_port, default=8443)
    d.add_argument("--admin-port", type=_port, default=8442)
    d.add_argument("--capture", default="auto", choices=["auto", "dxgi", "mss", "synthetic"])
    d.add_argument("--data-dir", type=Path, default=default_data_dir())

    v = sub.add_parser("devices", help="查看或撤销已记住的设备")
    v.add_argument("--revoke", metavar="DEVICE_ID", help="撤销一个设备")
    v.add_argument("--revoke-all", action="store_true", help="撤销全部设备")
    v.add_argument("--data-dir", type=Path, default=default_data_dir())
    return parser


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


SERVE_DEFAULTS = {"fps": 60, "max_res": (1920, 1080), "bitrate": 10_000, "jpeg_quality": 70, "jpeg_fps": 15}


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Settings for ``serve``: explicit command-line values beat values saved from the
    admin page, which in turn beat the built-in defaults."""

    def given(name: str) -> bool:
        return getattr(args, name, None) is not None

    def value(name: str):
        return getattr(args, name) if given(name) else SERVE_DEFAULTS[name]

    max_res = value("max_res")
    settings = Settings(
        port=args.port,
        admin_port=args.admin_port,
        bind=args.bind,
        fps=max(1, min(240, value("fps"))),
        max_width=max_res[0],
        max_height=max_res[1],
        bitrate_kbps=value("bitrate"),
        jpeg_quality=max(20, min(95, value("jpeg_quality"))),
        jpeg_fps=max(1, min(60, value("jpeg_fps"))),
        encoder=args.encoder,
        retune=args.retune,
        capture=args.capture,
        approval=not args.no_approval,
        control_enabled=not args.no_control,
        fake_input=args.fake_input,
        max_viewers=max(1, args.max_viewers),
        allow_cidrs=list(args.allow_cidr),
        pin_ttl_s=max(60, args.pin_ttl),
        data_dir=args.data_dir,
        open_admin=not args.no_open,
    )
    skip: set[str] = set()
    if given("fps"):
        skip.add("fps")
    if given("max_res"):
        skip.update(("max_width", "max_height"))
    if given("bitrate"):
        skip.add("bitrate_kbps")
    if given("jpeg_quality"):
        skip.add("jpeg_quality")
    if given("jpeg_fps"):
        skip.add("jpeg_fps")
    if args.no_approval:
        skip.add("approval")
    if args.no_control:
        skip.add("control_enabled")
    settings.load_live(skip)
    return settings


def _serve(args: argparse.Namespace) -> int:
    from .net import parse_cidrs
    from .server import serve

    try:
        parse_cidrs(args.allow_cidr)
    except ValueError as exc:
        print(f"--allow-cidr 无效：{exc}", file=sys.stderr)
        return 2
    settings = settings_from_args(args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("aiohttp").setLevel(logging.WARNING)
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


def _check(ok: bool, text: str) -> None:
    print(f"  [{'ok' if ok else ' x'}] {text}")


def _doctor(args: argparse.Namespace) -> int:
    print(f"{APP_NAME} {__version__} 环境检查")
    print(f"  Python {platform.python_version()} · {platform.platform()}")
    problems = 0

    print("\n依赖")
    for mod in ("aiohttp", "av", "cv2", "numpy", "cryptography", "dxcam", "mss", "qrcode", "psutil"):
        try:
            m = importlib.import_module(mod)
            _check(True, f"{mod} {getattr(m, '__version__', '')}")
        except Exception as exc:  # noqa: BLE001
            problems += 1
            _check(False, f"{mod}: {exc}")

    print("\n显示器与采集")
    from .capture import list_displays, open_capture

    displays = list_displays()
    for d in displays:
        print(f"  显示器 {d.index + 1}: {d.width}x{d.height} @ ({d.x},{d.y}){' 主屏' if d.primary else ''}  {d.id}")
    if not displays:
        problems += 1
        _check(False, "没有检测到显示器")
    else:
        try:
            src = open_capture(0, args.capture)
            start = time.perf_counter()
            img = None
            while img is None and time.perf_counter() - start < 2.0:
                t0 = time.perf_counter()
                img = src.grab()
                grab_ms = (time.perf_counter() - t0) * 1000
                if img is None:
                    time.sleep(0.02)
            src.close()
            if img is None:
                _check(True, f"采集后端 {src.backend} 可用（2 秒内屏幕没有变化，未取到新帧）")
            else:
                _check(True, f"采集后端 {src.backend}：{img.shape[1]}x{img.shape[0]}，取一帧 {grab_ms:.1f} ms")
        except Exception as exc:  # noqa: BLE001
            problems += 1
            _check(False, f"采集失败：{exc}")

    print("\nH.264 编码器")
    from .capture import open_synthetic
    from .encode import create_encoder, fit_size, probe_h264_encoders

    encoders = probe_h264_encoders()
    if not encoders:
        problems += 1
        _check(False, "没有可用的 H.264 编码器（只能用 JPEG 兼容模式）")
    src_w, src_h = (displays[0].width, displays[0].height) if displays else (1920, 1080)
    out_w, out_h = fit_size(src_w, src_h, 1920, 1080)
    synth = open_synthetic(src_w, src_h, 1000)
    frames = []
    while len(frames) < 30:
        f = synth.grab()
        if f is not None:
            frames.append(f)
    for name in encoders:
        try:
            enc = create_encoder("h264", src_w, src_h, out_w, out_h, 60, 10_000, backend=name)
            times = []
            for i, f in enumerate(frames):
                t0 = time.perf_counter()
                enc.encode(f, i * 16_667, force_keyframe=(i == 0))
                times.append((time.perf_counter() - t0) * 1000)
            enc.close()
            times.sort()
            _check(True, f"{name}: {src_w}x{src_h}→{out_w}x{out_h} 每帧（缩放+编码）中位 {times[len(times) // 2]:.1f} ms")
        except Exception as exc:  # noqa: BLE001
            _check(False, f"{name}: {exc}")

    print("\n网络")
    from .net import lan_addresses, primary_address, windows_network_report

    addresses = lan_addresses()
    primary = primary_address()
    for a in addresses:
        mark = "  ← 二维码使用此地址" if a.ip == primary else ""
        print(f"  {a.ip:<16} {a.kind:<8} {a.interface}{mark}")
    for warning in windows_network_report(args.port):
        problems += 1
        print(f"  [!] {warning}")
    for port in (args.port, args.admin_port):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1" if port == args.admin_port else "0.0.0.0", port))
                _check(True, f"端口 {port} 空闲")
            except OSError:
                problems += 1
                _check(False, f"端口 {port} 已被占用（换一个：--port / --admin-port）")

    print("\n证书")
    from .certs import ensure_certificates

    try:
        bundle = ensure_certificates(args.data_dir, [a.ip for a in addresses])
        _check(True, f"服务器证书 SHA-256 {bundle.cert_sha256}")
        _check(True, f"本地 CA    SHA-256 {bundle.ca_sha256}")
        print(f"  覆盖地址：{', '.join(bundle.sans)}")
        print(f"  存放目录：{args.data_dir}")
    except Exception as exc:  # noqa: BLE001
        problems += 1
        _check(False, f"证书生成失败：{exc}")

    print("\n结论：" + ("一切正常。" if problems == 0 else f"有 {problems} 项需要注意（见上方 [x] / [!]）。"))
    return 0 if problems == 0 else 1


def _devices(args: argparse.Namespace) -> int:
    from .auth import DeviceStore

    store = DeviceStore(args.data_dir / "devices.json")
    if args.revoke_all:
        for dev in store.list():
            store.revoke(dev.device_id)
        print("已撤销全部设备。")
        return 0
    if args.revoke:
        if store.revoke(args.revoke):
            print(f"已撤销 {args.revoke}（若服务正在运行，请在管理页面撤销以立即断开它）。")
            return 0
        print(f"没有找到设备 {args.revoke}", file=sys.stderr)
        return 1
    devices = store.list()
    if not devices:
        print("没有已记住的设备。")
        return 0
    for dev in devices:
        seen = datetime.fromtimestamp(dev.last_seen_at).strftime("%Y-%m-%d %H:%M")
        print(f"{dev.device_id}  {dev.name:<20}  最近 {seen}  {dev.last_ip}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "serve")
    args = build_parser().parse_args(argv)
    _setup_console()
    if args.cmd == "doctor":
        return _doctor(args)
    if args.cmd == "devices":
        return _devices(args)
    return _serve(args)


if __name__ == "__main__":
    sys.exit(main())
