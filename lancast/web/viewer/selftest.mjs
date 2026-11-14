/*
 * Node self-test for the pure protocol helpers.
 *
 *   node lancast/web/viewer/selftest.mjs
 *
 * No browser or npm dependencies are required.
 */

import assert from 'node:assert/strict';
import {
  parseFrameHeader,
  base64ToBytes,
  ClockOffsetEstimator,
  deviceNameFromUA,
  authFailReasonKey,
  closeCodeReasonKey,
  median,
  FRAME_HEADER_SIZE
} from './protocol.js';
import {
  INPUT_EVENT_LIMIT,
  MAX_REL_DELTA,
  InputSender,
  PointerControl,
  scrollToWheel,
  splitText,
  takeInputBatch,
  takeWheelStep,
  touchpadDelta
} from './control.js';
import {
  GAMEPAD_BUTTON_DEFS,
  GamepadController,
  STICK_TARGETS,
  TRIGGER_THRESHOLD,
  defaultGamepadMapping,
  gamepadAxes,
  gamepadButtonValue,
  gamepadPressed,
  normalizeGamepadMapping
} from './gamepad.js';
import {
  INPUT_TARGETS,
  KEY_CODES,
  KEY_GROUP_DEFS,
  MOUSE_TARGETS,
  isInputTarget,
  isKeyCode,
  keyLabel,
  targetEvent
} from './keycodes.js';
import {
  ITEM_KEYS_MAX,
  ITEM_SIZE_MAX,
  ITEM_SIZE_MIN,
  mappingPressEvents,
  mappingReleaseEvents,
  stickKeys,
  stickVector,
  validateLayout
} from './keymap.js';
import {
  AUTO_LEVELS,
  AutoQuality,
  QUALITY_PRESET_IDS,
  buildWatchQuality,
  defaultQualityPreferences,
  normalizeQualityPreferences
} from './quality.js';
import { initI18n, t } from './i18n.js';

let passed = 0;

function test(name, fn) {
  fn();
  passed += 1;
  console.log('ok ' + passed + ' - ' + name);
}

function approx(actual, expected, tolerance, label) {
  const margin = tolerance == null ? 1e-9 : tolerance;
  assert.ok(Math.abs(actual - expected) <= margin,
    (label || 'value') + ': expected ' + expected + ' +/- ' + margin + ', got ' + actual);
}

function recordingSender() {
  const log = [];
  return {
    log: log,
    keyDown(code) { log.push({ type: 'kd', code: code }); },
    keyUp(code) { log.push({ type: 'ku', code: code }); },
    mouseDown(button) { log.push({ type: 'mb', button: button, down: true }); },
    mouseUp(button) { log.push({ type: 'mb', button: button, down: false }); },
    wheel(dy, dx) { log.push({ type: 'mw', dy: dy, dx: dx }); },
    moveRel(dx, dy) { log.push({ type: 'mm', dx: dx, dy: dy }); },
    moveAbs(x, y) { log.push({ type: 'ma', x: x, y: y }); }
  };
}

function inputSender() {
  const sent = [];
  const sender = new InputSender({
    send(message) { sent.push(message); return true; },
    isEnabled() { return true; }
  });
  return { sender: sender, sent: sent };
}

function buildFrame(kind, flags, epoch, seq, tsHi, tsLo, payload) {
  const body = Buffer.from(payload || []);
  const frame = Buffer.alloc(FRAME_HEADER_SIZE + body.length);
  frame.writeUInt8(kind, 0);
  frame.writeUInt8(flags, 1);
  frame.writeUInt16BE(epoch, 2);
  frame.writeUInt32BE(seq >>> 0, 4);
  frame.writeUInt32BE(tsHi >>> 0, 8);
  frame.writeUInt32BE(tsLo >>> 0, 12);
  body.copy(frame, FRAME_HEADER_SIZE);
  return frame;
}

test('parses a 16-byte header and payload (big-endian)', () => {
  const payload = [0x00, 0x00, 0x00, 0x04, 0x65, 0x88, 0x84, 0x00, 0xff];
  const buffer = buildFrame(1, 1, 0xbeef, 0x01020304, 0, 0x89abcdef, payload);
  const frame = parseFrameHeader(buffer);
  assert.equal(frame.kind, 1);
  assert.equal(frame.flags, 1);
  assert.equal(frame.key, true);
  assert.equal(frame.epoch, 0xbeef);
  assert.equal(frame.seq, 0x01020304);
  assert.equal(frame.tsHi, 0);
  assert.equal(frame.tsLo, 0x89abcdef);
  assert.equal(frame.tsUs, 0x89abcdef);
  assert.deepEqual(Array.from(frame.payload), payload);
});

test('combines non-zero ts_hi with ts_lo without BigInt', () => {
  const buffer = buildFrame(2, 3, 7, 42, 1, 0x80000000, [1, 2, 3]);
  const frame = parseFrameHeader(buffer);
  assert.equal(frame.kind, 2);
  assert.equal(frame.key, true);
  assert.equal(frame.epoch, 7);
  assert.equal(frame.seq, 42);
  assert.equal(frame.tsHi, 1);
  assert.equal(frame.tsLo, 0x80000000);
  assert.equal(frame.tsUs, 4294967296 + 2147483648);
  assert.equal(frame.payload.length, 3);
  assert.deepEqual(Array.from(frame.payload), [1, 2, 3]);
});

test('honours typed-array byteOffset and explicit offset', () => {
  const buffer = buildFrame(2, 1, 9, 100, 0, 123456, [9, 8, 7]);
  const padded = Buffer.concat([Buffer.from([0xff, 0xff, 0xff]), buffer, Buffer.from([0xee])]);
  const view = new Uint8Array(padded.buffer, padded.byteOffset + 3, buffer.length);
  const frame = parseFrameHeader(view);
  assert.equal(frame.epoch, 9);
  assert.equal(frame.seq, 100);
  assert.deepEqual(Array.from(frame.payload), [9, 8, 7]);

  const withOffset = parseFrameHeader(
    new Uint8Array(padded.buffer, padded.byteOffset, 3 + buffer.length), 3);
  assert.equal(withOffset.tsUs, 123456);
  assert.deepEqual(Array.from(withOffset.payload), [9, 8, 7]);
});

test('rejects short frames', () => {
  assert.throws(() => parseFrameHeader(new Uint8Array(15)), RangeError);
});

