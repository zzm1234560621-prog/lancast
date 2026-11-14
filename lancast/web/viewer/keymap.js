/*
 * On-screen key layouts: data model, validation, built-in presets, the
 * touchable key layer and the layout editor.
 *
 * The pure model helpers (validateLayout, stickKeys, buildPresetLayouts) are
 * imported by selftest.mjs. The DOM classes are only constructed in app.js.
 */

import { t } from './i18n.js';
import {
  INPUT_TARGETS,
  KEY_GROUP_DEFS,
  isInputTarget,
  isMouseButtonTarget,
  isWheelTarget,
  keyLabel,
  mouseButtonFor,
  targetEvent,
  wheelDeltaFor
} from './keycodes.js';

export const LAYOUTS_STORAGE_KEY = 'lancast.layouts';
export const LAYOUT_STORAGE_KEY = 'lancast.layout';
export const ITEM_SIZE_MIN = 44;
export const ITEM_SIZE_MAX = 200;
export const ITEM_KEYS_MAX = 4;
export const LABEL_MAX = 12;
export const NAME_MAX = 40;
export const ITEMS_MAX = 60;

const ITEM_MODES = ['hold', 'toggle', 'tap'];
const STICK_TARGETS = ['wasd', 'arrows', 'mouse'];

function clamp(value, min, max) {
  return value < min ? min : value > max ? max : value;
}

function isPlainObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function cleanString(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function unknownField(object, allowed) {
  const keys = Object.keys(object);
  for (let i = 0; i < keys.length; i += 1) {
    if (allowed.indexOf(keys[i]) < 0) {
      return keys[i];
    }
  }
  return null;
}

export function cloneLayout(layout) {
  return deepClone(layout);
}

/** Deterministic-ish unique id for a new item. */
export function makeItemId(prefix, items) {
  const used = new Set();
  const list = Array.isArray(items) ? items : [];
  for (let i = 0; i < list.length; i += 1) {
    used.add(list[i] && list[i].id);
  }
  let counter = 1;
  while (used.has(prefix + counter)) {
    counter += 1;
  }
  return prefix + counter;
}

function fail(error) {
  return { ok: false, layout: null, error: error };
}

/**
 * Strict layout validation used for imports and for localStorage recovery.
 * Returns {ok:true, layout:<normalised copy>} or {ok:false,error:"..."}.
 */
export function validateLayout(input) {
  if (!isPlainObject(input)) {
    return fail('layout must be an object');
  }
  const topExtra = unknownField(input, ['name', 'items']);
  if (topExtra) {
    return fail('unknown layout field: ' + topExtra);
  }
  const name = cleanString(input.name);
  if (!name) {
    return fail('name must be a non-empty string');
  }
  if (name.length > NAME_MAX) {
    return fail('name is longer than ' + NAME_MAX + ' characters');
  }
  if (!Array.isArray(input.items)) {
    return fail('items must be an array');
  }
  if (input.items.length < 1) {
    return fail('items must contain at least one item');
  }
  if (input.items.length > ITEMS_MAX) {
    return fail('items may contain at most ' + ITEMS_MAX + ' items');
  }

  const items = [];
  const ids = new Set();
  for (let i = 0; i < input.items.length; i += 1) {
    const raw = input.items[i];
    const where = 'items[' + i + ']';
    if (!isPlainObject(raw)) {
      return fail(where + ' must be an object');
    }
    const itemExtra = unknownField(raw,
      ['id', 'type', 'label', 'keys', 'target', 'x', 'y', 'size', 'mode']);
    if (itemExtra) {
      return fail(where + ' has an unknown field: ' + itemExtra);
    }
    const id = cleanString(raw.id);
    if (!id) {
      return fail(where + '.id must be a non-empty string');
    }
    if (id.length > 40) {
      return fail(where + '.id is longer than 40 characters');
    }
    if (ids.has(id)) {
      return fail(where + '.id is duplicated');
    }
    ids.add(id);

    const x = raw.x;
    const y = raw.y;
    if (typeof x !== 'number' || !Number.isFinite(x) || x < 0 || x > 1) {
      return fail(where + '.x must be a number between 0 and 1');
    }
    if (typeof y !== 'number' || !Number.isFinite(y) || y < 0 || y > 1) {
      return fail(where + '.y must be a number between 0 and 1');
    }
    const size = raw.size;
    if (typeof size !== 'number' || !Number.isFinite(size) ||
        size < ITEM_SIZE_MIN || size > ITEM_SIZE_MAX) {
      return fail(where + '.size must be between ' + ITEM_SIZE_MIN + ' and ' + ITEM_SIZE_MAX);
    }

    if (raw.type === 'button') {
      const buttonExtra = unknownField(raw,
        ['id', 'type', 'label', 'keys', 'x', 'y', 'size', 'mode']);
      if (buttonExtra) {
        return fail(where + ' has a field not valid for a button: ' + buttonExtra);
      }
      const label = cleanString(raw.label);
      if (!label) {
        return fail(where + '.label must be a non-empty string');
      }
      if (label.length > LABEL_MAX) {
        return fail(where + '.label is longer than ' + LABEL_MAX + ' characters');
      }
      if (!Array.isArray(raw.keys) || raw.keys.length < 1 || raw.keys.length > ITEM_KEYS_MAX) {
        return fail(where + '.keys must contain 1 to ' + ITEM_KEYS_MAX + ' targets');
      }
      const keys = [];
      const seen = new Set();
      for (let j = 0; j < raw.keys.length; j += 1) {
        const key = raw.keys[j];
        if (typeof key !== 'string' || !isInputTarget(key)) {
          return fail(where + '.keys[' + j + '] is not a supported key or mouse target: ' +
            String(key));
        }
        if (seen.has(key)) {
          return fail(where + '.keys[' + j + '] is duplicated');
        }
        seen.add(key);
        keys.push(key);
      }
      let mode = 'hold';
      if (raw.mode !== undefined) {
        if (ITEM_MODES.indexOf(raw.mode) < 0) {
          return fail(where + '.mode must be hold, toggle or tap');
        }
        mode = raw.mode;
      }
      items.push({
        id: id,
        type: 'button',
        label: label,
        keys: keys,
        x: x,
        y: y,
        size: size,
        mode: mode
      });
    } else if (raw.type === 'stick') {
      const stickExtra = unknownField(raw, ['id', 'type', 'target', 'x', 'y', 'size']);
      if (stickExtra) {
        return fail(where + ' has a field not valid for a stick: ' + stickExtra);
      }
      if (STICK_TARGETS.indexOf(raw.target) < 0) {
        return fail(where + '.target must be wasd, arrows or mouse');
      }
      items.push({
        id: id,
        type: 'stick',
        target: raw.target,
        x: x,
        y: y,
        size: size
      });
    } else {
      return fail(where + '.type must be "button" or "stick"');
    }
  }

  return { ok: true, layout: { name: name, items: items }, error: null };
}

export function createButtonItem(partial, items) {
  const source = partial || {};
  return {
    id: source.id || makeItemId('b', items),
    type: 'button',
    label: source.label || t('editor.defaultButton'),
    keys: Array.isArray(source.keys) && source.keys.length > 0 ? source.keys.slice(0, ITEM_KEYS_MAX) : ['Space'],
    x: Number.isFinite(source.x) ? clamp(source.x, 0, 1) : 0.5,
    y: Number.isFinite(source.y) ? clamp(source.y, 0, 1) : 0.5,
    size: Number.isFinite(source.size) ? clamp(source.size, ITEM_SIZE_MIN, ITEM_SIZE_MAX) : 64,
    mode: ITEM_MODES.indexOf(source.mode) >= 0 ? source.mode : 'hold'
  };
}

export function createStickItem(partial, items) {
  const source = partial || {};
  return {
    id: source.id || makeItemId('s', items),
    type: 'stick',
    target: STICK_TARGETS.indexOf(source.target) >= 0 ? source.target : 'wasd',
    x: Number.isFinite(source.x) ? clamp(source.x, 0, 1) : 0.5,
    y: Number.isFinite(source.y) ? clamp(source.y, 0, 1) : 0.75,
    size: Number.isFinite(source.size) ? clamp(source.size, ITEM_SIZE_MIN, ITEM_SIZE_MAX) : 140
  };
}

/**
 * Joystick vector after the dead zone. dx/dy are normalised (-1 … 1).
 * The returned x/y keep the direction and rescale the magnitude outside the
 * dead zone to 0 … 1.
 */
export function stickVector(dx, dy, deadzone) {
  const x = Number(dx) || 0;
  const y = Number(dy) || 0;
  const dead = clamp(Number(deadzone) || 0, 0, 0.5);
  const magnitude = Math.sqrt(x * x + y * y);
  if (!(magnitude > dead) || magnitude <= 0) {
    return { active: false, x: 0, y: 0, magnitude: 0 };
  }
  const effective = clamp((Math.min(magnitude, 1) - dead) / Math.max(0.0001, 1 - dead), 0, 1);
  return {
    active: true,
    x: (x / magnitude) * effective,
    y: (y / magnitude) * effective,
    magnitude: effective
  };
}

/** 8-direction keyboard keys for a wasd/arrows stick vector. */
export function stickKeys(target, dx, dy, deadzone) {
  const vector = stickVector(dx, dy, deadzone);
  if (!vector.active || (target !== 'wasd' && target !== 'arrows')) {
    return [];
  }
  const magnitude = Math.sqrt((Number(dx) || 0) * (Number(dx) || 0) +
    (Number(dy) || 0) * (Number(dy) || 0));
  if (!(magnitude > 0)) {
    return [];
  }
  const nx = (Number(dx) || 0) / magnitude;
  const ny = (Number(dy) || 0) / magnitude;
  const threshold = 0.3827;
  const keys = [];
  const up = target === 'wasd' ? 'KeyW' : 'ArrowUp';
  const down = target === 'wasd' ? 'KeyS' : 'ArrowDown';
  const left = target === 'wasd' ? 'KeyA' : 'ArrowLeft';
  const right = target === 'wasd' ? 'KeyD' : 'ArrowRight';
  if (ny <= -threshold) {
    keys.push(up);
  } else if (ny >= threshold) {
    keys.push(down);
  }
  if (nx <= -threshold) {
    keys.push(left);
  } else if (nx >= threshold) {
    keys.push(right);
  }
  return keys;
}

/**
 * Protocol events for pressing a mapped button: keys down in listed order,
 * mouse buttons down, wheel targets fire once. Release is the exact reverse.
 * Pure helper so the sequence can be unit-tested without a DOM.
 */
export function mappingPressEvents(keys) {
  const list = Array.isArray(keys) ? keys : [];
  const events = [];
  for (let i = 0; i < list.length; i += 1) {
    const event = targetEvent(list[i], true);
    if (event) {
      events.push(event);
    }
  }
  return events;
}

export function mappingReleaseEvents(keys) {
  const list = Array.isArray(keys) ? keys : [];
  const events = [];
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const event = targetEvent(list[i], false);
    if (event) {
      events.push(event);
    }
  }
  return events;
}

