/*
 * LanCast viewer entry point: connection state machine, WebSocket transport,
 * streaming pipeline, toolbar and overlays.
 */

import { initI18n, applyI18n, getLang, t } from './i18n.js';
import { icon, cursorIcon } from './icons.js';
import {
  parseFrameHeader,
  ClockOffsetEstimator,
  deviceNameFromUA,
  authFailReasonKey,
  closeCodeReasonKey,
  median
} from './protocol.js';
import { H264Player, JpegPlayer, createPlayer, supportsH264 } from './player.js';
import { ZoomPan, preventNativeGestures } from './gestures.js';
import { InputSender, PointerControl, KeyboardForwarder, TextInput } from './control.js';
import { LayoutStore, KeyLayer, LayoutEditor } from './keymap.js';
import {
  SettingsPanel,
  normalizeInputSettings,
  describeStreamQuality
} from './settings.js';
import {
  normalizeQualityPreferences,
  buildWatchQuality,
  AutoQuality,
  AUTO_LEVELS
} from './quality.js';
import {
  GamepadController,
  normalizeGamepadMapping
} from './gamepad.js';

const K_DEVICE = 'lancast.device';
const K_NAME = 'lancast.name';
const K_DISPLAY = 'lancast.display';
const K_CODEC = 'lancast.codec';
const K_STATS = 'lancast.stats';
const K_FIT = 'lancast.fit';
const K_INPUT = 'lancast.input';
const K_QUALITY = 'lancast.quality';
const K_CTRL = 'lancast.ctrlmode';
const K_GAMEPAD = 'lancast.gamepad';

const CONTROL_MODES = ['view', 'touchpad', 'direct'];

const PING_INTERVAL_MS = 2000;
const STATS_INTERVAL_MS = 2000;
const TOOLBAR_HIDE_MS = 5000;
const RECONNECT_DELAYS = [1, 2, 4, 8, 10];
const AUTH_TIMEOUT_MS = 15000;

function $(id) {
  return document.getElementById(id);
}

function clamp01(value) {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

function pruneNumbers(list, cutoff) {
  while (list.length > 0 && list[0] < cutoff) {
    list.shift();
  }
}

function pruneEvents(list, cutoff) {
  while (list.length > 0 && list[0].t < cutoff) {
    list.shift();
  }
}

const els = {
  connecting: $('screen-connecting'),
  connectingCancel: $('connecting-cancel'),
  pair: $('screen-pair'),
  pairForm: $('pair-form'),
  pairError: $('pair-error'),
  pairSubmit: $('pair-submit'),
  pin: $('pin'),
  name: $('device-name'),
  remember: $('remember'),
  pending: $('screen-pending'),
  pendingDevice: $('pending-device'),
  pendingSeconds: $('pending-seconds'),
  pendingCancel: $('pending-cancel'),
  stream: $('screen-stream'),
  stage: $('stage'),
  zoomLayer: $('zoom-layer'),
  canvas: $('canvas'),
  cursor: $('cursor'),
  stats: $('stats'),
  statResolution: $('stat-resolution'),
  statMode: $('stat-mode'),
  statFps: $('stat-fps'),
  statBitrate: $('stat-bitrate'),
  statRtt: $('stat-rtt'),
  statLatency: $('stat-latency'),
  statDropped: $('stat-dropped'),
  statQueue: $('stat-queue'),
  statQuality: $('stat-quality'),
  statControl: $('stat-control'),
  statInput: $('stat-input'),
  toolbar: $('toolbar'),
  displayWrap: $('display-wrap'),
  displayBtn: $('display-btn'),
  displayLabel: $('display-label'),
  displayMenu: $('display-menu'),
  codecBtn: $('codec-btn'),
  codecLabel: $('codec-label'),
  codecMenu: $('codec-menu'),
  ctrlBtn: $('ctrl-btn'),
  ctrlLabel: $('ctrl-label'),
  ctrlMenu: $('ctrl-menu'),
  keysBtn: $('keys-btn'),
  keysMoreBtn: $('keys-more-btn'),
  keysMenu: $('keys-menu'),
  kbdBtn: $('kbd-btn'),
  settingsBtn: $('settings-btn'),
  keyLayer: $('key-layer'),
  textInput: $('text-input'),
  keyboardClose: $('keyboard-close'),
  fitBtn: $('fit-btn'),
  fitLabel: $('fit-label'),
  statsBtn: $('stats-btn'),
  fsBtn: $('fs-btn'),
  fsLabel: $('fs-label'),
  moreBtn: $('more-btn'),
  moreMenu: $('more-menu'),
  disconnectBtn: $('disconnect-btn'),
  forgetBtn: $('forget-btn'),
  overlayPaused: $('overlay-paused'),
  overlayReconnect: $('overlay-reconnect'),
  reconnectCountdown: $('reconnect-countdown'),
  retryNow: $('retry-now'),
  reconnectPair: $('reconnect-pair'),
  toast: $('toast'),
  ended: $('screen-ended'),
  endedReason: $('ended-reason'),
  endedServer: $('ended-server'),
  rePair: $('re-pair')
};

/* ---------- persisted preferences ---------- */

const store = {
  get: function (key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  },
  set: function (key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      /* private mode / full storage */
    }
  },
  remove: function (key) {
    try {
      window.localStorage.removeItem(key);
    } catch (error) {
      /* ignore */
    }
  }
};

function readDevice() {
  const raw = store.get(K_DEVICE);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.device_id === 'string' && typeof parsed.token === 'string' &&
        parsed.device_id.length > 0 && parsed.token.length > 0) {
      return { device_id: parsed.device_id, token: parsed.token };
    }
  } catch (error) {
    /* fall through and forget the broken value */
  }
  store.remove(K_DEVICE);
  return null;
}

function readStoredJSON(key) {
  const raw = store.get(key);
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    return null;
  }
}

function writeStoredJSON(key, value) {
  try {
    store.set(key, JSON.stringify(value));
  } catch (error) {
    /* ignore quota / serialisation errors */
  }
}

function saveDevice(deviceId, token) {
  if (!deviceId || !token) {
    return;
  }
  store.set(K_DEVICE, JSON.stringify({ device_id: String(deviceId), token: String(token) }));
}

function defaultDeviceName() {
  return deviceNameFromUA(navigator.userAgent || '', getLang());
}

function storedName() {
  const saved = store.get(K_NAME);
  if (saved) {
    const clean = saved.trim().slice(0, 40);
    if (clean) {
      return clean;
    }
  }
  return defaultDeviceName();
}

function saveName(name) {
  const clean = String(name || '').trim().slice(0, 40);
  if (clean) {
    store.set(K_NAME, clean);
  }
}

function pickInitialCodec() {
  const stored = store.get(K_CODEC);
  if (stored === 'h264' || stored === 'jpeg') {
    return stored;
  }
  return (typeof VideoDecoder !== 'undefined' && window.isSecureContext) ? 'h264' : 'jpeg';
}

function pickInitialDisplay() {
  const stored = parseInt(store.get(K_DISPLAY), 10);
  for (let i = 0; i < displays.length; i += 1) {
    if (displays[i].index === stored) {
      return stored;
    }
  }
  return displays.length > 0 ? displays[0].index : 0;
}

/* ---------- runtime state ---------- */

let phase = 'boot';
let ws = null;
let wsGen = 0;
let authTimer = 0;
let authAttempt = null;
let authed = false;
let server = null;
let displays = [];
let currentDisplay = 0;
let currentCodec = 'jpeg';
let stream = null;
let streamEpoch = -1;
let player = null;
let playerGen = 0;
let playerReady = false;
let earlyKeyframe = null;
let paused = false;
let lastByeReason = null;
let didStream = false;

let reconnectAttempt = 0;
let reconnectTimer = 0;
let reconnectTicker = 0;
let reconnectDeadline = 0;

let pingTimer = 0;
let statsSendTimer = 0;
let statsUiTimer = 0;
let pendingTimer = 0;
let pendingDeadline = 0;
let rateTimer = 0;
let rateUntil = 0;

let statsVisible = false;
let fitMode = 'contain';
let toolbarVisible = false;
let toolbarTimer = 0;
let toolbarHintShown = false;
let toastTimer = 0;
let layoutRaf = 0;

let wakeLock = null;

let currentFit = { x: 0, y: 0, w: 1, h: 1 };
let zoom = null;
let cursorShape = '';
let cursorState = { x: 0, y: 0, visible: false, shape: 'arrow' };

/* ---------- remote control / quality state ---------- */

let controlAllowed = false;
let viewerQualityAllowed = true;
let controlMode = 'view';
let preferredControlMode = 'view';
let editingLayout = false;
let keyboardOpen = false;
let inputSettings = normalizeInputSettings(null);
let qualityPrefs = normalizeQualityPreferences(null);
let gamepadMapping = normalizeGamepadMapping(null);
let autoLevel = 'standard';
let extremeMode = false;
let droppedTotal = 0;
let autoLatency = [];
let autoTimer = 0;
let qualityTimer = 0;
let lastControlDeniedAt = 0;

let sender = null;
let pointerControl = null;
let keyboardForwarder = null;
let textInput = null;
let layoutStore = null;
let keyLayer = null;
let layoutEditor = null;
let settingsPanel = null;
let gamepadController = null;

const autoQuality = new AutoQuality({ level: 'standard' });

const estimator = new ClockOffsetEstimator();
const pingSends = new Map();
let lastPingT = 0;
let renderTimes = [];
let netEvents = [];
let latencySamples = [];
let lastKeyframeRequest = 0;
let lastFallbackAt = 0;

/* ---------- tiny UI helpers ---------- */

function applyStaticIcons(root) {
  const nodes = (root || document).querySelectorAll('[data-icon]');
  for (let i = 0; i < nodes.length; i += 1) {
    const node = nodes[i];
    const name = node.getAttribute('data-icon');
    if (!name) {
      continue;
    }
    node.innerHTML = icon(name);
    node.setAttribute('aria-hidden', 'true');
  }
}

