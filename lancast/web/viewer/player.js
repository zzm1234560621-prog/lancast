/*
 * Decoders used by the viewer.
 *
 * Both players expose the same surface:
 *   configure(streamMessage) -> Promise<boolean>
 *   push(parsedFrame)
 *   close()
 *   decodeQueueSize (number)
 *   dropped (number)
 *   decodeMs  (last measured decode time in ms)
 *
 * Only the newest decoded picture is kept; older VideoFrames are closed
 * immediately, as required by docs/PROTOCOL.md §3.3.
 */

import { base64ToBytes } from './protocol.js';

const MAX_DECODE_QUEUE = 4;

function canvasContext(canvas) {
  let context = null;
  try {
    context = canvas.getContext('2d', { alpha: false, desynchronized: true });
  } catch (error) {
    context = null;
  }
  if (!context) {
    context = canvas.getContext('2d');
  }
  return context;
}

class BasePlayer {
  constructor(canvas, handlers) {
    this.canvas = canvas;
    this.ctx = canvas ? canvasContext(canvas) : null;
    this.handlers = handlers || {};
    this.dropped = 0;
    this.decodeMs = 0;
    this._raf = 0;
    this._running = false;
    this._disposed = false;
    this._tick = this._tick.bind(this);
  }

  _setSize(width, height) {
    const w = Math.max(1, Math.min(8192, Math.round(Number(width) || 1)));
    const h = Math.max(1, Math.min(8192, Math.round(Number(height) || 1)));
    if (this.canvas.width !== w) {
      this.canvas.width = w;
    }
    if (this.canvas.height !== h) {
      this.canvas.height = h;
    }
    if (this.ctx) {
      this.ctx.imageSmoothingEnabled = true;
    }
  }

  _startLoop() {
    if (this._running || this._disposed) {
      return;
    }
    this._running = true;
    this._raf = window.requestAnimationFrame(this._tick);
  }

  _tick() {
    if (!this._running || this._disposed) {
      return;
    }
    try {
      this._draw();
    } catch (error) {
      /* a single bad frame must not stop the loop */
    }
    this._raf = window.requestAnimationFrame(this._tick);
  }

  _emit(tsUs) {
    if (this._disposed || !this.handlers.onRendered) {
      return;
    }
    this.handlers.onRendered({
      tsUs: tsUs,
      decodeMs: this.decodeMs,
      width: this.canvas.width,
      height: this.canvas.height
    });
  }

  _draw() {
    /* implemented by subclasses */
  }

  _cleanup() {
    /* implemented by subclasses */
  }

  close() {
    if (this._disposed) {
      return;
    }
    this._disposed = true;
    this._running = false;
    if (this._raf) {
      window.cancelAnimationFrame(this._raf);
      this._raf = 0;
    }
    this._cleanup();
  }
}

export class H264Player extends BasePlayer {
  constructor(canvas, handlers) {
    super(canvas, handlers);
    this._decoder = null;
    this._decoderConfig = null;
    this._decoderGen = 0;
    this._latestFrame = null;
    this._pendingDecode = new Map();
    this._waitingKeyframe = true;
    this._keyTimer = 0;
    this._errorCount = 0;
    this._failed = false;
  }

  static supported() {
    return typeof VideoDecoder !== 'undefined' && typeof EncodedVideoChunk !== 'undefined';
  }

  async configure(message) {
    if (!H264Player.supported()) {
      return false;
    }
    this._setSize(message.width, message.height);
    const codec = message.codec_string || 'avc1.42E01E';
    let description = null;
    if (message.description) {
      try {
        description = base64ToBytes(message.description);
      } catch (error) {
        description = null;
      }
    }
    const base = {
      codec: codec,
      codedWidth: this.canvas.width,
      codedHeight: this.canvas.height,
      optimizeForLatency: true
    };
    if (description && description.byteLength > 0) {
      base.description = description;
    }

    const candidates = [];
    const hardware = Object.assign({}, base, { hardwareAcceleration: 'prefer-hardware' });
    const software = Object.assign({}, base);
    candidates.push(hardware);
    candidates.push(software);

    let chosen = null;
    for (let i = 0; i < candidates.length; i += 1) {
      const candidate = candidates[i];
      try {
        if (typeof VideoDecoder.isConfigSupported !== 'function') {
          chosen = candidate;
          break;
        }
        const result = await VideoDecoder.isConfigSupported(candidate);
        if (result && result.supported) {
          chosen = result.config || candidate;
          break;
        }
      } catch (error) {
        /* try the next candidate */
      }
    }

    if (!chosen) {
      return false;
    }
    this._decoderConfig = chosen;
    this._startLoop();
    return this._createDecoder();
  }

