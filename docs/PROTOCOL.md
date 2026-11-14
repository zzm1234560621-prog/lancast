# LanCast wire protocol — v1

This document is the contract between the Python server (`lancast/server.py`,
`lancast/pipeline.py`) and the browser clients (`lancast/web/viewer`,
`lancast/web/admin`). Anything not written here is not guaranteed.

## 1. Listeners

| Listener | Default address | TLS | Serves |
|---|---|---|---|
| **LAN**   | `0.0.0.0:8443`   | yes (TLS 1.2+, prefers 1.3) | viewer: `/`, `/static/viewer/*`, `/ws`, `/ca.crt`, `/healthz` |
| **Local** | `127.0.0.1:8442` | no  | everything above **plus** admin: `/admin`, `/static/admin/*`, `/admin/api/*` |

Request admission (enforced by the server before any handler runs):

* LAN listener: the peer IP must be loopback, RFC 1918 private, link-local,
  IPv6 ULA/link-local, or inside an extra CIDR given with `--allow-cidr`.
  Anything else gets `403` and the socket is closed.
* Local listener: the peer must be loopback **and** the `Host` header must be
  exactly `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`
  (DNS-rebinding defence). Otherwise `403`.
* `/admin/api/*` additionally requires the header `X-Admin-Token`.

Every response carries:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self';
    img-src 'self' data: blob:; connect-src 'self' wss://<host> ws://<host>;
    object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
Cache-Control: no-store
```

Consequences for page authors: **no inline `<script>`, no inline `<style>`, no
`style="..."` attributes, no `onclick=` attributes, no external CDNs/fonts.**
Setting `element.style.foo = ...` from JavaScript is allowed. Pages must work
fully offline on a LAN.

## 2. Static routes

| Path | Content |
|---|---|
| `/` | `lancast/web/viewer/index.html` |
| `/static/viewer/<file>` | files from `lancast/web/viewer/` (flat directory, no sub-folders) |
| `/admin` | `lancast/web/admin/index.html` (local listener only) |
| `/static/admin/<file>` | files from `lancast/web/admin/` (local listener only) |
| `/ca.crt` | the local CA certificate, DER, `application/x-x509-ca-cert`, `Content-Disposition: attachment; filename="LanCast-CA.crt"` |
| `/healthz` | `{"ok": true}` |

`index.html` must reference its assets with absolute paths, e.g.
`/static/viewer/app.js`. Supported asset extensions: `.html .js .mjs .css .svg
.png .ico .webmanifest .json`.

## 3. WebSocket `/ws`

* Same origin only: the `Origin` header must be `https://<Host>` (LAN) or
  `http://<Host>` (local). Otherwise the upgrade is refused with `403`.
* No subprotocol, no compression.
* **Text frames** carry JSON control messages (UTF-8). Client messages are at
  most 8 KiB.
* **Binary frames** carry media, server → client only. A client that sends a
  binary frame is disconnected (close code 4001).
* The server sends WebSocket pings every 15 s; browsers answer automatically.

### 3.1 Phase 1 — authentication

The client's **first message** must be `auth`, sent within 10 s of the socket
opening. Three methods:

```jsonc
// a remembered device
{"type":"auth","method":"device","device_id":"d_1a2b3c4d5e6f","token":"<device token>","client":{...}}
// 6-digit PIN typed by the user
{"type":"auth","method":"pin","pin":"123456","name":"iPad","remember":true,"client":{...}}
// one-time token scanned from the QR code (see §4)
{"type":"auth","method":"qr","token":"<qr token>","name":"iPad","remember":true,"client":{...}}
```

`client` (all fields optional, informational, truncated server-side):

```json
{"ua":"<navigator.userAgent>","screen":[1180,820,2],"webcodecs":true,"lang":"zh-CN"}
```

`name` is a user-editable device label (1–40 chars).

Server replies:

```jsonc
// new device, the PC owner must click "Allow" in the admin page (timeout 60 s)
{"type":"auth_pending","timeout_s":60}

// success
{"type":"auth_ok",
 "viewer_id":"v_9f8e7d6c",
 "device_id":"d_1a2b3c4d5e6f",        // null when remember=false
 "device_token":"<token>",            // ONLY present when a new token was issued; store it
 "server":{"name":"DESKTOP-ABC","version":"0.1.0"},
 "displays":[{"index":0,"name":"Display 1","width":2880,"height":1800,"primary":true}],
 "paused":false,
 "control":false,                    // may this viewer send input events (§3.7)
 "viewer_quality":true}               // may this viewer choose its own quality (§3.6)

// failure; the server closes the socket right after (code 4003 or 4008/4009)
{"type":"auth_fail","reason":"bad_credentials","retry_after_s":0}
```

`auth_fail.reason` values: `bad_credentials` (wrong PIN / QR token),
`expired` (QR token used or rotated), `unknown_device` (device token revoked or
unknown — client must forget it), `denied` (owner clicked Deny), `timeout`
(approval not answered), `rate_limited` (see `retry_after_s`), `server_full`,
`bad_request`.

### 3.2 Phase 2 — streaming

Client → server:

| Message | Meaning |
|---|---|
| `{"type":"watch","display":0,"codec":"h264","quality":{"preset":"standard"}}` | subscribe (or switch) to a display; `codec` is `"h264"` or `"jpeg"`; `quality` is optional (§3.6). Sending `watch` again with another `quality` switches quality |
| `{"type":"input","events":[...]}` | remote-control events (§3.7); only honoured when control is allowed |
| `{"type":"unwatch"}` | stop receiving media (e.g. page hidden) |
| `{"type":"keyframe"}` | ask for an IDR (decoder error / resync). Server rate-limits to 1/s per viewer |
| `{"type":"ping","t":12345.67}` | `t` = client `performance.now()`; server answers `pong` immediately |
| `{"type":"stats","fps":59.8,"kbps":8200,"rtt_ms":5.2,"latency_ms":38,"decode_ms":3.1,"dropped":0}` | every ~2 s, shown in the admin page |
| `{"type":"bye"}` | client is leaving |

Server → client (text):

```jsonc
// Describes the stream that the following binary frames belong to.
// Sent after every "watch" and whenever the encoder is reconfigured.
{"type":"stream","display":0,"codec":"h264","epoch":3,"width":1728,"height":1080,"fps":60,
 "codec_string":"avc1.640028","description":"<base64 AVCDecoderConfigurationRecord>",
 "quality":{"preset":"standard","max_width":1920,"max_height":1080,"fps":60,"bitrate_kbps":10000}}
{"type":"stream","display":0,"codec":"jpeg","epoch":4,"width":1728,"height":1080,"fps":15,
 "quality":{"preset":"standard","max_width":1920,"max_height":1080,"fps":15,"jpeg_quality":70}}

// The PC owner granted or revoked remote control for this viewer
// (the initial state is auth_ok.control).
{"type":"control","allowed":true}

// Mouse pointer of the watched display. x/y are normalised 0..1 of the display.
{"type":"cursor","x":0.4821,"y":0.2210,"visible":true,"shape":"arrow"}

{"type":"pong","t":12345.67,"server_us":981234567890}   // server monotonic clock, µs
{"type":"displays","displays":[ ... same objects as auth_ok ... ]}
{"type":"paused","paused":true}                          // host paused / resumed sharing
{"type":"error","code":"bad_display","message":"..."}
{"type":"bye","reason":"kicked"}                         // kicked | revoked | shutdown — then close
```

`cursor.shape` ∈ `arrow ibeam hand wait cross move size_ns size_we size_nwse
size_nesw no other`. Clients may render everything as an arrow.

### 3.3 Binary media frame