function setButtonIcon(button, name) {
  if (!button) {
    return;
  }
  const target = button.querySelector('[data-icon]');
  if (!target) {
    return;
  }
  target.setAttribute('data-icon', name);
  target.innerHTML = icon(name);
}

function setPairError(message) {
  els.pairError.textContent = message || '';
}

function toast(message) {
  if (!message) {
    return;
  }
  els.toast.textContent = message;
  els.toast.hidden = false;
  if (toastTimer) {
    window.clearTimeout(toastTimer);
  }
  toastTimer = window.setTimeout(function () {
    toastTimer = 0;
    els.toast.hidden = true;
  }, 3500);
}

function setPhase(next) {
  phase = next;
  document.documentElement.setAttribute('data-phase', next);
  els.connecting.hidden = next !== 'connecting';
  els.pair.hidden = next !== 'pair';
  els.pending.hidden = next !== 'pending';
  els.ended.hidden = next !== 'ended';
  const streamVisible = next === 'streaming' || next === 'paused' || next === 'reconnecting';
  els.stream.hidden = !streamVisible;
  els.overlayPaused.hidden = next !== 'paused';
  els.overlayReconnect.hidden = next !== 'reconnecting';

  if (next === 'streaming') {
    setToolbarVisible(true);
  } else {
    setToolbarVisible(false);
    if (textInput) {
      textInput.blur();
    }
    if (keyLayer) {
      setKeyLayerVisible(false);
    }
    if (layoutEditor && layoutEditor.opened) {
      layoutEditor.close();
    }
  }

  if (sender) {
    if (next === 'streaming' && controlAllowed && !editingLayout) {
      sender.setEnabled(true);
    } else {
      if (sender.enabled) {
        releaseAllInput(true);
      }
      sender.setEnabled(false);
    }
  }
  updateControlUi();
}

/* ---------- connection ---------- */

function wsUrl() {
  return (window.location.protocol === 'https:' ? 'wss://' : 'ws://') +
    window.location.host + '/ws';
}

function clientInfo() {
  let screenInfo = [];
  try {
    const scr = window.screen || {};
    screenInfo = [
      Math.max(1, Math.round(Number(scr.width) || window.innerWidth || 1)),
      Math.max(1, Math.round(Number(scr.height) || window.innerHeight || 1)),
      Number(window.devicePixelRatio) || 1
    ];
  } catch (error) {
    screenInfo = [window.innerWidth || 1, window.innerHeight || 1, 1];
  }
  return {
    ua: navigator.userAgent || '',
    screen: screenInfo,
    webcodecs: typeof VideoDecoder !== 'undefined',
    lang: navigator.language || 'en'
  };
}

function buildAuthMessage(auth) {
  const message = { type: 'auth', method: auth.method, client: clientInfo() };
  if (auth.method === 'device') {
    message.device_id = auth.device_id;
    message.token = auth.token;
  } else if (auth.method === 'pin') {
    message.pin = auth.pin;
    message.name = auth.name;
    message.remember = !!auth.remember;
  } else if (auth.method === 'qr') {
    message.token = auth.token;
    message.name = auth.name;
    message.remember = !!auth.remember;
  }
  return message;
}

function clearAuthTimer() {
  if (authTimer) {
    window.clearTimeout(authTimer);
    authTimer = 0;
  }
}

function closeSocket() {
  wsGen += 1;
  const socket = ws;
  ws = null;
  clearAuthTimer();
  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    try {
      socket.close(1000, 'bye');
    } catch (error) {
      /* already closed */
    }
  }
}

function send(message) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return false;
  }
  try {
    ws.send(JSON.stringify(message));
    return true;
  } catch (error) {
    return false;
  }
}

function startAuth(auth, options) {
  const opts = options || {};
  resetControlSession();
  clearReconnect();
  clearAuthTimer();
  stopPending();
  stopHeartbeat();
  disposePlayer();
  stream = null;
  streamEpoch = -1;
  playerReady = false;
  paused = false;
  authed = false;
  lastByeReason = null;
  authAttempt = auth;
  if (auth && (auth.method === 'pin' || auth.method === 'qr')) {
    saveName(auth.name);
  }

  if (opts.reconnect) {
    setPhase('reconnecting');
    els.reconnectCountdown.textContent = '';
  } else {
    setPhase('connecting');
    els.connecting.hidden = false;
  }

  closeSocket();
  const generation = ++wsGen;
  let socket = null;
  try {
    socket = new WebSocket(wsUrl());
  } catch (error) {
    transportFailed(auth, opts);
    return;
  }
  ws = socket;
  socket.binaryType = 'arraybuffer';

  authTimer = window.setTimeout(function () {
    if (generation !== wsGen) {
      return;
    }
    closeSocket();
    transportFailed(auth, opts);
  }, AUTH_TIMEOUT_MS);

  socket.onopen = function () {
    if (generation !== wsGen) {
      return;
    }
    try {
      socket.send(JSON.stringify(buildAuthMessage(auth)));
    } catch (error) {
      closeSocket();
      transportFailed(auth, opts);
    }
  };

  socket.onmessage = function (event) {
    if (generation !== wsGen) {
      return;
    }
    if (typeof event.data === 'string') {
      handleTextMessage(event.data);
      return;
    }
    if (event.data instanceof ArrayBuffer) {
      handleBinary(event.data);
      return;
    }
    if (event.data && typeof event.data.arrayBuffer === 'function') {
      event.data.arrayBuffer().then(function (buffer) {
        if (generation === wsGen) {
          handleBinary(buffer);
        }
      }).catch(function () {
        /* ignore malformed blob */
      });
    }
  };

  socket.onclose = function (event) {
    if (generation !== wsGen) {
      return;
    }
    ws = null;
    clearAuthTimer();
    const wasAuthed = authed;
    authed = false;
    handleSocketClose(event, auth, wasAuthed);
  };

  socket.onerror = function () {
    /* the close event carries the actionable information */
  };
}

function transportFailed(auth, opts) {
  if (phase === 'ended' || phase === 'pair') {
    return;
  }
  if (auth && auth.method === 'device' && readDevice()) {
    scheduleReconnect();
    return;
  }
  showPair('pair.err.network');
}

function handleSocketClose(event, auth, wasAuthed) {
  if (phase === 'ended' || phase === 'pair') {
    return;
  }
  if (lastByeReason) {
    return;
  }
  stopHeartbeat();
  disposePlayer();
  stream = null;
  streamEpoch = -1;
  playerReady = false;

  const code = Number(event.code) || 1006;
  const reasonText = String(event.reason || '').toLowerCase();
  const device = readDevice();
  const canReconnect = !!device && (wasAuthed || (auth && auth.method === 'device'));

  if (code === 4004) {
    const revoked = reasonText.indexOf('revok') >= 0;
    endSession(revoked ? 'revoked' : 'kicked', { forget: revoked });
    return;
  }
  if (code === 4009) {
    endSession('serverFull');
    return;
  }
  if (code === 4003) {
    showPair('ended.authFailed');
    return;
  }
  if (code === 4008) {
    showPair(null, { rateLimit: 300 });
    return;
  }
  if (code === 4001) {
    if (wasAuthed) {
      endSession('protocol');
    } else {
      showPair('ended.protocol');
    }
    return;
  }
  if (code === 1000 || code === 4000) {
    if (wasAuthed) {
      endSession('disconnected');
    } else {
      showPair('pair.err.network');
    }
    return;
  }
  if (code === 4010) {
    if (canReconnect) {
      scheduleReconnect();
    } else {
      endSession('shutdown');
    }
    return;
  }
  if (canReconnect) {
    scheduleReconnect();
    return;
  }
  if (wasAuthed) {
    showPair('pair.err.reconnect');
  } else {
    showPair('pair.err.network');
  }
}

function handleTextMessage(text) {
  let message = null;
  try {
    message = JSON.parse(text);
  } catch (error) {
    return;
  }
  if (!message || typeof message.type !== 'string') {
    return;
  }
  switch (message.type) {
    case 'auth_pending':
      handleAuthPending(message);
      break;
    case 'auth_ok':
      handleAuthOk(message);
      break;
    case 'auth_fail':
      handleAuthFail(message);
      break;
    case 'stream':
      setupStream(message);
      break;
    case 'control':
      handleControlMessage(message);
      break;
    case 'cursor':
      handleCursor(message);
      break;
    case 'pong':
      handlePong(message);
      break;
    case 'displays':
      handleDisplays(message);
      break;
    case 'paused':
      handlePaused(message);
      break;
    case 'error':
      handleServerError(message);
      break;
    case 'bye':
      handleBye(message);
      break;
    default:
      break;
  }
}

function pendingName() {
  if (authAttempt && typeof authAttempt.name === 'string' && authAttempt.name.trim()) {
    return authAttempt.name.trim().slice(0, 40);
  }
  return storedName();
}

function handleAuthPending(message) {
  if (phase === 'ended' || phase === 'pair') {
    return;
  }
  clearAuthTimer();
  const timeout = Number(message.timeout_s);
  els.pendingDevice.textContent = pendingName();
  startPendingCountdown(timeout > 0 ? timeout : 60);
  setPhase('pending');
}

function handleAuthOk(message) {
  clearAuthTimer();
  clearReconnect();
  stopPending();
  authed = true;
  didStream = true;
  reconnectAttempt = 0;
  lastByeReason = null;
  server = message.server || null;
  displays = normalizeDisplays(message.displays);
  paused = !!message.paused;
  controlAllowed = message.control === true;
  viewerQualityAllowed = message.viewer_quality !== false;
  controlMode = controlAllowed ? preferredControlMode : 'view';
  if (settingsPanel) {
    settingsPanel.setQualityAllowed(viewerQualityAllowed);
  }
  autoLevel = 'standard';
  autoQuality.reset(performance.now());
  const auth = authAttempt || {};

  if (message.device_token && auth.remember !== false) {
    const existing = readDevice();
    const id = message.device_id || (existing ? existing.device_id : null);
    if (id) {
      saveDevice(id, message.device_token);
    }
  }
  if (auth.method !== 'device' && auth.remember === false) {
    store.remove(K_DEVICE);
  }

  currentDisplay = pickInitialDisplay();
  currentCodec = pickInitialCodec();
  rateUntil = 0;
  clearRateTicker();
  els.pairSubmit.disabled = false;
  setPairError('');
  renderDisplays();
  updateModeLabel();
  updateFitLabel();
  if (zoom) {
    zoom.reset();
  }
  setPhase(paused ? 'paused' : 'streaming');
  if (!paused && !toolbarHintShown) {
    toolbarHintShown = true;
    toast(t('streaming.tapHint'));
  }
  resetMetrics();
  lastKeyframeRequest = 0;
  startHeartbeat();
  acquireWakeLock();
  sendWatch();
  updateStatsPanel();
  updateControlUi();
  renderControlMenu();
  renderKeysMenu();
  if (sender) {
    sender.setEnabled(inputAllowed());
  }
}