/* ---------- built-in presets ---------- */

const PRESET_TEXT = {
  game: {
    zh: {
      name: '游戏 WASD',
      jump: '跳', crouch: '蹲', run: '跑', lmb: '左键', rmb: '右键'
    },
    en: {
      name: 'Game WASD',
      jump: 'Jump', crouch: 'Crouch', run: 'Run', lmb: 'LMB', rmb: 'RMB'
    }
  },
  ppt: {
    zh: { name: 'PPT 演示', prev: '上一页', next: '下一页', present: '放映', exit: '退出', black: '黑屏' },
    en: { name: 'PPT presenter', prev: 'Prev', next: 'Next', present: 'Present', exit: 'Exit', black: 'Black' }
  },
  media: {
    zh: {
      name: '媒体', play: '播放/暂停', prev: '上一曲', next: '下一曲',
      volUp: '音量+', volDown: '音量-', mute: '静音'
    },
    en: {
      name: 'Media', play: 'Play/Pause', prev: 'Prev track', next: 'Next track',
      volUp: 'Vol +', volDown: 'Vol -', mute: 'Mute'
    }
  },
  shortcuts: {
    zh: {
      name: '常用快捷键', copy: '复制', paste: '粘贴', undo: '撤销', selectAll: '全选',
      save: '保存', switchWindow: '切换窗口', desktop: '显示桌面', taskManager: '任务管理器'
    },
    en: {
      name: 'Shortcuts', copy: 'Copy', paste: 'Paste', undo: 'Undo', selectAll: 'Select all',
      save: 'Save', switchWindow: 'Switch window', desktop: 'Show desktop', taskManager: 'Task manager'
    }
  }
};

function textFor(group, lang) {
  return PRESET_TEXT[group][lang === 'zh' ? 'zh' : 'en'];
}