```
offset size  field
0      u8    kind      1 = H.264 access unit, 2 = JPEG image
1      u8    flags     bit0 = keyframe (H.264 IDR; always 1 for JPEG)
2      u16   epoch     equals stream.epoch (mod 65536)
4      u32   seq       frame counter within the stream
8      u32   ts_hi     capture timestamp, server monotonic clock, microseconds, high 32 bits
12     u32   ts_lo     … low 32 bits            (ts_us = ts_hi * 2**32 + ts_lo)
16     …     payload
```

All integers are **big-endian**. No BigInt is needed on the client.

H.264 payload = one access unit in **AVCC form**: a sequence of
`u32 big-endian length` + NAL unit. SPS/PPS/AUD/SEI NAL units are removed;
parameter sets travel only in `stream.description` (an
`AVCDecoderConfigurationRecord`, i.e. "avcC"). Configure WebCodecs with:

```js
{codec: msg.codec_string, description: base64ToBytes(msg.description),
 codedWidth: msg.width, codedHeight: msg.height, optimizeForLatency: true}
```

JPEG payload = a complete baseline JPEG file.

Client rules:

1. Ignore frames whose `epoch` differs from the last `stream.epoch` received.
2. After (re)configuring a decoder, drop delta frames until a keyframe arrives.
3. If the decoder errors or falls behind (`decodeQueueSize > 4`), reset it,
   wait for the next keyframe and send `{"type":"keyframe"}`.
4. Render only the newest decoded frame on each animation frame; close older
   `VideoFrame`s immediately.

### 3.4 Latency estimate

`pong.server_us` is the server's monotonic clock when the pong was sent. With
`t0` = ping send time and `t1` = pong receive time (both `performance.now()`):

```
offset_ms = server_us/1000 - (t0 + t1)/2      // keep the sample with the smallest RTT
latency_ms = performance.now() - (ts_us/1000 - offset_ms)   // at the moment a frame is drawn
```

### 3.5 Close codes

| Code | Meaning |
|---|---|
| 1000 / 4000 | normal close (`bye`) |
| 4001 | protocol error, auth timeout, oversized or binary client message |
| 4003 | authentication failed or denied |
| 4004 | kicked or device revoked by the PC owner |
| 4008 | rate limited |
| 4009 | server full (`max_viewers` reached) |
| 4010 | server shutting down |

### 3.6 Quality (per viewer)

`watch.quality` selects what this viewer receives. Viewers asking for the same
effective quality share one encoder.

| `preset` | H.264: max size · fps · bitrate | JPEG: max size · quality · fps |
|---|---|---|
| `saver` (省流) | 960×540 · 30 · 2 000 kbps | 960×540 · 50 · 10 |
| `smooth` (流畅) | 1280×720 · 60 · 5 000 kbps | 1280×720 · 60 · 15 |
| `standard` (标准, default) | the PC's settings (default 1920×1080 · 60 · 10 000) | the PC's settings (70 · 15) |
| `high` (高清) | 2560×1440 · 60 · 20 000 kbps | 2560×1440 · 80 · 20 |
| `native` (原画) | no downscaling · 60 · 35 000 kbps | no downscaling · 85 · 20 |
| `custom` | from `max_width`, `max_height`, `fps`, `bitrate_kbps` | from `max_width`, `max_height`, `jpeg_quality`, `fps` |

Custom values are clamped server-side: size 320×240 … 7680×4320, fps 5 … 240,
bitrate 500 … 200 000 kbps, JPEG quality 20 … 95. Unknown presets mean `standard`.
If the PC owner disabled viewer-chosen quality (`auth_ok.viewer_quality` is
false) every viewer gets `standard`. A display runs at most 4 distinct encoders;
beyond that the closest existing one is reused. **`stream.quality` always
reports what was actually applied** — the viewer must display that, not what it
asked for.

"Auto" quality is a viewer-side policy (the viewer picks presets based on its
own measurements and re-sends `watch`); the server knows nothing about it.

### 3.7 Remote control