function handleAuthFail(message) {
  clearAuthTimer();
  stopPending();
  const reason = typeof message.reason === 'string' ? message.reason : '';
  if (reason === 'unknown_device') {
    store.remove(K_DEVICE);
  }
  if (reason === 'server_full') {
    endSession('serverFull');
    return;
  }
  if (reason === 'rate_limited') {
    const retry = Number(message.retry_after_s);
    showPair(null, { rateLimit: retry > 0 ? retry : 300 });
    return;
  }
  showPair(authFailReasonKey(reason));
}

function handleControlMessage(message) {
  const allowed = message && message.allowed === true;
  if (allowed === controlAllowed) {
    return;
  }
  applyControlPermission(allowed, allowed ? 'granted' : 'revoked');
}

function handleBye(message) {
  const reason = typeof message.reason === 'string' ? message.reason : '';
  lastByeReason = reason || 'bye';
  if (reason === 'revoked') {
    store.remove(K_DEVICE);
    endSession('revoked', { forget: true });
  } else if (reason === 'kicked') {
    endSession('kicked');
  } else if (reason === 'shutdown') {
    endSession('shutdown');
  } else {
    endSession('disconnected');
  }
}

function handleServerError(message) {
  if (message.code === 'control_denied') {
    handleControlDenied();
    return;
  }
  if (message.code === 'bad_display') {
    if (displays.length > 0) {
      currentDisplay = displays[0].index;
      store.set(K_DISPLAY, String(currentDisplay));
      renderDisplays();
    }
    toast(t('toast.badDisplay'));
    if (authed) {
      sendWatch();
    }
    return;
  }
  let detail = '';
  if (typeof message.message === 'string' && message.message.trim()) {
    detail = message.message.trim().slice(0, 160);
  }
  toast(detail || t('toast.serverError'));
}

/* ---------- reconnect ---------- */

function clearReconnect() {
  if (reconnectTimer) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = 0;
  }
  if (reconnectTicker) {
    window.clearInterval(reconnectTicker);
    reconnectTicker = 0;
  }
  reconnectDeadline = 0;
}

function scheduleReconnect() {
  if (phase === 'ended' || phase === 'pair') {
    return;
  }
  if (!readDevice()) {
    showPair('pair.err.reconnect');
    return;
  }
  stopHeartbeat();
  disposePlayer();
  stream = null;
  streamEpoch = -1;
  playerReady = false;
  closeSocket();
  setPhase('reconnecting');
  reconnectAttempt += 1;
  const index = Math.min(reconnectAttempt - 1, RECONNECT_DELAYS.length - 1);
  const delay = RECONNECT_DELAYS[index];
  reconnectDeadline = Date.now() + delay * 1000;
  updateReconnectOverlay();
  if (reconnectTicker) {
    window.clearInterval(reconnectTicker);
  }
  reconnectTicker = window.setInterval(updateReconnectOverlay, 250);
  if (reconnectTimer) {
    window.clearTimeout(reconnectTimer);
  }
  reconnectTimer = window.setTimeout(function () {
    reconnectTimer = 0;
    attemptReconnect();
  }, delay * 1000);
}

function updateReconnectOverlay() {
  if (phase !== 'reconnecting') {
    return;
  }
  const seconds = Math.max(0, Math.ceil((reconnectDeadline - Date.now()) / 1000));
  els.reconnectCountdown.textContent = t('overlay.retryIn', { seconds: seconds });
}

function attemptReconnect() {
  if (phase !== 'reconnecting') {
    return;
  }
  clearReconnect();
  const device = readDevice();
  if (!device) {
    showPair('pair.err.reconnect');
    return;
  }
  els.reconnectCountdown.textContent = t('overlay.reconnectHint');
  startAuth({
    method: 'device',
    device_id: device.device_id,
    token: device.token
  }, { reconnect: true });
}

function retryNow() {
  if (phase !== 'reconnecting') {
    return;
  }
  clearReconnect();
  attemptReconnect();
}

/* ---------- displays / stream ---------- */

function normalizeDisplays(list) {
  const result = [];
  if (!Array.isArray(list)) {
    return result;
  }
  for (let i = 0; i < list.length; i += 1) {
    const item = list[i];
    if (!item) {
      continue;
    }
    const index = Number(item.index);
    if (!Number.isFinite(index)) {
      continue;
    }
    result.push({
      index: index,
      name: typeof item.name === 'string' ? item.name : '',
      width: Number(item.width) || 0,
      height: Number(item.height) || 0,
      primary: !!item.primary
    });
  }
  return result;
}

function handleDisplays(message) {
  displays = normalizeDisplays(message.displays);
  let changed = false;
  let known = false;
  for (let i = 0; i < displays.length; i += 1) {
    if (displays[i].index === currentDisplay) {
      known = true;
      break;
    }
  }
  if (!known) {
    currentDisplay = displays.length > 0 ? displays[0].index : 0;
    store.set(K_DISPLAY, String(currentDisplay));
    changed = true;
  }
  renderDisplays();
  if (changed && authed && (phase === 'streaming' || phase === 'paused')) {
    sendWatch();
  }
}

function handlePaused(message) {
  if (!authed || phase === 'ended' || phase === 'pair') {
    return;
  }
  paused = !!message.paused;
  if (paused) {
    setPhase('paused');
  } else {
    setPhase('streaming');
    setToolbarVisible(true);
  }
}

function handleCursor(message) {
  if (extremeMode) {
    return;
  }
  const x = Number(message.x);
  const y = Number(message.y);
  if (Number.isFinite(x)) {
    cursorState.x = clamp01(x);
  }
  if (Number.isFinite(y)) {
    cursorState.y = clamp01(y);
  }
  cursorState.visible = message.visible !== false;
  if (typeof message.shape === 'string' && message.shape) {
    cursorState.shape = message.shape;
  }
  if (cursorState.shape !== cursorShape) {
    cursorShape = cursorState.shape;
    els.cursor.innerHTML = cursorIcon(cursorShape);
  }
  els.cursor.hidden = !cursorState.visible;
  els.stage.classList.toggle('remote-cursor', cursorState.visible);
  positionCursor();
  if (cursorState.visible && controlMode !== 'view' && zoom && zoom.scale > 1.0001) {
    zoom.ensureVisible(cursorState.x, cursorState.y, 48);
  }
}

function setupStream(message) {
  if (!message) {
    return;
  }
  const codec = message.codec;
  if (codec !== 'h264' && codec !== 'jpeg') {
    return;
  }
  const width = Math.round(Number(message.width));
  const height = Math.round(Number(message.height));
  if (!(width > 0) || !(height > 0)) {
    return;
  }

  stream = {
    display: Number(message.display) || 0,
    codec: codec,
    width: width,
    height: height,
    fps: Number(message.fps) || 0,
    codec_string: typeof message.codec_string === 'string' ? message.codec_string : '',
    description: typeof message.description === 'string' ? message.description : '',
    quality: message.quality && typeof message.quality === 'object' ? message.quality : null
  };
  streamEpoch = (Number(message.epoch) || 0) & 0xffff;
  currentCodec = codec;

  if (codec === 'h264' && !supportsH264()) {
    fallbackToJpeg(true);
    return;
  }

  updateModeLabel();
  if (settingsPanel) {
    settingsPanel.updateQualityActual(stream.quality);
  }
  if (qualityPrefs.preset === 'auto' && stream.quality &&
      AUTO_LEVELS.indexOf(stream.quality.preset) >= 0) {
    autoLevel = stream.quality.preset;
    autoQuality.setLevel(autoLevel, performance.now());
  }
  if (els.canvas.width !== width) {
    els.canvas.width = width;
  }
  if (els.canvas.height !== height) {
    els.canvas.height = height;
  }

  cursorState.visible = false;
  els.cursor.hidden = true;
  els.stage.classList.remove('remote-cursor');

  applyFit();
  disposePlayer();
  resetMetrics();
  if (zoom) {
    zoom.reset();
  }

  const generation = playerGen;
  let localPlayer = null;
  const handlers = {
    onRendered: function (info) {
      if (player !== localPlayer) {
        return;
      }
      recordRendered(info);
    },
    onNeedKeyframe: function () {
      requestKeyframe();
    },
    onFatal: function () {
      if (player !== localPlayer) {
        return;
      }
      fallbackToJpeg(true);
    }
  };

  try {
    localPlayer = createPlayer(codec, els.canvas, handlers);
  } catch (error) {
    if (codec === 'h264') {
      toast(t('toast.streamError'));
      fallbackToJpeg(false);
    } else {
      toast(t('toast.serverError'));
    }
    return;
  }
  player = localPlayer;

  Promise.resolve().then(async function () {
    let ok = false;
    try {
      ok = await localPlayer.configure(message);
    } catch (error) {
      ok = false;
    }
    if (generation !== playerGen || player !== localPlayer) {
      try {
        localPlayer.close();
      } catch (error) {
        /* ignore */
      }
      return;
    }
    if (!ok) {
      playerReady = false;
      if (codec === 'h264') {
        toast(t('toast.streamError'));
        fallbackToJpeg(false);
      } else {
        try {
          localPlayer.close();
        } catch (error) {
          /* ignore */
        }
        if (player === localPlayer) {
          player = null;
        }
        toast(t('toast.serverError'));
      }
      return;
    }
    playerReady = true;
    if (earlyKeyframe) {
      const frame = earlyKeyframe;
      earlyKeyframe = null;
      if (frame.epoch === streamEpoch && player === localPlayer) {
        localPlayer.push(frame);
      }
    }
    updateStatsPanel();
  });
}

