"""Capture -> encode -> fan-out.

Threads
-------
* one **capture thread** per display that has at least one subscribed output;
* one **encoder thread** per (display, codec) output;
* the **asyncio loop thread** does all socket I/O; fan-out runs there.

The capture thread publishes into a single-slot mailbox ("newest frame wins").
Encoder threads always encode the newest frame, so a slow encoder or a CPU
spike causes skipped frames, never a growing backlog: latency stays bounded.

Per-viewer queues apply the same rule to the network. A viewer that falls
behind drops its queued frames and waits for the next keyframe instead of
watching an ever older picture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import time
from base64 import b64encode
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

log = logging.getLogger("lancast.pipeline")

HEADER = struct.Struct("!BBHIII")      # kind, flags, epoch, seq, ts_hi, ts_lo  (docs/PROTOCOL.md §3.3)
KIND = {"h264": 1, "jpeg": 2}

# --------------------------------------------------------------------------- quality (PROTOCOL §3.6)

# preset: (max_width, max_height, fps, bitrate_kbps, jpeg_quality, jpeg_fps); "standard" = PC settings
QUALITY_PRESETS = {
    "saver": (960, 540, 30, 2_000, 50, 10),
    "smooth": (1280, 720, 60, 5_000, 60, 15),
    "high": (2560, 1440, 60, 20_000, 80, 20),
    "native": (7680, 4320, 60, 35_000, 85, 20),
}
CUSTOM_LIMITS = {
    "max_width": (320, 7680),
    "max_height": (240, 4320),
    "fps": (5, 240),
    "bitrate_kbps": (500, 200_000),
    "jpeg_quality": (20, 95),
}
MAX_OUTPUTS_PER_DISPLAY = 4


@dataclass(frozen=True)
class Quality:
    preset: str
    max_width: int
    max_height: int
    fps: int
    bitrate_kbps: int = 0     # H.264 only
    jpeg_quality: int = 0     # JPEG only

    def as_dict(self, codec: str) -> dict:
        d = {"preset": self.preset, "max_width": self.max_width, "max_height": self.max_height, "fps": self.fps}
        if codec == "h264":
            d["bitrate_kbps"] = self.bitrate_kbps
        else:
            d["jpeg_quality"] = self.jpeg_quality
        return d


def _clamped_int(value, name: str, default: int) -> int:
    lo, hi = CUSTOM_LIMITS[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        return default
    return int(min(max(int(value), lo), hi))


def resolve_quality(key: tuple, codec: str, settings) -> Quality:
    """Concrete values for a quality key (``standard`` follows the live PC settings)."""
    kind = key[0]
    if kind in QUALITY_PRESETS:
        w, h, fps, kbps, jq, jfps = QUALITY_PRESETS[kind]
        if codec == "h264":
            return Quality(kind, w, h, fps, bitrate_kbps=kbps)
        return Quality(kind, w, h, jfps, jpeg_quality=jq)
    if kind == "custom":
        _, w, h, fps, extra = key
        if codec == "h264":
            return Quality("custom", w, h, fps, bitrate_kbps=extra)
        return Quality("custom", w, h, fps, jpeg_quality=extra)
    s = settings
    if codec == "h264":
        return Quality("standard", s.max_width, s.max_height, s.fps, bitrate_kbps=s.bitrate_kbps)
    return Quality("standard", s.max_width, s.max_height, s.jpeg_fps, jpeg_quality=s.jpeg_quality)


def quality_key(request, codec: str, settings) -> tuple:
    """Normalise a viewer's ``watch.quality`` request. Equal keys share one encoder."""
    if not getattr(settings, "viewer_quality", True) or not isinstance(request, dict):
        return ("standard",)
    preset = request.get("preset")
    if preset in QUALITY_PRESETS:
        return (preset,)
    if preset != "custom":
        return ("standard",)
    std = resolve_quality(("standard",), codec, settings)
    w = _clamped_int(request.get("max_width"), "max_width", std.max_width)
    h = _clamped_int(request.get("max_height"), "max_height", std.max_height)
    fps = _clamped_int(request.get("fps"), "fps", std.fps)
    if codec == "h264":
        return ("custom", w, h, fps, _clamped_int(request.get("bitrate_kbps"), "bitrate_kbps", std.bitrate_kbps))
    return ("custom", w, h, fps, _clamped_int(request.get("jpeg_quality"), "jpeg_quality", std.jpeg_quality))


