/*
 * Settings drawer: input sensitivity, viewer quality and gamepad mapping.
 * All persistence is owned by app.js; this module only edits the objects it
 * is given and reports changes through callbacks.
 */

import { t } from './i18n.js';
import { KEY_GROUP_DEFS, keyLabel } from './keycodes.js';
import { GAMEPAD_BUTTON_DEFS, normalizeGamepadMapping, STICK_TARGETS } from './gamepad.js';
import {
  BITRATE_MBPS,
  H264_FPS_OPTIONS,
  JPEG_FPS_OPTIONS,
  JPEG_QUALITY,
  QUALITY_PRESET_IDS,
  qualityParts
} from './quality.js';

export const INPUT_DEFAULTS = {
  pointerSensitivity: 1,
  pointerAccel: true,
  scrollSpeed: 1,
  naturalScroll: true,
  virtualStickSensitivity: 1,
  gamepadStickSensitivity: 1,
  deadzone: 0.15
};

export const INPUT_LIMITS = {
  pointerSensitivity: [0.2, 5, 0.1],
  scrollSpeed: [0.2, 5, 0.1],
  virtualStickSensitivity: [0.2, 5, 0.1],
  gamepadStickSensitivity: [0.2, 5, 0.1],
  deadzone: [0, 0.5, 0.01]
};

function clamp(value, min, max) {
  return value < min ? min : value > max ? max : value;
}

function boolOr(value, fallback) {
  return typeof value === 'boolean' ? value : fallback;
}

export function normalizeInputSettings(raw) {
  const source = raw && typeof raw === 'object' ? raw : {};
  const result = {};
  const keys = Object.keys(INPUT_LIMITS);
  for (let i = 0; i < keys.length; i += 1) {
    const key = keys[i];
    const limits = INPUT_LIMITS[key];
    const value = Number(source[key]);
    const fallback = INPUT_DEFAULTS[key];
    result[key] = Number.isFinite(value) ? clamp(value, limits[0], limits[1]) : fallback;
  }
  result.pointerAccel = boolOr(source.pointerAccel, INPUT_DEFAULTS.pointerAccel);
  result.naturalScroll = boolOr(source.naturalScroll, INPUT_DEFAULTS.naturalScroll);
  return result;
}

function formatSetting(key, value) {
  if (key === 'deadzone') {
    return Number(value).toFixed(2);
  }
  return Number(value).toFixed(1);
}

function byId(id) {
  return document.getElementById(id);
}

function formatMbps(kbps) {
  const value = Number(kbps) / 1000;
  if (!Number.isFinite(value)) {
    return '';
  }
  return (Math.round(value * 10) / 10) + ' Mbps';
}

export function describeStreamQuality(quality) {
  const parts = qualityParts(quality);
  if (!parts) {
    return '';
  }
  const bits = [];
  const preset = QUALITY_PRESET_IDS.indexOf(parts.preset) >= 0 ? parts.preset : 'standard';
  bits.push(t('quality.' + preset));
  if (parts.width > 0 && parts.height > 0) {
    bits.push(parts.width + '\u00d7' + parts.height);
  }
  if (parts.fps > 0) {
    bits.push(parts.fps + ' ' + t('units.fps'));
  }
  if (parts.bitrateKbps > 0) {
    bits.push(formatMbps(parts.bitrateKbps));
  }
  if (parts.jpegQuality > 0) {
    bits.push('JPEG ' + parts.jpegQuality);
  }
  return bits.join(' \u00b7 ');
}

export class SettingsPanel {
  constructor(options) {
    const opts = options || {};
    this.getInput = opts.getInput;
    this.onInputChange = opts.onInputChange || function () {};
    this.getQuality = opts.getQuality;
    this.onQualityChange = opts.onQualityChange || function () {};
    this.getGamepad = opts.getGamepad;
    this.onGamepadChange = opts.onGamepadChange || function () {};
    this.getCodec = opts.getCodec || function () { return 'h264'; };
    this.getAutoLevel = opts.getAutoLevel || function () { return 'standard'; };
    this.onOpen = opts.onOpen || null;
    this.onClose = opts.onClose || null;

    this.root = byId('settings');
    this.panel = byId('settings-panel');
    this.qualityAllowed = true;
    this.gamepadConnected = false;
    this.gamepadBuilt = false;
    this.actualQuality = null;
    this.opened = false;

    this._bind();
  }

  isOpen() {
    return this.opened;
  }