function fallbackToJpeg(showToast) {
  if (currentCodec === 'jpeg') {
    return;
  }
  const now = Date.now();
  const repeated = now - lastFallbackAt < 4000;
  lastFallbackAt = now;
  currentCodec = 'jpeg';
  store.set(K_CODEC, 'jpeg');
  updateModeLabel();
  if (showToast && !repeated) {
    toast(t('toast.h264Unsupported'));
  }
  sendWatch();
}

function sendWatch() {
  if (!authed) {
    return;
  }
  releaseAllInput(true);
  disposePlayer();
  stream = null;
  streamEpoch = -1;
  resetMetrics();
  const quality = buildWatchQuality(qualityPrefs, currentCodec, currentDisplayInfo(), autoLevel);
  send({
    type: 'watch',
    display: currentDisplay,
    codec: currentCodec,
    quality: quality,
    extreme: extremeMode
  });
}

function requestKeyframe() {
  const now = performance.now();
  if (now - lastKeyframeRequest < 1000) {
    return;
  }
  lastKeyframeRequest = now;
  send({ type: 'keyframe' });
}

function disposePlayer() {
  playerGen += 1;
  playerReady = false;
  earlyKeyframe = null;
  if (player) {
    const old = player;
    player = null;
    droppedTotal += Number(old.dropped) || 0;
    try {
      old.close();
    } catch (error) {
      /* ignore */
    }
  }
}

function handleBinary(buffer) {
  const now = performance.now();
  const size = buffer.byteLength || 0;
  netEvents.push({ t: now, bytes: size });
  if (netEvents.length > 600) {
    netEvents.splice(0, netEvents.length - 300);
  }
  if (!stream || !player) {
    return;
  }
  let frame = null;
  try {
    frame = parseFrameHeader(buffer);
  } catch (error) {
    return;
  }
  if (frame.epoch !== streamEpoch) {
    return;
  }
  if (stream.codec === 'h264') {
    if (frame.kind !== 1) {
      return;
    }
  } else if (stream.codec === 'jpeg') {
    if (frame.kind !== 2) {
      return;
    }
  } else {
    return;
  }

  if (!playerReady) {
    if (frame.key) {
      earlyKeyframe = frame;
    }
    return;
  }
  player.push(frame);
}

function recordRendered(info) {
  if (!info) {
    return;
  }
  const now = performance.now();
  renderTimes.push(now);
  if (renderTimes.length > 400) {
    renderTimes.splice(0, renderTimes.length - 300);
  }
  if (Number.isFinite(info.tsUs) && estimator.hasSample) {
    const latency = estimator.latencyMs(info.tsUs, now);
    if (latency != null && latency >= 0 && latency < 120000) {
      latencySamples.push(latency);
      if (latencySamples.length > 30) {
        latencySamples.shift();
      }
      autoLatency.push({ t: now, ms: latency });
      while (autoLatency.length > 0 && autoLatency[0].t < now - 4000) {
        autoLatency.shift();
      }
    }
  }
  if (player && Number.isFinite(info.decodeMs)) {
    player.decodeMs = info.decodeMs;
  }
}

/* ---------- metrics / heartbeat ---------- */

function resetMetrics() {
  renderTimes = [];
  netEvents = [];
  latencySamples = [];
  autoLatency = [];
}

function measureWindow() {
  const now = performance.now();
  pruneNumbers(renderTimes, now - 2000);
  pruneEvents(netEvents, now - 2000);
  let frames = 0;
  for (let i = 0; i < renderTimes.length; i += 1) {
    if (renderTimes[i] >= now - 1000) {
      frames += 1;
    }
  }
  let bytes = 0;
  for (let i = 0; i < netEvents.length; i += 1) {
    if (netEvents[i].t >= now - 1000) {
      bytes += netEvents[i].bytes;
    }
  }
  return {
    fps: Math.round(frames * 10) / 10,
    kbps: Math.round(bytes * 8 / 1000)
  };
}

function startHeartbeat() {
  stopHeartbeat();
  sendPing();
  pingTimer = window.setInterval(sendPing, PING_INTERVAL_MS);
  statsSendTimer = window.setInterval(sendStats, STATS_INTERVAL_MS);
  statsUiTimer = window.setInterval(updateStatsPanel, 500);
  autoTimer = window.setInterval(evaluateAutoQuality, 2000);
}

function stopHeartbeat() {
  if (pingTimer) {
    window.clearInterval(pingTimer);
    pingTimer = 0;
  }
  if (statsSendTimer) {
    window.clearInterval(statsSendTimer);
    statsSendTimer = 0;
  }
  if (statsUiTimer) {
    window.clearInterval(statsUiTimer);
    statsUiTimer = 0;
  }
  if (autoTimer) {
    window.clearInterval(autoTimer);
    autoTimer = 0;
  }
}

function sendPing() {
  if (!authed || !ws || ws.readyState !== WebSocket.OPEN) {
    return;
  }
  const sentAt = performance.now();
  lastPingT = sentAt;
  pingSends.set(sentAt, sentAt);
  if (pingSends.size > 8) {
    const oldest = pingSends.keys().next().value;
    pingSends.delete(oldest);
  }
  send({ type: 'ping', t: sentAt });
}

function handlePong(message) {
  if (!Number.isFinite(Number(message.server_us))) {
    return;
  }
  let sentAt = pingSends.get(message.t);
  if (sentAt == null) {
    sentAt = lastPingT;
  }
  if (sentAt != null) {
    estimator.addSample(sentAt, performance.now(), Number(message.server_us));
    pingSends.delete(message.t);
  }
}

function sendStats() {
  if (!authed || !ws || ws.readyState !== WebSocket.OPEN) {
    return;
  }
  const sample = measureWindow();
  const payload = {
    type: 'stats',
    fps: sample.fps,
    kbps: sample.kbps,
    dropped: droppedTotal + (player ? player.dropped : 0)
  };
  const rtt = estimator.rttMs;
  const latency = median(latencySamples);
  if (rtt != null) {
    payload.rtt_ms = Math.round(rtt * 10) / 10;
  }
  if (latency != null) {
    payload.latency_ms = Math.round(latency);
  }
  if (player && Number.isFinite(player.decodeMs) && player.decodeMs > 0) {
    payload.decode_ms = Math.round(player.decodeMs * 10) / 10;
  }
  send(payload);
}

function updateStatsPanel() {
  if (!statsVisible) {
    return;
  }
  const sample = measureWindow();
  if (stream) {
    els.statResolution.textContent = stream.width + '×' + stream.height;
  } else {
    els.statResolution.textContent = '—';
  }
  els.statMode.textContent = t('streaming.codec.' + (currentCodec === 'h264' ? 'h264' : 'jpeg'));
  if (els.statQuality) {
    els.statQuality.textContent = stream && stream.quality
      ? describeStreamQuality(stream.quality) : '—';
  }
  if (els.statControl) {
    els.statControl.textContent = controlAllowed
      ? t('control.mode.' + controlMode) : t('control.mode.view');
  }
  if (els.statInput) {
    els.statInput.textContent = sender
      ? sender.ratePerSecond(performance.now()) + ' ' + t('units.eventsPerSecond') : '—';
  }
  els.statFps.textContent = sample.fps + ' ' + t('units.fps');
  els.statBitrate.textContent = sample.kbps + ' ' + t('units.kbps');
  const rtt = estimator.rttMs;
  els.statRtt.textContent = rtt == null ? '—' : rtt.toFixed(1) + ' ' + t('units.ms');
  const latency = median(latencySamples);
  els.statLatency.textContent = latency == null ? '—' : Math.round(latency) + ' ' + t('units.ms');
  els.statDropped.textContent = String(droppedTotal + (player ? player.dropped : 0));
  els.statQueue.textContent = player ? String(player.decodeQueueSize) : '—';
}

/* ---------- pending / rate limit / pair ---------- */

function startPendingCountdown(seconds) {
  stopPending();
  const total = Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds, 600) : 60;
  pendingDeadline = Date.now() + total * 1000;
  updatePendingCountdown();
  pendingTimer = window.setInterval(updatePendingCountdown, 250);
}

function updatePendingCountdown() {
  const left = Math.max(0, Math.ceil((pendingDeadline - Date.now()) / 1000));
  els.pendingSeconds.textContent = String(left);
}

function stopPending() {
  if (pendingTimer) {
    window.clearInterval(pendingTimer);
    pendingTimer = 0;
  }
  pendingDeadline = 0;
}

function clearRateTicker() {
  if (rateTimer) {
    window.clearInterval(rateTimer);
    rateTimer = 0;
  }
}

function startRateTicker() {
  clearRateTicker();
  const tick = function () {
    const left = Math.max(0, Math.ceil((rateUntil - Date.now()) / 1000));
    if (left <= 0) {
      rateUntil = 0;
      clearRateTicker();
      els.pairSubmit.disabled = false;
      setPairError('');
      return;
    }
    els.pairSubmit.disabled = true;
    setPairError(t('errors.auth.rateLimited', { seconds: left }));
  };
  tick();
  if (rateUntil > Date.now()) {
    rateTimer = window.setInterval(tick, 250);
  }
}

