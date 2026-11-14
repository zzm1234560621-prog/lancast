/*
 * Remote-control input pipeline.
 *
 *  - InputSender: event queue, one `input` message per animation frame,
 *    max 64 events, consecutive `mm` merging, held-key tracking and the 1 s
 *    empty-events heartbeat.
 *  - PointerControl: touchpad / direct-touch gestures on the video stage.
 *  - KeyboardForwarder: physical keyboard -> kd/ku.
 *  - TextInput: hidden textarea / IME -> tx.
 *
 * Pure helpers live at the top and are unit-tested by selftest.mjs.
 */

import { isKeyCode } from './keycodes.js';

export const INPUT_EVENT_LIMIT = 64;
export const MAX_REL_DELTA = 4000;
export const TAP_MOVE_LIMIT = 8;

function clamp(value, min, max) {
  return value < min ? min : value > max ? max : value;
}

function clamp01(value) {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

function finite(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function nowMs() {
  if (typeof performance !== 'undefined' && performance && typeof performance.now === 'function') {
    return performance.now();
  }
  return Date.now();
}

/**
 * Take up to `limit` events from a queue. Consecutive relative moves are
 * merged into a single mm event; the rest of the queue is returned untouched.
 */
export function takeInputBatch(queue, limit) {
  const source = Array.isArray(queue) ? queue : [];
  const max = Math.max(1, finite(limit, INPUT_EVENT_LIMIT));
  const events = [];
  let index = 0;
  for (; index < source.length; index += 1) {
    const event = source[index];
    if (!event || typeof event !== 'object' || typeof event.t !== 'string') {
      continue;
    }
    if (event.t === 'mm') {
      const last = events[events.length - 1];
      if (last && last.t === 'mm') {
        last.dx = clamp(last.dx + finite(event.dx, 0), -MAX_REL_DELTA, MAX_REL_DELTA);
        last.dy = clamp(last.dy + finite(event.dy, 0), -MAX_REL_DELTA, MAX_REL_DELTA);
        continue;
      }
    }
    if (events.length >= max) {
      break;
    }
    if (event.t === 'mm') {
      events.push({
        t: 'mm',
        dx: clamp(finite(event.dx, 0), -MAX_REL_DELTA, MAX_REL_DELTA),
        dy: clamp(finite(event.dy, 0), -MAX_REL_DELTA, MAX_REL_DELTA)
      });
    } else {
      events.push(Object.assign({}, event));
    }
  }
  return { events: events, rest: source.slice(index) };
}

/**
 * Touchpad pointer gain. Returns display-pixel deltas.
 *
 * base = display width (px) / currently displayed width (CSS px, including
 * zoom). Acceleration factor = clamp(0.6 + speed(px/ms) * 0.8, 0.6, 3.0).
 */
export function touchpadDelta(fingerDx, fingerDy, speed, displayWidth, displayedWidth,
  sensitivity, accelerate) {
  const w = finite(displayWidth, 0);
  const shown = finite(displayedWidth, 0);
  if (!(w > 0) || !(shown > 0)) {
    return { dx: 0, dy: 0 };
  }
  const base = w / shown;
  const sens = clamp(finite(sensitivity, 1), 0.2, 5);
  let factor = 1;
  if (accelerate !== false) {
    factor = clamp(0.6 + Math.max(0, finite(speed, 0)) * 0.8, 0.6, 3.0);
  }
  return {
    dx: finite(fingerDx, 0) * base * sens * factor,
    dy: finite(fingerDy, 0) * base * sens * factor
  };
}

/** 30 CSS px of two-finger travel equals one 120-unit wheel notch. */
export function scrollToWheel(pixels, speed) {
  return finite(pixels, 0) / 30 * 120 * clamp(finite(speed, 1), 0.2, 5);
}

/** Accumulate fractional wheel deltas and return the integer part to send. */
export function takeWheelStep(accumulator, delta) {
  const next = finite(accumulator, 0) + finite(delta, 0);
  let step = next < 0 ? Math.ceil(next) : Math.floor(next);
  step = clamp(step, -2400, 2400);
  return { step: step, rest: next - step };
}

/** Split Unicode text into chunks of at most `max` code points. */
export function splitText(text, max) {
  const limit = Math.max(1, finite(max, 200));
  const chars = Array.from(String(text == null ? '' : text));
  const chunks = [];
  for (let i = 0; i < chars.length; i += limit) {
    chunks.push(chars.slice(i, i + limit).join(''));
  }
  return chunks;
}

/**
 * Queue-based input sender. `send(message)` must return true when the
 * WebSocket accepted the message. `isEnabled` is checked before every flush.
 */
export class InputSender {
  constructor(options) {
    const opts = options || {};
    this.send = typeof opts.send === 'function' ? opts.send : function () { return false; };
    this.isEnabled = typeof opts.isEnabled === 'function' ? opts.isEnabled : function () { return true; };
    this.enabled = true;
    this.queue = [];
    this.keys = new Set();
    this.buttons = new Set();
    this.heldOrder = [];
    this.heartbeatPending = false;
    this.lastSent = nowMs();
    this.sentHistory = [];
    this._raf = 0;
    this._heartbeatTimer = 0;
    this._flush = this._flush.bind(this);
    this._startHeartbeat();
  }

  _canSend() {
    return this.enabled && !!this.isEnabled();
  }

  _startHeartbeat() {
    if (this._heartbeatTimer || typeof window === 'undefined') {
      return;
    }
    this._heartbeatTimer = window.setInterval(() => {
      if (!this.enabled || this.heldCount === 0) {
        return;
      }
      this.heartbeatPending = true;
      this._schedule();
    }, 1000);
  }

  _stopHeartbeat() {
    if (this._heartbeatTimer && typeof window !== 'undefined') {
      window.clearInterval(this._heartbeatTimer);
    }
    this._heartbeatTimer = 0;
  }

  setEnabled(enabled) {
    const next = !!enabled;
    if (!next) {
      this.enabled = false;
      this.queue.length = 0;
      this.heartbeatPending = false;
      this.clearHeld();
      if (this._raf && typeof window !== 'undefined') {
        window.cancelAnimationFrame(this._raf);
      }
      this._raf = 0;
      return;
    }
    this.enabled = true;
    this._startHeartbeat();
  }

  /**
   * Send everything queued right now. Used for the release-all path when a
   * mode change / permission loss is about to disable the sender; it bypasses
   * the "enabled" gate but still honours the 64-event and one-message rules.
   */
  flushNow() {
    if (this._raf) {
      if (typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
        window.cancelAnimationFrame(this._raf);
      } else if (typeof window !== 'undefined' && typeof window.clearTimeout === 'function') {
        window.clearTimeout(this._raf);
      } else if (typeof clearTimeout === 'function') {
        clearTimeout(this._raf);
      }
      this._raf = 0;
    }
    this.heartbeatPending = false;
    while (this.queue.length > 0) {
      const result = takeInputBatch(this.queue, INPUT_EVENT_LIMIT);
      this.queue = result.rest;
      if (result.events.length === 0) {
        break;
      }
      const ok = this.send({ type: 'input', events: result.events });
      if (!ok) {
        this.queue.length = 0;
        break;
      }
      this._recordSent(result.events.length);
    }
  }

  _schedule() {
    if (this._raf) {
      return;
    }
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      this._raf = window.requestAnimationFrame(this._flush);
    } else if (typeof window !== 'undefined' && typeof window.setTimeout === 'function') {
      this._raf = window.setTimeout(this._flush, 16);
    } else if (typeof setTimeout === 'function') {
      this._raf = setTimeout(this._flush, 16);
    }
  }

  _enqueue(event) {
    if (!this.enabled || !event) {
      return false;
    }
    this.queue.push(event);
    this._schedule();
    return true;
  }

  keyDown(code) {
    if (!isKeyCode(code) || this.keys.has(code)) {
      return false;
    }
    this.keys.add(code);
    this.heldOrder.push('k:' + code);
    return this._enqueue({ t: 'kd', k: code });
  }

  keyUp(code) {
    if (!this.keys.has(code)) {
      return false;
    }
    this.keys.delete(code);
    const index = this.heldOrder.indexOf('k:' + code);
    if (index >= 0) {
      this.heldOrder.splice(index, 1);
    }
    return this._enqueue({ t: 'ku', k: code });
  }

  mouseDown(button) {
    const name = String(button || 'left');
    if (this.buttons.has(name)) {
      return false;
    }
    this.buttons.add(name);
    this.heldOrder.push('m:' + name);
    return this._enqueue({ t: 'mb', b: name, d: true });
  }

  mouseUp(button) {
    const name = String(button || 'left');
    if (!this.buttons.has(name)) {
      return false;
    }
    this.buttons.delete(name);
    const index = this.heldOrder.indexOf('m:' + name);
    if (index >= 0) {
      this.heldOrder.splice(index, 1);
    }
    return this._enqueue({ t: 'mb', b: name, d: false });
  }

  wheel(dy, dx) {
    const wheelY = clamp(Math.round(finite(dy, 0)), -2400, 2400);
    const wheelX = clamp(Math.round(finite(dx, 0)), -2400, 2400);
    if (wheelY === 0 && wheelX === 0) {
      return false;
    }
    return this._enqueue({ t: 'mw', dy: wheelY, dx: wheelX });
  }

  moveRel(dx, dy) {
    const x = clamp(finite(dx, 0), -MAX_REL_DELTA, MAX_REL_DELTA);
    const y = clamp(finite(dy, 0), -MAX_REL_DELTA, MAX_REL_DELTA);
    if (x === 0 && y === 0) {
      return false;
    }
    return this._enqueue({ t: 'mm', dx: x, dy: y });
  }

  moveAbs(x, y) {
    if (!Number.isFinite(Number(x)) || !Number.isFinite(Number(y))) {
      return false;
    }
    return this._enqueue({ t: 'ma', x: clamp01(Number(x)), y: clamp01(Number(y)) });
  }

  text(value) {
    const chunks = splitText(value, 200);
    let sent = false;
    for (let i = 0; i < chunks.length; i += 1) {
      if (chunks[i]) {
        this._enqueue({ t: 'tx', s: chunks[i] });
        sent = true;
      }
    }
    return sent;
  }

  isKeyHeld(code) {
    return this.keys.has(code);
  }

  isButtonHeld(button) {
    return this.buttons.has(String(button || 'left'));
  }

  get heldCount() {
    return this.keys.size + this.buttons.size;
  }

  clearHeld() {
    this.keys.clear();
    this.buttons.clear();
    this.heldOrder.length = 0;
  }

  /** Release every held key/button in reverse press order. */
  releaseAll(send) {
    const order = this.heldOrder.slice();
    if (send !== false) {
      for (let i = order.length - 1; i >= 0; i -= 1) {
        const token = order[i];
        if (token.indexOf('k:') === 0) {
          this.keyUp(token.slice(2));
        } else if (token.indexOf('m:') === 0) {
          this.mouseUp(token.slice(2));
        }
      }
    }
    this.clearHeld();
  }

  _recordSent(count) {
    const now = nowMs();
    this.lastSent = now;
    this.sentHistory.push({ t: now, count: count });
    while (this.sentHistory.length > 0 && this.sentHistory[0].t < now - 2500) {
      this.sentHistory.shift();
    }
  }

  ratePerSecond(now) {
    const time = Number.isFinite(Number(now)) ? Number(now) : nowMs();
    let total = 0;
    for (let i = this.sentHistory.length - 1; i >= 0; i -= 1) {
      const item = this.sentHistory[i];
      if (item.t < time - 1000) {
        break;
      }
      total += item.count;
    }
    return total;
  }

  _flush() {
    this._raf = 0;
    if (!this._canSend()) {
      this.queue.length = 0;
      this.heartbeatPending = false;
      return;
    }
    const heartbeat = this.heartbeatPending;
    this.heartbeatPending = false;
    if (this.queue.length === 0) {
      if (heartbeat && this.heldCount > 0) {
        const ok = this.send({ type: 'input', events: [] });
        if (ok) {
          this._recordSent(0);
        }
      }
      return;
    }
    const result = takeInputBatch(this.queue, INPUT_EVENT_LIMIT);
    this.queue = result.rest;
    if (result.events.length > 0) {
      const ok = this.send({ type: 'input', events: result.events });
      if (ok) {
        this._recordSent(result.events.length);
      }
    }
    if (this.queue.length > 0) {
      this._schedule();
    }
  }
}

function buttonNameFromPointer(event) {
  if (event.button === 1) {
    return 'middle';
  }
  if (event.button === 2) {
    return 'right';
  }
  if (event.button === 3) {
    return 'x1';
  }
  if (event.button === 4) {
    return 'x2';
  }
  return event.button === 0 ? 'left' : null;
}

/**
 * Touchpad / direct-touch controller. One instance is bound to the stream
 * stage; it is only active when the app says so.
 */
export class PointerControl {
  constructor(options) {
    const opts = options || {};
    this.stage = opts.stage;
    this.sender = opts.sender;
    this.zoom = opts.zoom || null;
    this.getMode = opts.getMode || function () { return 'view'; };
    this.isActive = opts.isActive || function () { return false; };
    this.getSettings = opts.getSettings || function () { return {}; };
    this.getDisplayWidth = opts.getDisplayWidth || function () { return 0; };
    this.getFit = opts.getFit || function () { return { x: 0, y: 0, w: 1, h: 1 }; };
    this.getDisplayedWidth = opts.getDisplayedWidth || function () { return 1; };
    this.onTap = opts.onTap || null;
    this.mode = 'view';

    this.pointers = new Map();
    this.singleId = null;
    this.maxPointers = 0;
    this.groupMoved = false;
    this.groupStartT = 0;
    this.groupStartPoint = { x: 0, y: 0 };
    this.two = null;
    this.longPressTimer = 0;
    this.longPressFired = false;
    this.dragArmed = false;
    this.dragActive = false;
    this.dragPointer = null;
    this.accX = 0;
    this.accY = 0;
    this.lastTapTime = 0;
    this.pendingAbs = null;
    this.absRaf = 0;
    this._bound = false;

    this._onDown = this._handleDown.bind(this);
    this._onMove = this._handleMove.bind(this);
    this._onUp = this._handleUp.bind(this);
    this._onCancel = this._handleCancel.bind(this);
    this._onWheel = this._handleWheel.bind(this);
    this._onContext = function (event) { event.preventDefault(); };
  }

  bind() {
    if (this._bound || !this.stage) {
      return;
    }
    this._bound = true;
    this.stage.addEventListener('pointerdown', this._onDown, { passive: false });
    this.stage.addEventListener('pointermove', this._onMove, { passive: false });
    this.stage.addEventListener('pointerup', this._onUp, { passive: false });
    this.stage.addEventListener('pointercancel', this._onCancel, { passive: false });
    this.stage.addEventListener('wheel', this._onWheel, { passive: false });
    this.stage.addEventListener('contextmenu', this._onContext);
  }

  setMode(mode) {
    if (mode !== this.mode) {
      this.reset();
    }
    this.mode = mode;
  }

  reset(send) {
    if (send !== false && this.sender) {
      if (this.dragActive) {
        this.sender.mouseUp('left');
      }
      this.pointers.forEach((info) => {
        if (info && info.mouse && info.button) {
          this.sender.mouseUp(info.button);
        }
      });
    }
    if (this.longPressTimer) {
      window.clearTimeout(this.longPressTimer);
      this.longPressTimer = 0;
    }
    if (this.absRaf) {
      window.cancelAnimationFrame(this.absRaf);
      this.absRaf = 0;
    }
    this.pendingAbs = null;
    const ids = Array.from(this.pointers.keys());
    for (let i = 0; i < ids.length; i += 1) {
      try {
        this.stage.releasePointerCapture(ids[i]);
      } catch (error) {
        /* ignore */
      }
    }
    this.pointers.clear();
    this.singleId = null;
    this.maxPointers = 0;
    this.two = null;
    this.longPressFired = false;
    this.dragArmed = false;
    this.dragActive = false;
    this.dragPointer = null;
    this.accX = 0;
    this.accY = 0;
    this.lastTapTime = 0;
    this.groupMoved = false;
  }

  _local(event) {
    const rect = this.stage.getBoundingClientRect();
    return {
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
      cx: event.clientX,
      cy: event.clientY
    };
  }

  _stageSize() {
    return {
      w: this.stage.clientWidth || this.stage.offsetWidth || window.innerWidth || 1,
      h: this.stage.clientHeight || this.stage.offsetHeight || window.innerHeight || 1
    };
  }

  /** local stage point -> {x,y,inside} normalised over the visible picture. */
  _pointNorm(local) {
    const fit = this.getFit();
    const size = this._stageSize();
    if (!fit || !(fit.w > 0) || !(fit.h > 0)) {
      return { x: 0, y: 0, inside: false };
    }
    const scale = this.zoom ? this.zoom.scale : 1;
    const tx = this.zoom ? this.zoom.tx : 0;
    const ty = this.zoom ? this.zoom.ty : 0;
    const cx = size.w / 2;
    const cy = size.h / 2;
    const contentX = cx + (local.x - cx - tx) / scale;
    const contentY = cy + (local.y - cy - ty) / scale;
    const nx = (contentX - fit.x) / fit.w;
    const ny = (contentY - fit.y) / fit.h;
    return {
      x: clamp01(nx),
      y: clamp01(ny),
      inside: nx >= 0 && nx <= 1 && ny >= 0 && ny <= 1
    };
  }

  _scheduleAbs(norm) {
    if (!norm.inside) {
      return;
    }
    this.pendingAbs = { x: norm.x, y: norm.y };
    if (this.absRaf) {
      return;
    }
    this.absRaf = window.requestAnimationFrame(() => {
      this.absRaf = 0;
      const point = this.pendingAbs;
      this.pendingAbs = null;
      if (point) {
        this.sender.moveAbs(point.x, point.y);
      }
    });
  }

  _cancelLongPress() {
    if (this.longPressTimer) {
      window.clearTimeout(this.longPressTimer);
      this.longPressTimer = 0;
    }
  }

  _showToolbar() {
    if (this.onTap) {
      this.onTap();
    }
  }

  _insideKeyLayer(target) {
    return !!(target && target.closest && target.closest('.key-layer'));
  }

  _mousePoint(event) {
    const local = this._local(event);
    const norm = this._pointNorm(local);
    return norm;
  }

  _handleDown(event) {
    if (!this.isActive() || this.mode === 'view') {
      return;
    }
    if (this._insideKeyLayer(event.target)) {
      return;
    }
    if (event.pointerType === 'mouse') {
      if (this.mode !== 'direct') {
        return;
      }
      const norm = this._mousePoint(event);
      if (!norm.inside) {
        return;
      }
      const button = buttonNameFromPointer(event);
      if (!button) {
        return;
      }
      event.preventDefault();
      this.sender.moveAbs(norm.x, norm.y);
      this.sender.mouseDown(button);
      this.pointers.set(event.pointerId, {
        mouse: true, button: button, x: event.clientX, y: event.clientY
      });
      try {
        this.stage.setPointerCapture(event.pointerId);
      } catch (error) {
        /* ignore */
      }
      return;
    }

    const local = this._local(event);
    let norm = { x: 0, y: 0, inside: true };
    if (this.mode === 'direct') {
      norm = this._pointNorm(local);
      if (!norm.inside) {
        return;
      }
    }
    if (event.cancelable) {
      event.preventDefault();
    }
    try {
      this.stage.setPointerCapture(event.pointerId);
    } catch (error) {
      /* ignore */
    }

    const now = nowMs();
    const info = {
      mouse: false,
      x: local.x,
      y: local.y,
      cx: local.cx,
      cy: local.cy,
      startX: local.x,
      startY: local.y,
      startT: now,
      moved: false,
      norm: norm,
      accX: 0,
      accY: 0,
      lastMoveT: now
    };
    this.pointers.set(event.pointerId, info);

    if (this.pointers.size === 1) {
      this.singleId = event.pointerId;
      this.maxPointers = 1;
      this.groupMoved = false;
      this.groupStartT = now;
      this.groupStartPoint = { x: local.x, y: local.y };
      this.accX = 0;
      this.accY = 0;
      this.longPressFired = false;
      this.dragActive = false;
      this.dragPointer = event.pointerId;
      this.dragArmed = this.mode === 'touchpad' && (now - this.lastTapTime) <= 300;
      this.two = null;
      if (this.mode === 'direct') {
        this.sender.moveAbs(norm.x, norm.y);
        const point = info;
        this._cancelLongPress();
        this.longPressTimer = window.setTimeout(() => {
          this.longPressTimer = 0;
          if (!this.isActive() || this.mode !== 'direct' || this.pointers.size !== 1) {
            return;
          }
          const current = this.pointers.get(this.singleId);
          if (!current || current.moved) {
            return;
          }
          const target = this._pointNorm({ x: current.x, y: current.y });
          if (!target.inside) {
            return;
          }
          this.sender.moveAbs(target.x, target.y);
          this.sender.mouseDown('right');
          this.sender.mouseUp('right');
          this.longPressFired = true;
        }, 500);
        return;
      }
      return;
    }

    this.maxPointers = Math.max(this.maxPointers, this.pointers.size);
    this._cancelLongPress();
    if (this.dragActive) {
      this.sender.mouseUp('left');
      this.dragActive = false;
    }
    if (this.pointers.size === 2) {
      const values = Array.from(this.pointers.values());
      const first = values[0];
      const second = values[1];
      const dx = first.x - second.x;
      const dy = first.y - second.y;
      const mid = {
        x: (first.x + second.x) / 2,
        y: (first.y + second.y) / 2,
        cx: (first.cx + second.cx) / 2,
        cy: (first.cy + second.cy) / 2
      };
      this.two = {
        startDist: Math.max(1, Math.sqrt(dx * dx + dy * dy)),
        startMid: mid,
        lastMid: mid,
        startScale: this.zoom ? this.zoom.scale : 1,
        classified: '',
        accX: 0,
        accY: 0
      };
    } else {
      // Three or more fingers: keep the tap candidate alive for the
      // three-finger middle click. Any actual movement marks the group moved.
      this.two = null;
    }
  }

  _handleMove(event) {
    if (!this.isActive() || this.mode === 'view') {
      return;
    }
    if (event.pointerType === 'mouse') {
      if (this.mode !== 'direct') {
        return;
      }
      const norm = this._mousePoint(event);
      if (norm.inside) {
        this._scheduleAbs(norm);
      }
      return;
    }
    const info = this.pointers.get(event.pointerId);
    if (!info || info.mouse) {
      return;
    }
    const local = this._local(event);
    const dx = local.x - info.x;
    const dy = local.y - info.y;
    const now = nowMs();
    // Acceleration uses the instantaneous speed since the previous move, not
    // the average speed since pointer-down.
    const dt = Math.max(1, now - (info.lastMoveT || info.startT));
    info.lastMoveT = now;
    info.x = local.x;
    info.y = local.y;
    info.cx = local.cx;
    info.cy = local.cy;
    info.norm = this.mode === 'direct'
      ? this._pointNorm(local)
      : { x: 0, y: 0, inside: true };

    if (event.cancelable) {
      event.preventDefault();
    }

    if (this.pointers.size === 1) {
      if (this.maxPointers > 1) {
        return;
      }
      const totalDx = local.x - info.startX;
      const totalDy = local.y - info.startY;
      const totalDist = Math.sqrt(totalDx * totalDx + totalDy * totalDy);
      if (!info.moved && totalDist > TAP_MOVE_LIMIT) {
        info.moved = true;
        this.groupMoved = true;
      }
      if (this.mode === 'direct') {
        if (info.moved) {
          this._cancelLongPress();
        }
        // Points in the black bars are ignored, but a drag that started on
        // the picture keeps its button held until the finger lifts.
        if (!info.norm.inside) {
          return;
        }
        this._scheduleAbs(info.norm);
        if (info.moved && !this.dragActive && !this.longPressFired) {
          this.sender.mouseDown('left');
          this.dragActive = true;
        }
        return;
      }

      const settings = this.getSettings();
      info.accX += dx;
      info.accY += dy;
      if (!info.moved) {
        return;
      }
      let fingerDx = dx;
      let fingerDy = dy;
      if (!info.sentInitial) {
        fingerDx = info.accX;
        fingerDy = info.accY;
        info.sentInitial = true;
      }
      info.accX = 0;
      info.accY = 0;
      if (this.dragArmed && !this.dragActive) {
        this.sender.mouseDown('left');
        this.dragActive = true;
        this.dragArmed = false;
      }
      const speed = Math.sqrt(dx * dx + dy * dy) / dt;
      const delta = touchpadDelta(
        fingerDx,
        fingerDy,
        speed,
        this.getDisplayWidth(),
        this.getDisplayedWidth(),
        settings.pointerSensitivity,
        settings.pointerAccel
      );
      this.sender.moveRel(delta.dx, delta.dy);
      return;
    }

    if (this.pointers.size > 2) {
      // With three or more fingers no gesture is forwarded, but any real
      // movement disqualifies the three-finger tap.
      if (!this.groupMoved) {
        this.pointers.forEach((point) => {
          const totalDx = point.x - point.startX;
          const totalDy = point.y - point.startY;
          if (Math.sqrt(totalDx * totalDx + totalDy * totalDy) > TAP_MOVE_LIMIT) {
            this.groupMoved = true;
          }
        });
      }
      return;
    }

    if (this.pointers.size === 2 && this.two) {
      const values = Array.from(this.pointers.values());
      const first = values[0];
      const second = values[1];
      const ddx = first.x - second.x;
      const ddy = first.y - second.y;
      const dist = Math.max(1, Math.sqrt(ddx * ddx + ddy * ddy));
      const mid = {
        x: (first.x + second.x) / 2,
        y: (first.y + second.y) / 2,
        cx: (first.cx + second.cx) / 2,
        cy: (first.cy + second.cy) / 2
      };
      const two = this.two;
      if (!two.classified) {
        const distChange = Math.abs(dist - two.startDist);
        const mdx = mid.x - two.startMid.x;
        const mdy = mid.y - two.startMid.y;
        const midMove = Math.sqrt(mdx * mdx + mdy * mdy);
        if (distChange > 8 || midMove > 8) {
          two.classified = (distChange > 10 && distChange > midMove * 0.8) ? 'pinch' : 'scroll';
          this.groupMoved = true;
          two.lastMid = mid;
        } else {
          return;
        }
      }
      if (two.classified === 'pinch') {
        if (this.zoom) {
          this.zoom.zoomAt(mid.cx, mid.cy, two.startScale * (dist / two.startDist));
        }
        return;
      }
      if (two.classified === 'scroll') {
        const settings = this.getSettings();
        const speed = clamp(finite(settings.scrollSpeed, 1), 0.2, 5);
        const natural = settings.naturalScroll !== false;
        const moveDx = mid.x - two.lastMid.x;
        const moveDy = mid.y - two.lastMid.y;
        two.lastMid = mid;
        let wheelY = scrollToWheel(moveDy, speed);
        let wheelX = scrollToWheel(moveDx, speed);
        if (!natural) {
          wheelY = -wheelY;
        } else {
          wheelX = -wheelX;
        }
        const stepY = takeWheelStep(two.accY, wheelY);
        two.accY = stepY.rest;
        const stepX = takeWheelStep(two.accX, wheelX);
        two.accX = stepX.rest;
        if (stepY.step !== 0 || stepX.step !== 0) {
          this.sender.wheel(stepY.step, stepX.step);
        }
      }
    }
  }

  _handleUp(event) {
    if (!this.isActive()) {
      return;
    }
    const info = this.pointers.get(event.pointerId);
    if (!info) {
      return;
    }
    try {
      this.stage.releasePointerCapture(event.pointerId);
    } catch (error) {
      /* ignore */
    }
    if (event.pointerType === 'mouse' && info.mouse) {
      this.pointers.delete(event.pointerId);
      if (info.button) {
        this.sender.mouseUp(info.button);
      }
      return;
    }
    this.pointers.delete(event.pointerId);
    const now = nowMs();
    if (this.pointers.size > 0) {
      if (this.pointers.size === 1) {
        this.two = null;
      }
      return;
    }

    this._cancelLongPress();
    if (this.dragActive) {
      this.sender.mouseUp('left');
      this.dragActive = false;
      this.lastTapTime = 0;
    } else if (this.maxPointers === 1) {
      const infoMoved = info.moved || this.groupMoved;
      if (!this.longPressFired && !infoMoved && (now - this.groupStartT) < 320) {
        if (this.mode === 'direct') {
          const norm = this._pointNorm({ x: info.x, y: info.y });
          if (norm.inside) {
            this.sender.moveAbs(norm.x, norm.y);
            this.sender.mouseDown('left');
            this.sender.mouseUp('left');
          }
        } else {
          this.sender.mouseDown('left');
          this.sender.mouseUp('left');
        }
        this.lastTapTime = now;
        this._showToolbar();
      }
    } else if (this.mode === 'touchpad' && (this.maxPointers === 2 || this.maxPointers === 3) &&
        !this.groupMoved && (now - this.groupStartT) < 320) {
      const button = this.maxPointers === 2 ? 'right' : 'middle';
      this.sender.mouseDown(button);
      this.sender.mouseUp(button);
      this.lastTapTime = 0;
      this._showToolbar();
    }

    this.singleId = null;
    this.maxPointers = 0;
    this.two = null;
    this.groupMoved = false;
    this.longPressFired = false;
    this.dragArmed = false;
    this.accX = 0;
    this.accY = 0;
  }

  _handleCancel(event) {
    const info = this.pointers.get(event.pointerId);
    if (!info) {
      return;
    }
    this.pointers.delete(event.pointerId);
    if (this.dragActive) {
      this.sender.mouseUp('left');
      this.dragActive = false;
    }
    if (info.mouse && info.button) {
      this.sender.mouseUp(info.button);
    }
    if (this.pointers.size === 0) {
      this.reset();
    }
  }

  _handleWheel(event) {
    if (!this.isActive() || this.mode === 'view' || this._insideKeyLayer(event.target)) {
      return;
    }
    if (this.mode === 'direct') {
      event.preventDefault();
      let dy = event.deltaY;
      let dx = event.deltaX;
      if (event.deltaMode === 1) {
        dy *= 16;
        dx *= 16;
      } else if (event.deltaMode === 2) {
        dy *= 100;
        dx *= 100;
      }
      const wheelY = clamp(-Math.round(dy), -2400, 2400);
      const wheelX = clamp(Math.round(dx), -2400, 2400);
      if (wheelY !== 0 || wheelX !== 0) {
        this.sender.wheel(wheelY, wheelX);
      }
      return;
    }
    if (this.mode === 'touchpad' && this.zoom) {
      event.preventDefault();
      let delta = event.deltaY;
      if (event.deltaMode === 1) {
        delta *= 16;
      } else if (event.deltaMode === 2) {
        delta *= 100;
      }
      const factor = Math.exp(-delta * 0.0015);
      const next = clamp(this.zoom.scale * factor, 1, 6);
      if (Math.abs(next - this.zoom.scale) > 0.0005) {
        this.zoom.zoomAt(event.clientX, event.clientY, next);
      }
    }
  }
}

/**
 * Forwards physical keyboard events while a control mode is active.
 * Auto-repeat keydown is swallowed but not retransmitted.
 */
export class KeyboardForwarder {
  constructor(options) {
    const opts = options || {};
    this.sender = opts.sender;
    this.canForward = opts.canForward || function () { return false; };
    this._onDown = this._handleDown.bind(this);
    this._onUp = this._handleUp.bind(this);
    this._bound = false;
  }

  bind() {
    if (this._bound || typeof document === 'undefined') {
      return;
    }
    this._bound = true;
    document.addEventListener('keydown', this._onDown, true);
    document.addEventListener('keyup', this._onUp, true);
  }

  _isTextTarget(target) {
    if (!target || !target.tagName) {
      return false;
    }
    const tag = String(target.tagName).toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable;
  }

  _handleDown(event) {
    if (!this.canForward() || this._isTextTarget(event.target)) {
      return;
    }
    if (!isKeyCode(event.code)) {
      return;
    }
    if (event.cancelable) {
      event.preventDefault();
    }
    if (event.repeat || event.isComposing) {
      return;
    }
    this.sender.keyDown(event.code);
  }

  _handleUp(event) {
    if (!isKeyCode(event.code)) {
      return;
    }
    // Always clear a held key, even if focus moved into the hidden textarea
    // while it was down; otherwise the PC-side key would stay stuck.
    if (!this.sender.isKeyHeld(event.code)) {
      return;
    }
    if (!this._isTextTarget(event.target) && event.cancelable) {
      event.preventDefault();
    }
    this.sender.keyUp(event.code);
  }
}

/** Hidden textarea -> tx, with IME composition and Backspace/Enter/Tab keys. */
export class TextInput {
  constructor(options) {
    const opts = options || {};
    this.el = opts.el;
    this.sender = opts.sender;
    this.canSend = opts.canSend || function () { return true; };
    this.composing = false;
    this.skipInput = false;
    this._bound = false;
    this._onInput = this._handleInput.bind(this);
    this._onStart = this._handleStart.bind(this);
    this._onEnd = this._handleEnd.bind(this);
    this._onKey = this._handleKey.bind(this);
  }

  bind() {
    if (this._bound || !this.el) {
      return;
    }
    this._bound = true;
    this.el.addEventListener('input', this._onInput);
    this.el.addEventListener('compositionstart', this._onStart);
    this.el.addEventListener('compositionend', this._onEnd);
    this.el.addEventListener('keydown', this._onKey);
  }

  focus() {
    if (!this.el || !this.canSend()) {
      return false;
    }
    this.el.value = '';
    try {
      this.el.focus({ preventScroll: true });
    } catch (error) {
      this.el.focus();
    }
    return true;
  }

  blur() {
    if (this.el && typeof document !== 'undefined' && document.activeElement === this.el) {
      this.el.blur();
    }
  }

  _send(value) {
    if (!this.canSend()) {
      return;
    }
    const text = String(value || '');
    if (text) {
      this.sender.text(text);
    }
  }

  _handleInput() {
    if (this.composing) {
      return;
    }
    if (this.skipInput) {
      this.skipInput = false;
      this.el.value = '';
      return;
    }
    const value = this.el.value;
    if (value) {
      this._send(value);
      this.el.value = '';
    }
  }

  _handleStart() {
    this.composing = true;
  }

  _handleEnd(event) {
    this.composing = false;
    const data = event && typeof event.data === 'string' ? event.data : this.el.value;
    this.skipInput = true;
    if (typeof window !== 'undefined') {
      window.setTimeout(() => { this.skipInput = false; }, 0);
    }
    this.el.value = '';
    this._send(data);
  }

  _handleKey(event) {
    if (this.composing || !this.canSend()) {
      return;
    }
    if (event.code !== 'Backspace' && event.code !== 'Enter' && event.code !== 'Tab') {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    this.sender.keyDown(event.code);
    this.sender.keyUp(event.code);
    this.el.value = '';
  }
}