  _createDecoder() {
    this._clearKeyTimer();
    this._decoderGen += 1;
    const generation = this._decoderGen;
    this._errorCount = 0;
    try {
      const decoder = new VideoDecoder({
        output: (frame) => this._onOutput(frame, generation),
        error: (error) => this._onDecoderError(error, generation)
      });
      decoder.configure(this._decoderConfig);
      this._decoder = decoder;
      this._waitingKeyframe = true;
      this._scheduleKeyframeRequest();
      return true;
    } catch (error) {
      this._decoder = null;
      return false;
    }
  }

  _scheduleKeyframeRequest() {
    this._clearKeyTimer();
    if (this._disposed) {
      return;
    }
    this._keyTimer = window.setTimeout(() => {
      this._keyTimer = 0;
      if (!this._disposed && this._waitingKeyframe && this.handlers.onNeedKeyframe) {
        this.handlers.onNeedKeyframe();
      }
      if (!this._disposed && this._waitingKeyframe) {
        this._scheduleKeyframeRequest();
      }
    }, 2000);
  }

  _clearKeyTimer() {
    if (this._keyTimer) {
      window.clearTimeout(this._keyTimer);
      this._keyTimer = 0;
    }
  }

  _onOutput(frame, generation) {
    if (generation !== this._decoderGen || this._disposed) {
      try {
        frame.close();
      } catch (error) {
        /* ignore */
      }
      return;
    }
    const started = this._pendingDecode.get(frame.timestamp);
    if (started != null) {
      this.decodeMs = performance.now() - started;
      this._pendingDecode.delete(frame.timestamp);
    }
    this._errorCount = 0;
    // Draw as soon as the decoder hands us a frame. Waiting for the next rAF
    // can add up to one display refresh of latency; the canvas compositor
    // already presents the newest content at the next vsync.
    let drawn = false;
    if (this.ctx) {
      try {
        this.ctx.drawImage(frame, 0, 0, this.canvas.width, this.canvas.height);
        drawn = true;
      } catch (error) {
        drawn = false;
      }
    }
    if (drawn) {
      this._emit(frame.timestamp);
    }
    try {
      frame.close();
    } catch (error) {
      /* ignore */
    }
  }

  _onDecoderError(error, generation) {
    if (generation !== this._decoderGen || this._disposed) {
      return;
    }
    this._errorCount += 1;
    this.dropped += 1;
    if (this._errorCount > 3) {
      this._fail();
      return;
    }
    this._resetDecoder(true);
  }

  _fail() {
    if (this._failed || this._disposed) {
      return;
    }
    this._failed = true;
    this._clearKeyTimer();
    this._decoderGen += 1;
    if (this._decoder) {
      try {
        this._decoder.close();
      } catch (error) {
        /* ignore */
      }
      this._decoder = null;
    }
    if (this.handlers.onFatal) {
      this.handlers.onFatal();
    }
  }

  _resetDecoder(requestKeyframe) {
    if (this._disposed || this._failed) {
      return;
    }
    this._clearKeyTimer();
    this._decoderGen += 1;
    const old = this._decoder;
    this._decoder = null;
    if (old) {
      try {
        if (old.state !== 'closed') {
          old.close();
        }
      } catch (error) {
        /* ignore */
      }
    }
    if (this._latestFrame) {
      try {
        this._latestFrame.close();
      } catch (error) {
        /* ignore */
      }
      this._latestFrame = null;
    }
    this._pendingDecode.clear();
    this._waitingKeyframe = true;
    if (!this._decoderConfig) {
      this._fail();
      return;
    }
    if (!this._createDecoder()) {
      this._fail();
      return;
    }
    if (requestKeyframe && this.handlers.onNeedKeyframe) {
      this.handlers.onNeedKeyframe();
    }
  }