function showPair(errorKey, options) {
  const opts = options || {};
  resetControlSession();
  clearReconnect();
  stopPending();
  stopHeartbeat();
  disposePlayer();
  stream = null;
  streamEpoch = -1;
  playerReady = false;
  paused = false;
  authed = false;
  didStream = false;
  lastByeReason = null;
  authAttempt = null;
  reconnectAttempt = 0;
  server = null;
  displays = [];
  releaseWakeLock();
  closeSocket();
  setPhase('pair');

  els.pin.value = '';
  els.name.value = storedName();
  els.remember.checked = true;
  setPairError('');
  els.pairSubmit.disabled = false;
  if (els.pair) {
    els.pair.scrollTop = 0;
  }
  renderDisplays();
  hideCursor();
  if (zoom) {
    zoom.reset();
  }

  if (opts.rateLimit) {
    const seconds = Number(opts.rateLimit);
    rateUntil = Date.now() + (Number.isFinite(seconds) && seconds > 0 ? seconds : 300) * 1000;
  }
  if (rateUntil > Date.now()) {
    startRateTicker();
  } else {
    rateUntil = 0;
    clearRateTicker();
    if (errorKey) {
      setPairError(t(errorKey, opts.vars));
    }
  }
}

function endSession(reason, options) {
  const opts = options || {};
  resetControlSession();
  if (opts.forget || reason === 'revoked') {
    store.remove(K_DEVICE);
  }
  clearReconnect();
  stopPending();
  stopHeartbeat();
  disposePlayer();
  stream = null;
  streamEpoch = -1;
  playerReady = false;
  authed = false;
  didStream = false;
  paused = false;
  lastByeReason = null;
  reconnectAttempt = 0;
  rateUntil = 0;
  clearRateTicker();
  els.pairSubmit.disabled = false;
  releaseWakeLock();
  hideCursor();
  closeSocket();
  setPhase('ended');
  const key = 'ended.' + reason;
  const text = t(key);
  els.endedReason.textContent = text === key ? t('ended.connectionLost') : text;
  els.endedServer.textContent = server && server.name ? server.name : '';
}

function submitPair() {
  if (phase !== 'pair' || rateUntil > Date.now()) {
    return;
  }
  const pin = String(els.pin.value || '').replace(/\D/g, '').slice(0, 6);
  const name = String(els.name.value || '').trim().slice(0, 40);
  if (pin.length !== 6) {
    setPairError(t('pair.err.pin'));
    return;
  }
  if (!name) {
    setPairError(t('pair.err.name'));
    return;
  }
  saveName(name);
  setPairError('');
  startAuth({
    method: 'pin',
    pin: pin,
    name: name,
    remember: !!els.remember.checked
  });
}

function onPinInput() {
  const value = String(els.pin.value || '').replace(/\D/g, '').slice(0, 6);
  if (els.pin.value !== value) {
    els.pin.value = value;
  }
  if (value.length === 6 && phase === 'pair') {
    submitPair();
  }
}

/* ---------- layout / cursor / zoom ---------- */

function computeFit(width, height, viewportWidth, viewportHeight, mode) {
  const w = Math.max(1, width);
  const h = Math.max(1, height);
  const vw = Math.max(1, viewportWidth);
  const vh = Math.max(1, viewportHeight);
  const scale = mode === 'cover' ? Math.max(vw / w, vh / h) : Math.min(vw / w, vh / h);
  const fittedWidth = w * scale;
  const fittedHeight = h * scale;
  return {
    x: (vw - fittedWidth) / 2,
    y: (vh - fittedHeight) / 2,
    w: fittedWidth,
    h: fittedHeight
  };
}

function applyFit() {
  if (!els.stage) {
    return;
  }
  const vw = els.stage.clientWidth || window.innerWidth || 1;
  const vh = els.stage.clientHeight || window.innerHeight || 1;
  const width = stream ? stream.width : (els.canvas.width || vw);
  const height = stream ? stream.height : (els.canvas.height || vh);
  currentFit = computeFit(width, height, vw, vh, fitMode);
  els.canvas.style.left = currentFit.x + 'px';
  els.canvas.style.top = currentFit.y + 'px';
  els.canvas.style.width = currentFit.w + 'px';
  els.canvas.style.height = currentFit.h + 'px';
  if (zoom) {
    zoom.refresh();
  }
  positionCursor();
}

function scheduleLayout() {
  if (layoutRaf) {
    return;
  }
  layoutRaf = window.requestAnimationFrame(function () {
    layoutRaf = 0;
    applyFit();
    if (keyLayer && keyLayer.getVisible()) {
      keyLayer.refreshPositions();
    }
  });
}

function positionCursor() {
  if (!cursorState.visible || !els.cursor) {
    return;
  }
  const x = currentFit.x + cursorState.x * currentFit.w;
  const y = currentFit.y + cursorState.y * currentFit.h;
  els.cursor.style.left = x.toFixed(2) + 'px';
  els.cursor.style.top = y.toFixed(2) + 'px';
}

function hideCursor() {
  cursorState.visible = false;
  if (els.cursor) {
    els.cursor.hidden = true;
  }
  if (els.stage) {
    els.stage.classList.remove('remote-cursor');
  }
}

function createZoom() {
  zoom = new ZoomPan({
    viewport: els.stage,
    target: els.zoomLayer,
    getContentRect: function () {
      return currentFit;
    },
    onTap: function () {
      if (phase === 'streaming') {
        toggleToolbar();
      }
    }
  });
}

/* ---------- remote control / input / quality ---------- */

function currentDisplayInfo() {
  for (let i = 0; i < displays.length; i += 1) {
    if (displays[i].index === currentDisplay) {
      return displays[i];
    }
  }
  return null;
}

function inputAllowed() {
  return controlAllowed && phase === 'streaming' && !editingLayout;
}

function controlContextActive() {
  return controlAllowed && phase === 'streaming' && !editingLayout &&
    controlMode !== 'view' && !(settingsPanel && settingsPanel.isOpen());
}

function saveInputSettings() {
  writeStoredJSON(K_INPUT, inputSettings);
}

function saveQualityPrefs() {
  writeStoredJSON(K_QUALITY, qualityPrefs);
}

function saveGamepadMapping() {
  writeStoredJSON(K_GAMEPAD, gamepadMapping);
}

/** Send every held key/button up (reverse order) and reset local controllers. */
function releaseAllInput(send) {
  if (send !== false && sender) {
    sender.releaseAll(true);
    sender.flushNow();
  } else if (sender) {
    sender.queue.length = 0;
    sender.clearHeld();
  }
  if (pointerControl) {
    pointerControl.reset(false);
  }
  if (keyLayer) {
    keyLayer.clearPressed(false);
  }
  if (gamepadController) {
    try {
      gamepadController.reset(false);
    } catch (error) {
      /* ignore */
    }
  }
}

function updateControlUi() {
  const available = controlAllowed && phase === 'streaming';
  const buttons = [els.ctrlBtn, els.keysBtn, els.kbdBtn];
  for (let i = 0; i < buttons.length; i += 1) {
    const button = buttons[i];
    if (!button) {
      continue;
    }
    button.classList.toggle('is-disabled', !available);
    button.setAttribute('aria-disabled', available ? 'false' : 'true');
  }
  if (els.ctrlLabel) {
    els.ctrlLabel.textContent = t('control.mode.' + controlMode);
  }
  if (els.keysBtn && keyLayer) {
    const visible = keyLayer.getVisible();
    els.keysBtn.classList.toggle('is-active', visible);
    els.keysBtn.setAttribute('aria-pressed', visible ? 'true' : 'false');
  }
  if (els.kbdBtn) {
    els.kbdBtn.classList.toggle('is-active', keyboardOpen);
    els.kbdBtn.setAttribute('aria-pressed', keyboardOpen ? 'true' : 'false');
  }
  if (pointerControl) {
    pointerControl.setMode(controlMode);
  }
  if (zoom) {
    zoom.setEnabled(controlMode === 'view' && !editingLayout &&
      !(settingsPanel && settingsPanel.isOpen()));
  }
}

function applyControlPermission(allowed, reason) {
  const next = !!allowed;
  const previous = controlAllowed;
  if (next === previous) {
    updateControlUi();
    return;
  }
  controlAllowed = next;
  if (!next) {
    releaseAllInput(true);
    if (controlMode !== 'view') {
      applyControlMode('view', false);
    }
    setKeyLayerVisible(false);
    if (layoutEditor && layoutEditor.opened) {
      layoutEditor.close();
    }
    if (textInput) {
      textInput.blur();
    }
    if (reason === 'revoked') {
      toast(t('control.revoked'));
    } else if (reason === 'failed') {
      toast(t('control.failed'));
    } else if (reason !== 'initial' && reason !== 'silent') {
      toast(t('control.deniedToast'));
    }
  } else if (reason === 'granted') {
    toast(t('control.granted'));
  }
  updateControlUi();
  if (sender) {
    sender.setEnabled(inputAllowed());
  }
  updateStatsPanel();
}

function handleControlDenied() {
  const now = Date.now();
  if (now - lastControlDeniedAt > 4000) {
    lastControlDeniedAt = now;
    toast(t('control.failed'));
  }
  if (controlAllowed) {
    applyControlPermission(false, 'failed');
  }
}

function applyControlMode(mode, persist) {
  const next = CONTROL_MODES.indexOf(mode) >= 0 ? mode : 'view';
  if (next !== 'view' && !controlAllowed) {
    toast(t('control.deniedHint'));
    return;
  }
  if (next === controlMode) {
    updateControlUi();
    return;
  }
  releaseAllInput(true);
  controlMode = next;
  if (persist) {
    preferredControlMode = next;
    store.set(K_CTRL, next);
  }
  if (pointerControl) {
    pointerControl.setMode(next);
  }
  if (zoom) {
    zoom.setEnabled(next === 'view' && !(settingsPanel && settingsPanel.isOpen()));
  }
  updateControlUi();
  renderControlMenu();
}