test('decodes standard and URL-safe base64', () => {
  const bytes = Uint8Array.from([0, 1, 2, 3, 254, 255, 60, 61, 62, 63, 100]);
  const standard = Buffer.from(bytes).toString('base64');
  assert.deepEqual(Array.from(base64ToBytes(standard)), Array.from(bytes));

  const urlSafe = Buffer.from(bytes).toString('base64').replace(/\+/g, '-').replace(/\//g, '_');
  assert.deepEqual(Array.from(base64ToBytes(urlSafe)), Array.from(bytes));

  const spaced = standard.slice(0, 4) + '\n ' + standard.slice(4);
  assert.deepEqual(Array.from(base64ToBytes(spaced)), Array.from(bytes));

  assert.equal(base64ToBytes('').length, 0);
});

test('clock estimator keeps the minimum-RTT sample', () => {
  const estimator = new ClockOffsetEstimator();
  assert.equal(estimator.hasSample, false);
  assert.equal(estimator.latencyMs(1000000, 2000), null);

  estimator.addSample(1000, 1040, 2000000);   // offset 980, rtt 40
  estimator.addSample(2000, 2010, 2001300);   // offset -3.7, rtt 10 (best)
  estimator.addSample(3000, 3030, 3005000);   // offset 1985, rtt 30

  assert.equal(estimator.samples, 3);
  assert.equal(estimator.hasSample, true);
  assert.equal(estimator.rttMs, 10);
  assert.ok(Math.abs(estimator.offsetMs - (-3.7)) < 1e-9);

  const latency = estimator.latencyMs(1000000, 2000);
  assert.ok(Math.abs(latency - 996.3) < 1e-9);
});

test('UA to default device name: iPad Safari', () => {
  const ua = 'Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 ' +
    '(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1';
  assert.equal(deviceNameFromUA(ua, 'en'), 'iPad Safari');
  assert.equal(deviceNameFromUA(ua, 'zh-CN'), 'iPad Safari');
});

test('UA to default device name: iPadOS desktop-mode Safari', () => {
  const ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 ' +
    '(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1';
  assert.equal(deviceNameFromUA(ua, 'en'), 'iPad Safari');
});

test('UA to default device name: iPhone Safari', () => {
  const ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 ' +
    '(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1';
  assert.equal(deviceNameFromUA(ua, 'en'), 'iPhone Safari');
});

test('UA to default device name: Android Chrome phone', () => {
  const ua = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36';
  assert.equal(deviceNameFromUA(ua, 'zh-CN'), 'Android 手机 Chrome');
  assert.equal(deviceNameFromUA(ua, 'en'), 'Android phone Chrome');
});

test('UA to default device name: Android tablet', () => {
  const ua = 'Mozilla/5.0 (Linux; Android 13; SM-X700) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
  assert.equal(deviceNameFromUA(ua, 'zh-CN'), 'Android 平板 Chrome');
});

test('UA to default device name: Windows Edge', () => {
  const ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0';
  assert.equal(deviceNameFromUA(ua, 'zh-CN'), 'Windows 电脑 Edge');
  assert.equal(deviceNameFromUA(ua, 'en'), 'Windows PC Edge');
});

test('auth failure reasons map to translation keys', () => {
  assert.equal(authFailReasonKey('bad_credentials'), 'errors.auth.badPin');
  assert.equal(authFailReasonKey('expired'), 'errors.auth.expired');
  assert.equal(authFailReasonKey('unknown_device'), 'errors.auth.unknownDevice');
  assert.equal(authFailReasonKey('denied'), 'errors.auth.denied');
  assert.equal(authFailReasonKey('timeout'), 'errors.auth.timeout');
  assert.equal(authFailReasonKey('rate_limited'), 'errors.auth.rateLimited');
  assert.equal(authFailReasonKey('server_full'), 'ended.serverFull');
  assert.equal(authFailReasonKey('something_else'), 'errors.auth.failed');
});

test('close codes and bye reasons map to translation keys', () => {
  assert.equal(closeCodeReasonKey(4004), 'ended.kicked');
  assert.equal(closeCodeReasonKey(4004, 'revoked'), 'ended.revoked');
  assert.equal(closeCodeReasonKey(4004, 'kicked'), 'ended.kicked');
  assert.equal(closeCodeReasonKey(4010), 'ended.shutdown');
  assert.equal(closeCodeReasonKey(4009), 'ended.serverFull');
  assert.equal(closeCodeReasonKey(4003), 'ended.authFailed');
  assert.equal(closeCodeReasonKey(4001), 'ended.protocol');
  assert.equal(closeCodeReasonKey(1006), 'ended.connectionLost');
});

test('median returns the middle value or null', () => {
  assert.equal(median([]), null);
  assert.equal(median([5]), 5);
  assert.equal(median([9, 1, 5]), 5);
  assert.equal(median([4, 1, 3, 2]), 2.5);
  assert.equal(median([1, NaN, 3]), 2);
});

/* ---------- quality.js ---------- */

test('quality: default preferences have the documented shape', () => {
  assert.deepEqual(defaultQualityPreferences(), {
    preset: 'auto',
    custom: {
      h264: { resolution: '1080p', fps: 60, bitrateMbps: 10 },
      jpeg: { resolution: '1080p', fps: 15, quality: 70 }
    }
  });
});

test('quality: normalizeQualityPreferences falls back for corrupt input', () => {
  assert.deepEqual(normalizeQualityPreferences(null), defaultQualityPreferences());
  assert.deepEqual(normalizeQualityPreferences('garbage'), defaultQualityPreferences());
  assert.deepEqual(normalizeQualityPreferences({ preset: 'ultra', custom: 'nope' }),
    defaultQualityPreferences());
});

test('quality: normalizeQualityPreferences clamps numeric fields', () => {
  const prefs = normalizeQualityPreferences({
    preset: 'custom',
    custom: {
      h264: { resolution: '720p', fps: 30, bitrateMbps: 999 },
      jpeg: { resolution: '1440p', fps: 20, quality: 4.6 }
    }
  });
  assert.equal(prefs.preset, 'custom');
  assert.equal(prefs.custom.h264.resolution, '720p');
  assert.equal(prefs.custom.h264.fps, 30);
  assert.equal(prefs.custom.h264.bitrateMbps, 200);
  assert.equal(prefs.custom.jpeg.resolution, '1440p');
  assert.equal(prefs.custom.jpeg.fps, 20);
  assert.equal(prefs.custom.jpeg.quality, 30);
  assert.equal(normalizeQualityPreferences({ custom: { h264: { bitrateMbps: 0 } } })
    .custom.h264.bitrateMbps, 1);
});

test('quality: normalizeQualityPreferences rejects unknown enums', () => {
  const prefs = normalizeQualityPreferences({
    preset: 'turbo',
    custom: {
      h264: { resolution: '8k', fps: 17 },
      jpeg: { resolution: 'cinema', fps: 7, quality: 200 }
    }
  });
  assert.equal(prefs.preset, 'auto');
  assert.equal(prefs.custom.h264.resolution, '1080p');
  assert.equal(prefs.custom.h264.fps, 60);
  assert.equal(prefs.custom.jpeg.resolution, '1080p');
  assert.equal(prefs.custom.jpeg.fps, 15);
  assert.equal(prefs.custom.jpeg.quality, 95);
});

test('quality: buildWatchQuality maps auto to standard by default', () => {
  assert.deepEqual(buildWatchQuality(null, 'h264', null), { preset: 'standard' });
  assert.deepEqual(buildWatchQuality({ preset: 'auto' }, 'jpeg', null, 'nope'),
    { preset: 'standard' });
});

test('quality: buildWatchQuality maps auto to saver and smooth', () => {
  assert.deepEqual(buildWatchQuality({ preset: 'auto' }, 'h264', null, 'saver'),
    { preset: 'saver' });
  assert.deepEqual(buildWatchQuality({ preset: 'auto' }, 'h264', null, 'smooth'),
    { preset: 'smooth' });
});

test('quality: buildWatchQuality passes explicit presets through', () => {
  assert.deepEqual(buildWatchQuality({ preset: 'high' }, 'h264'), { preset: 'high' });
  assert.deepEqual(buildWatchQuality({ preset: 'native' }, 'jpeg'), { preset: 'native' });
});

test('quality: buildWatchQuality builds explicit H.264 custom fields', () => {
  const prefs = {
    preset: 'custom',
    custom: {
      h264: { resolution: '720p', fps: 30, bitrateMbps: 15 },
      jpeg: { resolution: '1080p', fps: 15, quality: 70 }
    }
  };
  assert.deepEqual(buildWatchQuality(prefs, 'h264'), {
    preset: 'custom',
    max_width: 1280,
    max_height: 720,
    fps: 30,
    bitrate_kbps: 15000
  });
});

test('quality: buildWatchQuality builds explicit JPEG custom fields', () => {
  const prefs = {
    preset: 'custom',
    custom: {
      h264: { resolution: '1080p', fps: 60, bitrateMbps: 10 },
      jpeg: { resolution: '1440p', fps: 20, quality: 85 }
    }
  };
  const built = buildWatchQuality(prefs, 'jpeg');
  assert.deepEqual(built, {
    preset: 'custom',
    max_width: 2560,
    max_height: 1440,
    fps: 20,
    jpeg_quality: 85
  });
  assert.equal(Object.prototype.hasOwnProperty.call(built, 'bitrate_kbps'), false);
});

test('quality: buildWatchQuality resolves source resolution from the display', () => {
  const sourcePrefs = {
    preset: 'custom',
    custom: {
      h264: { resolution: 'source', fps: 60, bitrateMbps: 10 },
      jpeg: { resolution: 'source', fps: 15, quality: 70 }
    }
  };
  assert.equal(buildWatchQuality(sourcePrefs, 'h264',
    { width: 2560, height: 1440 }).max_width, 2560);
  assert.equal(buildWatchQuality(sourcePrefs, 'h264',
    { width: 2560, height: 1440 }).max_height, 1440);
  assert.equal(buildWatchQuality(sourcePrefs, 'h264',
    { width: 0, height: 0 }).max_width, 1920);
  assert.equal(buildWatchQuality(sourcePrefs, 'jpeg').max_height, 1080);
});

test('quality: AutoQuality drops one level after more than 8 drops', () => {
  const auto = new AutoQuality();
  assert.equal(auto.level, 'standard');
  assert.equal(auto.tick(0, { dropped: 0, latencyMs: 50, decodeQueue: 0 }).changed, false);
  const result = auto.tick(2000, { dropped: 9, latencyMs: 50, decodeQueue: 0 });
  assert.equal(result.changed, true);
  assert.equal(result.level, 'smooth');
  assert.equal(result.direction, 'down');
  assert.equal(result.reason, 'dropped');
  assert.equal(result.cooldownMs, 6000);
  assert.equal(auto.lastSwitch, 2000);
});

test('quality: AutoQuality steps down to saver and never lower', () => {
  const auto = new AutoQuality();
  auto.tick(0, { dropped: 0, latencyMs: 50 });
  assert.equal(auto.tick(2000, { dropped: 9, latencyMs: 50 }).level, 'smooth');
  const second = auto.tick(9000, { dropped: 30, latencyMs: 50 });
  assert.equal(second.changed, true);
  assert.equal(second.level, 'saver');
  assert.equal(second.reason, 'dropped');
  const third = auto.tick(16000, { dropped: 90, latencyMs: 50 });
  assert.equal(third.changed, false);
  assert.equal(third.level, 'saver');
});

test('quality: AutoQuality drops on latency over 180 ms', () => {
  const auto = new AutoQuality();
  auto.tick(0, { dropped: 0, latencyMs: 50 });
  assert.equal(auto.tick(2000, { dropped: 0, latencyMs: 180 }).changed, false);
  const result = auto.tick(4000, { dropped: 0, latencyMs: 181 });
  assert.equal(result.changed, true);
  assert.equal(result.reason, 'latency');
  assert.equal(result.level, 'smooth');
});

test('quality: AutoQuality drops after two consecutive queue strikes', () => {
  const auto = new AutoQuality();
  const first = auto.tick(0, { dropped: 0, latencyMs: 50, decodeQueue: 3 });
  assert.equal(first.changed, false);
  assert.equal(auto.queueStrikes, 1);
  const second = auto.tick(2000, { dropped: 0, latencyMs: 50, decodeQueue: 4 });
  assert.equal(second.changed, true);
  assert.equal(second.reason, 'queue');
  assert.equal(auto.queueStrikes, 0);
});

test('quality: AutoQuality clears queue strikes on a clean tick', () => {
  const auto = new AutoQuality();
  auto.tick(0, { dropped: 0, latencyMs: 50, decodeQueue: 3 });
  auto.tick(2000, { dropped: 0, latencyMs: 50, decodeQueue: 0 });
  assert.equal(auto.queueStrikes, 0);
  const result = auto.tick(4000, { dropped: 0, latencyMs: 50, decodeQueue: 5 });
  assert.equal(result.changed, false);
  assert.equal(auto.queueStrikes, 1);
});

test('quality: AutoQuality ignores bad metrics during the 6 s cooldown', () => {
  const auto = new AutoQuality();
  auto.tick(0, { dropped: 0, latencyMs: 50 });
  auto.tick(2000, { dropped: 20, latencyMs: 50, decodeQueue: 0 });
  const during = auto.tick(3000, { dropped: 999, latencyMs: 500, decodeQueue: 9 });
  assert.equal(during.changed, false);
  assert.equal(during.cooldown, true);
  assert.equal(during.reason, 'cooldown');
  assert.equal(during.level, 'smooth');
  assert.equal(auto.tick(7999, { dropped: 999, latencyMs: 500, decodeQueue: 9 }).cooldown, true);
  const after = auto.tick(8000, { dropped: 999, latencyMs: 500, decodeQueue: 9 });
  assert.equal(after.changed, true);
  assert.equal(after.level, 'saver');
  assert.equal(after.reason, 'latency');
});

test('quality: AutoQuality treats a dropped counter reset as zero', () => {
  const auto = new AutoQuality();
  auto.tick(0, { dropped: 100, latencyMs: 50 });
  const result = auto.tick(2000, { dropped: 5, latencyMs: 50 });
  assert.equal(result.droppedDelta, 0);
  assert.equal(result.changed, false);
});

test('quality: AutoQuality climbs after 30 s of clean metrics', () => {
  const auto = new AutoQuality({ level: 'smooth' });
  assert.equal(auto.tick(0, { dropped: 0, latencyMs: 60 }).changed, false);
  const half = auto.tick(15000, { dropped: 0, latencyMs: 60 });
  assert.equal(half.changed, false);
  assert.equal(half.goodForMs, 15000);
  const up = auto.tick(30000, { dropped: 0, latencyMs: 60 });
  assert.equal(up.changed, true);
  assert.equal(up.level, 'standard');
  assert.equal(up.direction, 'up');
  assert.equal(up.reason, 'stable');
  const again = auto.tick(40000, { dropped: 0, latencyMs: 60 });
  assert.equal(again.changed, false);
  assert.equal(again.level, 'standard');
});

test('quality: AutoQuality restarts the good-metrics clock after a bad tick', () => {
  const auto = new AutoQuality({ level: 'smooth' });
  auto.tick(0, { dropped: 0, latencyMs: 60 });
  auto.tick(10000, { dropped: 0, latencyMs: 100 });
  assert.equal(auto.goodSince, null);
  auto.tick(15000, { dropped: 0, latencyMs: 60 });
  assert.equal(auto.tick(30000, { dropped: 0, latencyMs: 60 }).changed, false);
  const up = auto.tick(45000, { dropped: 0, latencyMs: 60 });
  assert.equal(up.changed, true);
  assert.equal(up.level, 'standard');
});

test('quality: AutoQuality levels and setLevel are validated', () => {
  assert.deepEqual(AUTO_LEVELS, ['saver', 'smooth', 'standard']);
  const auto = new AutoQuality({ level: 'bogus' });
  assert.equal(auto.level, 'standard');
  assert.equal(auto.setLevel('saver', 500), 'saver');
  assert.equal(auto.lastSwitch, 500);
  assert.equal(auto.setLevel('bogus', 900), 'saver');
  assert.equal(auto.lastSwitch, 500);
});

/* ---------- keycodes.js ---------- */

test('keycodes: KEY_CODES holds exactly 127 unique key codes', () => {
  assert.equal(KEY_CODES.length, 127);
  assert.equal(new Set(KEY_CODES).size, 127);
  assert.equal(KEY_CODES.indexOf('MouseLeft'), -1);
  assert.equal(KEY_CODES.indexOf('WheelUp'), -1);
  assert.ok(KEY_CODES.indexOf('KeyA') >= 0);
  assert.ok(KEY_CODES.indexOf('F24') >= 0);
  assert.ok(KEY_CODES.indexOf('NumpadEnter') >= 0);
  assert.ok(KEY_CODES.indexOf('AudioVolumeMute') >= 0);
});

test('keycodes: isKeyCode and isInputTarget separate keys from mouse targets', () => {
  assert.equal(isKeyCode('KeyA'), true);
  assert.equal(isKeyCode('MouseLeft'), false);
  assert.equal(isKeyCode('Bogus'), false);
  assert.equal(isInputTarget('KeyA'), true);
  assert.equal(isInputTarget('MouseX2'), true);
  assert.equal(isInputTarget('WheelDown'), true);
  assert.equal(isInputTarget('Bogus'), false);
  assert.equal(INPUT_TARGETS.length, KEY_CODES.length + 7);
});

test('keycodes: targetEvent translates keyboard targets', () => {
  assert.deepEqual(targetEvent('KeyA', true), { t: 'kd', k: 'KeyA' });
  assert.deepEqual(targetEvent('KeyA', false), { t: 'ku', k: 'KeyA' });
});

test('keycodes: targetEvent translates mouse buttons', () => {
  assert.deepEqual(targetEvent('MouseLeft', true), { t: 'mb', b: 'left', d: true });
  assert.deepEqual(targetEvent('MouseRight', false), { t: 'mb', b: 'right', d: false });
  assert.deepEqual(targetEvent('MouseX2', true), { t: 'mb', b: 'x2', d: true });
});

test('keycodes: targetEvent fires wheel targets only on press', () => {
  assert.deepEqual(targetEvent('WheelDown', true), { t: 'mw', dy: -120, dx: 0 });
  assert.deepEqual(targetEvent('WheelUp', true), { t: 'mw', dy: 120, dx: 0 });
  assert.equal(targetEvent('WheelUp', false), null);
  assert.equal(targetEvent('WheelDown', false), null);
  assert.equal(targetEvent('none', true), null);
  assert.equal(targetEvent('Bogus', false), null);
});

test('keycodes: every input target has a non-empty label', () => {
  for (let i = 0; i < INPUT_TARGETS.length; i += 1) {
    const label = keyLabel(INPUT_TARGETS[i]);
    assert.equal(typeof label, 'string');
    assert.ok(label.length > 0, 'empty label for ' + INPUT_TARGETS[i]);
  }
  assert.equal(keyLabel('KeyA'), 'A');
  assert.equal(keyLabel('Digit7'), '7');
  assert.equal(keyLabel('Numpad4'), 'Num 4');
  assert.equal(keyLabel('ArrowUp'), '\u2191');
});

/* ---------- keymap.js ---------- */

function validLayout() {
  return {
    name: 'Test layout',
    items: [
      { id: 'b1', type: 'button', label: 'A', keys: ['KeyA'], x: 0.5, y: 0.5, size: 60 },
      { id: 's1', type: 'stick', target: 'wasd', x: 0.2, y: 0.8, size: 140 }
    ]
  };
}

function expectLayoutFailure(layout, fragment) {
  const result = validateLayout(layout);
  assert.equal(result.ok, false);
  assert.equal(result.layout, null);
  assert.equal(typeof result.error, 'string');
  assert.ok(result.error.indexOf(fragment) >= 0,
    'expected error containing "' + fragment + '", got "' + result.error + '"');
}

test('keymap: validateLayout accepts a minimal layout', () => {
  const result = validateLayout(validLayout());
  assert.equal(result.ok, true);
  assert.equal(result.error, null);
  assert.equal(result.layout.name, 'Test layout');
  assert.equal(result.layout.items.length, 2);
  assert.equal(result.layout.items[0].mode, 'hold');
  assert.deepEqual(result.layout.items[0].keys, ['KeyA']);
  assert.equal(result.layout.items[1].target, 'wasd');
});

test('keymap: validateLayout rejects unknown fields', () => {
  const top = validLayout();
  top.extra = true;
  expectLayoutFailure(top, 'unknown layout field');
  const item = validLayout();
  item.items[0].foo = 1;
  expectLayoutFailure(item, 'unknown field');
});

test('keymap: validateLayout rejects unsupported and duplicate keys', () => {
  const unknown = validLayout();
  unknown.items[0].keys = ['KeyZZ'];
  expectLayoutFailure(unknown, 'not a supported key');
  const duplicated = validLayout();
  duplicated.items[0].keys = ['KeyA', 'KeyA'];
  expectLayoutFailure(duplicated, 'duplicated');
  const tooMany = validLayout();
  tooMany.items[0].keys = ['KeyA', 'KeyB', 'KeyC', 'KeyD', 'KeyE'];
  expectLayoutFailure(tooMany, 'keys must contain 1 to ' + ITEM_KEYS_MAX);
});

test('keymap: validateLayout rejects sizes and positions out of range', () => {
  const small = validLayout();
  small.items[0].size = ITEM_SIZE_MIN - 1;
  expectLayoutFailure(small, 'size must be between');
  const large = validLayout();
  large.items[1].size = ITEM_SIZE_MAX + 1;
  expectLayoutFailure(large, 'size must be between');
  const badX = validLayout();
  badX.items[0].x = -0.01;
  expectLayoutFailure(badX, '.x must be a number between 0 and 1');
  const badY = validLayout();
  badY.items[0].y = 1.01;
  expectLayoutFailure(badY, '.y must be a number between 0 and 1');
  const nan = validLayout();
  nan.items[0].x = NaN;
  expectLayoutFailure(nan, '.x must be a number between 0 and 1');
});

test('keymap: validateLayout rejects duplicate ids', () => {
  const layout = validLayout();
  layout.items[1].id = 'b1';
  expectLayoutFailure(layout, 'duplicated');
});

test('keymap: validateLayout rejects bad modes, types and stick targets', () => {
  const mode = validLayout();
  mode.items[0].mode = 'press';
  expectLayoutFailure(mode, 'mode must be hold, toggle or tap');
  const type = validLayout();
  type.items[0].type = 'dial';
  expectLayoutFailure(type, 'type must be "button" or "stick"');
  const target = validLayout();
  target.items[1].target = 'keyboard';
  expectLayoutFailure(target, 'target must be wasd, arrows or mouse');
});

test('keymap: validateLayout rejects empty layouts and names', () => {
  expectLayoutFailure({ name: 'Empty', items: [] }, 'at least one item');
  expectLayoutFailure({ name: '   ', items: validLayout().items },
    'name must be a non-empty string');
  expectLayoutFailure(null, 'layout must be an object');
});

test('keymap: stickKeys maps the eight arrow directions', () => {
  assert.deepEqual(stickKeys('arrows', 0.8, 0.2, 0), ['ArrowRight']);
  assert.deepEqual(stickKeys('arrows', -0.8, 0.2, 0), ['ArrowLeft']);
  assert.deepEqual(stickKeys('arrows', 0.2, -0.8, 0), ['ArrowUp']);
  assert.deepEqual(stickKeys('arrows', 0.2, 0.8, 0), ['ArrowDown']);
  assert.deepEqual(stickKeys('arrows', 0.8, -0.8, 0), ['ArrowUp', 'ArrowRight']);
  assert.deepEqual(stickKeys('arrows', 0.8, 0.8, 0), ['ArrowDown', 'ArrowRight']);
  assert.deepEqual(stickKeys('arrows', -0.8, 0.8, 0), ['ArrowDown', 'ArrowLeft']);
  assert.deepEqual(stickKeys('arrows', -0.8, -0.8, 0), ['ArrowUp', 'ArrowLeft']);
});

test('keymap: stickKeys maps WASD and ignores weak diagonal components', () => {
  assert.deepEqual(stickKeys('wasd', 0.8, 0.2, 0), ['KeyD']);
  assert.deepEqual(stickKeys('wasd', -0.2, 0.9, 0), ['KeyS']);
  assert.deepEqual(stickKeys('wasd', -0.8, -0.8, 0), ['KeyW', 'KeyA']);
  assert.deepEqual(stickKeys('wasd', 1, 0.4, 0), ['KeyD']);
});

test('keymap: stickKeys applies the dead zone and rejects mouse targets', () => {
  assert.deepEqual(stickKeys('arrows', 0.1, 0, 0.2), []);
  assert.deepEqual(stickKeys('arrows', 0, 0, 0), []);
  assert.deepEqual(stickKeys('mouse', 0.9, 0.9, 0), []);
});

test('keymap: stickVector rescales outside the dead zone', () => {
  const vector = stickVector(0.3, 0.4, 0.1);
  assert.equal(vector.active, true);
  approx(vector.magnitude, 0.4 / 0.9, 1e-9, 'magnitude');
  approx(vector.x, 0.6 * (0.4 / 0.9), 1e-9, 'x');
  approx(vector.y, 0.8 * (0.4 / 0.9), 1e-9, 'y');
  const full = stickVector(1, 0, 0);
  assert.equal(full.active, true);
  approx(full.magnitude, 1, 1e-9, 'full magnitude');
  approx(full.x, 1, 1e-9, 'full x');
});

test('keymap: stickVector clamps the dead zone and reports inactivity', () => {
  assert.deepEqual(stickVector(0.1, 0.1, 0.5), { active: false, x: 0, y: 0, magnitude: 0 });
  assert.deepEqual(stickVector(0, 0, 0), { active: false, x: 0, y: 0, magnitude: 0 });
  assert.equal(stickVector(0.4, 0, 0.9).active, false);
  approx(stickVector(2, 0, 0.5).magnitude, 1, 1e-9, 'capped magnitude');
});

test('keymap: mapping press/release events mirror order and skip wheel ups', () => {
  assert.deepEqual(mappingPressEvents(['ControlLeft', 'KeyC']), [
    { t: 'kd', k: 'ControlLeft' },
    { t: 'kd', k: 'KeyC' }
  ]);
  assert.deepEqual(mappingReleaseEvents(['ControlLeft', 'KeyC']), [
    { t: 'ku', k: 'KeyC' },
    { t: 'ku', k: 'ControlLeft' }
  ]);
  assert.deepEqual(mappingPressEvents(['WheelUp']), [{ t: 'mw', dy: 120, dx: 0 }]);
  assert.deepEqual(mappingReleaseEvents(['WheelUp']), []);
  assert.deepEqual(mappingPressEvents(['MouseLeft', 'none', null]), [
    { t: 'mb', b: 'left', d: true }
  ]);
});

/* ---------- control.js ---------- */

test('control: takeInputBatch merges consecutive relative moves', () => {
  const result = takeInputBatch([
    { t: 'mm', dx: 1, dy: 2 },
    { t: 'mm', dx: 3, dy: 4 },
    { t: 'kd', k: 'KeyA' },
    { t: 'mm', dx: 5, dy: 6 }
  ], INPUT_EVENT_LIMIT);
  assert.deepEqual(result.events, [
    { t: 'mm', dx: 4, dy: 6 },
    { t: 'kd', k: 'KeyA' },
    { t: 'mm', dx: 5, dy: 6 }
  ]);
  assert.deepEqual(result.rest, []);
});

test('control: takeInputBatch clamps moves and honours the 64-event limit', () => {
  const clamped = takeInputBatch([{ t: 'mm', dx: 5000, dy: -5000 }], INPUT_EVENT_LIMIT);
  assert.deepEqual(clamped.events, [{ t: 'mm', dx: MAX_REL_DELTA, dy: -MAX_REL_DELTA }]);
  const queue = [];
  for (let i = 0; i < 70; i += 1) {
    queue.push({ t: 'mw', dy: i, dx: 0 });
  }
  const batch = takeInputBatch(queue, INPUT_EVENT_LIMIT);
  assert.equal(batch.events.length, 64);
  assert.equal(batch.rest.length, 6);
  assert.equal(batch.events[0].dy, 0);
  assert.equal(batch.events[63].dy, 63);
  assert.equal(batch.rest[0].dy, 64);
  assert.equal(takeInputBatch(queue, 0).events.length, 1);
});

test('control: takeInputBatch skips malformed queue entries', () => {
  const result = takeInputBatch([null, {}, { t: 5 }, { t: 'kd', k: 'KeyA' }], 64);
  assert.deepEqual(result.events, [{ t: 'kd', k: 'KeyA' }]);
  assert.deepEqual(result.rest, []);
  assert.deepEqual(takeInputBatch(null, 64), { events: [], rest: [] });
});

test('control: touchpadDelta applies gain, sensitivity and acceleration', () => {
  assert.deepEqual(touchpadDelta(10, -5, 0, 1920, 960, 1, false), { dx: 20, dy: -10 });
  approx(touchpadDelta(10, 0, 1, 1000, 1000, 1, true).dx, 14, 1e-9, 'accelerated dx');
  approx(touchpadDelta(2, 0, 0, 1000, 1000, 10, false).dx, 10, 1e-9, 'sensitivity clamp');
  approx(touchpadDelta(1, 0, 100, 1000, 1000, 1, true).dx, 3, 1e-9, 'speed cap');
  assert.deepEqual(touchpadDelta(10, 10, 1, 0, 960, 1, true), { dx: 0, dy: 0 });
  assert.deepEqual(touchpadDelta(10, 10, 1, 1920, 0, 1, true), { dx: 0, dy: 0 });
});

test('control: scrollToWheel maps 30 px to one 120-unit notch', () => {
  assert.equal(scrollToWheel(30, 1), 120);
  assert.equal(scrollToWheel(15, 1), 60);
  assert.equal(scrollToWheel(-30, 1), -120);
  approx(scrollToWheel(30, 0), 24, 1e-9, 'minimum speed');
  approx(scrollToWheel(30, 99), 600, 1e-9, 'maximum speed');
});

test('control: takeWheelStep accumulates fractional notches', () => {
  assert.deepEqual(takeWheelStep(0, 120), { step: 120, rest: 0 });
  const positive = takeWheelStep(0.5, 0.7);
  assert.equal(positive.step, 1);
  approx(positive.rest, 0.2, 1e-9, 'positive rest');
  const negative = takeWheelStep(-0.5, -0.6);
  assert.equal(negative.step, -1);
  approx(negative.rest, -0.1, 1e-9, 'negative rest');
  const under = takeWheelStep(0, -0.5);
  assert.equal(Math.abs(under.step), 0);
  approx(under.rest, -0.5, 1e-9, 'unspent rest');
  const capped = takeWheelStep(0, 3000);
  assert.equal(capped.step, 2400);
  assert.equal(capped.rest, 600);
  assert.deepEqual(takeWheelStep('x', 'y'), { step: 0, rest: 0 });
});

test('control: splitText counts code points, not UTF-16 units', () => {
  assert.deepEqual(splitText('\u{1F600}\u{1F601}\u{1F602}', 2),
    ['\u{1F600}\u{1F601}', '\u{1F602}']);
  assert.deepEqual(splitText('a\u{1F600}b', 2), ['a\u{1F600}', 'b']);
  assert.deepEqual(splitText('abcde', 2), ['ab', 'cd', 'e']);
  assert.deepEqual(splitText('', 5), []);
  assert.deepEqual(splitText(null, 5), []);
  assert.deepEqual(splitText('ab', 0), ['a', 'b']);
  const long = splitText('x'.repeat(201), 200);
  assert.equal(long.length, 2);
  assert.equal(long[0].length, 200);
  assert.equal(long[1].length, 1);
});

test('control: InputSender deduplicates key down and up', () => {
  const harness = inputSender();
  try {
    assert.equal(harness.sender.keyDown('KeyA'), true);
    assert.equal(harness.sender.keyDown('KeyA'), false);
    assert.equal(harness.sender.keyDown('MouseLeft'), false);
    assert.equal(harness.sender.isKeyHeld('KeyA'), true);
    harness.sender.flushNow();
    assert.deepEqual(harness.sent, [{ type: 'input', events: [{ t: 'kd', k: 'KeyA' }] }]);
    harness.sent.length = 0;
    assert.equal(harness.sender.keyUp('KeyA'), true);
    assert.equal(harness.sender.keyUp('KeyA'), false);
    harness.sender.flushNow();
    assert.deepEqual(harness.sent, [{ type: 'input', events: [{ t: 'ku', k: 'KeyA' }] }]);
    assert.equal(harness.sender.heldCount, 0);
  } finally {
    harness.sender.setEnabled(false);
  }
});

test('control: InputSender releaseAll releases in reverse press order', () => {
  const harness = inputSender();
  try {
    harness.sender.keyDown('KeyA');
    harness.sender.keyDown('ControlLeft');
    harness.sender.mouseDown('right');
    harness.sender.flushNow();
    harness.sent.length = 0;
    harness.sender.releaseAll();
    harness.sender.flushNow();
    assert.deepEqual(harness.sent[0].events, [
      { t: 'mb', b: 'right', d: false },
      { t: 'ku', k: 'ControlLeft' },
      { t: 'ku', k: 'KeyA' }
    ]);
    assert.equal(harness.sender.heldCount, 0);
  } finally {
    harness.sender.setEnabled(false);
  }
});

test('control: InputSender releaseAll(false) just forgets held inputs', () => {
  const harness = inputSender();
  try {
    harness.sender.keyDown('KeyB');
    harness.sender.mouseDown('left');
    harness.sender.flushNow();
    harness.sent.length = 0;
    harness.sender.releaseAll(false);
    harness.sender.flushNow();
    assert.deepEqual(harness.sent, []);
    assert.equal(harness.sender.heldCount, 0);
    assert.equal(harness.sender.isKeyHeld('KeyB'), false);
    assert.equal(harness.sender.isButtonHeld('left'), false);
  } finally {
    harness.sender.setEnabled(false);
  }
});

test('control: InputSender flushNow merges mm and caps at 64 events', () => {
  const moves = inputSender();
  try {
    moves.sender.moveRel(3.4, -2.6);
    moves.sender.moveRel(1.1, 0.9);
    moves.sender.flushNow();
    assert.equal(moves.sent.length, 1);
    assert.equal(moves.sent[0].events.length, 1);
    approx(moves.sent[0].events[0].dx, 4.5, 1e-9, 'merged dx');
    approx(moves.sent[0].events[0].dy, -1.7, 1e-9, 'merged dy');
  } finally {
    moves.sender.setEnabled(false);
  }
  const many = inputSender();
  try {
    for (let i = 0; i < 70; i += 1) {
      many.sender.wheel(1, 0);
    }
    many.sender.flushNow();
    assert.equal(many.sent.length, 2);
    assert.equal(many.sent[0].events.length, 64);
    assert.equal(many.sent[1].events.length, 6);
    assert.equal(many.sender.queue.length, 0);
  } finally {
    many.sender.setEnabled(false);
  }
});

test('control: InputSender chunks text into 200-character events', () => {
  const harness = inputSender();
  try {
    harness.sender.text('x'.repeat(450));
    harness.sender.flushNow();
    assert.equal(harness.sent.length, 1);
    assert.deepEqual(harness.sent[0].events.map((event) => event.s.length), [200, 200, 50]);
    assert.equal(harness.sent[0].events.every((event) => event.t === 'tx'), true);
  } finally {
    harness.sender.setEnabled(false);
  }
});

test('control: PointerControl uses the time since the previous move', () => {
  const sink = recordingSender();
  const stage = {
    clientWidth: 1000,
    clientHeight: 800,
    getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 800 }; },
    setPointerCapture() {},
    releasePointerCapture() {}
  };
  const control = new PointerControl({
    stage: stage,
    sender: sink,
    getMode() { return 'touchpad'; },
    isActive() { return true; },
    getSettings() { return { pointerSensitivity: 1, pointerAccel: true }; },
    getDisplayWidth() { return 1000; },
    getDisplayedWidth() { return 1000; }
  });
  control.mode = 'touchpad';
  const base = performance.now();
  const info = {
    mouse: false,
    x: 100,
    y: 100,
    cx: 100,
    cy: 100,
    startX: 100,
    startY: 100,
    startT: base - 5000,
    lastMoveT: base - 100,
    moved: true,
    norm: { x: 0, y: 0, inside: true },
    accX: 0,
    accY: 0
  };
  control.pointers.set(7, info);
  const move = (x) => ({
    pointerId: 7,
    pointerType: 'touch',
    clientX: x,
    clientY: 100,
    cancelable: false,
    preventDefault() {}
  });
  control._handleMove(move(130));
  assert.equal(sink.log.length, 1);
  assert.equal(sink.log[0].type, 'mm');
  // 30 px in ~100 ms -> speed 0.3 -> factor 0.84 -> ~25 px. Averaging over
  // the 5 s since pointer-down would give only ~18 px.
  approx(sink.log[0].dx, 30 * (0.6 + 0.3 * 0.8), 2, 'first move dx');
  assert.ok(info.lastMoveT > base - 100);
  info.lastMoveT = performance.now() - 20;
  control._handleMove(move(160));
  assert.equal(sink.log.length, 2);
  approx(sink.log[1].dx, 30 * (0.6 + 1.5 * 0.8), 8, 'second move dx');

  // Without a previous-move timestamp the fallback is pointer-down time, not
  // "now" (which would look like a 1 ms burst and max out the acceleration).
  control.pointers.clear();
  sink.log.length = 0;
  control.pointers.set(8, {
    mouse: false,
    x: 200,
    y: 100,
    cx: 200,
    cy: 100,
    startX: 200,
    startY: 100,
    startT: base - 1000,
    lastMoveT: 0,
    moved: true,
    norm: { x: 0, y: 0, inside: true },
    accX: 0,
    accY: 0
  });
  control._handleMove({
    pointerId: 8,
    pointerType: 'touch',
    clientX: 230,
    clientY: 100,
    cancelable: false,
    preventDefault() {}
  });
  approx(sink.log[0].dx, 30 * (0.6 + (30 / 1000) * 0.8), 2, 'pointer-down fallback dx');
});

