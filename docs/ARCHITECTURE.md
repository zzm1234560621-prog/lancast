# LanCast architecture — v0.1

LanCast mirrors a Windows PC's screen to phones and tablets on the same LAN.
The viewing device only needs a browser (iOS/iPadOS Safari, Android Chrome,
any desktop browser). Several devices can watch at once, each choosing a display.

## Data path

```
 DXGI desktop duplication           OpenCV                 PyAV H.264 (QSV → x264)
 per-display thread  ──BGRA──►  resize + BGRA→YUV  ──►  zero-latency encoder, 1 encode
        │                                                per (display, codec)
        └── GetCursorInfo ──► "cursor" JSON                    │ AVCC access unit
                                                               ▼
                          asyncio loop: fan-out ──► per-viewer queue (drop-to-keyframe)
                                                               │
                                   WSS: TLS 1.3, TCP_NODELAY, small SO_SNDBUF
                                                               ▼
              browser: WebCodecs VideoDecoder (hardware) ──► <canvas>, newest-frame-only
              fallback: JPEG ──► createImageBitmap ──► <canvas>
```

Measured on the development machine (Core Ultra 9 185H, 2880×1800 screen →
1728×1080 @ 60 fps): DXGI copy ≈ 8 ms · scale + BGRA→I420 ≈ 8 ms ·
libx264 `zerolatency` encode ≈ 4 ms (Intel QSV via PyAV ≈ 10 ms, ~47 fps max) ·
capture → client arrival over loopback TLS: **p50 15 ms, p95 25 ms** (x264).
Add phone hardware decode 3–6 ms, at most one vsync, and the Wi-Fi hop:
glass-to-glass is typically 30–60 ms. The viewer's stats overlay shows the
live estimate (`scripts/measure_latency.py` reproduces the server-side numbers).

`--encoder auto` therefore prefers libx264 on CPUs with ≥ 8 logical cores and
hardware encoders on smaller machines (`lancast.encode.preferred_h264_backends`).
Scaling uses `INTER_AREA` before the colour conversion: converting first and
shrinking the planes is ~20 % faster, but OpenCV's I420 conversion point-samples
chroma, which makes coloured text alias.

### Why WebSocket + WebCodecs instead of WebRTC

* **One encode for N viewers.** A Python WebRTC stack (aiortc) encodes per peer
  connection; here one encoder output is fanned out to every viewer.
* **No jitter buffer.** Browsers add a WebRTC playout buffer that Safari does not
  let us tune; with WebCodecs we decode as bytes arrive and draw the newest frame.
* **LAN reality.** Loss on a home LAN is low; head-of-line blocking is handled by
  the per-viewer drop-to-keyframe queue instead of being hidden by buffering.
* **Fallback.** Browsers without WebCodecs (old iPads) still work via JPEG.

## Modules

| Module | Responsibility |
|---|---|
| `lancast/config.py` | `Settings` dataclass, data directory |
| `lancast/certs.py` | local CA (name-constrained to private ranges) + server certificate |
| `lancast/auth.py` | PIN/QR pairing, device tokens, rate limiting, approval queue |
| `lancast/pipeline.py` | per-display capture/encode threads, fan-out, backpressure |
| `lancast/server.py` | aiohttp app: listeners, admission checks, WebSocket, admin API |
| `lancast/cli.py` | command line: `serve`, `doctor`, `devices` |
| `lancast/capture/` | screen capture backends + cursor state |
| `lancast/encode/` | H.264 and JPEG encoders, H.264 bitstream helpers |
| `lancast/net/` | LAN address discovery, peer allow-list, QR codes, Windows network checks |
| `lancast/web/viewer/` | browser viewer (served at `/`) |
| `lancast/web/admin/` | local admin page (served at `/admin`) |

## Threading model