function renderControlMenu() {
  if (!els.ctrlMenu) {
    return;
  }
  els.ctrlMenu.innerHTML = '';
  const options = [
    { mode: 'view', key: 'control.mode.view' },
    { mode: 'touchpad', key: 'control.mode.touchpad' },
    { mode: 'direct', key: 'control.mode.direct' }
  ];
  for (let i = 0; i < options.length; i += 1) {
    const option = options[i];
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('role', 'menuitemradio');
    button.setAttribute('aria-checked', option.mode === controlMode ? 'true' : 'false');
    if (option.mode === controlMode) {
      button.className = 'is-selected';
    }
    if (!controlAllowed && option.mode !== 'view') {
      button.setAttribute('aria-disabled', 'true');
    }
    const check = document.createElement('span');
    check.className = 'icon menu-check';
    check.innerHTML = icon('check');
    const text = document.createElement('span');
    text.className = 'menu-text';
    const title = document.createElement('span');
    title.className = 'menu-title';
    title.textContent = t(option.key);
    text.appendChild(title);
    button.appendChild(check);
    button.appendChild(text);
    button.addEventListener('click', function () {
      if (option.mode !== 'view' && !controlAllowed) {
        toast(t('control.deniedHint'));
        closeMenus();
        return;
      }
      closeMenus();
      applyControlMode(option.mode, true);
      setToolbarVisible(true);
    });
    els.ctrlMenu.appendChild(button);
  }
}

function renderKeysMenu() {
  if (!els.keysMenu || !layoutStore) {
    return;
  }
  els.keysMenu.innerHTML = '';
  const layouts = layoutStore.getLayouts();
  if (layouts.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'menu-empty';
    empty.textContent = t('keys.none');
    els.keysMenu.appendChild(empty);
  }
  for (let i = 0; i < layouts.length; i += 1) {
    const layout = layouts[i];
    const selected = layout.name === layoutStore.getCurrentName();
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('role', 'menuitemradio');
    button.setAttribute('aria-checked', selected ? 'true' : 'false');
    if (selected) {
      button.className = 'is-selected';
    }
    const check = document.createElement('span');
    check.className = 'icon menu-check';
    check.innerHTML = icon('check');
    const text = document.createElement('span');
    text.className = 'menu-text';
    const title = document.createElement('span');
    title.className = 'menu-title';
    title.textContent = layout.name;
    text.appendChild(title);
    button.appendChild(check);
    button.appendChild(text);
    button.addEventListener('click', function () {
      if (!controlAllowed) {
        toast(t('control.deniedHint'));
        closeMenus();
        return;
      }
      if (layoutStore.select(layout.name)) {
        keyLayer.render();
        setKeyLayerVisible(true);
      }
      closeMenus();
      setToolbarVisible(true);
    });
    els.keysMenu.appendChild(button);
  }
  const edit = document.createElement('button');
  edit.type = 'button';
  edit.setAttribute('role', 'menuitem');
  edit.className = 'menu-action';
  const editIcon = document.createElement('span');
  editIcon.className = 'icon';
  editIcon.innerHTML = icon('sliders');
  const editText = document.createElement('span');
  editText.className = 'menu-text';
  const editTitle = document.createElement('span');
  editTitle.className = 'menu-item-title';
  editTitle.textContent = t('keys.edit');
  editText.appendChild(editTitle);
  edit.appendChild(editIcon);
  edit.appendChild(editText);
  edit.addEventListener('click', function () {
    if (!controlAllowed) {
      toast(t('control.deniedHint'));
      closeMenus();
      return;
    }
    closeMenus();
    if (layoutEditor) {
      layoutEditor.open();
    }
    setToolbarVisible(true);
  });
  els.keysMenu.appendChild(edit);
}

function setKeyLayerVisible(visible) {
  if (!keyLayer) {
    return;
  }
  if (visible) {
    keyLayer.setVisible(true);
    keyLayer.render();
  } else {
    keyLayer.setVisible(false);
  }
  if (els.keysBtn) {
    els.keysBtn.classList.toggle('is-active', !!visible);
    els.keysBtn.setAttribute('aria-pressed', visible ? 'true' : 'false');
  }
}

function toggleKeyLayer() {
  if (!controlAllowed) {
    toast(t('control.deniedHint'));
    return;
  }
  if (!keyLayer) {
    return;
  }
  setKeyLayerVisible(!keyLayer.getVisible());
  setToolbarVisible(true);
}

function setKeyboardOpen(open) {
  keyboardOpen = !!open;
  if (els.kbdBtn) {
    els.kbdBtn.classList.toggle('is-active', keyboardOpen);
    els.kbdBtn.setAttribute('aria-pressed', keyboardOpen ? 'true' : 'false');
  }
  if (els.keyboardClose) {
    els.keyboardClose.hidden = !keyboardOpen;
  }
  if (keyboardOpen) {
    setToolbarVisible(true);
  }
}

function closeKeyboard() {
  if (textInput) {
    textInput.blur();
  }
}

function openSettingsSheet() {
  if (!settingsPanel) {
    return;
  }
  closeKeyboard();
  if (settingsPanel.isOpen()) {
    settingsPanel.close();
  } else {
    settingsPanel.open();
  }
  setToolbarVisible(true);
}

function scheduleQualityWatch() {
  if (!authed || !viewerQualityAllowed) {
    return;
  }
  if (phase !== 'streaming' && phase !== 'paused') {
    return;
  }
  if (qualityTimer) {
    window.clearTimeout(qualityTimer);
  }
  qualityTimer = window.setTimeout(function () {
    qualityTimer = 0;
    if (authed && (phase === 'streaming' || phase === 'paused')) {
      sendWatch();
    }
  }, 150);
}

function evaluateAutoQuality() {
  if (!authed || phase !== 'streaming' || !viewerQualityAllowed) {
    return;
  }
  if (qualityPrefs.preset !== 'auto') {
    return;
  }
  const now = performance.now();
  while (autoLatency.length > 0 && autoLatency[0].t < now - 4000) {
    autoLatency.shift();
  }
  const samples = [];
  for (let i = 0; i < autoLatency.length; i += 1) {
    samples.push(autoLatency[i].ms);
  }
  const result = autoQuality.tick(now, {
    dropped: droppedTotal + (player ? player.dropped : 0),
    latencyMs: samples.length > 0 ? median(samples) : NaN,
    decodeQueue: player ? player.decodeQueueSize : 0
  });
  if (result.changed && AUTO_LEVELS.indexOf(result.level) >= 0) {
    autoLevel = result.level;
    sendWatch();
  }
}

function keyboardCanForward() {
  return controlAllowed && phase === 'streaming' && !editingLayout &&
    controlMode !== 'view' && !(settingsPanel && settingsPanel.isOpen()) &&
    !(layoutEditor && layoutEditor.opened) && !anyMenuOpen();
}

function createControlStack() {
  sender = new InputSender({
    send: function (message) {
      return send(message);
    },
    isEnabled: inputAllowed
  });
  sender.setEnabled(false);

  layoutStore = new LayoutStore({
    storage: (function () {
      try {
        return window.localStorage;
      } catch (error) {
        return null;
      }
    })(),
    language: getLang() === 'en' ? 'en' : 'zh'
  });

  pointerControl = new PointerControl({
    stage: els.stage,
    sender: sender,
    zoom: zoom,
    getMode: function () {
      return controlMode;
    },
    isActive: controlContextActive,
    getSettings: function () {
      return inputSettings;
    },
    getDisplayWidth: function () {
      const display = currentDisplayInfo();
      return display ? display.width : 0;
    },
    getFit: function () {
      return currentFit;
    },
    getDisplayedWidth: function () {
      const scale = zoom ? zoom.scale : 1;
      return Math.max(1, currentFit.w * scale);
    },
    onTap: function () {
      if (phase === 'streaming') {
        setToolbarVisible(true);
      }
    }
  });
  pointerControl.bind();

  keyLayer = new KeyLayer({
    root: els.keyLayer,
    sender: sender,
    store: layoutStore,
    getSettings: function () {
      return inputSettings;
    },
    isPlaying: inputAllowed,
    isEditing: function () {
      return editingLayout;
    },
    onEditItem: function (id) {
      if (layoutEditor && layoutEditor.opened) {
        layoutEditor.openItem(id);
      }
    },
    onChanged: function () {
      if (layoutStore) {
        layoutStore.commitCurrent();
      }
    }
  });
  keyLayer.render();
  keyLayer.setVisible(false);

  layoutEditor = new LayoutEditor({
    store: layoutStore,
    keyLayer: keyLayer,
    onOpen: function () {
      editingLayout = true;
      closeKeyboard();
      releaseAllInput(true);
      if (sender) {
        sender.setEnabled(false);
      }
      updateControlUi();
      setToolbarVisible(true);
    },
    onClose: function () {
      editingLayout = false;
      if (sender) {
        sender.setEnabled(inputAllowed());
      }
      updateControlUi();
    }
  });

  settingsPanel = new SettingsPanel({
    getInput: function () {
      return inputSettings;
    },
    onInputChange: function (next) {
      inputSettings = normalizeInputSettings(next);
      saveInputSettings();
    },
    getQuality: function () {
      return qualityPrefs;
    },
    onQualityChange: function (next) {
      extremeMode = false;
      qualityPrefs = normalizeQualityPreferences(next);
      saveQualityPrefs();
      if (qualityPrefs.preset === 'auto') {
        autoLevel = 'standard';
        autoQuality.reset(performance.now());
      }
      scheduleQualityWatch();
    },
    onExtremeChange: function (enabled) {
      extremeMode = !!enabled;
      scheduleQualityWatch();
    },
    getGamepad: function () {
      return gamepadMapping;
    },
    onGamepadChange: function (next) {
      gamepadMapping = normalizeGamepadMapping(next);
      saveGamepadMapping();
    },
    getCodec: function () {
      return currentCodec;
    },
    getAutoLevel: function () {
      return autoLevel;
    },
    onOpen: function () {
      closeKeyboard();
      releaseAllInput(true);
      updateControlUi();
    },
    onClose: function () {
      updateControlUi();
    }
  });
  settingsPanel.setQualityAllowed(viewerQualityAllowed);

  gamepadController = new GamepadController({
    sender: sender,
    getMapping: function () {
      return gamepadMapping;
    },
    getSettings: function () {
      return inputSettings;
    },
    isActive: function () {
      return controlContextActive() && !anyMenuOpen();
    },
    onConnectionChange: function (connected) {
      if (settingsPanel) {
        settingsPanel.setGamepadConnected(connected);
      }
    }
  });
  gamepadController.start();

  keyboardForwarder = new KeyboardForwarder({
    sender: sender,
    canForward: keyboardCanForward
  });
  keyboardForwarder.bind();

  textInput = new TextInput({
    el: els.textInput,
    sender: sender,
    canSend: function () {
      return inputAllowed();
    },
    onFocusChange: function (focused) {
      setKeyboardOpen(focused);
    }
  });
  textInput.bind();

  renderControlMenu();
  renderKeysMenu();
  updateControlUi();
}