  _bind() {
    const close = byId('settings-close');
    if (close) {
      close.addEventListener('click', () => this.close());
    }
    const backdrop = byId('settings-backdrop');
    if (backdrop) {
      backdrop.addEventListener('click', () => this.close());
    }

    const controlKeys = Object.keys(INPUT_LIMITS);
    for (let i = 0; i < controlKeys.length; i += 1) {
      const key = controlKeys[i];
      const range = byId('set-' + this._domName(key));
      const output = byId('set-' + this._domName(key) + '-val');
      if (range) {
        range.addEventListener('input', () => {
          const settings = this.getInput();
          settings[key] = clamp(Number(range.value), INPUT_LIMITS[key][0], INPUT_LIMITS[key][1]);
          if (output) {
            output.textContent = formatSetting(key, settings[key]);
          }
          this.onInputChange(settings);
        });
      }
    }
    const accel = byId('set-pointer-accel');
    if (accel) {
      accel.addEventListener('change', () => {
        const settings = this.getInput();
        settings.pointerAccel = !!accel.checked;
        this.onInputChange(settings);
      });
    }
    const natural = byId('set-natural-scroll');
    if (natural) {
      natural.addEventListener('change', () => {
        const settings = this.getInput();
        settings.naturalScroll = !!natural.checked;
        this.onInputChange(settings);
      });
    }
    const reset = byId('set-control-reset');
    if (reset) {
      reset.addEventListener('click', () => {
        const settings = this.getInput();
        const defaults = normalizeInputSettings(INPUT_DEFAULTS);
        Object.keys(defaults).forEach((key) => {
          settings[key] = defaults[key];
        });
        this.onInputChange(settings);
        this.syncInput();
      });
    }

    const presets = this.root ? this.root.querySelectorAll('[data-quality]') : [];
    for (let i = 0; i < presets.length; i += 1) {
      presets[i].addEventListener('click', () => {
        if (!this.qualityAllowed) {
          return;
        }
        const preset = presets[i].getAttribute('data-quality');
        if (QUALITY_PRESET_IDS.indexOf(preset) < 0) {
          return;
        }
        const quality = this.getQuality();
        quality.preset = preset;
        this.onQualityChange(quality);
        this.syncQuality();
      });
    }

    const h264Res = byId('quality-h264-res');
    const h264Fps = byId('quality-h264-fps');
    const h264Bitrate = byId('quality-h264-bitrate');
    const h264BitrateValue = byId('quality-h264-bitrate-val');
    const jpegRes = byId('quality-jpeg-res');
    const jpegFps = byId('quality-jpeg-fps');
    const jpegQuality = byId('quality-jpeg-quality');
    const jpegQualityValue = byId('quality-jpeg-quality-val');
    this.lowLatencyBtn = byId('quality-lowlatency');

    if (this.lowLatencyBtn) {
      this.lowLatencyBtn.addEventListener('click', () => {
        if (!this.qualityAllowed) {
          return;
        }
        const quality = this.getQuality();
        quality.preset = 'custom';
        quality.custom.h264.resolution = '1080p';
        quality.custom.h264.fps = 60;
        quality.custom.h264.bitrateMbps = 10;
        this.onQualityChange(quality);
        this.syncQuality();
      });
    }

    const markCustom = () => {
      if (!this.qualityAllowed) {
        return;
      }
      const quality = this.getQuality();
      quality.preset = 'custom';
      this.onQualityChange(quality);
      this.syncQuality();
    };

    if (h264Res) {
      h264Res.addEventListener('change', () => {
        this.getQuality().custom.h264.resolution = h264Res.value;
        markCustom();
      });
    }
    if (h264Fps) {
      h264Fps.addEventListener('change', () => {
        this.getQuality().custom.h264.fps = Number(h264Fps.value) || 60;
        markCustom();
      });
    }
    if (h264Bitrate) {
      h264Bitrate.addEventListener('input', () => {
        this.getQuality().custom.h264.bitrateMbps = Math.round(Number(h264Bitrate.value) || 10);
        if (h264BitrateValue) {
          h264BitrateValue.textContent = this.getQuality().custom.h264.bitrateMbps + ' Mbps';
        }
        markCustom();
      });
    }
    if (jpegRes) {
      jpegRes.addEventListener('change', () => {
        this.getQuality().custom.jpeg.resolution = jpegRes.value;
        markCustom();
      });
    }
    if (jpegFps) {
      jpegFps.addEventListener('change', () => {
        this.getQuality().custom.jpeg.fps = Number(jpegFps.value) || 15;
        markCustom();
      });
    }
    if (jpegQuality) {
      jpegQuality.addEventListener('input', () => {
        this.getQuality().custom.jpeg.quality = Math.round(Number(jpegQuality.value) || 70);
        if (jpegQualityValue) {
          jpegQualityValue.textContent = String(this.getQuality().custom.jpeg.quality);
        }
        markCustom();
      });
    }

    if (typeof document !== 'undefined') {
      document.addEventListener('keydown', (event) => {
        if (!this.opened || event.key !== 'Escape') {
          return;
        }
        event.preventDefault();
        this.close();
      }, true);
    }
  }