function touchController() {
  const sink = recordingSender();
  const stage = {
    clientWidth: 1000,
    clientHeight: 800,
    getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 800 }; },
    setPointerCapture() {},
    releasePointerCapture() {}
  };
  const control = new PointerControl({
    stage: stage,
    sender: sink,
    getMode() { return 'touchpad'; },
    isActive() { return true; },
    getSettings() { return { pointerSensitivity: 1, pointerAccel: true }; },
    getDisplayWidth() { return 1000; },
    getDisplayedWidth() { return 1000; }
  });
  control.mode = 'touchpad';
  return { control: control, sink: sink };
}

function touchPoint(id, x, y) {
  return {
    pointerId: id,
    pointerType: 'touch',
    clientX: x,
    clientY: y,
    button: 0,
    cancelable: false,
    preventDefault() {}
  };
}

test('control: three-finger tap sends a middle click', () => {
  const harness = touchController();
  harness.control._handleDown(touchPoint(1, 100, 100));
  harness.control._handleDown(touchPoint(2, 140, 100));
  harness.control._handleDown(touchPoint(3, 180, 100));
  harness.control._handleUp(touchPoint(1, 100, 100));
  harness.control._handleUp(touchPoint(2, 140, 100));
  harness.control._handleUp(touchPoint(3, 180, 100));
  assert.deepEqual(harness.sink.log, [
    { type: 'mb', button: 'middle', down: true },
    { type: 'mb', button: 'middle', down: false }
  ]);
});