  push(frame) {
    if (this._disposed || this._failed || !frame || frame.kind !== 1) {
      return;
    }
    const payload = frame.payload;
    if (!payload || payload.byteLength === 0) {
      return;
    }
    let decoder = this._decoder;
    if (!decoder || decoder.state !== 'configured') {
      this.dropped += 1;
      return;
    }

    if (decoder.decodeQueueSize > MAX_DECODE_QUEUE) {
      this.dropped += 1;
      this._resetDecoder(true);
      decoder = this._decoder;
      if (!decoder || !frame.key) {
        return;
      }
    }

    if (this._waitingKeyframe) {
      if (!frame.key) {
        this.dropped += 1;
        return;
      }
      this._waitingKeyframe = false;
      this._clearKeyTimer();
    }

    const timestamp = Number.isFinite(frame.tsUs) ? Math.max(0, Math.round(frame.tsUs)) : 0;
    this._pendingDecode.set(timestamp, performance.now());
    if (this._pendingDecode.size > 32) {
      const oldest = this._pendingDecode.keys().next().value;
      this._pendingDecode.delete(oldest);
    }

    try {
      decoder.decode(new EncodedVideoChunk({
        type: frame.key ? 'key' : 'delta',
        timestamp: timestamp,
        data: payload
      }));
    } catch (error) {
      this._pendingDecode.delete(timestamp);
      this.dropped += 1;
      this._resetDecoder(true);
    }
  }

  _draw() {
    const frame = this._latestFrame;
    if (!frame || !this.ctx) {
      return;
    }
    this._latestFrame = null;
    const timestamp = frame.timestamp;
    let drawn = false;
    try {
      this.ctx.drawImage(frame, 0, 0, this.canvas.width, this.canvas.height);
      drawn = true;
    } catch (error) {
      drawn = false;
    }
    try {
      frame.close();
    } catch (error) {
      /* ignore */
    }
    if (drawn) {
      this._emit(timestamp);
    }
  }

  get decodeQueueSize() {
    if (!this._decoder) {
      return 0;
    }
    try {
      return this._decoder.decodeQueueSize;
    } catch (error) {
      return 0;
    }
  }

  _cleanup() {
    this._clearKeyTimer();
    this._decoderGen += 1;
    if (this._decoder) {
      try {
        if (this._decoder.state !== 'closed') {
          this._decoder.close();
        }
      } catch (error) {
        /* ignore */
      }
      this._decoder = null;
    }
    if (this._latestFrame) {
      try {
        this._latestFrame.close();
      } catch (error) {
        /* ignore */
      }
      this._latestFrame = null;
    }
    this._pendingDecode.clear();
  }
}

export class JpegPlayer extends BasePlayer {
  constructor(canvas, handlers) {
    super(canvas, handlers);
    this._decoding = false;
    this._pending = null;
    this._latest = null;
    this._activeUrl = null;
    this._activeImage = null;
    this._imageResolve = null;
    this._startLoop();
  }

  configure(message) {
    this._setSize(message.width, message.height);
    this._startLoop();
    return Promise.resolve(true);
  }

  push(frame) {
    if (this._disposed || !frame || frame.kind !== 2) {
      return;
    }
    if (this._decoding) {
      if (this._pending) {
        this.dropped += 1;
      }
      this._pending = frame;
      return;
    }
    this._decode(frame);
  }

  async _decode(frame) {
    if (this._disposed) {
      return;
    }
    this._decoding = true;
    const start = performance.now();
    let handled = false;

    if (typeof createImageBitmap === 'function') {
      try {
        const blob = new Blob([frame.payload], { type: 'image/jpeg' });
        const bitmap = await createImageBitmap(blob);
        if (this._disposed) {
          try {
            bitmap.close();
          } catch (error) {
            /* ignore */
          }
          return;
        }
        this.decodeMs = performance.now() - start;
        this._acceptBitmap(bitmap, frame.tsUs);
        handled = true;
      } catch (error) {
        handled = false;
      }
    }

    if (!handled && !this._disposed) {
      await this._decodeWithImage(frame, start);
    }
    if (this._disposed) {
      return;
    }
    this._decoding = false;
    if (this._pending && !this._disposed) {
      const next = this._pending;
      this._pending = null;
      this._decode(next);
    }
  }

