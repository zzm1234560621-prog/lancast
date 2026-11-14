/*
 * LanCast viewer protocol helpers.
 *
 * This module is deliberately free of DOM and WebSocket references so that it
 * can be imported by Node in selftest.mjs. It implements the byte-level
 * contract from docs/PROTOCOL.md §3.3 and the clock estimate from §3.4.
 */

export const FRAME_HEADER_SIZE = 16;

const TWO_POW_32 = 4294967296;

function asDataView(input, offset) {
  if (input instanceof ArrayBuffer) {
    return new DataView(input, offset);
  }
  if (ArrayBuffer.isView(input)) {
    return new DataView(input.buffer, input.byteOffset + offset, input.byteLength - offset);
  }
  throw new TypeError('frame data must be an ArrayBuffer or a typed array');
}

/**
 * Parse one binary media frame.
 *
 * Layout (all integers big-endian):
 *   u8 kind, u8 flags, u16 epoch, u32 seq, u32 ts_hi, u32 ts_lo, payload…
 *
 * Returns {kind, flags, key, epoch, seq, tsHi, tsLo, tsUs, payload}.
 * `payload` is a Uint8Array view into the original buffer.
 */
export function parseFrameHeader(input, offset) {
  const start = offset == null ? 0 : offset;
  const view = asDataView(input, start);
  if (view.byteLength < FRAME_HEADER_SIZE) {
    throw new RangeError('binary frame is shorter than the 16 byte header');
  }
  const kind = view.getUint8(0);
  const flags = view.getUint8(1);
  const epoch = view.getUint16(2, false);
  const seq = view.getUint32(4, false);
  const tsHi = view.getUint32(8, false);
  const tsLo = view.getUint32(12, false);
  const payload = new Uint8Array(view.buffer, view.byteOffset + FRAME_HEADER_SIZE,
    view.byteLength - FRAME_HEADER_SIZE);
  return {
    kind: kind,
    flags: flags,
    key: (flags & 1) === 1,
    epoch: epoch,
    seq: seq,
    tsHi: tsHi,
    tsLo: tsLo,
    tsUs: tsHi * TWO_POW_32 + tsLo,
    payload: payload
  };
}

const BASE64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

function manualBase64Decode(text) {
  const clean = text.replace(/=+$/, '');
  const out = [];
  let buffer = 0;
  let bits = 0;
  for (let i = 0; i < clean.length; i += 1) {
    const value = BASE64_CHARS.indexOf(clean.charAt(i));
    if (value < 0) {
      throw new Error('invalid base64 character');
    }
    buffer = (buffer << 6) | value;
    bits += 6;
    while (bits >= 8) {
      bits -= 8;
      out.push((buffer >> bits) & 0xff);
    }
    if (bits > 0) {
      buffer &= (1 << bits) - 1;
    } else {
      buffer = 0;
    }
  }
  return new Uint8Array(out);
}

/** Decode standard base64 (whitespace and URL-safe variants tolerated). */
export function base64ToBytes(base64) {
  if (typeof base64 !== 'string') {
    throw new TypeError('base64 input must be a string');
  }
  let text = base64.replace(/\s+/g, '').replace(/-/g, '+').replace(/_/g, '/');
  if (text.length % 4 !== 0) {
    text += '===='.slice(0, 4 - (text.length % 4));
  }
  if (text.length === 0) {
    return new Uint8Array(0);
  }
  if (typeof atob === 'function') {
    const binary = atob(text);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }
  return manualBase64Decode(text);
}

/**
 * Keeps the clock-offset sample with the smallest round-trip time.
 * PROTOCOL §3.4:
 *   offset_ms = server_us / 1000 - (t0 + t1) / 2
 *   latency_ms = performance.now() - (ts_us / 1000 - offset_ms)
 */
export class ClockOffsetEstimator {
  constructor() {
    this._best = null;
    this.samples = 0;
  }

  addSample(t0, t1, serverUs) {
    const a = Number(t0);
    const b = Number(t1);
    const server = Number(serverUs);
    if (!Number.isFinite(a) || !Number.isFinite(b) || !Number.isFinite(server)) {
      return this._best;
    }
    const rttMs = Math.max(0, b - a);
    const offsetMs = server / 1000 - (a + b) / 2;
    this.samples += 1;
    if (!this._best || rttMs < this._best.rttMs) {
      this._best = { offsetMs: offsetMs, rttMs: rttMs, at: b };
    }
    return this._best;
  }

  get hasSample() {
    return this._best !== null;
  }

  get offsetMs() {
    return this._best ? this._best.offsetMs : null;
  }

  get rttMs() {
    return this._best ? this._best.rttMs : null;
  }