test('control: moving during a three-finger tap cancels the middle click', () => {
  const harness = touchController();
  harness.control._handleDown(touchPoint(1, 100, 100));
  harness.control._handleDown(touchPoint(2, 140, 100));
  harness.control._handleDown(touchPoint(3, 180, 100));
  harness.control._handleMove(touchPoint(2, 160, 112));
  assert.equal(harness.control.groupMoved, true);
  harness.control._handleUp(touchPoint(1, 100, 100));
  harness.control._handleUp(touchPoint(2, 160, 112));
  harness.control._handleUp(touchPoint(3, 180, 100));
  assert.deepEqual(harness.sink.log, []);
});

/* ---------- gamepad.js ---------- */

test('gamepad: default mapping covers every button and stick', () => {
  assert.deepEqual(defaultGamepadMapping(), {
    a: 'Space',
    b: 'ControlLeft',
    x: 'KeyR',
    y: 'KeyE',
    lb: 'KeyQ',
    rb: 'KeyF',
    lt: 'MouseRight',
    rt: 'MouseLeft',
    back: 'Tab',
    start: 'Escape',
    ls: 'ShiftLeft',
    rs: 'none',
    dpadUp: 'ArrowUp',
    dpadDown: 'ArrowDown',
    dpadLeft: 'ArrowLeft',
    dpadRight: 'ArrowRight',
    leftStick: 'wasd',
    rightStick: 'mouse'
  });
  assert.equal(GAMEPAD_BUTTON_DEFS.length, 16);
});