function resetControlSession() {
  releaseAllInput(true);
  controlAllowed = false;
  controlMode = 'view';
  editingLayout = false;
  if (pointerControl) {
    pointerControl.setMode('view');
  }
  setKeyLayerVisible(false);
  if (layoutEditor && layoutEditor.opened) {
    layoutEditor.close();
  }
  if (settingsPanel && settingsPanel.isOpen()) {
    settingsPanel.close();
  }
  if (textInput) {
    textInput.blur();
  }
  if (sender) {
    sender.setEnabled(false);
  }
  updateControlUi();
}

/* ---------- toolbar / menus ---------- */

function anyMenuOpen() {
  return !els.displayMenu.hidden || !els.codecMenu.hidden || !els.ctrlMenu.hidden ||
    !els.keysMenu.hidden || !els.moreMenu.hidden;
}

function closeMenus() {
  els.displayMenu.hidden = true;
  els.codecMenu.hidden = true;
  if (els.ctrlMenu) {
    els.ctrlMenu.hidden = true;
  }
  if (els.keysMenu) {
    els.keysMenu.hidden = true;
  }
  els.moreMenu.hidden = true;
  els.displayBtn.setAttribute('aria-expanded', 'false');
  els.codecBtn.setAttribute('aria-expanded', 'false');
  if (els.ctrlBtn) {
    els.ctrlBtn.setAttribute('aria-expanded', 'false');
  }
  if (els.keysMoreBtn) {
    els.keysMoreBtn.setAttribute('aria-expanded', 'false');
  }
  els.moreBtn.setAttribute('aria-expanded', 'false');
}

function toggleMenu(which) {
  closeKeyboard();
  let button = null;
  let menu = null;
  if (which === 'display') {
    button = els.displayBtn;
    menu = els.displayMenu;
  } else if (which === 'codec') {
    button = els.codecBtn;
    menu = els.codecMenu;
    renderCodecMenu();
  } else if (which === 'ctrl') {
    button = els.ctrlBtn;
    menu = els.ctrlMenu;
    renderControlMenu();
  } else if (which === 'keys') {
    button = els.keysMoreBtn;
    menu = els.keysMenu;
    renderKeysMenu();
  } else {
    button = els.moreBtn;
    menu = els.moreMenu;
  }
  if (!button || !menu) {
    return;
  }
  const opening = menu.hidden;
  closeMenus();
  if (opening) {
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    scheduleToolbarHide();
  }
}

function scheduleToolbarHide() {
  if (toolbarTimer) {
    window.clearTimeout(toolbarTimer);
    toolbarTimer = 0;
  }
  if (!toolbarVisible || phase !== 'streaming') {
    return;
  }
  toolbarTimer = window.setTimeout(function () {
    toolbarTimer = 0;
    if (!toolbarVisible) {
      return;
    }
    if (anyMenuOpen()) {
      scheduleToolbarHide();
      return;
    }
    if (keyboardOpen) {
      return;
    }
    if (phase === 'streaming') {
      setToolbarVisible(false);
    }
  }, TOOLBAR_HIDE_MS);
}

function setToolbarVisible(visible, autoHide) {
  toolbarVisible = !!visible;
  if (!els.toolbar) {
    return;
  }
  els.toolbar.classList.toggle('is-visible', toolbarVisible);
  if (toolbarVisible) {
    if (autoHide !== false) {
      scheduleToolbarHide();
    }
  } else {
    if (toolbarTimer) {
      window.clearTimeout(toolbarTimer);
      toolbarTimer = 0;
    }
    closeMenus();
  }
}

function toggleToolbar() {
  if (phase !== 'streaming') {
    return;
  }
  if (toolbarVisible) {
    setToolbarVisible(false);
  } else {
    setToolbarVisible(true);
  }
}

function renderDisplays() {
  const multiple = displays.length > 1;
  els.displayWrap.hidden = !multiple;
  if (!multiple) {
    els.displayMenu.hidden = true;
    els.displayBtn.setAttribute('aria-expanded', 'false');
    els.displayMenu.innerHTML = '';
    return;
  }
  let current = null;
  for (let i = 0; i < displays.length; i += 1) {
    if (displays[i].index === currentDisplay) {
      current = displays[i];
      break;
    }
  }
  if (!current) {
    current = displays[0];
  }
  const name = current.name || ('Display ' + (current.index + 1));
  els.displayLabel.textContent = name;
  els.displayLabel.title = name;
  els.displayBtn.setAttribute('aria-label', t('streaming.menuDisplay') + ': ' + name);
  renderDisplayMenu();
}

function renderDisplayMenu() {
  els.displayMenu.innerHTML = '';
  if (displays.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'menu-empty';
    empty.textContent = t('streaming.menuDisplay');
    els.displayMenu.appendChild(empty);
    return;
  }
  for (let i = 0; i < displays.length; i += 1) {
    const display = displays[i];
    const selected = display.index === currentDisplay;
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('role', 'menuitemradio');
    button.setAttribute('aria-checked', selected ? 'true' : 'false');
    if (selected) {
      button.className = 'is-selected';
    }
    const check = document.createElement('span');
    check.className = 'icon menu-check';
    check.innerHTML = icon('check');
    const text = document.createElement('span');
    text.className = 'menu-text';
    const title = document.createElement('span');
    title.className = 'menu-title';
    title.textContent = display.name || ('Display ' + (display.index + 1));
    const meta = document.createElement('span');
    meta.className = 'menu-meta';
    const bits = [];
    if (display.width > 0 && display.height > 0) {
      bits.push(display.width + '×' + display.height);
    }
    if (display.primary) {
      bits.push(t('display.primary'));
    }
    meta.textContent = bits.join(' · ');
    text.appendChild(title);
    if (bits.length > 0) {
      text.appendChild(meta);
    }
    button.appendChild(check);
    button.appendChild(text);
    button.addEventListener('click', function () {
      if (currentDisplay !== display.index) {
        currentDisplay = display.index;
        store.set(K_DISPLAY, String(currentDisplay));
        renderDisplays();
        closeMenus();
        sendWatch();
      } else {
        closeMenus();
      }
      setToolbarVisible(true);
    });
    els.displayMenu.appendChild(button);
  }
}

function h264Available() {
  return supportsH264() && window.isSecureContext;
}

function renderCodecMenu() {
  els.codecMenu.innerHTML = '';
  const options = [
    { codec: 'h264', key: 'streaming.codec.h264' },
    { codec: 'jpeg', key: 'streaming.codec.jpeg' }
  ];
  for (let i = 0; i < options.length; i += 1) {
    const option = options[i];
    const selected = option.codec === currentCodec;
    const available = option.codec !== 'h264' || h264Available();
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('role', 'menuitemradio');
    button.setAttribute('aria-checked', selected ? 'true' : 'false');
    if (selected) {
      button.className = 'is-selected';
    }
    if (!available) {
      button.setAttribute('aria-disabled', 'true');
    }
    const check = document.createElement('span');
    check.className = 'icon menu-check';
    check.innerHTML = icon('check');
    const text = document.createElement('span');
    text.className = 'menu-text';
    const title = document.createElement('span');
    title.className = 'menu-title';
    title.textContent = t(option.key);
    text.appendChild(title);
    if (!available) {
      const meta = document.createElement('span');
      meta.className = 'menu-meta';
      meta.textContent = t('streaming.h264DisabledHint');
      text.appendChild(meta);
    }
    button.appendChild(check);
    button.appendChild(text);
    button.addEventListener('click', function () {
      if (!available) {
        toast(t('streaming.h264DisabledHint'));
        closeMenus();
        return;
      }
      chooseCodec(option.codec);
    });
    els.codecMenu.appendChild(button);
  }
}

function chooseCodec(codec) {
  closeMenus();
  if (codec === currentCodec) {
    setToolbarVisible(true);
    return;
  }
  if (codec === 'h264' && !h264Available()) {
    toast(t('streaming.h264DisabledHint'));
    return;
  }
  currentCodec = codec;
  store.set(K_CODEC, codec);
  lastFallbackAt = 0;
  updateModeLabel();
  sendWatch();
  setToolbarVisible(true);
}

function updateModeLabel() {
  els.codecLabel.textContent = t('streaming.codec.' + (currentCodec === 'h264' ? 'h264' : 'jpeg'));
  els.codecBtn.setAttribute('aria-label', t('streaming.menuMode'));
  renderCodecMenu();
}

function updateFitLabel() {
  const key = fitMode === 'cover' ? 'streaming.fill' : 'streaming.fit';
  els.fitLabel.textContent = t(key);
  els.fitBtn.setAttribute('aria-label', t(key));
  els.fitBtn.title = t(key);
  setButtonIcon(els.fitBtn, fitMode === 'cover' ? 'crop' : 'fit');
}

function toggleFit() {
  fitMode = fitMode === 'cover' ? 'contain' : 'cover';
  store.set(K_FIT, fitMode);
  updateFitLabel();
  applyFit();
  setToolbarVisible(true);
}

function setStatsVisible(visible, persist) {
  statsVisible = !!visible;
  if (persist !== false) {
    store.set(K_STATS, statsVisible ? '1' : '0');
  }
  els.stats.hidden = !statsVisible;
  els.statsBtn.classList.toggle('is-active', statsVisible);
  els.statsBtn.setAttribute('aria-pressed', statsVisible ? 'true' : 'false');
  if (statsVisible) {
    updateStatsPanel();
  }
}

function toggleStats() {
  setStatsVisible(!statsVisible, true);
}

/* ---------- fullscreen ---------- */

function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement ||
    document.webkitCurrentFullScreenElement || null;
}

function fullscreenSupported() {
  return !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
}