  /** Estimated glass-to-glass latency for a capture timestamp, or null. */
  latencyMs(tsUs, nowMs) {
    if (!this._best) {
      return null;
    }
    const ts = Number(tsUs);
    const now = Number(nowMs);
    if (!Number.isFinite(ts) || !Number.isFinite(now)) {
      return null;
    }
    return now - (ts / 1000 - this._best.offsetMs);
  }
}

/**
 * Suggest a 1–40 character device label from a user-agent string.
 * lang is used only to localise the device part; browser names stay in their
 * usual Latin spelling (Safari, Chrome, Edge, …).
 */
export function deviceNameFromUA(ua, lang) {
  const text = String(ua || '');
  const chinese = String(lang || 'en').toLowerCase().indexOf('zh') === 0;

  let device = chinese ? '未知设备' : 'Device';
  const isIPad = /iPad/i.test(text) ||
    (/Macintosh/i.test(text) && /Mobile\//i.test(text));
  if (isIPad) {
    device = 'iPad';
  } else if (/iPhone|iPod/i.test(text)) {
    device = 'iPhone';
  } else if (/Android/i.test(text)) {
    device = /Mobile/i.test(text)
      ? (chinese ? 'Android 手机' : 'Android phone')
      : (chinese ? 'Android 平板' : 'Android tablet');
  } else if (/Windows NT/i.test(text)) {
    device = chinese ? 'Windows 电脑' : 'Windows PC';
  } else if (/CrOS/i.test(text)) {
    device = chinese ? 'ChromeOS 电脑' : 'ChromeOS device';
  } else if (/Macintosh|Mac OS X/i.test(text)) {
    device = 'Mac';
  } else if (/Linux|X11/i.test(text)) {
    device = chinese ? 'Linux 电脑' : 'Linux PC';
  }

  let browser = '';
  if (/EdgiOS\/|EdgA\/|Edg\//i.test(text)) {
    browser = 'Edge';
  } else if (/OPR\/|Opera/i.test(text)) {
    browser = 'Opera';
  } else if (/SamsungBrowser/i.test(text)) {
    browser = 'Samsung Internet';
  } else if (/FxiOS\/|Firefox\//i.test(text)) {
    browser = 'Firefox';
  } else if (/CriOS\/|Chrome\/|Chromium\//i.test(text)) {
    browser = 'Chrome';
  } else if (/Version\/[\d.]+.*Safari\/|Safari\//i.test(text)) {
    browser = 'Safari';
  } else {
    browser = chinese ? '浏览器' : 'Browser';
  }

  const name = (device + ' ' + browser).trim();
  return name.slice(0, 40) || (chinese ? '浏览器' : 'Browser');
}

/** auth_fail.reason → i18n key. */
export function authFailReasonKey(reason) {
  switch (reason) {
    case 'bad_credentials':
      return 'errors.auth.badPin';
    case 'expired':
      return 'errors.auth.expired';
    case 'unknown_device':
      return 'errors.auth.unknownDevice';
    case 'denied':
      return 'errors.auth.denied';
    case 'timeout':
      return 'errors.auth.timeout';
    case 'rate_limited':
      return 'errors.auth.rateLimited';
    case 'server_full':
      return 'ended.serverFull';
    case 'bad_request':
      return 'errors.auth.badRequest';
    default:
      return 'errors.auth.failed';
  }
}

/** WebSocket close code (and optional bye.reason) → i18n key. */
export function closeCodeReasonKey(code, byeReason) {
  if (byeReason === 'kicked') {
    return 'ended.kicked';
  }
  if (byeReason === 'revoked') {
    return 'ended.revoked';
  }
  if (byeReason === 'shutdown') {
    return 'ended.shutdown';
  }
  switch (code) {
    case 4001:
      return 'ended.protocol';
    case 4003:
      return 'ended.authFailed';
    case 4004:
      return 'ended.kicked';
    case 4008:
      return 'errors.auth.rateLimited';
    case 4009:
      return 'ended.serverFull';
    case 4010:
      return 'ended.shutdown';
    case 1000:
    case 4000:
      return 'ended.disconnected';
    default:
      return 'ended.connectionLost';
  }
}

/** Median of a numeric list, or null when empty. */
export function median(values) {
  if (!values || values.length === 0) {
    return null;
  }
  const sorted = [];
  for (let i = 0; i < values.length; i += 1) {
    const n = Number(values[i]);
    if (Number.isFinite(n)) {
      sorted.push(n);
    }
  }
  if (sorted.length === 0) {
    return null;
  }
  sorted.sort(function (a, b) { return a - b; });
  const middle = sorted.length >> 1;
  if (sorted.length % 2 === 1) {
    return sorted[middle];
  }
  return (sorted[middle - 1] + sorted[middle]) / 2;
}