test('gamepad: normalizeGamepadMapping repairs corrupt values', () => {
  assert.deepEqual(normalizeGamepadMapping(null), defaultGamepadMapping());
  assert.deepEqual(normalizeGamepadMapping('nope'), defaultGamepadMapping());
  const fixed = normalizeGamepadMapping({
    a: 'KeyZZ',
    b: 42,
    x: 'none',
    lt: 'MouseX1',
    leftStick: 'keyboard',
    rightStick: 'arrows',
    extra: 'KeyQ'
  });
  assert.equal(fixed.a, 'Space');
  assert.equal(fixed.b, 'ControlLeft');
  assert.equal(fixed.x, 'none');
  assert.equal(fixed.lt, 'MouseX1');
  assert.equal(fixed.leftStick, 'wasd');
  assert.equal(fixed.rightStick, 'arrows');
  assert.equal(Object.prototype.hasOwnProperty.call(fixed, 'extra'), false);
  assert.deepEqual(Object.keys(fixed).sort(), Object.keys(defaultGamepadMapping()).sort());
});

test('gamepad: gamepadButtonValue reads numbers, objects and flags', () => {
  assert.equal(gamepadButtonValue(null, 0), 0);
  assert.equal(gamepadButtonValue({ buttons: [] }, 0), 0);
  assert.equal(gamepadButtonValue({ buttons: [0.75] }, 0), 0.75);
  assert.equal(gamepadButtonValue({ buttons: [{ value: 0.3, pressed: false }] }, 0), 0.3);
  assert.equal(gamepadButtonValue({ buttons: [{ value: 0, pressed: true }] }, 0), 1);
  assert.equal(gamepadButtonValue({ buttons: [{ value: 0, pressed: false }] }, 0), 0);
  assert.equal(gamepadButtonValue({ buttons: [0.75] }, 4), 0);
});

