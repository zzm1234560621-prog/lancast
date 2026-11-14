"""Capture backend benchmark.

Runs continuous one-shot grabs for a few seconds on DXGI and MSS and reports
frame yield, per-grab latency split by ``frame`` / ``None`` results, and
process CPU usage.  The desktop is often static, so a low number of non-None
frames is normal (``None`` means "no new frame", not "failure").

Usage (from the repository root)::

    .venv\\Scripts\\python.exe -m lancast.capture.bench
    .venv\\Scripts\\python.exe -m lancast.capture.bench --seconds 5 --display 0
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

import psutil

from . import CaptureError, open_capture


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[index]


def _latency_line(label: str, values: list[float]) -> str:
    if not values:
        return f"  {label:<18} n=0"
    return (
        f"  {label:<18} n={len(values):<5} "
        f"mean={statistics.mean(values):7.3f} ms  "
        f"median={statistics.median(values):7.3f} ms  "
        f"p95={_p95(values):7.3f} ms"
    )


def run_backend(display_index: int, backend: str, seconds: float) -> bool:
    process = psutil.Process()
    print(f"\n=== backend={backend} display={display_index} duration={seconds:.2f}s ===")

    opened = time.perf_counter()
    try:
        source = open_capture(display_index, backend)
    except CaptureError as exc:
        print(f"  unavailable: {exc}")
        return False
    except Exception as exc:  # pragma: no cover - unexpected backend failure
        print(f"  failed to open: {type(exc).__name__}: {exc}")
        return False
    open_ms = (time.perf_counter() - opened) * 1000.0

    display = source.display
    print(
        f"  resolution: {display.width}x{display.height} "
        f"(x={display.x}, y={display.y}, id={display.id})"
    )
    print(f"  open time: {open_ms:.1f} ms")

    frames: list[float] = []
    nones: list[float] = []
    process.cpu_percent(None)  # prime the interval counter
    started = time.perf_counter()
    try:
        while True:
            now = time.perf_counter()
            if now - started >= seconds:
                break
            begin = time.perf_counter()
            frame = source.grab()
            elapsed_ms = (time.perf_counter() - begin) * 1000.0
            if frame is None:
                nones.append(elapsed_ms)
            else:
                frames.append(elapsed_ms)
    finally:
        source.close()
    wall = time.perf_counter() - started
    cpu_percent = process.cpu_percent(None)

    total = len(frames) + len(nones)
    print(f"  grabs: {total} in {wall:.2f}s")
    print(
        f"  non-None frames: {len(frames)} "
        f"({len(frames) / wall:.2f} fps), None: {len(nones)}"
    )
    print(_latency_line("frame grab:", frames))
    print(_latency_line("None grab:", nones))
    print(f"  process CPU: {cpu_percent:.1f}% (psutil, whole process)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        action="append",
        choices=("dxgi", "mss"),
        dest="backends",
        help="backend to benchmark (repeatable; default: dxgi and mss)",
    )
    parser.add_argument("--display", type=int, default=0, help="display index")
    parser.add_argument(
        "--seconds", type=float, default=3.0, help="duration per backend"
    )
    args = parser.parse_args(argv)

    backends = args.backends or ["dxgi", "mss"]
    if args.seconds <= 0:
        parser.error("--seconds must be positive")

    print(
        f"LanCast capture benchmark: {args.seconds:.1f}s per backend, "
        f"display {args.display}"
    )
    ok = True
    for backend in backends:
        try:
            ok = run_backend(args.display, backend, args.seconds) and ok
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
    print(
        "\nNote: a None grab means 'no new desktop frame / unchanged image', "
        "not an error."
    )
    print(
        "CPU is whole-process usage over the run; this tight one-shot loop "
        "spins when grabs return None quickly."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
