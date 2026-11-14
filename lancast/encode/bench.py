"""LanCast encode benchmark.

Run from the repository root:

    .venv\\Scripts\\python.exe lancast/encode/bench.py

It measures every H.264 backend that :func:`lancast.encode.probe_h264_encoders`
reports as working, using the real screen size of this machine (2880x1800)
scaled to 1728x1080, then the JPEG fallback at quality 70.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import av
import cv2
import numpy as np
import psutil

# Make ``python lancast/encode/bench.py`` work without installing the package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lancast.encode import create_encoder, fit_size, probe_h264_encoders  # noqa: E402

SRC_W, SRC_H = 2880, 1800
OUT_W, OUT_H = fit_size(SRC_W, SRC_H, 1920, 1080)
FPS = 60
BITRATE_KBPS = 10000
FRAMES = 300
JPEG_QUALITY = 70


def _build_desktop() -> np.ndarray:
    """A static, desktop-like 2880x1800 BGRA background."""
    frame = np.empty((SRC_H, SRC_W, 4), dtype=np.uint8)
    # Dark blue-grey wallpaper with a subtle vertical gradient.
    frame[..., 0] = np.linspace(22, 42, SRC_H, dtype=np.uint8)[:, None]
    frame[..., 1] = np.linspace(28, 52, SRC_H, dtype=np.uint8)[:, None]
    frame[..., 2] = np.linspace(38, 70, SRC_H, dtype=np.uint8)[:, None]
    frame[..., 3] = 255

    cv2.rectangle(frame, (0, 0), (SRC_W - 1, 64), (48, 48, 52, 255), -1)
    cv2.putText(
        frame,
        "LanCast benchmark desktop",
        (36, 46),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.1,
        (235, 235, 235, 255),
        2,
        cv2.LINE_AA,
    )

    # Main application window.
    cv2.rectangle(frame, (150, 140), (2700, 1540), (238, 238, 242, 255), -1)
    cv2.rectangle(frame, (150, 140), (2700, 220), (58, 84, 140, 255), -1)
    cv2.putText(
        frame,
        "Document  -  LanCast",
        (190, 194),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.1,
        (255, 255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    for index, label in enumerate(("File", "Edit", "View", "Help")):
        cv2.putText(
            frame,
            label,
            (320 + index * 150, 194),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            (230, 230, 230, 255),
            2,
            cv2.LINE_AA,
        )

    # Sidebar with vivid colour swatches (simulates UI chrome / thumbnails).
    colours = (
        (60, 76, 231, 255),
        (80, 185, 240, 255),
        (90, 200, 120, 255),
        (210, 160, 60, 255),
        (180, 90, 200, 255),
    )
    for index, colour in enumerate(colours):
        top = 280 + index * 230
        cv2.rectangle(frame, (190, top), (560, top + 190), colour, -1)
        cv2.putText(
            frame,
            "item %d" % (index + 1),
            (220, top + 130),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (255, 255, 255, 255),
            2,
            cv2.LINE_AA,
        )

    # Text area: thin, high-frequency glyphs make the encoder work.
    for row in range(40):
        y = 300 + row * 30
        cv2.putText(
            frame,
            "The quick brown fox jumps over the lazy dog 0123456789  "
            "screen content %02d" % row,
            (640, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            (32, 34, 40, 255),
            1,
            cv2.LINE_AA,
        )

    # Taskbar.
    cv2.rectangle(frame, (0, SRC_H - 90), (SRC_W - 1, SRC_H - 1), (28, 30, 34, 255), -1)
    for index in range(12):
        left = 30 + index * 72
        cv2.rectangle(
            frame,
            (left, SRC_H - 78),
            (left + 52, SRC_H - 16),
            (70 + index * 7, 90, 150, 255),
            -1,
        )
    return frame


_DESKTOP = _build_desktop()


def _frame(index: int) -> np.ndarray:
    """Copy the desktop and add moving text, colour blocks and fine lines."""
    frame = _DESKTOP.copy()

    # Moving colour block.
    x = 640 + (index * 17) % 1900
    y = 320 + (index * 11) % 900
    colour = (40 + (index * 3) % 180, 80 + (index * 5) % 150, 210, 255)
    cv2.rectangle(frame, (x, y), (x + 300, y + 220), colour, -1)
    cv2.rectangle(
        frame, (x + 20, y + 20), (x + 280, y + 200), (255, 255, 255, 255), 2
    )

    # Moving text.
    text_x = 660 + (index * 9) % 1700
    cv2.putText(
        frame,
        "frame %04d  " % index + "moving desktop text 0123456789 " * 3,
        (text_x, 1300 + (index % 4) * 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (20, 30, 50, 255),
        2,
        cv2.LINE_AA,
    )

    # A scrolling sparkline of thin 1 px lines.
    points = np.empty((80, 2), dtype=np.int32)
    for column in range(80):
        points[column] = (
            700 + column * 24,
            SRC_H - 160 - int(
                60 + 40 * np.sin((column + index) * 0.35) + 8 * np.sin(index * 0.7)
            ),
        )
    cv2.polylines(frame, [points], False, (80, 220, 255, 255), 1, cv2.LINE_AA)

    # Small moving cursor-like marker.
    cx = (index * 23) % (SRC_W - 40)
    cy = (index * 13) % (SRC_H - 40)
    cv2.drawMarker(
        frame, (cx, cy), (255, 255, 255, 255), cv2.MARKER_CROSS, 18, 2
    )
    return frame


def _cpu(process: psutil.Process) -> float:
    times = process.cpu_times()
    return times.user + times.system


def _p95(values: list[float]) -> float:
    return float(np.percentile(values, 95)) if values else 0.0


def _report_h264(backend: str, frames: int) -> None:
    encoder = create_encoder(
        "h264",
        SRC_W,
        SRC_H,
        OUT_W,
        OUT_H,
        FPS,
        BITRATE_KBPS,
        backend=backend,
    )
    process = psutil.Process()
    convert_ms: list[float] = []
    encode_ms: list[float] = []
    total_bytes = 0
    idr_sizes: list[int] = []
    keyframes = 0
    cpu_used = 0.0
    wall_used = 0.0
    dropped = 0
    try:
        for index in range(frames):
            bgra = _frame(index)
            cpu_before = _cpu(process)
            started = time.perf_counter_ns()
            video_frame = encoder._prepare_frame(bgra)  # noqa: SLF001 - benchmark
            converted = time.perf_counter_ns()
            packet = encoder._encode_packets(  # noqa: SLF001 - benchmark
                video_frame, index * 1_000_000 // FPS
            )
            finished = time.perf_counter_ns()
            cpu_after = _cpu(process)

            convert_ms.append((converted - started) / 1e6)
            encode_ms.append((finished - converted) / 1e6)
            cpu_used += cpu_after - cpu_before
            wall_used += (finished - started) / 1e9
            if packet is None:
                dropped += 1
                continue
            total_bytes += len(packet.data)
            if packet.keyframe:
                keyframes += 1
                idr_sizes.append(len(packet.data))
    finally:
        encoder.close()

    duration = max(frames, 1) / float(FPS)
    bitrate = total_bytes * 8 / duration / 1000.0
    cpu_percent = 100.0 * cpu_used / wall_used if wall_used else 0.0
    idr_mean = float(np.mean(idr_sizes)) if idr_sizes else 0.0

    print("== H.264 backend: %s ==" % encoder.name)
    print(
        "   source %dx%d BGRA -> %dx%d, %d fps, %d kbps, %d frames"
        % (SRC_W, SRC_H, OUT_W, OUT_H, FPS, BITRATE_KBPS, frames)
    )
    print(
        "   scale+colour : mean %6.2f ms   p95 %6.2f ms"
        % (float(np.mean(convert_ms)), _p95(convert_ms))
    )
    print(
        "   encode       : mean %6.2f ms   p95 %6.2f ms"
        % (float(np.mean(encode_ms)), _p95(encode_ms))
    )
    print(
        "   bitrate      : %8.1f kbps average (%.2f MB total)"
        % (bitrate, total_bytes / 1e6)
    )
    print(
        "   IDR size     : %8.1f bytes average (%d keyframes)"
        % (idr_mean, keyframes)
    )
    print(
        "   process CPU  : %8.1f %% (during convert+encode; can exceed 100%%)"
        % cpu_percent
    )
    if dropped:
        print("   dropped      : %d frames" % dropped)
    print()


def _report_jpeg(frames: int) -> None:
    encoder = create_encoder(
        "jpeg",
        SRC_W,
        SRC_H,
        OUT_W,
        OUT_H,
        FPS,
        0,
        jpeg_quality=JPEG_QUALITY,
    )
    process = psutil.Process()
    times: list[float] = []
    sizes: list[int] = []
    cpu_used = 0.0
    wall_used = 0.0
    try:
        for index in range(frames):
            bgra = _frame(index)
            cpu_before = _cpu(process)
            started = time.perf_counter_ns()
            packet = encoder.encode(bgra, index * 1_000_000 // FPS)
            finished = time.perf_counter_ns()
            cpu_after = _cpu(process)
            times.append((finished - started) / 1e6)
            cpu_used += cpu_after - cpu_before
            wall_used += (finished - started) / 1e9
            if packet is not None:
                sizes.append(len(packet.data))
    finally:
        encoder.close()

    print("== JPEG fallback (quality %d) ==" % JPEG_QUALITY)
    print("   output %dx%d, %d frames" % (OUT_W, OUT_H, frames))
    print(
        "   frame time   : mean %6.2f ms   p95 %6.2f ms"
        % (float(np.mean(times)), _p95(times))
    )
    print(
        "   size         : %8.1f bytes average (%.2f MB total)"
        % (float(np.mean(sizes)) if sizes else 0.0, sum(sizes) / 1e6)
    )
    print(
        "   process CPU  : %8.1f %% (during scale+convert+JPEG; can exceed 100%%)"
        % (100.0 * cpu_used / wall_used if wall_used else 0.0)
    )
    print()


def main() -> int:
    print("LanCast encode benchmark")
    print(
        "Python %s, PyAV %s, OpenCV %s, numpy %s"
        % (
            sys.version.split()[0],
            av.__version__,
            cv2.__version__,
            np.__version__,
        )
    )
    print(
        "input %dx%d BGRA -> %dx%d, %d fps, %d kbps, %d frames per backend"
        % (SRC_W, SRC_H, OUT_W, OUT_H, FPS, BITRATE_KBPS, FRAMES)
    )
    working = probe_h264_encoders()
    print("working H.264 backends: %s" % (", ".join(working) or "(none)"))
    print()
    if not working:
        print("No working H.264 backend; only JPEG will be benchmarked.")

    for backend in working:
        _report_h264(backend, FRAMES)
    _report_jpeg(FRAMES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