A viewer may control the PC's mouse and keyboard only when the PC owner allowed
it: at approval time ("允许控制"), or later per device / per viewer in the admin
page. `auth_ok.control` gives the initial state, `{"type":"control","allowed":…}`
announces changes. The owner can also switch remote control off globally, and
pausing sharing suspends input too. Input arriving without permission is dropped
and answered with `{"type":"error","code":"control_denied"}` (at most once per 5 s).

Client → server, batched (send at most one message per animation frame):

```jsonc
{"type":"input","events":[
  {"t":"mm","dx":12.5,"dy":-3.0},          // relative pointer move, display pixels, sensitivity already applied
  {"t":"ma","x":0.5123,"y":0.2210},        // absolute pointer position, normalised 0..1 on the WATCHED display
  {"t":"mb","b":"left","d":true},          // mouse button down (d=true) / up (d=false): left right middle x1 x2
  {"t":"mw","dy":120,"dx":0},              // wheel in WHEEL_DELTA units (120 = one notch); dy>0 = scroll up, dx>0 = right
  {"t":"kd","k":"KeyW"},                   // key down, k = KeyboardEvent.code (list below)
  {"t":"ku","k":"KeyW"},                   // key up
  {"t":"tx","s":"你好"}                     // type Unicode text
]}
```

Limits (server-enforced; violations are dropped, repeated abuse closes with 4001):
at most 64 events per message and 120 `input` messages per second; |dx|,|dy| ≤ 4000;
|wheel| ≤ 2400 per event; `tx` ≤ 200 characters per event and ≤ 2000 per second;
`ma` is ignored while not watching a display.

Stuck-input protection: while any key or button is held the viewer sends an
empty `{"type":"input","events":[]}` heartbeat every second. The server releases
everything this viewer holds if no `input` message arrived for 3 s, and also on
unwatch, disconnect, loss of permission and pause.

Supported key codes (`KeyboardEvent.code` names):

```
KeyA … KeyZ   Digit0 … Digit9   F1 … F24
ArrowUp ArrowDown ArrowLeft ArrowRight Home End PageUp PageDown Insert Delete
Backspace Tab Enter Space Escape CapsLock NumLock ScrollLock PrintScreen Pause ContextMenu
ShiftLeft ShiftRight ControlLeft ControlRight AltLeft AltRight MetaLeft MetaRight
Minus Equal BracketLeft BracketRight Backslash Semicolon Quote Backquote Comma Period Slash IntlBackslash
Numpad0 … Numpad9 NumpadAdd NumpadSubtract NumpadMultiply NumpadDivide NumpadDecimal NumpadEnter
MediaPlayPause MediaTrackNext MediaTrackPrevious MediaStop AudioVolumeUp AudioVolumeDown AudioVolumeMute
BrowserBack BrowserForward BrowserRefresh
```

Mouse buttons and the wheel are not key codes; key-mapping UIs that offer
"MouseLeft" or "WheelUp" as targets translate them into `mb` / `mw` events.

## 4. Pairing

* **PIN**: 6 random digits, **single use**. It rotates after a successful
  pairing, after its TTL (default 10 min) and after 5 failed attempts in total.
* **QR token**: 128-bit random, URL-safe, same lifetime as the PIN (they rotate
  together). The QR code encodes `https://<lan-ip>:<port>/#pair=<token>`. The
  token lives in the URL *fragment*, so it never appears in an HTTP request
  line, server log or `Referer`. The viewer reads it from `location.hash`,
  immediately removes it with `history.replaceState`, then authenticates with
  `method:"qr"`.
* **Rate limit**: max 5 failed authentications per source IP per 5 minutes;
  then that IP gets `rate_limited` for 5 minutes.
* **Approval** (on by default): a device pairing with PIN or QR waits until the
  owner clicks Allow/Deny in the admin page (60 s timeout).
