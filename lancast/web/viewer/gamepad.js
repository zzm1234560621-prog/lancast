/*
 * Gamepad mapping and polling.
 *
 * The standard-mapping layout (A/B/X/Y, bumpers, triggers, Back/Start,
 * stick clicks and the d-pad) is exposed as a plain object in
 * localStorage["lancast.gamepad"]. Pure helpers are Node-testable; the
 * controller uses requestAnimationFrame in the browser.
 */

import { isInputTarget } from './keycodes.js';
import { stickKeys, stickVector } from './keymap.js';

export const GAMEPAD_STORAGE_KEY = 'lancast.gamepad';

export const GAMEPAD_BUTTON_DEFS = [
  { field: 'a', index: 0, labelKey: 'gamepad.a' },
  { field: 'b', index: 1, labelKey: 'gamepad.b' },
  { field: 'x', index: 2, labelKey: 'gamepad.x' },
  { field: 'y', index: 3, labelKey: 'gamepad.y' },
  { field: 'lb', index: 4, labelKey: 'gamepad.lb' },
  { field: 'rb', index: 5, labelKey: 'gamepad.rb' },
  { field: 'lt', index: 6, labelKey: 'gamepad.lt' },
  { field: 'rt', index: 7, labelKey: 'gamepad.rt' },
  { field: 'back', index: 8, labelKey: 'gamepad.back' },
  { field: 'start', index: 9, labelKey: 'gamepad.start' },
  { field: 'ls', index: 10, labelKey: 'gamepad.ls' },
  { field: 'rs', index: 11, labelKey: 'gamepad.rs' },
  { field: 'dpadUp', index: 12, labelKey: 'gamepad.dpadUp' },
  { field: 'dpadDown', index: 13, labelKey: 'gamepad.dpadDown' },
  { field: 'dpadLeft', index: 14, labelKey: 'gamepad.dpadLeft' },
  { field: 'dpadRight', index: 15, labelKey: 'gamepad.dpadRight' }
];

export const STICK_TARGETS = ['wasd', 'arrows', 'mouse', 'none'];

export function defaultGamepadMapping() {
  return {
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
  };
}

function validTarget(value) {
  return value === 'none' || (typeof value === 'string' && isInputTarget(value));
}

function validStick(value) {
  return STICK_TARGETS.indexOf(value) >= 0;
}

/** Fill missing/unknown fields with defaults; never throws. */
export function normalizeGamepadMapping(raw) {
  const base = defaultGamepadMapping();
  if (!raw || typeof raw !== 'object') {
    return base;
  }
  const result = {};
  for (let i = 0; i < GAMEPAD_BUTTON_DEFS.length; i += 1) {
    const field = GAMEPAD_BUTTON_DEFS[i].field;
    result[field] = validTarget(raw[field]) ? raw[field] : base[field];
  }
  result.leftStick = validStick(raw.leftStick) ? raw.leftStick : base.leftStick;
  result.rightStick = validStick(raw.rightStick) ? raw.rightStick : base.rightStick;
  return result;
}

/** Analog triggers count as pressed past this value. */
export const TRIGGER_THRESHOLD = 0.5;

export function gamepadButtonValue(pad, index) {
  if (!pad || !pad.buttons || !pad.buttons[index]) {
    return 0;
  }
  const button = pad.buttons[index];
  if (typeof button === 'number') {
    return button;
  }
  const value = Number(button.value);
  if (Number.isFinite(value) && value > 0) {
    return value;
  }
  return button.pressed ? 1 : 0;
}

export function gamepadPressed(pad, index) {
  return gamepadButtonValue(pad, index) > TRIGGER_THRESHOLD ||
    !!(pad && pad.buttons && pad.buttons[index] && pad.buttons[index].pressed);
}

export function gamepadAxes(pad, side) {
  const axes = pad && pad.axes ? pad.axes : [];
  if (side === 'right') {
    return { x: Number(axes[2]) || 0, y: Number(axes[3]) || 0 };
  }
  return { x: Number(axes[0]) || 0, y: Number(axes[1]) || 0 };
}

function nowMillis() {
  if (typeof performance !== 'undefined' && performance && typeof performance.now === 'function') {
    return performance.now();
  }
  return Date.now();
}

/**
 * Polls navigator.getGamepads() in a rAF loop. Buttons only emit on state
 * changes; mouse sticks integrate movement every frame.
 */
