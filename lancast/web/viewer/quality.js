/*
 * Viewer-side quality policy.
 *
 * The server only understands concrete presets; "auto" lives entirely in the
 * browser and is implemented as the state machine at the bottom of this file.
 * No DOM access, so selftest.mjs can unit-test the policy in Node.
 */

export const QUALITY_PRESET_IDS = ['auto', 'saver', 'smooth', 'standard', 'high', 'native', 'custom'];
export const CONCRETE_PRESETS = ['saver', 'smooth', 'standard', 'high', 'native'];
export const AUTO_LEVELS = ['saver', 'smooth', 'standard'];

export const H264_FPS_OPTIONS = [15, 30, 45, 60, 90, 120];
export const JPEG_FPS_OPTIONS = [5, 10, 15, 20, 30];

export const RESOLUTION_OPTIONS = [
  { id: '720p', width: 1280, height: 720 },
  { id: '1080p', width: 1920, height: 1080 },
  { id: '1440p', width: 2560, height: 1440 },
  { id: '2160p', width: 3840, height: 2160 },
  { id: 'source', width: 0, height: 0 }
];

export const BITRATE_MBPS = { min: 1, max: 100, step: 1, def: 10 };
export const JPEG_QUALITY = { min: 30, max: 95, step: 1, def: 70 };

function clampNumber(value, min, max, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return fallback;
  }
  return n < min ? min : n > max ? max : n;
}

function oneOf(value, list, fallback) {
  return list.indexOf(value) >= 0 ? value : fallback;
}

function resolutionId(value) {
  for (let i = 0; i < RESOLUTION_OPTIONS.length; i += 1) {
    if (RESOLUTION_OPTIONS[i].id === value) {
      return value;
    }
  }
  return '1080p';
}

export function defaultQualityPreferences() {
  return {
    preset: 'auto',
    custom: {
      h264: { resolution: '1080p', fps: 60, bitrateMbps: BITRATE_MBPS.def },
      jpeg: { resolution: '1080p', fps: 15, quality: JPEG_QUALITY.def }
    }
  };
}

/** Coerce anything (including a corrupt localStorage value) into a valid shape. */
export function normalizeQualityPreferences(raw) {
  const base = defaultQualityPreferences();
  if (!raw || typeof raw !== 'object') {
    return base;
  }
  const preset = QUALITY_PRESET_IDS.indexOf(raw.preset) >= 0 ? raw.preset : base.preset;
  const custom = raw.custom && typeof raw.custom === 'object' ? raw.custom : {};
  const h264 = custom.h264 && typeof custom.h264 === 'object' ? custom.h264 : {};
  const jpeg = custom.jpeg && typeof custom.jpeg === 'object' ? custom.jpeg : {};
  return {
    preset: preset,
    custom: {
      h264: {
        resolution: resolutionId(h264.resolution),
        fps: oneOf(Number(h264.fps), H264_FPS_OPTIONS, base.custom.h264.fps),
        bitrateMbps: Math.round(clampNumber(
          h264.bitrateMbps, BITRATE_MBPS.min, BITRATE_MBPS.max, base.custom.h264.bitrateMbps))
      },
      jpeg: {
        resolution: resolutionId(jpeg.resolution),
        fps: oneOf(Number(jpeg.fps), JPEG_FPS_OPTIONS, base.custom.jpeg.fps),
        quality: Math.round(clampNumber(
          jpeg.quality, JPEG_QUALITY.min, JPEG_QUALITY.max, base.custom.jpeg.quality))
      }
    }
  };
}

/** Resolve a resolution token to pixel dimensions for the watched display. */
export function resolveResolution(token, display) {
  for (let i = 0; i < RESOLUTION_OPTIONS.length; i += 1) {
    const option = RESOLUTION_OPTIONS[i];
    if (option.id === token) {
      if (option.width > 0) {
        return { width: option.width, height: option.height };
      }
      const width = Math.round(Number(display && display.width) || 0);
      const height = Math.round(Number(display && display.height) || 0);
      if (width > 0 && height > 0) {
        return { width: width, height: height };
      }
      return { width: 1920, height: 1080 };
    }
  }
  return { width: 1920, height: 1080 };
}

/**
 * Build the `quality` object for a `watch` message.
 * `autoLevel` is only used when the preference is "auto".
 */
export function buildWatchQuality(preferences, codec, display, autoLevel) {
  const prefs = normalizeQualityPreferences(preferences);
  let preset = prefs.preset;
  if (preset === 'auto') {
    preset = AUTO_LEVELS.indexOf(autoLevel) >= 0 ? autoLevel : 'standard';
  }
  if (preset !== 'custom') {
    return { preset: preset };
  }
  if (codec === 'jpeg') {
    const custom = prefs.custom.jpeg;
    const size = resolveResolution(custom.resolution, display);
    return {
      preset: 'custom',
      max_width: size.width,
      max_height: size.height,
      fps: custom.fps,
      jpeg_quality: custom.quality
    };
  }
  const custom = prefs.custom.h264;
  const size = resolveResolution(custom.resolution, display);
  return {
    preset: 'custom',
    max_width: size.width,
    max_height: size.height,
    fps: custom.fps,
    bitrate_kbps: custom.bitrateMbps * 1000
  };
}