test('gamepad: gamepadPressed applies the analog trigger threshold', () => {
  assert.equal(TRIGGER_THRESHOLD, 0.5);
  assert.equal(gamepadPressed({ buttons: [{ value: 0.5, pressed: false }] }, 0), false);
  assert.equal(gamepadPressed({ buttons: [{ value: 0.51, pressed: false }] }, 0), true);
  assert.equal(gamepadPressed({ buttons: [{ value: 0.2, pressed: true }] }, 0), true);
  assert.equal(gamepadPressed({ buttons: [{ value: 0, pressed: false }] }, 0), false);
  assert.equal(gamepadPressed(null, 0), false);
});

test('gamepad: gamepadAxes reads both sticks with numeric fallbacks', () => {
  const pad = { axes: [0.5, -0.25, 0.1, 0.9] };
  assert.deepEqual(gamepadAxes(pad, 'left'), { x: 0.5, y: -0.25 });
  assert.deepEqual(gamepadAxes(pad, 'right'), { x: 0.1, y: 0.9 });
  assert.deepEqual(gamepadAxes({ axes: [] }, 'left'), { x: 0, y: 0 });
  assert.deepEqual(gamepadAxes(null, 'right'), { x: 0, y: 0 });
  assert.deepEqual(gamepadAxes({ axes: [NaN, undefined, null, '0.4'] }, 'right'),
    { x: 0, y: 0.4 });
});

