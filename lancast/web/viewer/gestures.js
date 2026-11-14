/*
 * Pinch / pan / double-tap / wheel zoom for the video stage.
 *
 * The controller only writes a CSS transform on `target`; the base size and
 * position of the canvas inside that target are managed by app.js. Coordinates
 * are relative to `viewport` (the full-screen stage).
 */

const MIN_SCALE = 1;
const MAX_SCALE = 6;
const TAP_DELAY = 250;
const DOUBLE_WINDOW = 300;
const MOVE_LIMIT = 10;

function clamp(value, min, max) {
  return value < min ? min : value > max ? max : value;
}

function distance(a, b) {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return Math.sqrt(dx * dx + dy * dy);
}

function midpoint(a, b) {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export class ZoomPan {
  constructor(options) {
    const opts = options || {};
    this.viewport = opts.viewport;
    this.target = opts.target;
    this.getContentRect = opts.getContentRect || this._fallbackRect.bind(this);
    this.onTap = opts.onTap || null;
    this.onZoomChange = opts.onZoomChange || null;

    this.scale = 1;
    this.tx = 0;
    this.ty = 0;
    this.enabled = true;

    this.pointers = new Map();
    this._pinch = null;
    this._pan = null;
    this._tap = null;
    this._tapTimer = 0;
    this._lastTap = null;
    this._doubleCandidate = false;
    this._pinchMoved = false;
    this._destroyed = false;

    if (!this.viewport || !this.target) {
      throw new Error('ZoomPan needs viewport and target elements');
    }

    this._onPointerDown = this._handlePointerDown.bind(this);
    this._onPointerMove = this._handlePointerMove.bind(this);
    this._onPointerUp = this._handlePointerUp.bind(this);
    this._onPointerCancel = this._handlePointerUp.bind(this);
    this._onWheel = this._handleWheel.bind(this);
    this._onGesture = this._handleGesture.bind(this);
    this._onDoubleClick = function (event) { event.preventDefault(); };
    this._onTouchMove = this._handleTouchMove.bind(this);

    this.viewport.addEventListener('pointerdown', this._onPointerDown, { passive: false });
    this.viewport.addEventListener('pointermove', this._onPointerMove, { passive: false });
    this.viewport.addEventListener('pointerup', this._onPointerUp, { passive: false });
    this.viewport.addEventListener('pointercancel', this._onPointerCancel, { passive: false });
    this.viewport.addEventListener('wheel', this._onWheel, { passive: false });
    this.viewport.addEventListener('dblclick', this._onDoubleClick);
    this.viewport.addEventListener('gesturestart', this._onGesture, { passive: false });
    this.viewport.addEventListener('gesturechange', this._onGesture, { passive: false });
    this.viewport.addEventListener('gestureend', this._onGesture, { passive: false });
    this.viewport.addEventListener('touchmove', this._onTouchMove, { passive: false });

    this.refresh();
  }

  _fallbackRect() {
    const size = this._viewportSize();
    return { x: 0, y: 0, w: size.w, h: size.h };
  }

  _viewportSize() {
    const width = this.viewport.clientWidth || this.viewport.offsetWidth || window.innerWidth || 1;
    const height = this.viewport.clientHeight || this.viewport.offsetHeight || window.innerHeight || 1;
    return { w: width, h: height };
  }

  _localPoint(clientX, clientY) {
    const rect = this.viewport.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  _contentRect() {
    let rect;
    try {
      rect = this.getContentRect();
    } catch (error) {
      rect = null;
    }
    if (!rect || !Number.isFinite(rect.x) || !Number.isFinite(rect.y) ||
        !(rect.w > 0) || !(rect.h > 0)) {
      return this._fallbackRect();
    }
    return rect;
  }

  _clampTranslation() {
    const size = this._viewportSize();
    const rect = this._contentRect();
    const centerX = size.w / 2;
    const centerY = size.h / 2;
    const scale = this.scale;

    if (scale <= 1.0001) {
      this.tx = 0;
      this.ty = 0;
      return;
    }

    const scaledW = rect.w * scale;
    const scaledH = rect.h * scale;

    if (scaledW <= size.w) {
      this.tx = (size.w - scaledW) / 2 - centerX - (rect.x - centerX) * scale;
    } else {
      const minTx = size.w - centerX - (rect.x + rect.w - centerX) * scale;
      const maxTx = -centerX - (rect.x - centerX) * scale;
      this.tx = clamp(this.tx, minTx, maxTx);
    }

    if (scaledH <= size.h) {
      this.ty = (size.h - scaledH) / 2 - centerY - (rect.y - centerY) * scale;
    } else {
      const minTy = size.h - centerY - (rect.y + rect.h - centerY) * scale;
      const maxTy = -centerY - (rect.y - centerY) * scale;
      this.ty = clamp(this.ty, minTy, maxTy);
    }
  }

  _apply() {
    this.target.style.transform = 'translate3d(' + this.tx + 'px,' + this.ty + 'px,0) scale(' + this.scale + ')';
    if (this.onZoomChange) {
      this.onZoomChange(this.scale);
    }
  }

  refresh() {
    if (this._destroyed) {
      return;
    }
    this.scale = clamp(this.scale, MIN_SCALE, MAX_SCALE);
    if (this.scale <= 1.0001) {
      this.scale = 1;
      this.tx = 0;
      this.ty = 0;
    }
    this._clampTranslation();
    this._apply();
  }

  reset() {
    this.scale = 1;
    this.tx = 0;
    this.ty = 0;
    this._clampTranslation();
    this._apply();
  }

  /**
   * Enable/disable the built-in pinch/pan/tap handling. Disabling clears all
   * in-flight gesture state but deliberately keeps the current zoom, because
   * control modes still zoom through PointerControl and zoomAt(). Page-level
   * gesture suppression (gesturestart/touchmove) stays active either way.
   */
  setEnabled(enabled) {
    const next = !!enabled;
    if (next === this.enabled) {
      return;
    }
    this.enabled = next;
    if (!next) {
      const ids = Array.from(this.pointers.keys());
      for (let i = 0; i < ids.length; i += 1) {
        try {
          this.viewport.releasePointerCapture(ids[i]);
        } catch (error) {
          /* ignore */
        }
      }
      this.pointers.clear();
      this._pinch = null;
      this._pan = null;
      this._tap = null;
      this._pinchMoved = false;
      this._doubleCandidate = false;
      this._lastTap = null;
      if (this._tapTimer) {
        clearTimeout(this._tapTimer);
        this._tapTimer = 0;
      }
    }
  }

  /**
   * Pan (if needed) so that a normalised (0..1) point of the picture stays at
   * least `margin` CSS px inside the viewport. Only has an effect while zoomed.
   */
  ensureVisible(x, y, margin) {
    if (this._destroyed || this.scale <= 1.0001) {
      return;
    }
    const nx = Number(x);
    const ny = Number(y);
    if (!Number.isFinite(nx) || !Number.isFinite(ny)) {
      return;
    }
    const rect = this._contentRect();
    const size = this._viewportSize();
    const centerX = size.w / 2;
    const centerY = size.h / 2;
    const edge = Math.max(0, Number(margin) || 0);
    const minX = Math.min(edge, size.w / 2);
    const minY = Math.min(edge, size.h / 2);
    const maxX = Math.max(minX, size.w - minX);
    const maxY = Math.max(minY, size.h - minY);
    const contentX = rect.x + clamp(nx, 0, 1) * rect.w;
    const contentY = rect.y + clamp(ny, 0, 1) * rect.h;
    const screenX = centerX + this.tx + (contentX - centerX) * this.scale;
    const screenY = centerY + this.ty + (contentY - centerY) * this.scale;
    if (screenX < minX) {
      this.tx += minX - screenX;
    } else if (screenX > maxX) {
      this.tx -= screenX - maxX;
    }
    if (screenY < minY) {
      this.ty += minY - screenY;
    } else if (screenY > maxY) {
      this.ty -= screenY - maxY;
    }
    this._clampTranslation();
    this._apply();
  }

  zoomAt(clientX, clientY, nextScale) {
    const point = this._localPoint(clientX, clientY);
    const size = this._viewportSize();
    const centerX = size.w / 2;
    const centerY = size.h / 2;
    const oldScale = this.scale;
    const newScale = clamp(Number(nextScale), MIN_SCALE, MAX_SCALE);
    if (!Number.isFinite(newScale) || oldScale <= 0) {
      return;
    }
    const ratio = newScale / oldScale;
    this.tx = point.x - centerX - (point.x - centerX - this.tx) * ratio;
    this.ty = point.y - centerY - (point.y - centerY - this.ty) * ratio;
    this.scale = newScale;
    this._clampTranslation();
    this._apply();
  }

  _beginPinch() {
    const values = Array.from(this.pointers.values());
    if (values.length < 2) {
      this._pinch = null;
      return;
    }
    const first = values[0];
    const second = values[1];
    const mid = midpoint(first, second);
    const size = this._viewportSize();
    const centerX = size.w / 2;
    const centerY = size.h / 2;
    this._pinch = {
      distance: Math.max(1, distance(first, second)),
      scale: this.scale,
      contentX: centerX + (mid.x - centerX - this.tx) / this.scale,
      contentY: centerY + (mid.y - centerY - this.ty) / this.scale
    };
    this._pinchMoved = true;
    if (this._tapTimer) {
      clearTimeout(this._tapTimer);
      this._tapTimer = 0;
    }
    this._doubleCandidate = false;
  }

  _startPanFromRemaining() {
    const values = Array.from(this.pointers.values());
    if (values.length !== 1) {
      this._pan = null;
      return;
    }
    this._pan = {
      x: values[0].x,
      y: values[0].y,
      tx: this.tx,
      ty: this.ty
    };
  }

  _handlePointerDown(event) {
    if (this._destroyed || !this.enabled) {
      return;
    }
    if (event.pointerType === 'mouse' && event.button !== 0) {
      return;
    }
    const point = this._localPoint(event.clientX, event.clientY);
    const hadPointers = this.pointers.size > 0;

    if (!hadPointers) {
      this._pinchMoved = false;
      this._doubleCandidate = false;
      const last = this._lastTap;
      if (last && (Date.now() - last.time) < DOUBLE_WINDOW && distance(point, last) < 30) {
        this._doubleCandidate = true;
        if (this._tapTimer) {
          clearTimeout(this._tapTimer);
          this._tapTimer = 0;
        }
      }
    }

    this.pointers.set(event.pointerId, point);
    try {
      this.viewport.setPointerCapture(event.pointerId);
    } catch (error) {
      /* not available on some old Safari builds */
    }

    if (this.pointers.size === 1) {
      this._tap = {
        id: event.pointerId,
        x: point.x,
        y: point.y,
        time: Date.now(),
        moved: false,
        multi: false
      };
      this._pan = { x: point.x, y: point.y, tx: this.tx, ty: this.ty };
      this._pinch = null;
    } else {
      if (this._tap) {
        this._tap.multi = true;
      }
      this._beginPinch();
    }

    if (event.cancelable) {
      event.preventDefault();
    }
  }

  _handlePointerMove(event) {
    if (this._destroyed || !this.enabled || !this.pointers.has(event.pointerId)) {
      return;
    }
    const point = this._localPoint(event.clientX, event.clientY);
    this.pointers.set(event.pointerId, point);

    if (this.pointers.size >= 2 && this._pinch) {
      const values = Array.from(this.pointers.values());
      const first = values[0];
      const second = values[1];
      const dist = Math.max(1, distance(first, second));
      const mid = midpoint(first, second);
      const size = this._viewportSize();
      const centerX = size.w / 2;
      const centerY = size.h / 2;
      let scale = this._pinch.scale * (dist / this._pinch.distance);
      scale = clamp(scale, MIN_SCALE, MAX_SCALE);
      this.scale = scale;
      this.tx = mid.x - centerX - (this._pinch.contentX - centerX) * scale;
      this.ty = mid.y - centerY - (this._pinch.contentY - centerY) * scale;
      this._clampTranslation();
      this._apply();
      if (event.cancelable) {
        event.preventDefault();
      }
      return;
    }

    const tap = this._tap;
    if (tap && tap.moved === false) {
      const dx = point.x - tap.x;
      const dy = point.y - tap.y;
      if (Math.sqrt(dx * dx + dy * dy) > MOVE_LIMIT) {
        tap.moved = true;
      }
    }

    if (this.pointers.size === 1 && this._pan && this.scale > 1.0001) {
      this.tx = this._pan.tx + (point.x - this._pan.x);
      this.ty = this._pan.ty + (point.y - this._pan.y);
      this._clampTranslation();
      this._apply();
      if (event.cancelable) {
        event.preventDefault();
      }
    }
  }

  _handlePointerUp(event) {
    if (this._destroyed || !this.enabled) {
      return;
    }
    if (!this.pointers.has(event.pointerId)) {
      return;
    }
    const point = this._localPoint(event.clientX, event.clientY);
    const wasPinching = this.pointers.size >= 2;
    this.pointers.delete(event.pointerId);
    try {
      this.viewport.releasePointerCapture(event.pointerId);
    } catch (error) {
      /* ignore */
    }

    if (wasPinching) {
      this._pinch = null;
      if (this.pointers.size === 1) {
        this._startPanFromRemaining();
        if (this._tap) {
          this._tap.multi = true;
        }
      }
    }

    if (this.pointers.size > 0) {
      return;
    }

    const tap = this._tap;
    const now = Date.now();
    const valid = tap && tap.id === event.pointerId && !tap.moved && !tap.multi &&
      !this._pinchMoved && event.type !== 'pointercancel' &&
      (now - tap.time) < 320;

    this._pan = null;
    this._pinch = null;

    if (valid) {
      if (this._doubleCandidate) {
        this._doubleCandidate = false;
        this._lastTap = null;
        if (this._tapTimer) {
          clearTimeout(this._tapTimer);
          this._tapTimer = 0;
        }
        this._doubleTap(point);
      } else {
        this._scheduleTap(point);
      }
    } else {
      this._doubleCandidate = false;
    }
    this._tap = null;
  }

  _scheduleTap(point) {
    this._lastTap = { x: point.x, y: point.y, time: Date.now() };
    if (this._tapTimer) {
      clearTimeout(this._tapTimer);
    }
    this._tapTimer = window.setTimeout(() => {
      this._tapTimer = 0;
      this._lastTap = null;
      if (this.onTap) {
        this.onTap(point);
      }
    }, TAP_DELAY);
  }

  _doubleTap(point) {
    const probe = this.viewport.getBoundingClientRect();
    const target = this.scale > 1.05 ? 1 : 2.5;
    this.zoomAt(probe.left + point.x, probe.top + point.y, target);
  }

  _handleWheel(event) {
    if (this._destroyed || !this.enabled) {
      return;
    }
    event.preventDefault();
    let delta = event.deltaY;
    if (event.deltaMode === 1) {
      delta *= 16;
    } else if (event.deltaMode === 2) {
      delta *= 100;
    }
    const factor = Math.exp(-delta * 0.0015);
    const next = clamp(this.scale * factor, MIN_SCALE, MAX_SCALE);
    if (Math.abs(next - this.scale) > 0.0005) {
      this.zoomAt(event.clientX, event.clientY, next);
    }
  }

  _handleGesture(event) {
    if (event.cancelable) {
      event.preventDefault();
    }
  }

  _handleTouchMove(event) {
    if (event.touches && event.touches.length > 1 && event.cancelable) {
      event.preventDefault();
    }
  }

  destroy() {
    this._destroyed = true;
    if (this._tapTimer) {
      clearTimeout(this._tapTimer);
      this._tapTimer = 0;
    }
    this.viewport.removeEventListener('pointerdown', this._onPointerDown);
    this.viewport.removeEventListener('pointermove', this._onPointerMove);
    this.viewport.removeEventListener('pointerup', this._onPointerUp);
    this.viewport.removeEventListener('pointercancel', this._onPointerCancel);
    this.viewport.removeEventListener('wheel', this._onWheel);
    this.viewport.removeEventListener('dblclick', this._onDoubleClick);
    this.viewport.removeEventListener('gesturestart', this._onGesture);
    this.viewport.removeEventListener('gesturechange', this._onGesture);
    this.viewport.removeEventListener('gestureend', this._onGesture);
    this.viewport.removeEventListener('touchmove', this._onTouchMove);
    this.pointers.clear();
  }
}

/**
 * Stops iOS Safari's page-level pinch zoom and two-finger scrolling. Safe to
 * call once from app.js; listener options are deliberately non-passive.
 */
export function preventNativeGestures(doc) {
  const target = doc || document;
  const stop = function (event) {
    if (event.cancelable) {
      event.preventDefault();
    }
  };
  target.addEventListener('gesturestart', stop, { passive: false });
  target.addEventListener('gesturechange', stop, { passive: false });
  target.addEventListener('gestureend', stop, { passive: false });
}