def _quality_distance(a: Quality, b: Quality) -> float:
    import math

    d = abs(math.log2(a.max_height / b.max_height)) + abs(math.log2(a.fps / b.fps))
    if a.bitrate_kbps and b.bitrate_kbps:
        d += abs(math.log2(a.bitrate_kbps / b.bitrate_kbps))
    if a.jpeg_quality and b.jpeg_quality:
        d += abs(a.jpeg_quality - b.jpeg_quality) / 50
    return d

CAPTURE_POLL_S = 0.004        # how often to look for a new desktop frame while idle
CURSOR_INTERVAL_S = 1 / 60    # pointer position is sampled at most 60 times a second
REFRESH_AFTER_S = 0.3         # re-encode a still screen once so that text sharpens
MIN_FORCED_IDR_S = 0.25       # at most ~4 forced IDRs per second per output
LINGER_S = 5.0                # keep an idle capture thread alive for quick re-subscribes
IDLE_WAIT_S = 0.25            # encoder wake-up period when nothing happens


def monotonic_us() -> int:
    # perf_counter (QueryPerformanceCounter on Windows) is monotonic, sub-microsecond and
    # system-wide. time.monotonic() on Windows only ticks every ~15.6 ms, which would
    # quantise every latency figure the viewers compute.
    return time.perf_counter_ns() // 1000


@dataclass(frozen=True)
class Frame:
    image: np.ndarray     # (H, W, 4) BGRA, never mutated after publication
    frame_id: int
    ts_us: int            # capture time, monotonic microseconds
    t: float              # capture time, perf_counter()


class Rate:
    """Events per second over a sliding one-second window.

    Written by a worker thread and read by the loop thread, hence the lock:
    both sides trim the window, and two concurrent trims could pop an empty deque.
    """

    def __init__(self) -> None:
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._times and now - self._times[0] > 1.0:
            self._times.popleft()

    def tick(self, now: float) -> None:
        with self._lock:
            self._times.append(now)
            self._trim(now)

    def value(self, now: float) -> float:
        with self._lock:
            self._trim(now)
            return float(len(self._times))


# --------------------------------------------------------------------------- per-viewer queue

class ViewerQueue:
    """Outgoing messages of one viewer. Loop thread only.

    Priority: control messages > newest cursor position > media. Media follows
    the drop-to-keyframe policy described in the module docstring.
    """

    MAX_FRAMES = 2   # queued video frames tolerated before dropping (~33 ms at 60 fps)

    def __init__(self, request_keyframe: Callable[[], None]) -> None:
        self._request_keyframe = request_keyframe
        self._control: deque = deque()
        self._cursor: str | None = None
        self._media: deque[tuple[bool, str | bytes]] = deque()   # (is_frame, payload)
        self._frames = 0
        self._event = asyncio.Event()
        self._closed = False
        self._codec: str | None = None
        self._epoch: int | None = None
        self._waiting_key = True
        self.max_frames = self.MAX_FRAMES
        self.cursor_enabled = True
        self.dropped = 0
        self.sent_frames = 0
        self.sent_bytes = 0

    # -- producers ---------------------------------------------------------------
    def reset(self, codec: str | None) -> None:
        """Forget queued media (new subscription or unsubscribe)."""
        self._media.clear()
        self._frames = 0
        self._codec = codec
        self._epoch = None
        self._waiting_key = True

    def set_extreme(self, enabled: bool) -> None:
        """Ultra-low-latency viewers keep one frame and skip cursor updates."""
        self.max_frames = 1 if enabled else self.MAX_FRAMES
        self.cursor_enabled = not enabled
        if enabled:
            self._cursor = None

    def put_control(self, text: str) -> None:
        if not self._closed:
            self._control.append(text)
            self._event.set()

    def put_cursor(self, text: str) -> None:
        if not self._closed and self.cursor_enabled:
            self._cursor = text
            self._event.set()

    def finish(self, code: int, reason: str) -> None:
        """Close the socket after the control messages queued so far."""
        if not self._closed:
            self._control.append(("close", code, reason))
            self._event.set()

    def put_frame(self, data: bytes, keyframe: bool, epoch: int, stream_msg: str) -> None:
        if self._closed or self._codec is None:
            return
        if epoch != self._epoch:
            # New stream configuration: the decoder must be reconfigured first.
            self._drop_frames()
            self._media.append((False, stream_msg))
            self._epoch = epoch
            self._waiting_key = True
        if self._codec == "jpeg":
            # Every JPEG stands alone, so only the newest one is worth sending.
            self.dropped += self._drop_frames()
        elif self._waiting_key:
            if not keyframe:
                return
            self._waiting_key = False
        elif keyframe:
            # Skip ahead: nothing queued before a keyframe is needed any more.
            self.dropped += self._drop_frames()
        elif self._frames >= self.max_frames:
            self.dropped += self._drop_frames()
            self._waiting_key = True
            self._request_keyframe()
            return
        self._media.append((True, data))
        self._frames += 1
        self._event.set()

    def _drop_frames(self) -> int:
        if not self._frames:
            return 0
        n = self._frames
        self._media = deque(item for item in self._media if not item[0])
        self._frames = 0
        return n

    # -- consumer ----------------------------------------------------------------
    def close(self) -> None:
        self._closed = True
        self._event.set()

    async def next(self):
        """Next item to send: ``str`` (text), ``bytes`` (media), a ``("close", code, reason)``
        tuple, or ``None`` once closed."""
        while True:
            if self._closed:
                return None
            if self._control:
                return self._control.popleft()
            if self._cursor is not None:
                text, self._cursor = self._cursor, None
                return text
            if self._media:
                is_frame, payload = self._media.popleft()
                if is_frame:
                    self._frames -= 1
                return payload
            self._event.clear()
            await self._event.wait()