  _decodeWithImage(frame, start) {
    return new Promise((resolve) => {
      let url = null;
      try {
        url = URL.createObjectURL(new Blob([frame.payload], { type: 'image/jpeg' }));
      } catch (error) {
        this.dropped += 1;
        resolve();
        return;
      }
      const image = new Image();
      this._activeUrl = url;
      this._activeImage = image;
      this._imageResolve = resolve;
      let done = false;
      const finish = (ok) => {
        if (done) {
          return;
        }
        done = true;
        if (ok && !this._disposed) {
          this.decodeMs = performance.now() - start;
          this._acceptImage(image, frame.tsUs);
        } else if (!ok) {
          this.dropped += 1;
        }
        try {
          URL.revokeObjectURL(url);
        } catch (error) {
          /* ignore */
        }
        if (this._activeUrl === url) {
          this._activeUrl = null;
          this._activeImage = null;
        }
        this._imageResolve = null;
        resolve();
      };
      image.onload = () => finish(true);
      image.onerror = () => finish(false);
      image.src = url;
    });
  }

  _acceptBitmap(bitmap, tsUs) {
    if (this._disposed) {
      try {
        bitmap.close();
      } catch (error) {
        /* ignore */
      }
      return;
    }
    if (this._latest) {
      if (this._latest.bitmap) {
        try {
          this._latest.bitmap.close();
        } catch (error) {
          /* ignore */
        }
      }
      // A newer decoded image replacing an unpresented one is not a drop.
    }
    this._latest = { bitmap: bitmap, image: null, tsUs: tsUs };
  }

  _acceptImage(image, tsUs) {
    if (this._disposed) {
      return;
    }
    if (this._latest) {
      if (this._latest.bitmap) {
        try {
          this._latest.bitmap.close();
        } catch (error) {
          /* ignore */
        }
      }
      // A newer decoded image replacing an unpresented one is not a drop.
    }
    this._latest = { bitmap: null, image: image, tsUs: tsUs };
  }

  _draw() {
    const item = this._latest;
    if (!item || !this.ctx) {
      return;
    }
    this._latest = null;
    let drawn = false;
    try {
      this.ctx.drawImage(item.bitmap || item.image, 0, 0, this.canvas.width, this.canvas.height);
      drawn = true;
    } catch (error) {
      drawn = false;
    }
    if (item.bitmap) {
      try {
        item.bitmap.close();
      } catch (error) {
        /* ignore */
      }
    }
    if (drawn) {
      this._emit(item.tsUs);
    }
  }

  get decodeQueueSize() {
    return this._decoding ? 1 : 0;
  }

  _cleanup() {
    this._pending = null;
    if (this._latest) {
      if (this._latest.bitmap) {
        try {
          this._latest.bitmap.close();
        } catch (error) {
          /* ignore */
        }
      }
      this._latest = null;
    }
    if (this._activeImage) {
      this._activeImage.onload = null;
      this._activeImage.onerror = null;
    }
    if (this._activeUrl) {
      try {
        URL.revokeObjectURL(this._activeUrl);
      } catch (error) {
        /* ignore */
      }
      this._activeUrl = null;
      this._activeImage = null;
    }
    if (this._imageResolve) {
      const resolve = this._imageResolve;
      this._imageResolve = null;
      resolve();
    }
  }
}

export function supportsH264() {
  return H264Player.supported();
}

export function createPlayer(codec, canvas, handlers) {
  if (codec === 'h264') {
    return new H264Player(canvas, handlers);
  }
  if (codec === 'jpeg') {
    return new JpegPlayer(canvas, handlers);
  }
  throw new Error('unknown codec: ' + codec);
}