export function buildPresetLayouts(lang) {
  const language = lang === 'zh' ? 'zh' : 'en';
  const g = textFor('game', language);
  const p = textFor('ppt', language);
  const m = textFor('media', language);
  const s = textFor('shortcuts', language);

  return [
    {
      name: g.name,
      items: [
        { id: 'g-stick', type: 'stick', target: 'wasd', x: 0.16, y: 0.72, size: 140 },
        { id: 'g-jump', type: 'button', label: g.jump, keys: ['Space'], x: 0.8, y: 0.7, size: 64, mode: 'hold' },
        { id: 'g-crouch', type: 'button', label: g.crouch, keys: ['ControlLeft'], x: 0.72, y: 0.85, size: 56, mode: 'hold' },
        { id: 'g-run', type: 'button', label: g.run, keys: ['ShiftLeft'], x: 0.87, y: 0.85, size: 56, mode: 'hold' },
        { id: 'g-e', type: 'button', label: 'E', keys: ['KeyE'], x: 0.91, y: 0.59, size: 50, mode: 'hold' },
        { id: 'g-r', type: 'button', label: 'R', keys: ['KeyR'], x: 0.79, y: 0.57, size: 50, mode: 'hold' },
        { id: 'g-q', type: 'button', label: 'Q', keys: ['KeyQ'], x: 0.67, y: 0.65, size: 50, mode: 'hold' },
        { id: 'g-esc', type: 'button', label: 'Esc', keys: ['Escape'], x: 0.92, y: 0.41, size: 50, mode: 'hold' },
        { id: 'g-tab', type: 'button', label: 'Tab', keys: ['Tab'], x: 0.79, y: 0.39, size: 50, mode: 'hold' },
        { id: 'g-lmb', type: 'button', label: g.lmb, keys: ['MouseLeft'], x: 0.67, y: 0.49, size: 50, mode: 'hold' },
        { id: 'g-rmb', type: 'button', label: g.rmb, keys: ['MouseRight'], x: 0.55, y: 0.59, size: 50, mode: 'hold' }
      ]
    },
    {
      name: p.name,
      items: [
        { id: 'p-prev', type: 'button', label: p.prev, keys: ['ArrowLeft'], x: 0.16, y: 0.82, size: 64, mode: 'tap' },
        { id: 'p-next', type: 'button', label: p.next, keys: ['ArrowRight'], x: 0.36, y: 0.82, size: 64, mode: 'tap' },
        { id: 'p-present', type: 'button', label: p.present, keys: ['F5'], x: 0.56, y: 0.82, size: 64, mode: 'tap' },
        { id: 'p-exit', type: 'button', label: p.exit, keys: ['Escape'], x: 0.74, y: 0.82, size: 64, mode: 'tap' },
        { id: 'p-black', type: 'button', label: p.black, keys: ['KeyB'], x: 0.9, y: 0.82, size: 64, mode: 'tap' }
      ]
    },
    {
      name: m.name,
      items: [
        { id: 'm-play', type: 'button', label: m.play, keys: ['MediaPlayPause'], x: 0.13, y: 0.8, size: 56, mode: 'tap' },
        { id: 'm-prev', type: 'button', label: m.prev, keys: ['MediaTrackPrevious'], x: 0.29, y: 0.8, size: 56, mode: 'tap' },
        { id: 'm-next', type: 'button', label: m.next, keys: ['MediaTrackNext'], x: 0.45, y: 0.8, size: 56, mode: 'tap' },
        { id: 'm-volup', type: 'button', label: m.volUp, keys: ['AudioVolumeUp'], x: 0.61, y: 0.8, size: 56, mode: 'tap' },
        { id: 'm-voldown', type: 'button', label: m.volDown, keys: ['AudioVolumeDown'], x: 0.77, y: 0.8, size: 56, mode: 'tap' },
        { id: 'm-mute', type: 'button', label: m.mute, keys: ['AudioVolumeMute'], x: 0.91, y: 0.8, size: 56, mode: 'tap' }
      ]
    },
    {
      name: s.name,
      items: [
        { id: 's-copy', type: 'button', label: s.copy, keys: ['ControlLeft', 'KeyC'], x: 0.16, y: 0.8, size: 56, mode: 'tap' },
        { id: 's-paste', type: 'button', label: s.paste, keys: ['ControlLeft', 'KeyV'], x: 0.38, y: 0.8, size: 56, mode: 'tap' },
        { id: 's-undo', type: 'button', label: s.undo, keys: ['ControlLeft', 'KeyZ'], x: 0.6, y: 0.8, size: 56, mode: 'tap' },
        { id: 's-select', type: 'button', label: s.selectAll, keys: ['ControlLeft', 'KeyA'], x: 0.82, y: 0.8, size: 56, mode: 'tap' },
        { id: 's-save', type: 'button', label: s.save, keys: ['ControlLeft', 'KeyS'], x: 0.16, y: 0.62, size: 56, mode: 'tap' },
        { id: 's-switch', type: 'button', label: s.switchWindow, keys: ['AltLeft', 'Tab'], x: 0.38, y: 0.62, size: 56, mode: 'tap' },
        { id: 's-desktop', type: 'button', label: s.desktop, keys: ['MetaLeft', 'KeyD'], x: 0.6, y: 0.62, size: 56, mode: 'tap' },
        { id: 's-task', type: 'button', label: s.taskManager, keys: ['ControlLeft', 'ShiftLeft', 'Escape'], x: 0.82, y: 0.62, size: 56, mode: 'tap' }
      ]
    }
  ];
}

/** Default (Chinese) preset table; handy for tests and older callers. */
export const PRESET_LAYOUTS = buildPresetLayouts('zh');

/* ---------- storage ---------- */

function storageGet(storage, key) {
  try {
    return storage ? storage.getItem(key) : null;
  } catch (error) {
    return null;
  }
}

function storageSet(storage, key, value) {
  try {
    if (storage) {
      storage.setItem(key, value);
    }
  } catch (error) {
    /* private mode / quota */
  }
}

function storageRemove(storage, key) {
  try {
    if (storage) {
      storage.removeItem(key);
    }
  } catch (error) {
    /* ignore */
  }
}

/**
 * localStorage-backed collection of layouts ("lancast.layouts" +
 * "lancast.layout"). All reads are validated; a corrupt entry is replaced by
 * the built-in presets.
 */
export class LayoutStore {
  constructor(options) {
    const opts = options || {};
    this.storage = opts.storage || null;
    this.language = opts.language === 'en' ? 'en' : 'zh';
    this.presets = buildPresetLayouts(this.language);
    this.layouts = [];
    this.currentName = '';
    this.load();
  }

  load() {
    let parsed = null;
    const raw = storageGet(this.storage, LAYOUTS_STORAGE_KEY);
    if (raw) {
      try {
        parsed = JSON.parse(raw);
      } catch (error) {
        parsed = null;
      }
    }
    const layouts = [];
    const names = new Set();
    if (Array.isArray(parsed)) {
      for (let i = 0; i < parsed.length; i += 1) {
        const result = validateLayout(parsed[i]);
        if (result.ok && !names.has(result.layout.name)) {
          names.add(result.layout.name);
          layouts.push(result.layout);
        }
      }
    }
    if (layouts.length === 0) {
      this.layouts = deepClone(this.presets);
      this.currentName = this.layouts[0] ? this.layouts[0].name : '';
      this.save();
      return;
    }
    this.layouts = layouts;
    const stored = storageGet(this.storage, LAYOUT_STORAGE_KEY);
    this.currentName = this.getLayout(stored) ? stored : layouts[0].name;
  }