# --------------------------------------------------------------------------- encoder output

class Output:
    """One encoded stream for one display, codec and quality, shared by all its viewers."""

    def __init__(self, display: "DisplayPipeline", codec: str, qkey: tuple = ("standard",)) -> None:
        self.display = display
        self.hub = display.hub
        self.codec = codec
        self.qkey = qkey
        self.key = (codec, qkey)
        self.applied: Quality | None = None   # quality the current encoder was opened with
        self.viewers: set = set()          # loop thread only
        self.epoch = 0
        self.encoder_name: str | None = None
        self.width = 0
        self.height = 0
        self.encode_ms = 0.0
        self.rate = Rate()
        self.dead = False
        self._force = True
        self._stop = False
        self._thread = threading.Thread(
            target=self._run, name=f"lancast-enc{display.index}-{codec}", daemon=True
        )

    def quality(self) -> Quality:
        return resolve_quality(self.qkey, self.codec, self.hub.settings)

    @property
    def fps(self) -> int:
        return max(1, int(self.quality().fps))

    def start(self) -> None:
        self._thread.start()

    def request_keyframe(self) -> None:
        self._force = True
        self.display.wake()

    def stop(self) -> None:
        self._stop = True
        self.dead = True
        self.display.wake()

    def _open(self, w: int, h: int):
        from .encode import create_encoder, fit_size

        q = self.quality()
        out_w, out_h = fit_size(w, h, q.max_width, q.max_height)
        if self.codec == "h264":
            enc = create_encoder("h264", w, h, out_w, out_h, q.fps, q.bitrate_kbps, backend=self.hub.settings.encoder)
        else:
            enc = create_encoder("jpeg", w, h, out_w, out_h, q.fps, 0, jpeg_quality=q.jpeg_quality)
        self.applied = q
        self.encoder_name = enc.name
        self.width, self.height = enc.out_width, enc.out_height
        log.info("display %d %s/%s: %s %dx%d -> %dx%d @ %d fps",
                 self.display.index, self.codec, q.preset, enc.name, w, h, enc.out_width, enc.out_height, q.fps)
        return enc

    def _stream_msg(self, config) -> str:
        q = self.applied or self.quality()
        msg = {
            "type": "stream",
            "display": self.display.index,
            "codec": self.codec,
            "epoch": self.epoch,
            "width": config.width,
            "height": config.height,
            "fps": q.fps,
            "quality": q.as_dict(self.codec),
        }
        if self.codec == "h264":
            msg["codec_string"] = config.codec_string
            msg["description"] = b64encode(config.description).decode("ascii")
        return json.dumps(msg)

    def _run(self) -> None:
        encoder = None
        enc_key = None
        config = None
        stream_msg = ""
        seq = 0
        last_id = -1
        refreshed = True
        next_due = 0.0
        last_forced = -1e9
        try:
            while not self._stop:
                frame = self.display.wait_frame(last_id, IDLE_WAIT_S)
                if self._stop:
                    break
                if self.hub.paused:
                    # wait_frame returns at once while an unencoded frame is pending,
                    # so sleep here instead of spinning until sharing resumes.
                    time.sleep(0.05)
                    continue
                if frame is None:
                    continue
                now = time.perf_counter()
                is_new = frame.frame_id != last_id
                force = self._force and now - last_forced >= MIN_FORCED_IDR_S
                refresh = (self.codec == "h264" and not is_new and not refreshed
                           and now - frame.t >= REFRESH_AFTER_S)
                if not (is_new or force or refresh):
                    if self._force:   # a forced IDR is pending but rate-limited
                        time.sleep(max(0.001, MIN_FORCED_IDR_S - (now - last_forced)))
                    continue
                if not force and now < next_due:
                    time.sleep(next_due - now)
                    continue

                h, w = frame.image.shape[:2]
                key = (w, h, self.hub.settings_version)
                if encoder is None or key != enc_key:
                    if encoder is not None:
                        encoder.close()
                        encoder = None
                    encoder = self._open(w, h)
                    enc_key = key
                    force = True
                    config = None   # new epoch + fresh "stream" message (it carries the applied quality)
                if force:
                    self._force = False
                    last_forced = now

                # Re-encoding an unchanged screen (refresh or forced IDR) shows the screen
                # as it is *now* -- the capture source reported no change since -- so stamp
                # it with the current time; otherwise viewers would report fake latency spikes.
                ts_us = frame.ts_us if is_new else monotonic_us()
                t0 = time.perf_counter()
                ef = encoder.encode(frame.image, ts_us, force_keyframe=force)
                t1 = time.perf_counter()
                ms = (t1 - t0) * 1000.0
                self.encode_ms = ms if self.encode_ms == 0.0 else self.encode_ms * 0.9 + ms * 0.1
                # 80 % of the frame interval: tolerate jitter without adding latency.
                next_due = now + 0.8 / self.fps
                if ef is None:
                    continue
                if is_new:
                    refreshed = False
                elif refresh:
                    refreshed = True
                last_id = frame.frame_id
                self.rate.tick(t1)

                if ef.config != config:
                    config = ef.config
                    self.epoch += 1
                    stream_msg = self._stream_msg(config)
                seq = (seq + 1) & 0xFFFFFFFF
                ts = ef.pts_us
                header = HEADER.pack(
                    KIND[self.codec],
                    1 if ef.keyframe else 0,
                    self.epoch & 0xFFFF,
                    seq,
                    (ts >> 32) & 0xFFFFFFFF,
                    ts & 0xFFFFFFFF,
                )
                self.hub.post(self._fanout, header + ef.data, ef.keyframe, self.epoch, stream_msg)
        except Exception as exc:  # noqa: BLE001 - report to viewers instead of dying silently
            log.exception("encoder for display %d (%s) failed", self.display.index, self.codec)
            self.hub.post(self.hub._output_failed, self, f"编码失败：{exc}")
        finally:
            if encoder is not None:
                try:
                    encoder.close()
                except Exception:  # noqa: BLE001
                    pass

    def _fanout(self, data: bytes, keyframe: bool, epoch: int, stream_msg: str) -> None:
        if self.dead:
            return
        for viewer in list(self.viewers):
            viewer.queue.put_frame(data, keyframe, epoch, stream_msg)