* **asyncio loop thread**: aiohttp, all WebSocket I/O, auth, fan-out, admin API.
* **One `DisplayPipeline` thread per display that has ≥ 1 viewer**:
  grab → for each active codec output: encode → `loop.call_soon_threadsafe(fanout)`.
* A `CaptureSource` or `Encoder` object is created, used and closed **on its
  pipeline thread only**. `list_displays()`, `get_cursor()` and the probing
  functions are thread-safe.

---

## Contracts for component implementers

Python 3.11, Windows 10/11 x64. Dependencies available in `.venv`:
`numpy`, `opencv-python-headless` (`cv2`), `av` (PyAV), `dxcam`, `mss`,
`aiohttp`, `cryptography`, `qrcode`, `psutil`, `pytest`.
Use only these plus the standard library (`ctypes` is fine).

### `lancast.capture`

```python
from dataclasses import dataclass
import numpy as np

@dataclass(frozen=True)
class DisplayInfo:
    index: int        # position in list_displays() (0 = primary)
    id: str           # OS device name, e.g. r"\\.\DISPLAY1"
    name: str         # human label, e.g. "Display 1"
    x: int            # top-left in virtual-desktop PHYSICAL pixels
    y: int
    width: int        # native physical pixels, rotation applied
    height: int
    primary: bool

class CaptureError(Exception): ...

def ensure_dpi_awareness() -> None
    """Make the process per-monitor DPI aware (idempotent). Called on import."""

def list_displays() -> list[DisplayInfo]
    """Primary first, then by (x, y). Physical pixels."""

def open_capture(display_index: int, backend: str = "auto") -> "CaptureSource"
    """backend: "auto" (dxgi, falling back to mss), "dxgi", "mss", "synthetic"."""

def open_synthetic(width: int = 1280, height: int = 720, fps: int = 60) -> "CaptureSource"
    """Animated test pattern (moving bar + frame counter), no OS dependency."""

class CaptureSource:          # protocol
    display: DisplayInfo
    backend: str              # "dxgi" | "mss" | "synthetic"
    def grab(self) -> "np.ndarray | None":
        """Return a NEW C-contiguous uint8 array (H, W, 4) in BGRA order for the whole
        display at native resolution, or None if nothing changed since the last call.
        Must return within ~5 ms when nothing changed (never block for a full frame).
        Never mutates an array after returning it. Recovers internally from transient
        failures (DXGI access lost, mode change, UAC secure desktop) by re-initialising
        and returning None; raises CaptureError only if the display is gone for good.
        A resolution change simply yields arrays of the new shape."""
    def close(self) -> None: ...

@dataclass(frozen=True)
class CursorState:
    x: float          # normalised to the given display; 0..1 when on it
    y: float
    visible: bool     # False when hidden or not on this display
    shape: str        # arrow ibeam hand wait cross move size_ns size_we size_nwse size_nesw no other

def get_cursor(display: DisplayInfo) -> CursorState
```

### `lancast.encode`

```python
@dataclass(frozen=True)
class H264Config:
    codec_string: str     # "avc1.PPCCLL" lowercase hex of SPS bytes 1..3, e.g. "avc1.640028"
    description: bytes    # AVCDecoderConfigurationRecord ("avcC"), incl. High-profile extension bytes
    width: int
    height: int

@dataclass(frozen=True)
class JpegConfig:
    width: int
    height: int

@dataclass(frozen=True)
class EncodedFrame:
    data: bytes           # h264: ONE access unit in AVCC form (u32 BE length + NAL ...),
                          #       with SPS/PPS/AUD/SEI NAL units removed
                          # jpeg: a complete JPEG file
    keyframe: bool        # h264: contains an IDR slice; jpeg: always True
    pts_us: int           # copied from the encode() argument
    config: "H264Config | JpegConfig"   # equal (==) objects while the configuration is unchanged

class Encoder:            # protocol
    codec: str            # "h264" | "jpeg"
    name: str             # backend, e.g. "h264_qsv", "libx264", "jpeg"
    out_width: int
    out_height: int
    def encode(self, bgra: "np.ndarray", pts_us: int, force_keyframe: bool = False) -> "EncodedFrame | None":
        """bgra: (src_height, src_width, 4) uint8. Scales to the output size itself.
        ZERO DELAY: returns the frame for THIS input in the same call (no B-frames,
        no look-ahead, no async queue). The first frame and every force_keyframe=True
        frame must be an IDR. Returns None only if the backend dropped the frame."""
    def close(self) -> None: ...

def fit_size(src_w: int, src_h: int, max_w: int, max_h: int) -> tuple[int, int]
    """Keep aspect ratio, fit inside max_w x max_h, never upscale, both dims even."""

def probe_h264_encoders() -> list[str]
    """Backends that actually work on this machine, best first. Cached after first call."""

def create_encoder(codec: str, src_width: int, src_height: int, out_width: int, out_height: int,
                   fps: int, bitrate_kbps: int, jpeg_quality: int = 70,
                   backend: str = "auto") -> Encoder
```