  save() {
    storageSet(this.storage, LAYOUTS_STORAGE_KEY, JSON.stringify(this.layouts));
    storageSet(this.storage, LAYOUT_STORAGE_KEY, this.currentName);
  }

  getLayouts() {
    return this.layouts;
  }

  getLayout(name) {
    for (let i = 0; i < this.layouts.length; i += 1) {
      if (this.layouts[i].name === name) {
        return this.layouts[i];
      }
    }
    return null;
  }

  getCurrentName() {
    return this.currentName;
  }

  getCurrentLayout() {
    return this.getLayout(this.currentName) || this.layouts[0] || null;
  }

  select(name) {
    if (this.getLayout(name)) {
      this.currentName = name;
      this.save();
      return true;
    }
    return false;
  }

  commitCurrent() {
    this.save();
  }

  addLayout(layout) {
    const result = validateLayout(layout);
    if (!result.ok) {
      return result;
    }
    if (this.getLayout(result.layout.name)) {
      return { ok: false, error: 'a layout with this name already exists' };
    }
    this.layouts.push(result.layout);
    this.currentName = result.layout.name;
    this.save();
    return { ok: true, layout: result.layout };
  }

  replaceLayout(layout) {
    const result = validateLayout(layout);
    if (!result.ok) {
      return result;
    }
    for (let i = 0; i < this.layouts.length; i += 1) {
      if (this.layouts[i].name === result.layout.name) {
        this.layouts[i] = result.layout;
        this.currentName = result.layout.name;
        this.save();
        return { ok: true, layout: result.layout };
      }
    }
    return this.addLayout(result.layout);
  }

  removeLayout(name) {
    if (this.layouts.length <= 1) {
      return { ok: false, error: 'the last layout cannot be deleted' };
    }
    for (let i = 0; i < this.layouts.length; i += 1) {
      if (this.layouts[i].name === name) {
        this.layouts.splice(i, 1);
        if (this.currentName === name) {
          this.currentName = this.layouts[0].name;
        }
        this.save();
        return { ok: true };
      }
    }
    return { ok: false, error: 'layout not found' };
  }

  renameLayout(oldName, newName) {
    const clean = cleanString(newName);
    if (!clean) {
      return { ok: false, error: 'name must be non-empty' };
    }
    if (clean.length > NAME_MAX) {
      return { ok: false, error: 'name is longer than ' + NAME_MAX + ' characters' };
    }
    if (clean !== oldName && this.getLayout(clean)) {
      return { ok: false, error: 'a layout with this name already exists' };
    }
    for (let i = 0; i < this.layouts.length; i += 1) {
      if (this.layouts[i].name === oldName) {
        this.layouts[i].name = clean;
        if (this.currentName === oldName) {
          this.currentName = clean;
        }
        this.save();
        return { ok: true };
      }
    }
    return { ok: false, error: 'layout not found' };
  }

  /** Replace built-in presets by name, keep user layouts. */
  resetPresets() {
    const presets = buildPresetLayouts(this.language);
    for (let i = 0; i < presets.length; i += 1) {
      const preset = deepClone(presets[i]);
      let found = false;
      for (let j = 0; j < this.layouts.length; j += 1) {
        if (this.layouts[j].name === preset.name) {
          this.layouts[j] = preset;
          found = true;
          break;
        }
      }
      if (!found) {
        this.layouts.push(preset);
      }
    }
    if (presets[0]) {
      this.currentName = presets[0].name;
    }
    this.save();
    return this.layouts;
  }
}

/* ---------- touchable key layer ---------- */

function stageSizeOf(element) {
  const rect = element.getBoundingClientRect();
  return {
    left: rect.left,
    top: rect.top,
    w: element.clientWidth || rect.width || 1,
    h: element.clientHeight || rect.height || 1
  };
}

export class KeyLayer {
  constructor(options) {
    const opts = options || {};
    this.root = opts.root;
    this.sender = opts.sender;
    this.store = opts.store;
    this.getSettings = opts.getSettings || function () { return {}; };
    this.isPlaying = opts.isPlaying || function () { return false; };
    this.isEditing = opts.isEditing || function () { return false; };
    this.onEditItem = opts.onEditItem || null;
    this.onChanged = opts.onChanged || null;
    this.elements = new Map();
    this.pressed = new Map();
    this.sticks = new Map();
    this.editing = false;
    this.visible = false;
    this.mouseRaf = 0;
    this.mouseLast = 0;
  }

  setVisible(visible) {
    this.visible = !!visible;
    if (this.root) {
      this.root.hidden = !this.visible;
    }
    if (!this.visible) {
      this.clearPressed();
    }
  }

  getVisible() {
    return this.visible && !!(this.root && !this.root.hidden);
  }

  enterEdit() {
    this.editing = true;
    this.clearPressed();
    this.render();
  }

  exitEdit() {
    this.editing = false;
    this.render();
  }

  render() {
    if (!this.root) {
      return;
    }
    this.clearPressed();
    this.root.innerHTML = '';
    this.elements.clear();
    const layout = this.store ? this.store.getCurrentLayout() : null;
    if (!layout) {
      return;
    }
    for (let i = 0; i < layout.items.length; i += 1) {
      this._createItem(layout.items[i]);
    }
  }

  _createItem(item) {
    const node = document.createElement('div');
    node.className = 'key-item key-item-' + item.type + (this.editing ? ' is-editing' : '');
    node.setAttribute('data-item-id', item.id);
    this._positionItem(node, item);
    if (item.type === 'button') {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'key-button';
      button.setAttribute('aria-label', item.label);
      const label = document.createElement('span');
      label.className = 'key-button-label';
      label.textContent = item.label;
      button.appendChild(label);
      node.appendChild(button);
      if (!this.editing) {
        this._bindButton(item, node, button);
      }
    } else {
      const stick = document.createElement('div');
      stick.className = 'key-stick';
      stick.setAttribute('role', 'presentation');
      const ring = document.createElement('span');
      ring.className = 'key-stick-ring';
      const knob = document.createElement('span');
      knob.className = 'key-stick-knob';
      stick.appendChild(ring);
      stick.appendChild(knob);
      node.appendChild(stick);
      if (!this.editing) {
        this._bindStick(item, node, stick, knob);
      }
    }
    if (this.editing) {
      this._bindEditDrag(item, node);
    }
    this.root.appendChild(node);
    this.elements.set(item.id, node);
  }

  _positionItem(node, item) {
    const viewport = this.root ? stageSizeOf(this.root) : { w: 1, h: 1 };
    const width = Math.max(1, viewport.w);
    const height = Math.max(1, viewport.h);
    const halfX = Math.min(0.5, item.size / 2 / width);
    const halfY = Math.min(0.5, item.size / 2 / height);
    node.style.left = (clamp(item.x, halfX, 1 - halfX) * 100) + '%';
    node.style.top = (clamp(item.y, halfY, 1 - halfY) * 100) + '%';
    node.style.width = item.size + 'px';
    node.style.height = item.size + 'px';
  }