# --------------------------------------------------------------------------- capture

class DisplayPipeline:
    """Capture thread for one display plus its encoder outputs."""

    def __init__(self, hub: "StreamHub", index: int, previous: threading.Thread | None = None) -> None:
        self.hub = hub
        self.index = index
        self.outputs: dict[str, Output] = {}    # loop thread only
        self.cursor_msg: str | None = None      # loop thread only
        self.target_fps = 60                    # written by the loop thread
        self.backend: str | None = None
        self.capture_ms = 0.0
        self.rate = Rate()
        self.dead = False
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._stop = False
        self._previous = previous
        self._thread = threading.Thread(target=self._run, name=f"lancast-cap{index}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self.dead = True
        for out in list(self.outputs.values()):
            out.stop()
        self.wake()

    def threads(self) -> list[threading.Thread]:
        return [self._thread, *(o._thread for o in self.outputs.values())]

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def wait_frame(self, last_id: int, timeout: float) -> Frame | None:
        with self._cond:
            if self._frame is None or self._frame.frame_id == last_id:
                self._cond.wait(timeout)
            return self._frame

    def _run(self) -> None:
        if self._previous is not None:
            self._previous.join(timeout=3.0)   # let the old capture release the output first
        from .capture import CaptureError, get_cursor, open_capture

        try:
            src = open_capture(self.index, self.hub.settings.capture)
        except Exception as exc:  # noqa: BLE001
            log.error("cannot open display %d: %s", self.index, exc)
            self.hub.post(self.hub._display_failed, self, f"无法采集显示器 {self.index + 1}：{exc}")
            return
        self.backend = src.backend
        track_cursor = src.backend != "synthetic"
        frame_id = 0
        next_grab = 0.0
        next_cursor = 0.0
        last_cursor = None
        try:
            while not self._stop:
                now = time.perf_counter()
                if self.hub.paused:
                    time.sleep(0.05)
                    continue
                if now >= next_grab:
                    img = src.grab()
                    if img is not None:
                        t1 = time.perf_counter()
                        ms = (t1 - now) * 1000.0
                        self.capture_ms = ms if self.capture_ms == 0.0 else self.capture_ms * 0.9 + ms * 0.1
                        frame_id += 1
                        with self._cond:
                            self._frame = Frame(img, frame_id, monotonic_us(), t1)
                            self._cond.notify_all()
                        self.rate.tick(t1)
                        next_grab = now + 1.0 / max(1, self.target_fps)
                if track_cursor and now >= next_cursor:
                    next_cursor = now + CURSOR_INTERVAL_S
                    cur = get_cursor(src.display)
                    if cur != last_cursor:
                        last_cursor = cur
                        self.hub.post(self._cursor_changed, cur)
                now2 = time.perf_counter()
                wake_at = next_grab if next_grab > now2 else now2 + CAPTURE_POLL_S
                if track_cursor:
                    wake_at = min(wake_at, next_cursor)
                time.sleep(max(0.0005, wake_at - now2))
        except CaptureError as exc:
            self.hub.post(self.hub._display_failed, self, f"显示器 {self.index + 1} 采集中断：{exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("capture thread for display %d failed", self.index)
            self.hub.post(self.hub._display_failed, self, f"显示器 {self.index + 1} 采集出错：{exc}")
        finally:
            try:
                src.close()
            except Exception:  # noqa: BLE001
                pass

    def _cursor_changed(self, cur) -> None:
        msg = json.dumps({
            "type": "cursor",
            "x": round(float(cur.x), 4),
            "y": round(float(cur.y), 4),
            "visible": bool(cur.visible),
            "shape": cur.shape,
        })
        self.cursor_msg = msg
        for out in self.outputs.values():
            for viewer in out.viewers:
                viewer.queue.put_cursor(msg)


# --------------------------------------------------------------------------- hub

class StreamHub:
    """Starts and stops pipelines on demand and routes viewers to outputs.

    All public methods run on the asyncio loop thread. A viewer is any object
    with ``queue`` (a :class:`ViewerQueue`) and an ``output`` attribute.
    """

    def __init__(self, settings, loop: asyncio.AbstractEventLoop,
                 on_event: Callable[[str, str], None] | None = None) -> None:
        self.settings = settings
        self.loop = loop
        self.paused = False
        self.settings_version = 0
        self.displays: dict[int, DisplayPipeline] = {}
        self._linger: dict[int, asyncio.TimerHandle] = {}
        self._retired: dict[int, threading.Thread] = {}
        self._on_event = on_event or (lambda level, msg: None)

    def post(self, fn, *args) -> None:
        """Run ``fn(*args)`` on the loop thread (callable from any thread)."""
        try:
            self.loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass   # loop already closed during shutdown

    def subscribe(self, viewer, index: int, codec: str, quality=None) -> Output:
        """Route ``viewer`` to the output for (display, codec, quality); see PROTOCOL §3.6."""
        self.unsubscribe(viewer)
        dp = self.displays.get(index)
        if dp is None or dp.dead:
            dp = DisplayPipeline(self, index, previous=self._retired.pop(index, None))
            self.displays[index] = dp
            dp.start()
        handle = self._linger.pop(index, None)
        if handle is not None:
            handle.cancel()
        qkey = quality_key(quality, codec, self.settings)
        out = dp.outputs.get((codec, qkey))
        if out is None or out.dead:
            live = [o for o in dp.outputs.values() if not o.dead]
            same_codec = [o for o in live if o.codec == codec]
            if len(live) >= MAX_OUTPUTS_PER_DISPLAY and same_codec:
                # Too many encoders on this display already: reuse the closest one.
                wanted = resolve_quality(qkey, codec, self.settings)
                out = min(same_codec, key=lambda o: _quality_distance(wanted, o.quality()))
            else:
                out = Output(dp, codec, qkey)
                dp.outputs[out.key] = out
                out.start()
        self._update_fps(dp)
        viewer.queue.reset(codec)
        out.viewers.add(viewer)
        viewer.output = out
        out.request_keyframe()
        if dp.cursor_msg:
            viewer.queue.put_cursor(dp.cursor_msg)
        return out

    def unsubscribe(self, viewer) -> None:
        out = getattr(viewer, "output", None)
        if out is None:
            return
        viewer.output = None
        viewer.queue.reset(None)
        out.viewers.discard(viewer)
        dp = out.display
        if not out.viewers:
            out.stop()
            if dp.outputs.get(out.key) is out:
                del dp.outputs[out.key]
            self._update_fps(dp)
        if not dp.outputs and not dp.dead and dp.index not in self._linger:
            self._linger[dp.index] = self.loop.call_later(LINGER_S, self._retire, dp)

    def _update_fps(self, dp: DisplayPipeline) -> None:
        rates = [o.fps for o in dp.outputs.values() if not o.dead]
        dp.target_fps = max(rates) if rates else 1

    def _retire(self, dp: DisplayPipeline) -> None:
        self._linger.pop(dp.index, None)
        if dp.outputs:
            return
        dp.stop()
        if self.displays.get(dp.index) is dp:
            del self.displays[dp.index]
        self._retired[dp.index] = dp._thread

    def _display_failed(self, dp: DisplayPipeline, message: str) -> None:
        if dp.dead and self.displays.get(dp.index) is not dp:
            return
        self._on_event("error", message)
        err = json.dumps({"type": "error", "code": "capture_failed", "message": message})
        for out in list(dp.outputs.values()):
            for viewer in list(out.viewers):
                viewer.output = None
                viewer.queue.reset(None)
                viewer.queue.put_control(err)
            out.viewers.clear()
        dp.stop()
        if self.displays.get(dp.index) is dp:
            del self.displays[dp.index]
        self._retired[dp.index] = dp._thread

    def _output_failed(self, out: Output, message: str) -> None:
        self._on_event("error", message)
        err = json.dumps({"type": "error", "code": "encoder_failed", "message": message})
        for viewer in list(out.viewers):
            viewer.output = None
            viewer.queue.reset(None)
            viewer.queue.put_control(err)
        out.viewers.clear()
        out.stop()
        dp = out.display
        if dp.outputs.get(out.key) is out:
            del dp.outputs[out.key]
        self._update_fps(dp)

    def settings_changed(self) -> None:
        """Encoders re-open with the new settings on their next frame."""
        self.settings_version += 1
        for dp in self.displays.values():
            self._update_fps(dp)
            for out in dp.outputs.values():
                out.request_keyframe()

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        if not self.paused:
            for dp in self.displays.values():
                for out in dp.outputs.values():
                    out.request_keyframe()

    def info(self) -> dict:
        """Live pipeline numbers for the admin page."""
        now = time.perf_counter()
        result = {}
        for idx, dp in self.displays.items():
            result[idx] = {
                "backend": dp.backend,
                "capture_fps": dp.rate.value(now),
                "capture_ms": round(dp.capture_ms, 2),
                "outputs": [
                    {
                        "codec": o.codec,
                        "quality": (o.applied or o.quality()).as_dict(o.codec),
                        "encoder": o.encoder_name,
                        "width": o.width,
                        "height": o.height,
                        "fps": o.rate.value(now),
                        "encode_ms": round(o.encode_ms, 2),
                        "viewers": len(o.viewers),
                    }
                    for o in dp.outputs.values()
                ],
            }
        return result

    def stop_all(self) -> list[threading.Thread]:
        for handle in self._linger.values():
            handle.cancel()
        self._linger.clear()
        threads: list[threading.Thread] = list(self._retired.values())
        for dp in list(self.displays.values()):
            threads.extend(dp.threads())
            dp.stop()
        self.displays.clear()
        return threads