`lancast/encode/h264util.py` (pure functions, unit-tested):
`split_annexb(data) -> list[bytes]`, `nal_type(nal) -> int`,
`to_avcc(nals) -> bytes`, `build_avcc_record(sps, pps) -> bytes`,
`codec_string(sps) -> str`.

### `lancast.input`

Remote-control injection. The server-side policy (permissions, limits, held-key
tracking, watchdog) lives in `lancast/control.py`; this package only turns
validated events into OS input.

```python
KEY_CODES: frozenset[str]        # exactly the KeyboardEvent.code list in PROTOCOL.md §3.7
BUTTONS = ("left", "right", "middle", "x1", "x2")

def key_info(code: str) -> tuple[int, int, bool]
    """(virtual-key, scan code, extended-key flag); ValueError for unknown codes."""

class InputBackend:              # protocol
    name: str                    # "sendinput" | "fake"
    def move_relative(self, dx: int, dy: int) -> None
    def move_absolute(self, x: int, y: int) -> None      # virtual-desktop PHYSICAL pixels
    def button(self, button: str, down: bool) -> None    # one of BUTTONS, else ValueError
    def wheel(self, dy: int, dx: int = 0) -> None        # WHEEL_DELTA units; dy > 0 scrolls up, dx > 0 right
    def key(self, code: str, down: bool) -> None         # code in KEY_CODES, else ValueError
    def text(self, s: str) -> None                       # Unicode text (KEYEVENTF_UNICODE, surrogate pairs)
    def cursor_position(self) -> tuple[int, int]         # physical pixels

class FakeInputBackend(InputBackend):
    calls: list[tuple]           # e.g. ("key", "KeyW", True); keeps a virtual cursor position

def create_input_backend(fake: bool = False) -> InputBackend
    """SendInput on Windows; FakeInputBackend when fake=True or on other platforms."""
```

All methods are called from the asyncio loop thread; each call must return
within a millisecond (SendInput is non-blocking).

### `lancast.net`

```python
@dataclass(frozen=True)
class LanAddress:
    ip: str                 # "192.168.1.20"
    interface: str          # "Wi-Fi"
    kind: str               # "lan" | "virtual" | "vpn" | "loopback"
    is_default_route: bool

def lan_addresses() -> list[LanAddress]          # IPv4, best candidate first
def primary_address() -> "str | None"            # best IP to put in the QR code
def is_allowed_peer(ip: str, extra_cidrs=()) -> bool
def parse_cidrs(values) -> list                  # list[ipaddress.IPv4Network | IPv6Network]; ValueError on bad input
def qr_svg(text: str) -> str                     # standalone SVG document
def qr_svg_data_uri(text: str) -> str            # "data:image/svg+xml;base64,..."
def qr_terminal(text: str) -> str                # compact console rendering (Unicode half blocks)
def windows_network_report(port: int) -> list[str]   # read-only diagnostics, Chinese messages
```