export class GamepadController {
  constructor(options) {
    const opts = options || {};
    this.sender = opts.sender;
    this.getMapping = opts.getMapping || defaultGamepadMapping;
    this.getSettings = opts.getSettings || function () { return {}; };
    this.isActive = opts.isActive || function () { return false; };
    this.onConnectionChange = opts.onConnectionChange || null;
    this.pads = new Map();
    this.connected = false;
    this.running = false;
    this.raf = 0;
    this.last = 0;
    this._tick = this._tick.bind(this);
    this._onConnected = this._handleConnected.bind(this);
    this._onDisconnected = this._handleDisconnected.bind(this);
    this._bound = false;
  }

  start() {
    if (this._bound || typeof window === 'undefined') {
      return;
    }
    this._bound = true;
    window.addEventListener('gamepadconnected', this._onConnected);
    window.addEventListener('gamepaddisconnected', this._onDisconnected);
    this._startLoop();
  }

  _startLoop() {
    if (this.running) {
      return;
    }
    this.running = true;
    this.last = nowMillis();
    this.raf = window.requestAnimationFrame(this._tick);
  }

  _handleConnected() {
    this._refreshConnection();
  }

  _handleDisconnected(event) {
    const index = event && event.gamepad ? event.gamepad.index : -1;
    if (this.pads.has(index)) {
      this._releasePad(this.pads.get(index), true);
      this.pads.delete(index);
    }
    this._refreshConnection();
  }

  _readPads() {
    if (typeof navigator === 'undefined' || typeof navigator.getGamepads !== 'function') {
      return [];
    }
    let list = [];
    try {
      list = navigator.getGamepads() || [];
    } catch (error) {
      list = [];
    }
    const out = [];
    for (let i = 0; i < list.length; i += 1) {
      if (list[i]) {
        out.push(list[i]);
      }
    }
    return out;
  }

  _refreshConnection() {
    const pads = this._readPads();
    const connected = pads.length > 0;
    if (connected !== this.connected) {
      this.connected = connected;
      if (this.onConnectionChange) {
        this.onConnectionChange(connected);
      }
    }
    return pads;
  }

  _stateFor(index) {
    let state = this.pads.get(index);
    if (!state) {
      state = {
        index: index,
        buttons: {},
        stickKeys: {
          left: { target: 'none', keys: [] },
          right: { target: 'none', keys: [] }
        },
        stickMouse: {
          left: { active: false, x: 0, y: 0 },
          right: { active: false, x: 0, y: 0 }
        }
      };
      this.pads.set(index, state);
    }
    return state;
  }

  _sendTarget(target, down) {
    if (!target || target === 'none') {
      return;
    }
    if (target.charAt(0) === 'M' || target.charAt(0) === 'W') {
      if (target === 'MouseLeft' || target === 'MouseRight' || target === 'MouseMiddle' ||
          target === 'MouseX1' || target === 'MouseX2') {
        const name = target.slice(5).toLowerCase();
        if (down) {
          this.sender.mouseDown(name);
        } else {
          this.sender.mouseUp(name);
        }
        return;
      }
      if (down && target === 'WheelUp') {
        this.sender.wheel(120, 0);
        return;
      }
      if (down && target === 'WheelDown') {
        this.sender.wheel(-120, 0);
        return;
      }
      // A wheel target has no release edge; don't fall through to keyUp with
      // a name that is not a key code.
      if (target === 'WheelUp' || target === 'WheelDown') {
        return;
      }
    }
    if (!isInputTarget(target)) {
      return;
    }
    if (down) {
      this.sender.keyDown(target);
    } else {
      this.sender.keyUp(target);
    }
  }

  _releasePad(state, send) {
    // Release every button that was pressed when the pad goes away; otherwise
    // the PC keeps the key / mouse button held forever.
    const held = state.buttons || {};
    const fields = Object.keys(held);
    for (let i = 0; i < fields.length; i += 1) {
      const previous = held[fields[i]];
      if (send && previous && previous.pressed && previous.target && previous.target !== 'none') {
        this._sendTarget(previous.target, false);
      }
    }
    state.buttons = {};
    const sides = ['left', 'right'];
    for (let i = 0; i < sides.length; i += 1) {
      const side = sides[i];
      const stick = state.stickKeys[side];
      if (send) {
        for (let k = 0; k < stick.keys.length; k += 1) {
          this.sender.keyUp(stick.keys[k]);
        }
      }
      stick.keys = [];
      stick.target = 'none';
      state.stickMouse[side] = { active: false, x: 0, y: 0 };
    }
  }