test('gamepad: _releasePad releases every held button target', () => {
  const sink = recordingSender();
  const control = new GamepadController({ sender: sink });
  const state = control._stateFor(0);
  state.buttons.a = { pressed: true, target: 'Space' };
  state.buttons.rt = { pressed: true, target: 'MouseLeft' };
  state.buttons.lt = { pressed: true, target: 'WheelUp' };
  state.buttons.b = { pressed: false, target: 'none' };
  state.stickKeys.left = { target: 'wasd', keys: ['KeyW', 'KeyA'] };
  state.stickKeys.right = { target: 'arrows', keys: ['ArrowRight'] };
  state.stickMouse.left = { active: true, x: 1, y: 0 };
  control._releasePad(state, true);
  assert.deepEqual(sink.log, [
    { type: 'ku', code: 'Space' },
    { type: 'mb', button: 'left', down: false },
    { type: 'ku', code: 'KeyW' },
    { type: 'ku', code: 'KeyA' },
    { type: 'ku', code: 'ArrowRight' }
  ]);
  assert.deepEqual(state.buttons, {});
  assert.deepEqual(state.stickKeys.left, { target: 'none', keys: [] });
  assert.deepEqual(state.stickMouse.left, { active: false, x: 0, y: 0 });
});

test('gamepad: _releasePad with send=false only clears state', () => {
  const sink = recordingSender();
  const control = new GamepadController({ sender: sink });
  const state = control._stateFor(0);
  state.buttons.a = { pressed: true, target: 'Space' };
  state.stickKeys.left = { target: 'wasd', keys: ['KeyW'] };
  control._releasePad(state, false);
  assert.deepEqual(sink.log, []);
  assert.deepEqual(state.buttons, {});
  assert.deepEqual(state.stickKeys.left.keys, []);
});

