"""Pick the fastest available H.264 encoder on this machine.

The first start on a new PC benchmarks every working backend with a synthetic
moving desktop and caches the result in ``<data-dir>/autotune.json``. Later
starts reuse the cached choice; ``lancast serve --retune`` forces a new run.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from pathlib import Path

import numpy as np

from . import fit_size, probe_h264_encoders
from . import create_encoder

log = logging.getLogger("lancast.autotune")

CACHE_FILE = "autotune.json"
CACHE_VERSION = 1


def _bench_frame(width: int, height: int) -> np.ndarray:
    """A desktop-like BGRA frame that is cheap to mutate between encodes."""
    frame = np.empty((height, width, 4), dtype=np.uint8)
    frame[..., 0] = 28
    frame[..., 1] = 32
    frame[..., 2] = 40
    frame[..., 3] = 255
    # Small toolbar-ish band and one bright moving block.
    frame[0:min(80, height), :, 0] = 48
    frame[0:min(80, height), :, 1] = 50
    frame[0:min(80, height), :, 2] = 56
    return frame


def _draw_motion(frame: np.ndarray, step: int) -> None:
    height, width = frame.shape[:2]
    size = max(32, min(width, height) // 6)
    x = (step * 17) % max(1, width - size)
    y = (step * 11) % max(1, height - size)
    frame[y:y + size, x:x + size, 0] = 230
    frame[y:y + size, x:x + size, 1] = 235
    frame[y:y + size, x:x + size, 2] = 245


def benchmark_backends(
    backends: list[str],
    source_width: int,
    source_height: int,
    out_width: int,
    out_height: int,
    fps: int,
    bitrate_kbps: int,
    frames: int = 12,
    warmup: int = 3,
) -> list[dict]:
    """Return ``[{backend, median_ms, fps, error}]`` sorted fastest first."""
    results: list[dict] = []
    frame = _bench_frame(source_width, source_height)
    for index, backend in enumerate(backends):
        encoder = None
        try:
            encoder = create_encoder(
                "h264", source_width, source_height, out_width, out_height,
                fps, bitrate_kbps, backend=backend,
            )
            for warm in range(warmup):
                _draw_motion(frame, warm)
                encoder.encode(frame, warm * 1_000_000 // max(1, fps))
            times: list[float] = []
            for step in range(frames):
                _draw_motion(frame, warmup + step)
                started = time.perf_counter()
                encoder.encode(frame, (warmup + step) * 1_000_000 // max(1, fps))
                times.append((time.perf_counter() - started) * 1000.0)
            if not times:
                continue
            median_ms = statistics.median(times)
            results.append({
                "backend": backend,
                "median_ms": round(median_ms, 2),
                "fps": round(1000.0 / median_ms, 1) if median_ms > 0 else 0.0,
                "error": None,
            })
        except Exception as exc:  # noqa: BLE001 - a broken backend must not stop startup
            results.append({
                "backend": backend,
                "median_ms": None,
                "fps": 0.0,
                "error": str(exc)[:200],
            })
        finally:
            if encoder is not None:
                try:
                    encoder.close()
                except Exception:  # noqa: BLE001
                    pass
    results.sort(key=lambda item: (
        item["median_ms"] is None,
        item["median_ms"] if item["median_ms"] is not None else 1e9,
        item["backend"],
    ))
    return results


def _cache_path(data_dir) -> Path:
    return Path(data_dir) / CACHE_FILE


def _cache_key(settings, source: tuple[int, int], out: tuple[int, int]) -> dict:
    return {
        "version": CACHE_VERSION,
        "source": list(source),
        "output": list(out),
        "available": sorted(probe_h264_encoders()),
        "fps": int(settings.fps),
        "bitrate_kbps": int(settings.bitrate_kbps),
    }


def _load_cache(settings, key: dict) -> dict | None:
    try:
        raw = json.loads(_cache_path(settings.data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    for field in ("version", "source", "output", "available", "fps", "bitrate_kbps"):
        if raw.get(field) != key[field]:
            return None
    if raw.get("chosen") not in key["available"]:
        return None
    return raw


def _save_cache(settings, key: dict, chosen: str, results: list[dict]) -> None:
    payload = dict(key)
    payload["chosen"] = chosen
    payload["results"] = results
    path = _cache_path(settings.data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def choose_encoder(settings, display, working: list[str], force: bool = False) -> tuple[str | None, dict | None]:
    """Return the backend to use and a report, or ``(None, None)`` if no H.264."""
    if settings.encoder and settings.encoder != "auto":
        return settings.encoder, None
    if not working:
        return None, None

    source = (
        max(320, int(getattr(display, "width", 0) or 1920)),
        max(240, int(getattr(display, "height", 0) or 1080)),
    )
    out_width, out_height = fit_size(
        source[0], source[1],
        min(1920, settings.max_width),
        min(1080, settings.max_height),
    )
    key = _cache_key(settings, source, (out_width, out_height))
    if not force:
        cached = _load_cache(settings, key)
        if cached:
            log.info(
                "自动测速：沿用上次选择 %s（缓存）", cached["chosen"],
            )
            return cached["chosen"], cached
    report = {
        "source": list(source),
        "output": [out_width, out_height],
        "results": benchmark_backends(
            working, source[0], source[1], out_width, out_height,
            max(1, min(240, settings.fps)), settings.bitrate_kbps,
        ),
    }
    valid = [item for item in report["results"] if item["median_ms"] is not None]
    chosen = valid[0]["backend"] if valid else working[0]
    report["chosen"] = chosen
    report["chosen_ms"] = valid[0]["median_ms"] if valid else None
    _save_cache(settings, key, chosen, report["results"])
    parts = ", ".join(
        f"{item['backend']}={item['median_ms']}ms"
        for item in valid[:6]
    )
    log.info("自动测速：选择 %s（%s；%dx%d -> %dx%d）",
             chosen, parts or "无可用结果", source[0], source[1], out_width, out_height)
    return chosen, report