* **Remember device**: with `remember:true` the server issues a 256-bit device
  token once (`auth_ok.device_token`). The server stores only its SHA-256. The
  viewer stores `{"device_id":…, "token":…}` in `localStorage["lancast.device"]`
  and reconnects with `method:"device"` — no PIN, no approval — until the owner
  revokes it.

## 5. Admin HTTP API (local listener only)

The admin page URL printed in the console is
`http://127.0.0.1:8442/admin#token=<admin token>`. The page reads the token from
`location.hash` (keep a copy in `sessionStorage` so reloads work) and sends it as
`X-Admin-Token` on every API call. POST bodies are JSON with
`Content-Type: application/json`. Responses are JSON. Errors use 4xx with
`{"ok":false,"error":"<message>"}`; `401` means the token is missing or wrong.

### `GET /admin/api/state`

```jsonc
{
  "server": {
    "name": "DESKTOP-ABC", "version": "0.1.0", "started_at": 1731550000.0,
    "port": 8443, "admin_port": 8442, "paused": false,
    "urls": [ {"url":"https://192.168.1.20:8443","interface":"Wi-Fi","kind":"lan","primary":true} ],
    "cert_sha256": "AB:CD:…", "ca_sha256": "12:34:…",
    "encoder": "h264_qsv", "capture_backend": "dxgi",
    "warnings": ["…human readable, already localised…"]
  },
  "pairing": {
    "pin": "123456", "expires_in_s": 512,
    "qr_url": "https://192.168.1.20:8443/#pair=…",
    "qr_svg": "data:image/svg+xml;base64,…"
  },
  "settings": {"fps":60,"max_width":1920,"max_height":1080,"bitrate_kbps":10000,
               "jpeg_quality":70,"jpeg_fps":15,"approval":true,
               "control_enabled":true,          // master switch for remote control
               "viewer_quality":true,           // viewers may pick their own quality
               "max_viewers":8},
  "displays": [ {"index":0,"name":"Display 1","width":2880,"height":1800,"primary":true} ],
  "pending":  [ {"request_id":"r_ab12cd34","name":"iPad","ip":"192.168.3.20","ua":"…","method":"qr","age_s":3} ],
  "viewers":  [ {"viewer_id":"v_9f8e7d6c","device_id":"d_…","name":"iPad","ip":"192.168.3.20","ua":"…",
                 "display":0,"codec":"h264","connected_s":120,"fps":59.8,"kbps":8200,
                 "rtt_ms":6.1,"latency_ms":38,"drops":0,
                 "control":false,               // remote control allowed for this viewer
                 "controlling":false,           // input received in the last 3 s
                 "quality":{"preset":"standard","max_width":1920,"max_height":1080,"fps":60,"bitrate_kbps":10000}} ],
  "devices":  [ {"device_id":"d_…","name":"iPad","created_at":1731550000.0,"last_seen_at":1731551000.0,
                 "last_ip":"192.168.3.20","online":true,"can_control":false} ],
  "log":      [ {"t":1731550000.1,"level":"info","msg":"…"} ]      // newest last, max 100
}
```

Numbers in `viewers` may be `null` when unknown.

### Mutations

| Request | Body |
|---|---|
| `POST /admin/api/approve` | `{"request_id":"r_…","allow":true,"control":false}` — `control:true` also grants remote control |
| `POST /admin/api/device/control` | `{"device_id":"d_…","control":true}` — remembered device; applies to its live sessions too |
| `POST /admin/api/viewer/control` | `{"viewer_id":"v_…","control":true}` — live viewer; persisted when it is a remembered device |
| `POST /admin/api/kick` | `{"viewer_id":"v_…"}` |
| `POST /admin/api/revoke` | `{"device_id":"d_…"}` (also disconnects its live sessions) |
| `POST /admin/api/pin/rotate` | `{}` |
| `POST /admin/api/pause` | `{"paused":true}` |
| `POST /admin/api/settings` | any subset of `settings` keys except `max_viewers`; applied live (`control_enabled:false` immediately suspends every viewer's control) |

All return `{"ok":true}` on success.