test('gamepad: reset() releases held buttons by default', () => {
  const sink = recordingSender();
  const control = new GamepadController({ sender: sink });
  const state = control._stateFor(1);
  state.buttons.a = { pressed: true, target: 'Space' };
  control.reset();
  assert.deepEqual(sink.log, [{ type: 'ku', code: 'Space' }]);
  assert.equal(control.pads.size, 0);

  const quietSink = recordingSender();
  const quiet = new GamepadController({ sender: quietSink });
  const quietState = quiet._stateFor(2);
  quietState.buttons.a = { pressed: true, target: 'Space' };
  quiet.reset(false);
  assert.deepEqual(quietSink.log, []);
  assert.equal(quiet.pads.size, 0);
});

test('gamepad: disconnect releases held buttons before dropping the pad', () => {
  const sink = recordingSender();
  const control = new GamepadController({ sender: sink });
  const state = control._stateFor(2);
  state.buttons.rt = { pressed: true, target: 'MouseRight' };
  control._handleDisconnected({ gamepad: { index: 2 } });
  assert.deepEqual(sink.log, [{ type: 'mb', button: 'right', down: false }]);
  assert.equal(control.pads.has(2), false);
  assert.equal(control.connected, false);
});

test('gamepad: inactive tick releases held buttons instead of dropping them', () => {
  const sink = recordingSender();
  const control = new GamepadController({ sender: sink, isActive() { return false; } });
  control.running = true;
  control.last = 0;
  const state = control._stateFor(0);
  state.buttons.a = { pressed: true, target: 'KeyA' };
  const originalWindow = globalThis.window;
  globalThis.window = { requestAnimationFrame() { return 1; } };
  try {
    control._tick();
  } finally {
    if (originalWindow === undefined) {
      delete globalThis.window;
    } else {
      globalThis.window = originalWindow;
    }
  }
  assert.deepEqual(sink.log, [{ type: 'ku', code: 'KeyA' }]);
  assert.equal(control.pads.size, 0);
  assert.equal(control.raf, 1);
});

test('gamepad: _updateButtons only emits on state changes', () => {
  const sink = recordingSender();
  const control = new GamepadController({ sender: sink });
  const state = control._stateFor(0);
  const mapping = defaultGamepadMapping();
  const pressed = { buttons: [{ pressed: true, value: 1 }] };
  const released = { buttons: [{ pressed: false, value: 0 }] };
  control._updateButtons(pressed, state, mapping);
  control._updateButtons(pressed, state, mapping);
  assert.deepEqual(sink.log, [{ type: 'kd', code: 'Space' }]);
  control._updateButtons(released, state, mapping);
  assert.deepEqual(sink.log, [
    { type: 'kd', code: 'Space' },
    { type: 'ku', code: 'Space' }
  ]);
  const trigger = { buttons: [] };
  trigger.buttons[7] = { pressed: true, value: 0.9 };
  control._updateButtons(trigger, state, mapping);
  assert.deepEqual(sink.log[2], { type: 'mb', button: 'left', down: true });
});

/* ---------- i18n.js ---------- */

test('i18n: settings.gamepad exists in both dictionaries', () => {
  assert.equal(initI18n('en'), 'en');
  assert.equal(t('settings.gamepad'), 'Gamepad');
  assert.equal(initI18n('zh-CN'), 'zh');
  assert.equal(t('settings.gamepad'), '\u624b\u67c4');
  assert.equal(initI18n('en'), 'en');
});

test('i18n: dynamic settings prefixes resolve', () => {
  const keys = ['settings.gamepad'];
  for (let i = 0; i < QUALITY_PRESET_IDS.length; i += 1) {
    keys.push('quality.' + QUALITY_PRESET_IDS[i]);
  }
  for (let i = 0; i < GAMEPAD_BUTTON_DEFS.length; i += 1) {
    keys.push('gamepad.' + GAMEPAD_BUTTON_DEFS[i].field);
  }
  for (let i = 0; i < STICK_TARGETS.length; i += 1) {
    keys.push('gamepad.stick.' + STICK_TARGETS[i]);
  }
  for (let i = 0; i < MOUSE_TARGETS.length; i += 1) {
    keys.push('target.' + MOUSE_TARGETS[i]);
  }
  for (let i = 0; i < KEY_GROUP_DEFS.length; i += 1) {
    keys.push(KEY_GROUP_DEFS[i].labelKey);
  }
  const languages = ['en', 'zh'];
  for (let l = 0; l < languages.length; l += 1) {
    initI18n(languages[l]);
    for (let k = 0; k < keys.length; k += 1) {
      assert.notEqual(t(keys[k]), keys[k],
        'missing translation for ' + keys[k] + ' in ' + languages[l]);
    }
  }
  initI18n('en');
});

console.log('\nAll ' + passed + ' self-tests passed.');