  reset(send = true) {
    this.pads.forEach((state) => {
      this._releasePad(state, send);
    });
    this.pads.clear();
  }

  _updateButtons(pad, state, mapping) {
    for (let i = 0; i < GAMEPAD_BUTTON_DEFS.length; i += 1) {
      const def = GAMEPAD_BUTTON_DEFS[i];
      const pressed = gamepadPressed(pad, def.index);
      const previous = state.buttons[def.field];
      const target = mapping[def.field];
      if (pressed && (!previous || !previous.pressed)) {
        if (target && target !== 'none') {
          this._sendTarget(target, true);
          state.buttons[def.field] = { pressed: true, target: target };
        } else {
          state.buttons[def.field] = { pressed: true, target: 'none' };
        }
      } else if (!pressed && previous && previous.pressed) {
        if (previous.target && previous.target !== 'none') {
          this._sendTarget(previous.target, false);
        }
        state.buttons[def.field] = { pressed: false, target: 'none' };
      }
    }
  }

  _updateStick(side, axes, state, mapping, deadzone, sensitivity, dt) {
    const target = side === 'left' ? mapping.leftStick : mapping.rightStick;
    const keyState = state.stickKeys[side];
    if (target === 'wasd' || target === 'arrows') {
      state.stickMouse[side] = { active: false, x: 0, y: 0 };
      const next = stickKeys(target, axes.x, axes.y, deadzone);
      if (keyState.target !== target) {
        for (let i = 0; i < keyState.keys.length; i += 1) {
          this.sender.keyUp(keyState.keys[i]);
        }
        keyState.keys = [];
        keyState.target = target;
      }
      const previous = keyState.keys;
      for (let i = 0; i < previous.length; i += 1) {
        if (next.indexOf(previous[i]) < 0) {
          this.sender.keyUp(previous[i]);
        }
      }
      for (let i = 0; i < next.length; i += 1) {
        if (previous.indexOf(next[i]) < 0) {
          this.sender.keyDown(next[i]);
        }
      }
      keyState.keys = next;
      return;
    }
    if (keyState.keys.length > 0) {
      for (let i = 0; i < keyState.keys.length; i += 1) {
        this.sender.keyUp(keyState.keys[i]);
      }
      keyState.keys = [];
    }
    keyState.target = target;
    if (target !== 'mouse') {
      state.stickMouse[side] = { active: false, x: 0, y: 0 };
      return;
    }
    const vector = stickVector(axes.x, axes.y, deadzone);
    state.stickMouse[side] = { active: vector.active, x: vector.x, y: vector.y };
    if (vector.active) {
      this.sender.moveRel(
        vector.x * sensitivity * 900 * dt,
        vector.y * sensitivity * 900 * dt
      );
    }
  }

  _tick() {
    this.raf = 0;
    if (!this.running) {
      return;
    }
    const now = nowMillis();
    const dt = Math.min(0.05, Math.max(0, (now - this.last) / 1000));
    this.last = now;
    const pads = this._refreshConnection();
    if (!this.isActive()) {
      // Queue the releases before dropping the pad state; the sender flushes
      // them while the input channel is still enabled.
      this.pads.forEach((state) => this._releasePad(state, true));
      this.pads.clear();
      this.raf = window.requestAnimationFrame(this._tick);
      return;
    }
    const mapping = this.getMapping();
    const settings = this.getSettings();
    const deadzone = Math.max(0, Math.min(0.5, Number(settings.deadzone) || 0));
    const sensitivity = Math.max(0.2, Math.min(5, Number(settings.gamepadStickSensitivity) || 1));
    const seen = new Set();
    for (let i = 0; i < pads.length; i += 1) {
      const pad = pads[i];
      if (!pad || !pad.connected) {
        continue;
      }
      seen.add(pad.index);
      const state = this._stateFor(pad.index);
      this._updateButtons(pad, state, mapping);
      this._updateStick('left', gamepadAxes(pad, 'left'), state, mapping, deadzone, sensitivity, dt);
      this._updateStick('right', gamepadAxes(pad, 'right'), state, mapping, deadzone, sensitivity, dt);
    }
    this.pads.forEach((state, index) => {
      if (!seen.has(index)) {
        this._releasePad(state, true);
        this.pads.delete(index);
      }
    });
    this.raf = window.requestAnimationFrame(this._tick);
  }
}