  refreshPositions() {
    const layout = this.store ? this.store.getCurrentLayout() : null;
    if (!layout || !this.root) {
      return;
    }
    for (let i = 0; i < layout.items.length; i += 1) {
      const item = layout.items[i];
      const node = this.elements.get(item.id);
      if (node) {
        this._positionItem(node, item);
      }
    }
  }

  updateItem(item) {
    const node = this.elements.get(item.id);
    if (!node) {
      return;
    }
    this._positionItem(node, item);
    if (item.type === 'button') {
      const button = node.querySelector('.key-button');
      const label = node.querySelector('.key-button-label');
      if (label) {
        label.textContent = item.label;
      }
      if (button) {
        button.setAttribute('aria-label', item.label);
      }
    }
  }

  setActive(itemId, active) {
    const node = this.elements.get(itemId);
    if (!node) {
      return;
    }
    const target = node.querySelector('.key-button') || node.querySelector('.key-stick');
    if (target) {
      target.classList.toggle('is-active', !!active);
    }
    node.classList.toggle('is-active', !!active);
  }

  _bindButton(item, node, button) {
    const activate = () => {
      if (!this.isPlaying()) {
        return;
      }
      const state = this.pressed.get(item.id);
      if (item.mode === 'toggle') {
        if (state && state.active) {
          this._releasePressed(item);
          this.setActive(item.id, false);
        } else {
          this._pressTargets(item);
          this.pressed.set(item.id, { item: item, targets: item.keys.slice(), active: true, pointer: null });
          this.setActive(item.id, true);
        }
        return;
      }
      if (state) {
        return;
      }
      this._pressTargets(item);
      this.pressed.set(item.id, { item: item, targets: item.keys.slice(), active: true, pointer: null });
      this.setActive(item.id, true);
      if (item.mode === 'tap') {
        this._releasePressed(item);
        this.setActive(item.id, false);
        window.setTimeout(() => {
          if (!this.pressed.has(item.id)) {
            this.setActive(item.id, false);
          }
        }, 120);
      }
    };

    button.addEventListener('pointerdown', (event) => {
      if (!this.isPlaying()) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (item.mode === 'toggle') {
        activate();
        return;
      }
      if (this.pressed.has(item.id)) {
        return;
      }
      const pointerId = event.pointerId;
      activate();
      const state = this.pressed.get(item.id);
      if (state) {
        state.pointer = pointerId;
      }
      try {
        button.setPointerCapture(pointerId);
      } catch (error) {
        /* ignore */
      }
    });
    const finish = (event) => {
      if (item.mode === 'toggle' || item.mode === 'tap') {
        return;
      }
      const state = this.pressed.get(item.id);
      if (!state || (state.pointer != null && event.pointerId !== state.pointer)) {
        return;
      }
      this._releasePressed(item);
      this.setActive(item.id, false);
    };
    button.addEventListener('pointerup', finish);
    button.addEventListener('pointercancel', finish);
    button.addEventListener('contextmenu', function (event) { event.preventDefault(); });
  }

  _pressTargets(item) {
    for (let i = 0; i < item.keys.length; i += 1) {
      this._sendTarget(item.keys[i], true);
    }
  }

  _releaseTargets(targets) {
    for (let i = targets.length - 1; i >= 0; i -= 1) {
      this._sendTarget(targets[i], false);
    }
  }

  _sendTarget(target, down) {
    const event = targetEvent(target, down);
    if (!event) {
      return;
    }
    if (event.t === 'kd') {
      this.sender.keyDown(event.k);
    } else if (event.t === 'ku') {
      this.sender.keyUp(event.k);
    } else if (event.t === 'mb') {
      if (event.d) {
        this.sender.mouseDown(event.b);
      } else {
        this.sender.mouseUp(event.b);
      }
    } else if (event.t === 'mw') {
      this.sender.wheel(event.dy, event.dx);
    }
  }

  _releasePressed(item) {
    const state = this.pressed.get(item.id);
    if (!state) {
      return;
    }
    this._releaseTargets(state.targets);
    this.pressed.delete(item.id);
  }

  _bindStick(item, node, stick, knob) {
    const radius = Math.max(1, item.size / 2);
    stick.addEventListener('pointerdown', (event) => {
      if (!this.isPlaying()) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      const size = stageSizeOf(node);
      const state = {
        item: item,
        pointerId: event.pointerId,
        cx: size.left + size.w / 2,
        cy: size.top + size.h / 2,
        radius: radius,
        keys: [],
        vector: { active: false, x: 0, y: 0, magnitude: 0 },
        knob: knob,
        node: node
      };
      this.sticks.set(item.id, state);
      try {
        stick.setPointerCapture(event.pointerId);
      } catch (error) {
        /* ignore */
      }
      this._updateStick(state, event.clientX, event.clientY);
    });
    stick.addEventListener('pointermove', (event) => {
      const state = this.sticks.get(item.id);
      if (!state || state.pointerId !== event.pointerId) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      this._updateStick(state, event.clientX, event.clientY);
    });
    const finish = (event) => {
      const state = this.sticks.get(item.id);
      if (!state || state.pointerId !== event.pointerId) {
        return;
      }
      this._endStick(state);
    };
    stick.addEventListener('pointerup', finish);
    stick.addEventListener('pointercancel', finish);
    stick.addEventListener('contextmenu', function (event) { event.preventDefault(); });
  }

  _updateStick(state, clientX, clientY) {
    const settings = this.getSettings();
    const deadzone = Number(settings.deadzone) || 0;
    let dx = (clientX - state.cx) / state.radius;
    let dy = (clientY - state.cy) / state.radius;
    const magnitude = Math.sqrt(dx * dx + dy * dy);
    if (magnitude > 1) {
      dx /= magnitude;
      dy /= magnitude;
    }
    const vector = stickVector(dx, dy, deadzone);
    state.vector = vector;
    const knobOffset = state.radius - Math.min(18, state.radius * 0.32);
    state.knob.style.transform = 'translate(-50%, -50%) translate(' +
      (vector.x * knobOffset) + 'px,' + (vector.y * knobOffset) + 'px)';
    state.node.classList.toggle('is-active', vector.active);
    if (state.item.target === 'mouse') {
      this._ensureMouseLoop();
      return;
    }
    const nextKeys = vector.active
      ? stickKeys(state.item.target, dx, dy, deadzone)
      : [];
    const previous = state.keys;
    for (let i = 0; i < previous.length; i += 1) {
      if (nextKeys.indexOf(previous[i]) < 0) {
        this.sender.keyUp(previous[i]);
      }
    }
    for (let i = 0; i < nextKeys.length; i += 1) {
      if (previous.indexOf(nextKeys[i]) < 0) {
        this.sender.keyDown(nextKeys[i]);
      }
    }
    state.keys = nextKeys;
  }