/** Split a stream.quality object into display-friendly numeric parts. */
export function qualityParts(quality) {
  if (!quality || typeof quality !== 'object') {
    return null;
  }
  const preset = typeof quality.preset === 'string' && quality.preset ? quality.preset : 'standard';
  const width = Math.round(Number(quality.max_width) || 0);
  const height = Math.round(Number(quality.max_height) || 0);
  const fps = Math.round(Number(quality.fps) || 0);
  const bitrateKbps = Math.round(Number(quality.bitrate_kbps) || 0);
  const jpegQuality = Math.round(Number(quality.jpeg_quality) || 0);
  return {
    preset: preset,
    width: width,
    height: height,
    fps: fps,
    bitrateKbps: bitrateKbps,
    jpegQuality: jpegQuality
  };
}

export function autoLevelIndex(level) {
  return AUTO_LEVELS.indexOf(level);
}

/**
 * Auto quality state machine from the viewer spec.
 *
 * Call tick() once per 2 s with cumulative decoder-drop count, median latency
 * over the last 4 s and current decodeQueueSize. The returned object says
 * whether the preset should change. Changes are followed by a 6 s cooldown.
 */
export class AutoQuality {
  constructor(options) {
    const opts = options || {};
    this.cooldownMs = Number(opts.cooldownMs) >= 0 ? Number(opts.cooldownMs) : 6000;
    this.goodMs = Number(opts.goodMs) >= 0 ? Number(opts.goodMs) : 30000;
    this.level = AUTO_LEVELS.indexOf(opts.level) >= 0 ? opts.level : 'standard';
    this.lastSwitch = null;
    this.queueStrikes = 0;
    this.goodSince = null;
    this.droppedHistory = [];
    this.lastTick = null;
  }

  reset(now) {
    this.level = 'standard';
    this.lastSwitch = null;
    this.queueStrikes = 0;
    this.goodSince = null;
    this.droppedHistory = [];
    this.lastTick = Number.isFinite(Number(now)) ? Number(now) : null;
  }

  setLevel(level, now) {
    if (AUTO_LEVELS.indexOf(level) < 0) {
      return this.level;
    }
    this.level = level;
    this.lastSwitch = Number.isFinite(Number(now)) ? Number(now) : null;
    this.goodSince = null;
    this.queueStrikes = 0;
    this.droppedHistory = [];
    return this.level;
  }

  _droppedDelta(now, dropped) {
    const history = this.droppedHistory;
    const cutoff = now - 4000;
    history.push({ t: now, dropped: dropped });
    while (history.length > 2 && history[1].t <= cutoff) {
      history.shift();
    }
    while (history.length > 40) {
      history.shift();
    }
    const base = history[0];
    const delta = dropped - (base ? base.dropped : dropped);
    return delta < 0 ? 0 : delta;
  }

  _changeTo(level, now, dropped, direction, reason) {
    this.level = level;
    this.lastSwitch = now;
    this.goodSince = null;
    this.queueStrikes = 0;
    this.droppedHistory = [{ t: now, dropped: dropped }];
    return { level: level, changed: true, direction: direction, reason: reason, cooldownMs: this.cooldownMs };
  }

  tick(now, metrics) {
    const time = Number.isFinite(Number(now)) ? Number(now) : 0;
    const stats = metrics || {};
    const dropped = Math.max(0, Number(stats.dropped) || 0);
    const latency = Number(stats.latencyMs);
    const latencyKnown = Number.isFinite(latency) && latency >= 0;
    const queue = Math.max(0, Number(stats.decodeQueue) || 0);
    this.lastTick = time;

    const droppedDelta = this._droppedDelta(time, dropped);
    if (queue > 2) {
      this.queueStrikes += 1;
    } else {
      this.queueStrikes = 0;
    }

    if (this.lastSwitch != null && time - this.lastSwitch < this.cooldownMs) {
      this.goodSince = null;
      return {
        level: this.level,
        changed: false,
        direction: null,
        reason: 'cooldown',
        cooldown: true,
        droppedDelta: droppedDelta
      };
    }

    const tooManyDrops = droppedDelta > 8;
    const tooMuchLatency = latencyKnown && latency > 180;
    const queueStuck = this.queueStrikes >= 2;

    if (this.level !== 'saver' && (tooManyDrops || tooMuchLatency || queueStuck)) {
      const next = AUTO_LEVELS[Math.max(0, AUTO_LEVELS.indexOf(this.level) - 1)];
      return this._changeTo(next, time, dropped, 'down',
        tooManyDrops ? 'dropped' : tooMuchLatency ? 'latency' : 'queue');
    }

    const good = droppedDelta === 0 && latencyKnown && latency < 90;
    if (good) {
      if (this.goodSince == null) {
        this.goodSince = time;
      }
    } else {
      this.goodSince = null;
    }

    if (this.level !== 'standard' && this.goodSince != null &&
        time - this.goodSince >= this.goodMs) {
      const next = AUTO_LEVELS[Math.min(AUTO_LEVELS.length - 1, AUTO_LEVELS.indexOf(this.level) + 1)];
      return this._changeTo(next, time, dropped, 'up', 'stable');
    }

    return {
      level: this.level,
      changed: false,
      direction: null,
      reason: 'steady',
      cooldown: false,
      droppedDelta: droppedDelta,
      goodForMs: this.goodSince == null ? 0 : time - this.goodSince
    };
  }
}