function updateFullscreenUI() {
  const supported = fullscreenSupported();
  els.fsBtn.hidden = !supported;
  if (!supported) {
    return;
  }
  const active = !!fullscreenElement();
  els.fsLabel.textContent = t(active ? 'streaming.exitFullscreen' : 'streaming.fullscreen');
  els.fsBtn.setAttribute('aria-label', els.fsLabel.textContent);
  els.fsBtn.title = els.fsLabel.textContent;
  setButtonIcon(els.fsBtn, active ? 'minimize' : 'maximize');
}

function toggleFullscreen() {
  if (!fullscreenSupported()) {
    return;
  }
  if (fullscreenElement()) {
    const exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (!exit) {
      return;
    }
    try {
      const result = exit.call(document);
      if (result && typeof result.catch === 'function') {
        result.catch(function () {});
      }
    } catch (error) {
      /* ignore */
    }
    return;
  }
  const element = els.stream;
  const request = element.requestFullscreen || element.webkitRequestFullscreen;
  if (!request) {
    return;
  }
  try {
    const result = request.call(element);
    if (result && typeof result.catch === 'function') {
      result.catch(function () {});
    }
  } catch (error) {
    /* ignore */
  }
}

function onFullscreenChange() {
  updateFullscreenUI();
  window.setTimeout(applyFit, 80);
  if (phase === 'streaming') {
    setToolbarVisible(true);
  }
}

/* ---------- wake lock ---------- */

function acquireWakeLock() {
  if (!navigator.wakeLock || typeof navigator.wakeLock.request !== 'function' || document.hidden) {
    return;
  }
  if (wakeLock && !wakeLock.released) {
    return;
  }
  navigator.wakeLock.request('screen').then(function (lock) {
    wakeLock = lock;
    lock.addEventListener('release', function () {
      if (wakeLock === lock) {
        wakeLock = null;
      }
    });
  }).catch(function () {
    wakeLock = null;
  });
}

function releaseWakeLock() {
  const lock = wakeLock;
  wakeLock = null;
  if (lock && typeof lock.release === 'function') {
    try {
      const result = lock.release();
      if (result && typeof result.catch === 'function') {
        result.catch(function () {});
      }
    } catch (error) {
      /* ignore */
    }
  }
}

/* ---------- events ---------- */

function bindEvents() {
  els.pairForm.addEventListener('submit', function (event) {
    event.preventDefault();
    submitPair();
  });
  els.pin.addEventListener('input', onPinInput);

  els.connectingCancel.addEventListener('click', function () {
    showPair();
  });
  els.pendingCancel.addEventListener('click', function () {
    closeSocket();
    showPair();
  });

  els.displayBtn.addEventListener('click', function (event) {
    event.stopPropagation();
    toggleMenu('display');
    setToolbarVisible(true);
  });
  els.codecBtn.addEventListener('click', function (event) {
    event.stopPropagation();
    toggleMenu('codec');
    setToolbarVisible(true);
  });
  if (els.ctrlBtn) {
    els.ctrlBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      if (!controlAllowed) {
        toast(t('control.deniedHint'));
      }
      toggleMenu('ctrl');
      setToolbarVisible(true);
    });
  }
  if (els.keysBtn) {
    els.keysBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      toggleKeyLayer();
    });
  }
  if (els.keysMoreBtn) {
    els.keysMoreBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      if (!controlAllowed) {
        toast(t('control.deniedHint'));
        return;
      }
      toggleMenu('keys');
      setToolbarVisible(true);
    });
  }
  if (els.kbdBtn) {
    els.kbdBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      if (!controlAllowed) {
        toast(t('control.deniedHint'));
        return;
      }
      if (textInput && textInput.isFocused()) {
        closeKeyboard();
      } else if (textInput) {
        textInput.focus();
      }
      setToolbarVisible(true);
    });
  }
  if (els.keyboardClose) {
    els.keyboardClose.addEventListener('click', function (event) {
      event.stopPropagation();
      closeKeyboard();
    });
  }
  if (els.settingsBtn) {
    els.settingsBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      openSettingsSheet();
    });
  }
  els.moreBtn.addEventListener('click', function (event) {
    event.stopPropagation();
    toggleMenu('more');
    setToolbarVisible(true);
  });
  els.fitBtn.addEventListener('click', function () {
    toggleFit();
  });
  els.statsBtn.addEventListener('click', function () {
    toggleStats();
    setToolbarVisible(true);
  });
  els.fsBtn.addEventListener('click', function () {
    toggleFullscreen();
    setToolbarVisible(true);
  });
  els.disconnectBtn.addEventListener('click', function () {
    closeMenus();
    disconnectUser(false);
  });
  els.forgetBtn.addEventListener('click', function () {
    closeMenus();
    disconnectUser(true);
  });
  els.retryNow.addEventListener('click', function () {
    retryNow();
  });
  els.reconnectPair.addEventListener('click', function () {
    clearReconnect();
    showPair();
  });
  els.rePair.addEventListener('click', function () {
    store.remove(K_DEVICE);
    showPair();
  });

  els.toolbar.addEventListener('pointerdown', function () {
    if (phase === 'streaming') {
      scheduleToolbarHide();
    }
  }, { passive: true });
  els.toolbar.addEventListener('pointermove', function () {
    if (phase === 'streaming' && toolbarVisible) {
      scheduleToolbarHide();
    }
  }, { passive: true });

  els.stage.addEventListener('pointerdown', function () {
    if (textInput && textInput.isFocused()) {
      closeKeyboard();
    }
  }, true);
  els.stage.addEventListener('pointermove', function (event) {
    if (event.pointerType === 'mouse' && phase === 'streaming' && toolbarVisible) {
      scheduleToolbarHide();
    }
  }, { passive: true });
  els.stage.addEventListener('contextmenu', function (event) {
    event.preventDefault();
  });

  document.addEventListener('pointerdown', function (event) {
    if (!anyMenuOpen()) {
      return;
    }
    if (els.toolbar.contains(event.target)) {
      return;
    }
    closeMenus();
  }, true);

  document.addEventListener('keydown', function (event) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    if (phase !== 'streaming' && phase !== 'paused') {
      return;
    }
    if (controlMode !== 'view' || (settingsPanel && settingsPanel.isOpen())) {
      return;
    }
    const target = event.target;
    const tag = target && target.tagName ? String(target.tagName).toLowerCase() : '';
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      return;
    }
    const key = String(event.key || '').toLowerCase();
    if (key === 'f') {
      if (fullscreenSupported()) {
        event.preventDefault();
        toggleFullscreen();
      }
    } else if (key === 's') {
      event.preventDefault();
      toggleStats();
    } else if (key === 'escape') {
      closeMenus();
    }
  });

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      releaseWakeLock();
      releaseAllInput(true);
      if (sender) {
        sender.setEnabled(false);
      }
      if (authed && (phase === 'streaming' || phase === 'paused')) {
        send({ type: 'unwatch' });
      }
      return;
    }
    if (authed && (phase === 'streaming' || phase === 'paused')) {
      sendWatch();
      if (sender) {
        sender.setEnabled(inputAllowed());
      }
      acquireWakeLock();
    } else if (phase === 'reconnecting') {
      updateReconnectOverlay();
      if (reconnectDeadline > 0 && Date.now() >= reconnectDeadline && !reconnectTimer) {
        attemptReconnect();
      }
    }
  });

  window.addEventListener('resize', scheduleLayout);
  window.addEventListener('orientationchange', function () {
    window.setTimeout(scheduleLayout, 220);
  });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', scheduleLayout);
  }

  document.addEventListener('fullscreenchange', onFullscreenChange);
  document.addEventListener('webkitfullscreenchange', onFullscreenChange);

  window.addEventListener('pagehide', function () {
    releaseWakeLock();
    releaseAllInput(true);
    if (authed) {
      send({ type: 'bye' });
    }
  });
}

function disconnectUser(forget) {
  if (forget) {
    store.remove(K_DEVICE);
  }
  if (lastByeReason === null) {
    send({ type: 'bye' });
  }
  endSession(forget ? 'forgotten' : 'disconnected', { forget: forget });
}

/* ---------- boot ---------- */

function takePairToken() {
  let token = null;
  const hash = window.location.hash || '';
  if (hash.indexOf('pair=') >= 0) {
    try {
      const params = new URLSearchParams(hash.charAt(0) === '#' ? hash.slice(1) : hash);
      token = params.get('pair');
    } catch (error) {
      token = null;
    }
    try {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    } catch (error) {
      try {
        window.location.hash = '';
      } catch (nested) {
        /* ignore */
      }
    }
  }
  return token && token.length > 0 ? token : null;
}

function boot() {
  const qrToken = takePairToken();
  applyStaticIcons(document);
  initI18n();
  applyI18n(document);
  document.title = t('app.title');
  currentCodec = pickInitialCodec();
  statsVisible = store.get(K_STATS) === '1';
  fitMode = store.get(K_FIT) === 'cover' ? 'cover' : 'contain';
  inputSettings = normalizeInputSettings(readStoredJSON(K_INPUT));
  qualityPrefs = normalizeQualityPreferences(readStoredJSON(K_QUALITY));
  gamepadMapping = normalizeGamepadMapping(readStoredJSON(K_GAMEPAD));
  const storedMode = store.get(K_CTRL);
  preferredControlMode = CONTROL_MODES.indexOf(storedMode) >= 0 ? storedMode : 'view';
  controlMode = preferredControlMode;
  els.name.value = storedName();
  els.remember.checked = true;
  setStatsVisible(statsVisible, false);
  updateFitLabel();
  updateModeLabel();
  updateFullscreenUI();
  bindEvents();
  createZoom();
  createControlStack();
  preventNativeGestures(document);

  const device = readDevice();
  if (qrToken) {
    const name = storedName();
    saveName(name);
    startAuth({
      method: 'qr',
      token: qrToken,
      name: name,
      remember: true
    });
  } else if (device) {
    startAuth({
      method: 'device',
      device_id: device.device_id,
      token: device.token
    });
  } else {
    showPair();
  }
}

boot();