  _endStick(state) {
    for (let i = 0; i < state.keys.length; i += 1) {
      this.sender.keyUp(state.keys[i]);
    }
    state.keys = [];
    state.vector = { active: false, x: 0, y: 0, magnitude: 0 };
    state.knob.style.transform = 'translate(-50%, -50%)';
    state.node.classList.remove('is-active');
    this.sticks.delete(state.item.id);
  }

  _ensureMouseLoop() {
    if (this.mouseRaf) {
      return;
    }
    this.mouseLast = nowMillis();
    const tick = () => {
      this.mouseRaf = 0;
      let active = false;
      const now = nowMillis();
      const dt = Math.min(0.05, Math.max(0, (now - this.mouseLast) / 1000));
      this.mouseLast = now;
      const settings = this.getSettings();
      const sensitivity = clamp(Number(settings.virtualStickSensitivity) || 1, 0.2, 5);
      this.sticks.forEach((state) => {
        if (state.item.target === 'mouse' && state.vector.active) {
          active = true;
          this.sender.moveRel(
            state.vector.x * sensitivity * 900 * dt,
            state.vector.y * sensitivity * 900 * dt
          );
        }
      });
      if (active) {
        this.mouseRaf = window.requestAnimationFrame(tick);
      }
    };
    this.mouseRaf = window.requestAnimationFrame(tick);
  }

  _bindEditDrag(item, node) {
    let drag = null;
    node.addEventListener('pointerdown', (event) => {
      if (!this.editing) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      const viewport = stageSizeOf(this.root);
      drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        itemX: item.x,
        itemY: item.y,
        moved: false,
        viewport: viewport
      };
      try {
        node.setPointerCapture(event.pointerId);
      } catch (error) {
        /* ignore */
      }
    });
    node.addEventListener('pointermove', (event) => {
      if (!drag || drag.pointerId !== event.pointerId) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      const dx = event.clientX - drag.startX;
      const dy = event.clientY - drag.startY;
      if (!drag.moved && Math.sqrt(dx * dx + dy * dy) > 3) {
        drag.moved = true;
      }
      if (!drag.moved) {
        return;
      }
      const halfX = item.size / 2 / Math.max(1, drag.viewport.w);
      const halfY = item.size / 2 / Math.max(1, drag.viewport.h);
      item.x = clamp(drag.itemX + dx / Math.max(1, drag.viewport.w), halfX, 1 - halfX);
      item.y = clamp(drag.itemY + dy / Math.max(1, drag.viewport.h), halfY, 1 - halfY);
      this._positionItem(node, item);
    });
    const finish = (event) => {
      if (!drag || drag.pointerId !== event.pointerId) {
        return;
      }
      const moved = drag.moved;
      drag = null;
      if (moved) {
        if (this.onChanged) {
          this.onChanged();
        }
      } else if (this.onEditItem) {
        this.onEditItem(item.id);
      }
    };
    node.addEventListener('pointerup', finish);
    node.addEventListener('pointercancel', finish);
  }

  clearPressed(send) {
    if (send !== false) {
      this.pressed.forEach((state) => {
        this._releaseTargets(state.targets);
      });
      this.sticks.forEach((state) => {
        for (let i = 0; i < state.keys.length; i += 1) {
          this.sender.keyUp(state.keys[i]);
        }
      });
    }
    if (!this.root) {
      this.pressed.clear();
      this.sticks.clear();
      return;
    }
    const active = this.root.querySelectorAll('.is-active');
    for (let i = 0; i < active.length; i += 1) {
      active[i].classList.remove('is-active');
    }
    const handles = this.root.querySelectorAll('.key-stick-knob');
    for (let i = 0; i < handles.length; i += 1) {
      handles[i].style.transform = 'translate(-50%, -50%)';
    }
    this.pressed.clear();
    this.sticks.clear();
  }

  releaseAll(send) {
    if (send !== false) {
      this.pressed.forEach((state) => {
        this._releaseTargets(state.targets);
      });
      this.sticks.forEach((state) => {
        for (let i = 0; i < state.keys.length; i += 1) {
          this.sender.keyUp(state.keys[i]);
        }
      });
    }
    this.clearPressed(false);
  }
}

function nowMillis() {
  if (typeof performance !== 'undefined' && performance && typeof performance.now === 'function') {
    return performance.now();
  }
  return Date.now();
}

/* ---------- layout editor ---------- */

function byId(id) {
  return document.getElementById(id);
}

function setMessage(node, text, isError) {
  if (!node) {
    return;
  }
  node.textContent = text || '';
  node.classList.toggle('is-error', !!isError);
}

export class LayoutEditor {
  constructor(options) {
    const opts = options || {};
    this.store = opts.store;
    this.keyLayer = opts.keyLayer;
    this.onOpen = opts.onOpen || null;
    this.onClose = opts.onClose || null;
    this.selectedId = null;
    this.opened = false;
    this.root = byId('key-editor');
    this.panel = byId('key-editor-panel');
    this.collapseBtn = byId('key-editor-collapse');
    this.revealBtn = byId('key-editor-reveal');
    this.layoutSelect = byId('layout-select');
    this.itemEditor = byId('item-editor');
    this.emptyNote = byId('item-empty');
    this.labelRow = byId('item-label-row');
    this.labelInput = byId('item-label');
    this.keysRow = byId('item-keys-row');
    this.keysBox = byId('item-keys');
    this.modeRow = byId('item-mode-row');
    this.modeSelect = byId('item-mode');
    this.targetRow = byId('item-target-row');
    this.targetSelect = byId('item-target');
    this.sizeInput = byId('item-size');
    this.sizeValue = byId('item-size-val');
    this.importError = byId('layout-import-error');
    this.importBox = byId('layout-import');
    this.exportBox = byId('layout-export');
    this.message = byId('layout-message');
    this._bind();
  }