  _domName(key) {
    const map = {
      pointerSensitivity: 'pointer-sens',
      scrollSpeed: 'scroll-speed',
      virtualStickSensitivity: 'virtual-stick',
      gamepadStickSensitivity: 'gamepad-stick',
      deadzone: 'deadzone'
    };
    return map[key] || key;
  }

  open() {
    this.opened = true;
    if (this.root) {
      this.root.hidden = false;
    }
    this.syncInput();
    this.syncQuality();
    this.syncGamepad();
    if (this.onOpen) {
      this.onOpen();
    }
  }

  close() {
    this.opened = false;
    if (this.root) {
      this.root.hidden = true;
    }
    if (this.onClose) {
      this.onClose();
    }
  }

  syncInput() {
    const settings = normalizeInputSettings(this.getInput());
    const keys = Object.keys(INPUT_LIMITS);
    for (let i = 0; i < keys.length; i += 1) {
      const key = keys[i];
      const range = byId('set-' + this._domName(key));
      const output = byId('set-' + this._domName(key) + '-val');
      if (range) {
        range.value = String(settings[key]);
      }
      if (output) {
        output.textContent = formatSetting(key, settings[key]);
      }
    }
    const accel = byId('set-pointer-accel');
    if (accel) {
      accel.checked = !!settings.pointerAccel;
    }
    const natural = byId('set-natural-scroll');
    if (natural) {
      natural.checked = !!settings.naturalScroll;
    }
  }

  syncQuality() {
    const quality = this.getQuality();
    const preset = quality && QUALITY_PRESET_IDS.indexOf(quality.preset) >= 0
      ? quality.preset : 'standard';
    const presets = this.root ? this.root.querySelectorAll('[data-quality]') : [];
    for (let i = 0; i < presets.length; i += 1) {
      const active = presets[i].getAttribute('data-quality') === preset;
      presets[i].classList.toggle('is-selected', active);
      presets[i].setAttribute('aria-pressed', active ? 'true' : 'false');
      presets[i].disabled = !this.qualityAllowed;
    }
    if (this.lowLatencyBtn) {
      this.lowLatencyBtn.disabled = !this.qualityAllowed;
    }

    const custom = quality.custom;
    const h264Res = byId('quality-h264-res');
    if (h264Res) {
      h264Res.value = custom.h264.resolution;
      h264Res.disabled = !this.qualityAllowed;
    }
    const h264Fps = byId('quality-h264-fps');
    if (h264Fps) {
      h264Fps.value = String(custom.h264.fps);
      h264Fps.disabled = !this.qualityAllowed;
    }
    const h264Bitrate = byId('quality-h264-bitrate');
    if (h264Bitrate) {
      h264Bitrate.value = String(custom.h264.bitrateMbps);
      h264Bitrate.disabled = !this.qualityAllowed;
    }
    const h264BitrateValue = byId('quality-h264-bitrate-val');
    if (h264BitrateValue) {
      h264BitrateValue.textContent = custom.h264.bitrateMbps + ' Mbps';
    }
    const jpegRes = byId('quality-jpeg-res');
    if (jpegRes) {
      jpegRes.value = custom.jpeg.resolution;
      jpegRes.disabled = !this.qualityAllowed;
    }
    const jpegFps = byId('quality-jpeg-fps');
    if (jpegFps) {
      jpegFps.value = String(custom.jpeg.fps);
      jpegFps.disabled = !this.qualityAllowed;
    }
    const jpegQuality = byId('quality-jpeg-quality');
    if (jpegQuality) {
      jpegQuality.value = String(custom.jpeg.quality);
      jpegQuality.disabled = !this.qualityAllowed;
    }
    const jpegQualityValue = byId('quality-jpeg-quality-val');
    if (jpegQualityValue) {
      jpegQualityValue.textContent = String(custom.jpeg.quality);
    }

    const h264Box = byId('quality-custom-h264');
    if (h264Box) {
      h264Box.hidden = preset !== 'custom' || this.getCodec() !== 'h264';
    }
    const jpegBox = byId('quality-custom-jpeg');
    if (jpegBox) {
      jpegBox.hidden = preset !== 'custom' || this.getCodec() !== 'jpeg';
    }
    const fixed = byId('quality-fixed');
    if (fixed) {
      fixed.hidden = this.qualityAllowed;
    }
    this.updateQualityActual(this.actualQuality);
  }