  _bind() {
    const done = byId('key-editor-done');
    if (done) {
      done.addEventListener('click', () => this.close());
    }
    if (this.collapseBtn) {
      this.collapseBtn.addEventListener('click', () => this.collapse());
    }
    if (this.revealBtn) {
      this.revealBtn.addEventListener('click', () => this.expand());
    }
    if (this.layoutSelect) {
      this.layoutSelect.addEventListener('change', () => {
        if (this.store.select(this.layoutSelect.value)) {
          this.selectedId = null;
          this.keyLayer.render();
          this.renderItemEditor();
          this.renderLayoutList();
        }
      });
    }
    const newButton = byId('layout-new');
    if (newButton) {
      newButton.addEventListener('click', () => this._newLayout());
    }
    const renameButton = byId('layout-rename');
    if (renameButton) {
      renameButton.addEventListener('click', () => this._renameLayout());
    }
    const deleteButton = byId('layout-delete');
    if (deleteButton) {
      deleteButton.addEventListener('click', () => this._deleteLayout());
    }
    const restoreButton = byId('layout-restore');
    if (restoreButton) {
      restoreButton.addEventListener('click', () => this._restorePresets());
    }
    const addButton = byId('item-add-button');
    if (addButton) {
      addButton.addEventListener('click', () => this._addItem('button'));
    }
    const addStick = byId('item-add-stick');
    if (addStick) {
      addStick.addEventListener('click', () => this._addItem('stick'));
    }
    const deleteItem = byId('item-delete');
    if (deleteItem) {
      deleteItem.addEventListener('click', () => this._deleteItem());
    }
    if (this.labelInput) {
      this.labelInput.addEventListener('input', () => {
        const item = this._selectedItem();
        if (!item) {
          return;
        }
        item.label = String(this.labelInput.value || '').slice(0, LABEL_MAX);
        this.keyLayer.updateItem(item);
        this._commit();
      });
    }
    if (this.modeSelect) {
      this.modeSelect.addEventListener('change', () => {
        const item = this._selectedItem();
        if (!item || item.type !== 'button') {
          return;
        }
        item.mode = ITEM_MODES.indexOf(this.modeSelect.value) >= 0 ? this.modeSelect.value : 'hold';
        this._commit();
      });
    }
    if (this.targetSelect) {
      this.targetSelect.addEventListener('change', () => {
        const item = this._selectedItem();
        if (!item || item.type !== 'stick') {
          return;
        }
        item.target = STICK_TARGETS.indexOf(this.targetSelect.value) >= 0 ? this.targetSelect.value : 'wasd';
        this._commit();
      });
    }
    if (this.sizeInput) {
      const applySize = () => {
        const item = this._selectedItem();
        if (!item) {
          return;
        }
        item.size = clamp(Number(this.sizeInput.value) || 64, ITEM_SIZE_MIN, ITEM_SIZE_MAX);
        if (this.sizeValue) {
          this.sizeValue.textContent = String(item.size);
        }
        this.keyLayer.updateItem(item);
        this._commit();
      };
      this.sizeInput.addEventListener('input', applySize);
      this.sizeInput.addEventListener('change', applySize);
    }
    const exportButton = byId('layout-export-btn');
    if (exportButton) {
      exportButton.addEventListener('click', () => this._export());
    }
    const copyButton = byId('layout-copy');
    if (copyButton) {
      copyButton.addEventListener('click', () => this._copyExport());
    }
    const importButton = byId('layout-import-btn');
    if (importButton) {
      importButton.addEventListener('click', () => this._import());
    }
    const backdrop = byId('key-editor-backdrop');
    if (backdrop) {
      backdrop.addEventListener('click', () => this.close());
    }
  }

  collapse() {
    if (!this.opened || !this.root) {
      return;
    }
    this.root.classList.add('is-collapsed');
    if (this.revealBtn) {
      this.revealBtn.hidden = false;
    }
  }

  expand() {
    if (this.root) {
      this.root.classList.remove('is-collapsed');
    }
    if (this.revealBtn) {
      this.revealBtn.hidden = true;
    }
  }

  open() {
    this.opened = true;
    this.expand();
    if (this.root) {
      this.root.hidden = false;
    }
    this.selectedId = null;
    if (this.keyLayer) {
      this.keyLayer.setVisible(true);
      this.keyLayer.enterEdit();
    }
    this.renderLayoutList();
    this.renderItemEditor();
    setMessage(this.message, '');
    if (this.exportBox) {
      this.exportBox.value = '';
    }
    if (this.importBox) {
      this.importBox.value = '';
    }
    setMessage(this.importError, '');
    if (this.onOpen) {
      this.onOpen();
    }
  }

  close() {
    this.opened = false;
    this.expand();
    if (this.root) {
      this.root.hidden = true;
    }
    if (this.keyLayer) {
      this.keyLayer.exitEdit();
    }
    if (this.onClose) {
      this.onClose();
    }
  }

  renderLayoutList() {
    if (!this.layoutSelect) {
      return;
    }
    this.layoutSelect.innerHTML = '';
    const layouts = this.store.getLayouts();
    const current = this.store.getCurrentName();
    for (let i = 0; i < layouts.length; i += 1) {
      const option = document.createElement('option');
      option.value = layouts[i].name;
      option.textContent = layouts[i].name;
      option.selected = layouts[i].name === current;
      this.layoutSelect.appendChild(option);
    }
  }

  openItem(itemId) {
    this.selectedId = itemId;
    this.expand();
    this.renderItemEditor();
  }

  _selectedItem() {
    const layout = this.store.getCurrentLayout();
    if (!layout) {
      return null;
    }
    for (let i = 0; i < layout.items.length; i += 1) {
      if (layout.items[i].id === this.selectedId) {
        return layout.items[i];
      }
    }
    return null;
  }

  renderItemEditor() {
    const item = this._selectedItem();
    if (!this.itemEditor) {
      return;
    }
    this.itemEditor.hidden = !item;
    if (this.emptyNote) {
      this.emptyNote.hidden = !!item;
    }
    if (!item) {
      if (this.keysBox) {
        this.keysBox.innerHTML = '';
      }
      return;
    }
    const isButton = item.type === 'button';
    if (this.labelRow) {
      this.labelRow.hidden = !isButton;
    }
    if (this.keysRow) {
      this.keysRow.hidden = !isButton;
    }
    if (this.modeRow) {
      this.modeRow.hidden = !isButton;
    }
    if (this.targetRow) {
      this.targetRow.hidden = isButton;
    }
    if (isButton) {
      if (this.labelInput) {
        this.labelInput.value = item.label;
      }
      if (this.modeSelect) {
        this.modeSelect.value = item.mode;
      }
      this._renderKeyPicker(item);
    } else if (this.targetSelect) {
      this.targetSelect.value = item.target;
    }
    if (this.sizeInput) {
      this.sizeInput.value = String(item.size);
    }
    if (this.sizeValue) {
      this.sizeValue.textContent = String(item.size);
    }
  }

  _renderKeyPicker(item) {
    if (!this.keysBox) {
      return;
    }
    this.keysBox.innerHTML = '';
    for (let g = 0; g < KEY_GROUP_DEFS.length; g += 1) {
      const group = KEY_GROUP_DEFS[g];
      const section = document.createElement('div');
      section.className = 'key-picker-group';
      const title = document.createElement('div');
      title.className = 'key-picker-title';
      title.textContent = t(group.labelKey);
      section.appendChild(title);
      const chips = document.createElement('div');
      chips.className = 'key-picker-chips';
      for (let i = 0; i < group.codes.length; i += 1) {
        const code = group.codes[i];
        const index = item.keys.indexOf(code);
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'key-chip' + (index >= 0 ? ' is-selected' : '');
        chip.setAttribute('aria-pressed', index >= 0 ? 'true' : 'false');
        const name = document.createElement('span');
        name.textContent = code.indexOf('Mouse') === 0 || code.indexOf('Wheel') === 0
          ? t('target.' + code)
          : keyLabel(code);
        chip.appendChild(name);
        if (index >= 0) {
          const order = document.createElement('span');
          order.className = 'key-chip-order';
          order.textContent = String(index + 1);
          chip.appendChild(order);
        }
        chip.addEventListener('click', () => {
          const at = item.keys.indexOf(code);
          if (at >= 0) {
            item.keys.splice(at, 1);
          } else if (item.keys.length < ITEM_KEYS_MAX) {
            item.keys.push(code);
          } else {
            setMessage(this.message, t('editor.tooManyKeys'), true);
            return;
          }
          this._commit();
          this._renderKeyPicker(item);
          this.keyLayer.updateItem(item);
        });
        chips.appendChild(chip);
      }
      section.appendChild(chips);
      this.keysBox.appendChild(section);
    }
  }

  _commit() {
    const layout = this.store.getCurrentLayout();
    const result = validateLayout(layout);
    if (!result.ok) {
      setMessage(this.message, t('editor.invalid') + ' ' + result.error, true);
      return false;
    }
    layout.items = result.layout.items;
    this.store.commitCurrent();
    setMessage(this.message, '');
    return true;
  }

  _newLayout() {
    const suggested = t('editor.defaultLayoutName');
    let name = null;
    try {
      name = window.prompt(t('editor.newLayoutPrompt'), suggested);
    } catch (error) {
      name = null;
    }
    if (name == null) {
      return;
    }
    const clean = String(name).trim().slice(0, NAME_MAX);
    if (!clean) {
      setMessage(this.message, t('editor.nameRequired'), true);
      return;
    }
    const layout = {
      name: clean,
      items: [createButtonItem({ id: 'b1', label: t('editor.defaultButton') }, [])]
    };
    const result = this.store.addLayout(layout);
    if (!result.ok) {
      setMessage(this.message, result.error, true);
      return;
    }
    this.selectedId = 'b1';
    this.keyLayer.render();
    this.renderLayoutList();
    this.renderItemEditor();
  }

  _renameLayout() {
    const oldName = this.store.getCurrentName();
    let name = null;
    try {
      name = window.prompt(t('editor.renamePrompt'), oldName);
    } catch (error) {
      name = null;
    }
    if (name == null) {
      return;
    }
    const result = this.store.renameLayout(oldName, name);
    if (!result.ok) {
      setMessage(this.message, result.error, true);
      return;
    }
    this.renderLayoutList();
    setMessage(this.message, t('editor.saved'));
  }

  _deleteLayout() {
    const name = this.store.getCurrentName();
    let ok = false;
    try {
      ok = window.confirm(t('editor.deleteLayoutConfirm', { name: name }));
    } catch (error) {
      ok = false;
    }
    if (!ok) {
      return;
    }
    const result = this.store.removeLayout(name);
    if (!result.ok) {
      setMessage(this.message, result.error, true);
      return;
    }
    this.selectedId = null;
    this.keyLayer.render();
    this.renderLayoutList();
    this.renderItemEditor();
  }

  _restorePresets() {
    let ok = false;
    try {
      ok = window.confirm(t('editor.restoreConfirm'));
    } catch (error) {
      ok = false;
    }
    if (!ok) {
      return;
    }
    this.store.resetPresets();
    this.selectedId = null;
    this.keyLayer.render();
    this.renderLayoutList();
    this.renderItemEditor();
    setMessage(this.message, t('editor.presetsRestored'));
  }

  _addItem(type) {
    const layout = this.store.getCurrentLayout();
    if (!layout) {
      return;
    }
    let item;
    if (type === 'stick') {
      item = createStickItem({ x: 0.5, y: 0.72 }, layout.items);
    } else {
      item = createButtonItem({ x: 0.5, y: 0.5 }, layout.items);
    }
    layout.items.push(item);
    this.selectedId = item.id;
    this._commit();
    this.keyLayer.render();
    this.renderItemEditor();
  }

  _deleteItem() {
    const layout = this.store.getCurrentLayout();
    const item = this._selectedItem();
    if (!layout || !item) {
      return;
    }
    layout.items = layout.items.filter(function (candidate) { return candidate.id !== item.id; });
    if (layout.items.length === 0) {
      layout.items.push(createButtonItem({ id: 'b1' }, []));
    }
    this.selectedId = null;
    this._commit();
    this.keyLayer.render();
    this.renderItemEditor();
  }

  _export() {
    if (!this.exportBox) {
      return;
    }
    const layout = this.store.getCurrentLayout();
    this.exportBox.value = JSON.stringify(layout, null, 2);
    try {
      this.exportBox.focus();
      this.exportBox.select();
    } catch (error) {
      /* ignore */
    }
  }

  _copyExport() {
    if (!this.exportBox) {
      return;
    }
    if (!this.exportBox.value) {
      this._export();
    }
    const text = this.exportBox.value;
    const done = () => setMessage(this.message, t('editor.copied'));
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      navigator.clipboard.writeText(text).then(done).catch(() => {
        try {
          this.exportBox.select();
        } catch (error) {
          /* ignore */
        }
      });
      return;
    }
    try {
      this.exportBox.select();
      document.execCommand('copy');
      done();
    } catch (error) {
      setMessage(this.message, t('editor.copyFailed'), true);
    }
  }

  _import() {
    setMessage(this.importError, '');
    if (!this.importBox) {
      return;
    }
    const text = String(this.importBox.value || '').trim();
    if (!text) {
      setMessage(this.importError, t('editor.importEmpty'), true);
      return;
    }
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      setMessage(this.importError, t('editor.importJson') + ' ' + error.message, true);
      return;
    }
    const result = validateLayout(parsed);
    if (!result.ok) {
      setMessage(this.importError, t('editor.importInvalid') + ' ' + result.error, true);
      return;
    }
    const existing = this.store.getLayout(result.layout.name);
    if (existing) {
      let replace = false;
      try {
        replace = window.confirm(t('editor.replaceConfirm', { name: result.layout.name }));
      } catch (error) {
        replace = false;
      }
      if (!replace) {
        return;
      }
      this.store.replaceLayout(result.layout);
    } else {
      const added = this.store.addLayout(result.layout);
      if (!added.ok) {
        setMessage(this.importError, added.error, true);
        return;
      }
    }
    this.selectedId = null;
    this.keyLayer.render();
    this.renderLayoutList();
    this.renderItemEditor();
    this.importBox.value = '';
    setMessage(this.message, t('editor.imported'));
  }
}