  setQualityAllowed(allowed) {
    this.qualityAllowed = !!allowed;
    this.syncQuality();
  }

  updateQualityActual(quality) {
    if (quality !== undefined) {
      this.actualQuality = quality;
    }
    const node = byId('quality-actual');
    if (!node) {
      return;
    }
    const description = describeStreamQuality(this.actualQuality);
    if (!description) {
      node.textContent = t('quality.waiting');
      return;
    }
    const prefs = this.getQuality();
    if (prefs && prefs.preset === 'auto') {
      node.textContent = t('quality.autoCurrent', {
        level: t('quality.' + this.getAutoLevel())
      }) + ' \u00b7 ' + description;
    } else {
      node.textContent = description;
    }
  }

  setGamepadConnected(connected) {
    this.gamepadConnected = !!connected;
    if (this.gamepadConnected && !this.gamepadBuilt) {
      this._buildGamepad();
      this.gamepadBuilt = true;
    }
    this.syncGamepad();
  }

  _buildGamepad() {
    const root = byId('gamepad-mappings');
    if (!root) {
      return;
    }
    root.innerHTML = '';
    const mapping = normalizeGamepadMapping(this.getGamepad());
    const makeRow = (labelText) => {
      const row = document.createElement('div');
      row.className = 'setting-row setting-row-select';
      const label = document.createElement('label');
      label.className = 'setting-label';
      label.textContent = labelText;
      const select = document.createElement('select');
      select.className = 'select';
      row.appendChild(label);
      row.appendChild(select);
      root.appendChild(row);
      return select;
    };

    for (let i = 0; i < GAMEPAD_BUTTON_DEFS.length; i += 1) {
      const def = GAMEPAD_BUTTON_DEFS[i];
      const select = makeRow(t(def.labelKey));
      this._fillTargetSelect(select, mapping[def.field]);
      select.addEventListener('change', () => {
        const target = normalizeGamepadMapping(this.getGamepad());
        target[def.field] = select.value;
        this.onGamepadChange(target);
      });
    }
    const stickLabels = { leftStick: 'gamepad.leftStick', rightStick: 'gamepad.rightStick' };
    ['leftStick', 'rightStick'].forEach((field) => {
      const select = makeRow(t(stickLabels[field]));
      for (let i = 0; i < STICK_TARGETS.length; i += 1) {
        const option = document.createElement('option');
        option.value = STICK_TARGETS[i];
        option.textContent = t('gamepad.stick.' + STICK_TARGETS[i]);
        select.appendChild(option);
      }
      select.value = mapping[field];
      select.addEventListener('change', () => {
        const target = normalizeGamepadMapping(this.getGamepad());
        target[field] = select.value;
        this.onGamepadChange(target);
      });
    });
  }

  _fillTargetSelect(select, value) {
    const none = document.createElement('option');
    none.value = 'none';
    none.textContent = t('gamepad.none');
    select.appendChild(none);
    for (let g = 0; g < KEY_GROUP_DEFS.length; g += 1) {
      const group = KEY_GROUP_DEFS[g];
      const optgroup = document.createElement('optgroup');
      optgroup.label = t(group.labelKey);
      for (let i = 0; i < group.codes.length; i += 1) {
        const code = group.codes[i];
        const option = document.createElement('option');
        option.value = code;
        option.textContent = code.indexOf('Mouse') === 0 || code.indexOf('Wheel') === 0
          ? t('target.' + code)
          : keyLabel(code);
        optgroup.appendChild(option);
      }
      select.appendChild(optgroup);
    }
    select.value = value || 'none';
  }

  syncGamepad() {
    const section = byId('gamepad-section');
    if (section) {
      section.hidden = !this.gamepadConnected;
    }
    if (!this.gamepadConnected || !this.gamepadBuilt) {
      return;
    }
    const root = byId('gamepad-mappings');
    if (!root) {
      return;
    }
    const mapping = normalizeGamepadMapping(this.getGamepad());
    const selects = root.querySelectorAll('select');
    let index = 0;
    for (let i = 0; i < GAMEPAD_BUTTON_DEFS.length; i += 1) {
      if (selects[index]) {
        selects[index].value = mapping[GAMEPAD_BUTTON_DEFS[i].field];
      }
      index += 1;
    }
    if (selects[index]) {
      selects[index].value = mapping.leftStick;
    }
    if (selects[index + 1]) {
      selects[index + 1].value = mapping.rightStick;
    }
  }
}
