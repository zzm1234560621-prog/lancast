/*
 * LanCast local admin page.
 *
 * Polls /admin/api/state once a second while the tab is visible and updates
 * the existing DOM in place. The settings form is populated only on the first
 * successful load and when the user explicitly resets it, so polling never
 * overwrites values being edited.
 */

import {
  ApiError,
  getState,
  getToken,
  initToken,
  onUnauthorized,
  postJson
} from "./api.js";
import { applyI18n, lang, t } from "./i18n.js";
import { hydrateIcons, icon, makeIcon } from "./icons.js";

const POLL_INTERVAL_MS = 1000;
const MAX_LOG_ENTRIES = 100;
const TOAST_DURATION_MS = 3400;

const dom = {
  serverName: document.getElementById("server-name"),
  shareStatus: document.getElementById("share-status"),
  shareStatusText: document.getElementById("share-status-text"),
  pauseButton: document.getElementById("pause-button"),
  pauseIcon: document.querySelector("#pause-button .icon-slot"),
  pauseLabel: document.getElementById("pause-label"),
  controlStatus: document.getElementById("control-status"),
  stopControls: document.getElementById("stop-controls"),
  controlDisabled: document.getElementById("control-disabled"),

  bannerConnection: document.getElementById("banner-connection"),
  bannerAuth: document.getElementById("banner-auth"),

  warnings: document.getElementById("warnings"),
  warningsList: document.getElementById("warnings-list"),

  pendingPanel: document.getElementById("pending-panel"),
  pendingCount: document.getElementById("pending-count"),
  pendingList: document.getElementById("pending-list"),

  qrImage: document.getElementById("qr-image"),
  qrMissing: document.getElementById("qr-missing"),
  primaryUrl: document.getElementById("primary-url"),
  copyPrimary: document.getElementById("copy-primary"),
  otherAddresses: document.getElementById("other-addresses"),
  otherCount: document.getElementById("other-count"),
  otherList: document.getElementById("other-list"),
  pinCode: document.getElementById("pin-code"),
  pinExpiry: document.getElementById("pin-expiry"),
  rotatePin: document.getElementById("rotate-pin"),
  rotatePinLabel: document.getElementById("rotate-pin-label"),

  viewersCount: document.getElementById("viewers-count"),
  viewersBody: document.getElementById("viewers-body"),
  devicesCount: document.getElementById("devices-count"),
  devicesBody: document.getElementById("devices-body"),
  logCount: document.getElementById("log-count"),
  logList: document.getElementById("log-list"),
  logEmpty: document.getElementById("log-empty"),

  settingsForm: document.getElementById("settings-form"),
  fps: document.getElementById("setting-fps"),
  resolution: document.getElementById("setting-resolution"),
  bitrate: document.getElementById("setting-bitrate"),
  jpegQuality: document.getElementById("setting-jpeg-quality"),
  jpegQualityValue: document.getElementById("jpeg-quality-value"),
  jpegFps: document.getElementById("setting-jpeg-fps"),
  approval: document.getElementById("setting-approval"),
  controlEnabled: document.getElementById("setting-control-enabled"),
  viewerQuality: document.getElementById("setting-viewer-quality"),
  settingsError: document.getElementById("settings-error"),
  saveSettings: document.getElementById("save-settings"),
  saveSettingsLabel: document.getElementById("save-settings-label"),
  resetSettings: document.getElementById("reset-settings"),

  certFingerprint: document.getElementById("cert-fingerprint"),
  caFingerprint: document.getElementById("ca-fingerprint"),
  copyCertFingerprint: document.getElementById("copy-cert-fingerprint"),
  copyCaFingerprint: document.getElementById("copy-ca-fingerprint"),

  toastRegion: document.getElementById("toast-region")
};

const app = {
  data: null,
  authFailed: false,
  busy: new Set(),
  pollTimer: null,
  pollInFlight: false,
  pollQueued: false,
  pinDeadline: null,
  settingsInitialized: false,
  pendingSeen: new Set(),
  pendingNodes: new Map(),
  viewerNodes: new Map(),
  deviceNodes: new Map(),
  logNodes: new Map(),
  otherSignature: "",
  warningSignature: "",
  qrSource: "",
  initialized: false
};

let audioContext = null;

/* ------------------------------------------------------------------ */
/* Small DOM helpers                                                   */
/* ------------------------------------------------------------------ */

function el(tag, className = "", text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function setText(node, value) {
  if (!node) return;
  const text = value === null || value === undefined ? "" : String(value);
  if (node.textContent !== text) node.textContent = text;
}

function setButtonIcon(slot, name) {
  if (!slot || slot.dataset.icon === name) return;
  slot.dataset.icon = name;
  slot.innerHTML = icon(name);
}

function makeButton(className, iconName, label) {
  const button = el("button", className);
  button.type = "button";
  button.append(makeIcon(iconName), el("span", "", label));
  return button;
}

function orderNodes(container, nodes) {
  let cursor = container.firstChild;
  for (const node of nodes) {
    if (node === cursor) {
      cursor = cursor.nextSibling;
    } else {
      container.insertBefore(node, cursor);
    }
  }
}

function makeEmptyRow(colspan, text) {
  const row = el("tr", "empty-row");
  const cell = el("td", "cell-empty", text);
  cell.colSpan = colspan;
  row.append(cell);
  return row;
}

function ensureEmptyRow(tbody, colspan, text) {
  const current = tbody.querySelector(".empty-row");
  if (current) {
    setText(current.firstElementChild, text);
    return;
  }
  tbody.replaceChildren(makeEmptyRow(colspan, text));
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function pruneBusy(prefix, presentIds) {
  for (const key of Array.from(app.busy)) {
    if (key.startsWith(prefix) && !presentIds.has(key.slice(prefix.length))) {
      app.busy.delete(key);
    }
  }
}

function isControlEnabled() {
  const settings = app.data && app.data.settings;
  return !(settings && settings.control_enabled === false);
}

function hasControllingViewer() {
  return asArray(app.data && app.data.viewers).some(
    (item) => Boolean(item && item.controlling)
  );
}

function findViewer(viewerId) {
  if (!viewerId) return null;
  return (
    asArray(app.data && app.data.viewers).find(
      (item) => String(item.viewer_id) === viewerId
    ) || null
  );
}

function findDevice(deviceId) {
  if (!deviceId) return null;
  return (
    asArray(app.data && app.data.devices).find(
      (item) => String(item.device_id) === deviceId
    ) || null
  );
}

/* ------------------------------------------------------------------ */
/* Bootstrap                                                           */
/* ------------------------------------------------------------------ */

function init() {
  if (app.initialized) return;
  app.initialized = true;

  applyI18n(document);
  hydrateIcons(document);
  document.title = t("pageTitle");
  syncJpegQualityOutput();
  bindEvents();

  onUnauthorized(handleAuthFailure);
  const token = initToken();

  window.setInterval(tickLocal, POLL_INTERVAL_MS);

  if (!token) {
    handleAuthFailure();
    return;
  }

  startPolling();
}

function bindEvents() {
  dom.pauseButton.addEventListener("click", togglePause);
  dom.stopControls.addEventListener("click", stopAllControls);
  dom.rotatePin.addEventListener("click", rotatePin);
  dom.copyPrimary.addEventListener("click", () =>
    copyFromButton(dom.copyPrimary, dom.primaryUrl.dataset.copyValue || "")
  );
  dom.copyCertFingerprint.addEventListener("click", () =>
    copyFromButton(dom.copyCertFingerprint, dom.certFingerprint.dataset.copyValue || "")
  );
  dom.copyCaFingerprint.addEventListener("click", () =>
    copyFromButton(dom.copyCaFingerprint, dom.caFingerprint.dataset.copyValue || "")
  );

  dom.settingsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveSettings();
  });
  dom.resetSettings.addEventListener("click", resetSettings);
  dom.settingsForm.addEventListener("input", (event) => {
    hideSettingsError();
    if (event.target === dom.jpegQuality) syncJpegQualityOutput();
  });
  dom.settingsForm.addEventListener("change", hideSettingsError);

  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("pointerdown", unlockAudio, {
    once: true,
    passive: true
  });
  window.addEventListener("hashchange", () => {
    const previous = getToken();
    const next = initToken();
    if (next && next !== previous) {
      window.location.reload();
    }
  });
}

/* ------------------------------------------------------------------ */
/* Polling                                                              */
/* ------------------------------------------------------------------ */

function startPolling() {
  stopPolling();
  if (app.authFailed || document.hidden) return;

  if (app.pollInFlight) {
    app.pollQueued = true;
  } else {
    void pollOnce();
  }
  app.pollTimer = window.setInterval(() => {
    void pollOnce();
  }, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (app.pollTimer !== null) {
    window.clearInterval(app.pollTimer);
    app.pollTimer = null;
  }
}

function onVisibilityChange() {
  if (document.hidden) {
    stopPolling();
    return;
  }
  tickLocal();
  if (!app.authFailed) startPolling();
}

function refreshNow() {
  if (app.authFailed) return;
  if (app.pollInFlight) {
    app.pollQueued = true;
    return;
  }
  void pollOnce();
}

async function pollOnce() {
  if (app.pollInFlight || app.authFailed || document.hidden) return;
  app.pollInFlight = true;

  try {
    const data = await getState();
    if (!data || typeof data !== "object") {
      throw new ApiError("invalid state response", { code: "bad_state" });
    }
    applyState(data);
  } catch (error) {
    handlePollError(error);
  } finally {
    app.pollInFlight = false;
    if (app.pollQueued) {
      app.pollQueued = false;
      if (!app.authFailed && !document.hidden) {
        void pollOnce();
      }
    }
  }
}

function handlePollError(error) {
  if (error && error.status === 401) {
    handleAuthFailure();
    return;
  }
  if (!document.hidden) {
    showConnectionBanner();
  }
}

function applyState(data) {
  const firstState = app.data === null;
  const previousPending = app.pendingSeen;

  app.data = data;

  const pending = asArray(data.pending);
  const nextPendingIds = new Set(
    pending.map((item, index) => pendingKey(item, index))
  );

  if (!firstState && previousPending) {
    for (const id of nextPendingIds) {
      if (!previousPending.has(id)) {
        playBeep();
        break;
      }
    }
  }
  app.pendingSeen = nextPendingIds;

  renderHeader();
  renderWarnings(data.server);
  renderPairing(data.server, data.pairing);
  renderPending(pending);
  renderViewers(asArray(data.viewers));
  renderDevices(asArray(data.devices));
  renderSecurity(data.server);
  renderLog(asArray(data.log));

  if (!app.settingsInitialized) {
    populateSettings(data.settings);
    app.settingsInitialized = true;
  }
  updateSettingsControls();
  updateDocumentTitle();
  hideConnectionBanner();
  tickLocal();
}

function handleAuthFailure() {
  if (app.authFailed) return;
  app.authFailed = true;
  stopPolling();
  hideConnectionBanner();
  dom.bannerAuth.hidden = false;
  document.title = t("pageTitle");
  dom.rotatePin.disabled = true;

  renderHeader();
  if (app.data) {
    renderPending(asArray(app.data.pending));
    renderViewers(asArray(app.data.viewers));
    renderDevices(asArray(app.data.devices));
  }
  updateSettingsControls();
}

/* ------------------------------------------------------------------ */
/* Header                                                              */
/* ------------------------------------------------------------------ */

function renderHeader() {
  const server =
    app.data && app.data.server && typeof app.data.server === "object"
      ? app.data.server
      : null;
  const paused = Boolean(server && server.paused);
  const hasData = Boolean(server);

  setText(dom.serverName, server && server.name ? server.name : "—");
  dom.serverName.title = server && server.name ? String(server.name) : "";

  dom.shareStatus.classList.toggle("is-active", hasData && !paused);
  dom.shareStatus.classList.toggle("is-paused", hasData && paused);
  dom.shareStatus.classList.toggle("is-neutral", !hasData);

  if (!hasData) {
    setText(dom.shareStatusText, t("connecting"));
  } else {
    setText(dom.shareStatusText, t(paused ? "paused" : "sharing"));
  }

  setText(dom.pauseLabel, t(paused ? "resumeSharing" : "pauseSharing"));
  setButtonIcon(dom.pauseIcon, paused ? "play" : "pause");
  dom.pauseButton.disabled =
    !hasData || app.authFailed || app.busy.has("pause");

  updateControlSummary();
}

function updateControlSummary() {
  const controlling = hasControllingViewer();
  const enabled = isControlEnabled();

  dom.controlStatus.hidden = !controlling;
  dom.stopControls.hidden = !controlling;
  dom.controlDisabled.hidden = controlling || enabled;
  dom.stopControls.disabled =
    !controlling || app.authFailed || app.busy.has("stop-controls");
}

/* ------------------------------------------------------------------ */
/* Warnings                                                            */
/* ------------------------------------------------------------------ */

function renderWarnings(server) {
  const warnings = asArray(server && server.warnings)
    .filter((item) => item !== null && item !== undefined && String(item).trim())
    .map((item) => String(item));
  const signature = JSON.stringify(warnings);

  dom.warnings.hidden = warnings.length === 0;
  if (signature === app.warningSignature) return;
  app.warningSignature = signature;

  const fragment = document.createDocumentFragment();
  for (const warning of warnings) {
    fragment.append(el("li", "", warning));
  }
  dom.warningsList.replaceChildren(fragment);
}

/* ------------------------------------------------------------------ */
/* Pairing / connection card                                           */
/* ------------------------------------------------------------------ */

function renderPairing(server, pairingValue) {
  const pairing =
    pairingValue && typeof pairingValue === "object" ? pairingValue : {};
  const urls = asArray(server && server.urls).filter(
    (item) => item && typeof item.url === "string" && item.url
  );

  let primary = urls.find((item) => item.primary) || urls[0] || null;
  const primaryUrl = primary ? primary.url : "";

  setText(dom.primaryUrl, primaryUrl || "—");
  dom.primaryUrl.dataset.copyValue = primaryUrl;
  dom.copyPrimary.disabled = !primaryUrl;

  renderOtherAddresses(urls, primary);

  const qrSource = typeof pairing.qr_svg === "string" ? pairing.qr_svg : "";
  if (qrSource && qrSource !== app.qrSource) {
    dom.qrImage.src = qrSource;
    app.qrSource = qrSource;
  } else if (!qrSource && app.qrSource) {
    dom.qrImage.removeAttribute("src");
    app.qrSource = "";
  }
  dom.qrImage.alt = t("qrAlt");
  dom.qrImage.hidden = !qrSource;
  dom.qrMissing.hidden = Boolean(qrSource);

  setText(dom.pinCode, formatPin(pairing.pin));

  const expiresIn = Number(pairing.expires_in_s);
  if (
    pairing.pin !== null &&
    pairing.pin !== undefined &&
    String(pairing.pin) &&
    Number.isFinite(expiresIn) &&
    expiresIn >= 0
  ) {
    app.pinDeadline = Date.now() + expiresIn * 1000;
  } else {
    app.pinDeadline = null;
  }
  updatePinCountdown();

  dom.rotatePin.disabled =
    !app.data || app.authFailed || app.busy.has("rotate-pin");
}

function renderOtherAddresses(urls, primary) {
  const primaryUrl = primary ? primary.url : "";
  const others = urls.filter((item) => item !== primary && item.url !== primaryUrl);
  const signature = JSON.stringify(
    others.map((item) => [item.url, item.interface || "", item.kind || ""])
  );

  if (signature !== app.otherSignature) {
    app.otherSignature = signature;
    const fragment = document.createDocumentFragment();

    for (const item of others) {
      const row = el("li");
      row.append(el("code", "", item.url));
      if (item.interface) {
        row.append(el("span", "interface", item.interface));
      }
      if (item.kind) {
        row.append(el("span", "tag", kindLabel(item.kind)));
      }
      fragment.append(row);
    }
    dom.otherList.replaceChildren(fragment);
  }

  dom.otherCount.textContent = String(others.length);
  dom.otherAddresses.hidden = others.length === 0;
}

function kindLabel(kind) {
  const raw = String(kind || "").toLowerCase();
  if (!raw) return "";
  const key = `kind${raw.charAt(0).toUpperCase()}${raw.slice(1)}`;
  const translated = t(key);
  return translated === key ? raw : translated;
}

/* ------------------------------------------------------------------ */
/* Pending requests                                                    */
/* ------------------------------------------------------------------ */

function pendingKey(item, index) {
  if (item && item.request_id !== null && item.request_id !== undefined) {
    return String(item.request_id);
  }
  return `pending-${index}`;
}

function renderPending(pending) {
  const list = asArray(pending);
  dom.pendingPanel.hidden = list.length === 0;
  setText(dom.pendingCount, list.length || "");

  const seen = new Set();
  const nodes = [];

  list.forEach((item, index) => {
    const key = pendingKey(item, index);
    seen.add(key);
    let node = app.pendingNodes.get(key);
    if (!node) {
      node = createPendingNode(key);
      app.pendingNodes.set(key, node);
    }
    updatePendingNode(node, item, key);
    nodes.push(node);
  });

  pruneBusy("approve:", seen);

  for (const [key, node] of Array.from(app.pendingNodes)) {
    if (!seen.has(key)) {
      node.remove();
      app.pendingNodes.delete(key);
    }
  }

  if (list.length) {
    orderNodes(dom.pendingList, nodes);
  }
}

function createPendingNode(key) {
  const row = el("article", "pending-item");
  row.dataset.pendingKey = key;

  const main = el("div", "pending-main");
  const titleRow = el("div", "pending-title-row");
  const name = el("strong", "pending-name", "—");
  const method = el("span", "tag tag-accent");
  titleRow.append(name, method);

  const meta = el("div", "pending-meta");
  const ip = el("span", "pending-meta-item");
  const ua = el("span", "pending-meta-item pending-ua");
  const waiting = el("span", "pending-meta-item");
  waiting.append(makeIcon("clock"), el("span", "waiting-text", ""));
  meta.append(ip, ua, waiting);
  main.append(titleRow, meta);

  const actions = el("div", "pending-actions");
  const watch = makeButton(
    "btn btn-primary btn-small",
    "eye",
    t("allowWatch")
  );
  const control = makeButton(
    "btn btn-secondary btn-small",
    "mouse-pointer",
    t("allowControl")
  );
  const deny = makeButton(
    "btn btn-danger-ghost btn-small",
    "x",
    t("deny")
  );
  actions.append(watch, control, deny);

  watch.addEventListener("click", () => decidePending(key, "watch"));
  control.addEventListener("click", () => decidePending(key, "control"));
  deny.addEventListener("click", () => decidePending(key, "deny"));

  row.append(main, actions);

  row.__refs = {
    name,
    method,
    ip,
    ua,
    waiting,
    watch,
    control,
    deny
  };
  row.__firstSeenAt = Date.now();
  return row;
}

function updatePendingNode(node, item, key) {
  const refs = node.__refs;
  const requestId =
    item.request_id === null || item.request_id === undefined
      ? ""
      : String(item.request_id);
  const busy = app.busy.has(`approve:${key}`);
  const age = Number(item.age_s);

  node.dataset.pendingKey = key;
  node.dataset.requestId = requestId;
  node.classList.toggle("is-busy", busy);
  node.setAttribute("aria-busy", String(busy));
  node.__firstSeenAt =
    Date.now() - (Number.isFinite(age) && age > 0 ? age : 0) * 1000;

  setText(refs.name, item.name || "—");
  refs.name.title = item.name ? String(item.name) : "";
  setText(refs.method, methodLabel(item.method));
  setText(refs.ip, item.ip || "—");
  setText(refs.ua, shortUserAgent(item.ua));
  refs.ua.title = item.ua ? String(item.ua) : "";
  updatePendingWaiting(node);

  refs.watch.disabled = busy || app.authFailed || !requestId;
  refs.control.disabled = busy || app.authFailed || !requestId;
  refs.deny.disabled = busy || app.authFailed || !requestId;
}

function updatePendingWaiting(node) {
  if (!node || !node.__refs) return;
  const seconds = Math.max(
    0,
    Math.floor((Date.now() - node.__firstSeenAt) / 1000)
  );
  setText(node.__refs.waiting.querySelector(".waiting-text"), t("waitingSeconds", { n: seconds }));
}

function methodLabel(method) {
  const raw = String(method || "").toLowerCase();
  if (raw === "qr") return t("methodQr");
  if (raw === "pin") return t("methodPin");
  return method ? String(method) : "—";
}

async function decidePending(key, action) {
  const busyKey = `approve:${key}`;
  if (app.busy.has(busyKey) || app.authFailed) return;
  if (action !== "watch" && action !== "control" && action !== "deny") return;

  const node = app.pendingNodes.get(key);
  const requestId = node ? node.dataset.requestId : "";
  if (!requestId) return;

  const item = findPendingItem(key) || {};
  const name = item.name ? String(item.name) : requestId;

  if (action === "control" && !window.confirm(t("allowControlConfirm", { name }))) {
    return;
  }

  const allow = action !== "deny";
  const payload = { request_id: requestId, allow };
  if (allow) payload.control = action === "control";

  app.busy.add(busyKey);
  if (node) updatePendingNode(node, item, key);

  let succeeded = false;
  try {
    await postJson("/admin/api/approve", payload);
    succeeded = true;
    hideConnectionBanner();
    showToast(
      action === "deny"
        ? t("denied")
        : action === "control"
          ? t("allowedControlling", { name })
          : t("allowedWatching", { name })
    );
    refreshNow();
  } catch (error) {
    showActionError(error);
  } finally {
    if (!succeeded) {
      app.busy.delete(busyKey);
      if (node) updatePendingNode(node, findPendingItem(key) || {}, key);
    }
  }
}

function findPendingItem(key) {
  const pending = asArray(app.data && app.data.pending);
  return pending.find((item, index) => pendingKey(item, index) === key) || null;
}

/* ------------------------------------------------------------------ */
/* Viewers                                                             */
/* ------------------------------------------------------------------ */

function renderViewers(viewers) {
  const list = asArray(viewers);
  setText(dom.viewersCount, list.length || "");

  if (!list.length) {
    for (const node of app.viewerNodes.values()) node.remove();
    app.viewerNodes.clear();
    ensureEmptyRow(dom.viewersBody, 13, t("noViewers"));
    return;
  }

  const empty = dom.viewersBody.querySelector(".empty-row");
  if (empty) empty.remove();

  const seen = new Set();
  const nodes = [];

  list.forEach((item, index) => {
    const viewerId =
      item.viewer_id === null || item.viewer_id === undefined
        ? ""
        : String(item.viewer_id);
    const key = viewerId ? `id:${viewerId}` : `index:${index}`;
    seen.add(key);

    let row = app.viewerNodes.get(key);
    if (!row) {
      row = createViewerRow();
      app.viewerNodes.set(key, row);
    }
    updateViewerRow(row, item, viewerId);
    nodes.push(row);
  });

  pruneBusy("kick:", new Set(list.map((item) => String(item.viewer_id))));
  pruneBusy(
    "viewer-control:",
    new Set(list.map((item) => String(item.viewer_id)))
  );

  for (const [key, row] of Array.from(app.viewerNodes)) {
    if (!seen.has(key)) {
      row.remove();
      app.viewerNodes.delete(key);
    }
  }
  orderNodes(dom.viewersBody, nodes);
}

function createViewerRow() {
  const row = el("tr");

  const nameCell = el("td", "cell-name");
  const nameLine = el("div", "name-line");
  const nameText = el("span", "viewer-name", "—");
  const controlBadge = el("span", "tag tag-control");
  controlBadge.append(
    makeIcon("mouse-pointer"),
    el("span", "", t("controllingBadge"))
  );
  controlBadge.hidden = true;
  nameLine.append(nameText, controlBadge);
  nameCell.append(nameLine);

  const controlCell = el("td", "control-col");
  const controlLabel = el("label", "switch switch-inline");
  const controlInput = el("input");
  controlInput.type = "checkbox";
  const controlTrack = el("span", "switch-track");
  controlTrack.setAttribute("aria-hidden", "true");
  controlLabel.append(controlInput, controlTrack);
  controlCell.append(controlLabel);

  const refs = {
    name: nameCell,
    nameText,
    controlBadge,
    ip: el("td", "", "—"),
    display: el("td", "", "—"),
    mode: el("td", "", "—"),
    quality: el("td", "cell-quality", "—"),
    control: controlCell,
    controlInput,
    fps: el("td", "num", "—"),
    bitrate: el("td", "num", "—"),
    rtt: el("td", "num", "—"),
    latency: el("td", "num", "—"),
    drops: el("td", "num", "—"),
    connected: el("td", "num", "—")
  };

  const actionCell = el("td", "actions-col");
  const kickButton = makeButton("btn btn-danger-ghost btn-small", "unplug", t("kick"));
  kickButton.addEventListener("click", () => {
    const viewerId = kickButton.dataset.viewerId || "";
    if (viewerId) kickViewer(viewerId);
  });
  actionCell.append(kickButton);

  controlInput.addEventListener("change", () => {
    void toggleViewerControl(row, controlInput);
  });

  row.append(
    refs.name,
    refs.ip,
    refs.display,
    refs.mode,
    refs.quality,
    refs.control,
    refs.fps,
    refs.bitrate,
    refs.rtt,
    refs.latency,
    refs.drops,
    refs.connected,
    actionCell
  );

  row.__refs = refs;
  row.__kickButton = kickButton;
  return row;
}

function updateViewerRow(row, item, viewerId) {
  const refs = row.__refs;
  const kickBusy = viewerId ? app.busy.has(`kick:${viewerId}`) : false;
  const controlBusy = viewerId
    ? app.busy.has(`viewer-control:${viewerId}`)
    : false;

  row.dataset.viewerId = viewerId;

  const displayName = item.name ? String(item.name) : "—";
  setText(refs.nameText, displayName);
  refs.name.title = item.name ? displayName : "";
  refs.controlBadge.hidden = !Boolean(item.controlling);

  setText(refs.ip, item.ip || "—");
  setText(refs.display, displayLabel(item.display));
  setText(refs.mode, codecLabel(item.codec));
  setText(refs.quality, qualityLabel(item.quality, item.codec));
  setText(refs.fps, formatMetric(item.fps, 1));
  setText(
    refs.bitrate,
    item.kbps === null || item.kbps === undefined
      ? "—"
      : formatMetric(Number(item.kbps) / 1000, 1, true)
  );
  setText(refs.rtt, formatMetric(item.rtt_ms, 1));
  setText(refs.latency, formatMetric(item.latency_ms, 0));
  setText(refs.drops, formatMetric(item.drops, 0));
  setText(refs.connected, formatDuration(item.connected_s));

  refs.controlInput.checked = Boolean(item.control);
  refs.controlInput.disabled = !viewerId || controlBusy || app.authFailed;
  const controlName = item.name ? String(item.name) : viewerId || "—";
  const controlLabel = t("controlSwitchAria", { name: controlName });
  refs.controlInput.setAttribute("aria-label", controlLabel);
  refs.controlInput.title = isControlEnabled()
    ? controlLabel
    : `${controlLabel} · ${t("controlDisabled")}`;

  row.__kickButton.disabled = !viewerId || kickBusy || app.authFailed;
  row.__kickButton.dataset.viewerId = viewerId;
}

const QUALITY_PRESET_KEYS = Object.freeze({
  saver: "qualitySaver",
  smooth: "qualitySmooth",
  standard: "qualityStandard",
  high: "qualityHigh",
  native: "qualityNative",
  custom: "qualityCustom"
});

function qualityPresetLabel(preset) {
  const raw = preset === null || preset === undefined ? "" : String(preset);
  if (!raw) return "";
  const key = QUALITY_PRESET_KEYS[raw.toLowerCase()];
  return key ? t(key) : raw;
}

function shortBitrate(kbps) {
  return `${formatMetric(Number(kbps) / 1000, 1)}M`;
}

function qualityLabel(quality, codec) {
  if (!quality || typeof quality !== "object") return "—";

  const parts = [];
  const preset = qualityPresetLabel(quality.preset);
  if (preset) parts.push(preset);

  const width = Number(quality.max_width);
  const height = Number(quality.max_height);
  if (Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0) {
    parts.push(`${formatMetric(width, 0)}×${formatMetric(height, 0)}`);
  }

  const fps = Number(quality.fps);
  if (Number.isFinite(fps) && fps > 0) {
    parts.push(`${formatMetric(fps, 1)}fps`);
  }

  const jpegQuality = Number(quality.jpeg_quality);
  const isJpeg = String(codec || "").toLowerCase() === "jpeg";
  if (isJpeg) {
    if (Number.isFinite(jpegQuality) && jpegQuality > 0) {
      parts.push(`Q${formatMetric(jpegQuality, 0)}`);
    }
  } else {
    const bitrate = Number(quality.bitrate_kbps);
    if (Number.isFinite(bitrate) && bitrate > 0) {
      parts.push(shortBitrate(bitrate));
    }
  }

  return parts.length ? parts.join(" · ") : "—";
}

function displayLabel(index) {
  if (index === null || index === undefined || index === "") return "—";
  const number = Number(index);
  if (!Number.isFinite(number)) return "—";

  const displays = asArray(app.data && app.data.displays);
  const display = displays.find((item) => Number(item.index) === number);
  if (display && display.name) return String(display.name);
  return t("displayN", { n: number + 1 });
}

function codecLabel(codec) {
  if (codec === null || codec === undefined || codec === "") return "—";
  const raw = String(codec).toLowerCase();
  if (raw === "h264") return "H.264";
  if (raw === "jpeg") return "JPEG";
  return String(codec).toUpperCase();
}

async function kickViewer(viewerId) {
  const busyKey = `kick:${viewerId}`;
  if (!viewerId || app.busy.has(busyKey) || app.authFailed) return;

  app.busy.add(busyKey);
  renderViewers(asArray(app.data && app.data.viewers));

  let succeeded = false;
  try {
    await postJson("/admin/api/kick", { viewer_id: viewerId });
    succeeded = true;
    hideConnectionBanner();
    showToast(t("kicked"));
    refreshNow();
  } catch (error) {
    showActionError(error);
  } finally {
    if (!succeeded) {
      app.busy.delete(busyKey);
      renderViewers(asArray(app.data && app.data.viewers));
    }
  }
}

async function toggleViewerControl(row, input) {
  const viewerId = row.dataset.viewerId || "";
  const item = findViewer(viewerId);
  const current = Boolean(item && item.control);
  const desired = Boolean(input.checked);

  if (!viewerId || desired === current) {
    input.checked = current;
    return;
  }
  if (app.authFailed || app.busy.has(`viewer-control:${viewerId}`)) {
    input.checked = current;
    return;
  }

  const name = item && item.name ? String(item.name) : viewerId;
  if (desired) {
    let message = t("controlConfirm", { name });
    if (!isControlEnabled()) {
      message += `\n\n${t("controlMasterOffHint")}`;
    }
    if (!window.confirm(message)) {
      input.checked = current;
      return;
    }
  }

  const busyKey = `viewer-control:${viewerId}`;
  app.busy.add(busyKey);
  renderViewers(asArray(app.data && app.data.viewers));

  try {
    await postJson("/admin/api/viewer/control", {
      viewer_id: viewerId,
      control: desired
    });
    if (item) {
      item.control = desired;
      if (!desired) item.controlling = false;
    }
    const deviceId = item && item.device_id ? String(item.device_id) : "";
    const device = findDevice(deviceId);
    if (device) device.can_control = desired;
    hideConnectionBanner();
    showToast(
      desired && !isControlEnabled()
        ? t("controlRecordedOff")
        : t(desired ? "controlAllowed" : "controlOff")
    );
    refreshNow();
  } catch (error) {
    showActionError(error);
  } finally {
    app.busy.delete(busyKey);
    renderViewers(asArray(app.data && app.data.viewers));
    renderDevices(asArray(app.data && app.data.devices));
    renderHeader();
  }
}

/* ------------------------------------------------------------------ */
/* Remembered devices                                                  */
/* ------------------------------------------------------------------ */

function renderDevices(devices) {
  const list = asArray(devices);
  setText(dom.devicesCount, list.length || "");

  if (!list.length) {
    for (const node of app.deviceNodes.values()) node.remove();
    app.deviceNodes.clear();
    ensureEmptyRow(dom.devicesBody, 6, t("noDevices"));
    return;
  }

  const empty = dom.devicesBody.querySelector(".empty-row");
  if (empty) empty.remove();

  const seen = new Set();
  const nodes = [];

  list.forEach((item, index) => {
    const deviceId =
      item.device_id === null || item.device_id === undefined
        ? ""
        : String(item.device_id);
    const key = deviceId ? `id:${deviceId}` : `index:${index}`;
    seen.add(key);

    let row = app.deviceNodes.get(key);
    if (!row) {
      row = createDeviceRow();
      app.deviceNodes.set(key, row);
    }
    updateDeviceRow(row, item, deviceId);
    nodes.push(row);
  });

  pruneBusy(
    "revoke:",
    new Set(list.map((item) => String(item.device_id)))
  );
  pruneBusy(
    "device-control:",
    new Set(list.map((item) => String(item.device_id)))
  );

  for (const [key, row] of Array.from(app.deviceNodes)) {
    if (!seen.has(key)) {
      row.remove();
      app.deviceNodes.delete(key);
    }
  }
  orderNodes(dom.devicesBody, nodes);
}

function createDeviceRow() {
  const row = el("tr");
  const refs = {
    name: el("td", "cell-name", "—"),
    lastUsed: el("td", "", "—"),
    lastIp: el("td", "", "—"),
    onlineCell: el("td"),
    controlCell: el("td", "control-col")
  };

  const onlineWrap = el("span", "online-state");
  const dot = el("span", "dot");
  const onlineLabel = el("span", "", "");
  onlineWrap.append(dot, onlineLabel);
  refs.onlineCell.append(onlineWrap);

  const controlLabel = el("label", "switch switch-inline");
  const controlInput = el("input");
  controlInput.type = "checkbox";
  const controlTrack = el("span", "switch-track");
  controlTrack.setAttribute("aria-hidden", "true");
  controlLabel.append(controlInput, controlTrack);
  refs.controlCell.append(controlLabel);

  const actionCell = el("td", "actions-col");
  const revokeButton = makeButton(
    "btn btn-danger-ghost btn-small",
    "user-x",
    t("revoke")
  );
  revokeButton.addEventListener("click", () => {
    const deviceId = revokeButton.dataset.deviceId || "";
    if (deviceId) revokeDevice(deviceId);
  });
  actionCell.append(revokeButton);

  controlInput.addEventListener("change", () => {
    void toggleDeviceControl(row, controlInput);
  });

  row.append(
    refs.name,
    refs.lastUsed,
    refs.lastIp,
    refs.onlineCell,
    refs.controlCell,
    actionCell
  );
  row.__refs = refs;
  row.__controlInput = controlInput;
  row.__onlineDot = dot;
  row.__onlineLabel = onlineLabel;
  row.__revokeButton = revokeButton;
  return row;
}

function updateDeviceRow(row, item, deviceId) {
  const refs = row.__refs;
  const online = Boolean(item.online);
  const revokeBusy = deviceId ? app.busy.has(`revoke:${deviceId}`) : false;
  const controlBusy = deviceId
    ? app.busy.has(`device-control:${deviceId}`)
    : false;

  row.dataset.deviceId = deviceId;
  row.dataset.lastSeen = String(item.last_seen_at ?? "");

  setText(refs.name, item.name || "—");
  refs.name.title = item.name ? String(item.name) : "";
  setText(refs.lastUsed, relativeTime(item.last_seen_at));
  setText(refs.lastIp, item.last_ip || "—");
  setText(refs.onlineLabel, t(online ? "online" : "offline"));
  row.__onlineDot.className = online ? "dot dot-online" : "dot";

  row.__controlInput.checked = Boolean(item.can_control);
  row.__controlInput.disabled = !deviceId || controlBusy || app.authFailed;
  const controlName = item.name ? String(item.name) : deviceId || "—";
  const controlLabel = t("controlSwitchAria", { name: controlName });
  row.__controlInput.setAttribute("aria-label", controlLabel);
  row.__controlInput.title = isControlEnabled()
    ? controlLabel
    : `${controlLabel} · ${t("controlDisabled")}`;

  row.__revokeButton.disabled = !deviceId || revokeBusy || app.authFailed;
  row.__revokeButton.dataset.deviceId = deviceId;
}

async function revokeDevice(deviceId) {
  const busyKey = `revoke:${deviceId}`;
  if (!deviceId || app.busy.has(busyKey) || app.authFailed) return;

  const device = asArray(app.data && app.data.devices).find(
    (item) => String(item.device_id) === deviceId
  );
  const name = device && device.name ? String(device.name) : deviceId;
  if (!window.confirm(t("revokeConfirm", { name }))) return;

  app.busy.add(busyKey);
  renderDevices(asArray(app.data && app.data.devices));

  let succeeded = false;
  try {
    await postJson("/admin/api/revoke", { device_id: deviceId });
    succeeded = true;
    hideConnectionBanner();
    showToast(t("revoked"));
    refreshNow();
  } catch (error) {
    showActionError(error);
  } finally {
    if (!succeeded) {
      app.busy.delete(busyKey);
      renderDevices(asArray(app.data && app.data.devices));
    }
  }
}

async function toggleDeviceControl(row, input) {
  const deviceId = row.dataset.deviceId || "";
  const device = findDevice(deviceId);
  const current = Boolean(device && device.can_control);
  const desired = Boolean(input.checked);

  if (!deviceId || desired === current) {
    input.checked = current;
    return;
  }
  if (app.authFailed || app.busy.has(`device-control:${deviceId}`)) {
    input.checked = current;
    return;
  }

  const name = device && device.name ? String(device.name) : deviceId;
  if (desired) {
    let message = t("controlConfirm", { name });
    if (!isControlEnabled()) {
      message += `\n\n${t("controlMasterOffHint")}`;
    }
    if (!window.confirm(message)) {
      input.checked = current;
      return;
    }
  }

  const busyKey = `device-control:${deviceId}`;
  app.busy.add(busyKey);
  renderDevices(asArray(app.data && app.data.devices));

  try {
    await postJson("/admin/api/device/control", {
      device_id: deviceId,
      control: desired
    });
    if (device) device.can_control = desired;
    for (const viewer of asArray(app.data && app.data.viewers)) {
      if (String(viewer.device_id) !== deviceId) continue;
      viewer.control = desired;
      if (!desired) viewer.controlling = false;
    }
    hideConnectionBanner();
    showToast(
      desired && !isControlEnabled()
        ? t("controlRecordedOff")
        : t(desired ? "controlAllowed" : "controlOff")
    );
    refreshNow();
  } catch (error) {
    showActionError(error);
  } finally {
    app.busy.delete(busyKey);
    renderDevices(asArray(app.data && app.data.devices));
    renderViewers(asArray(app.data && app.data.viewers));
    renderHeader();
  }
}

/* ------------------------------------------------------------------ */
/* Event log                                                           */
/* ------------------------------------------------------------------ */

function renderLog(logValue) {
  const list = asArray(logValue).slice(-MAX_LOG_ENTRIES);
  setText(dom.logCount, list.length || "");

  if (!list.length) {
    for (const node of app.logNodes.values()) node.remove();
    app.logNodes.clear();
    dom.logEmpty.hidden = false;
    dom.logList.hidden = true;
    return;
  }

  dom.logEmpty.hidden = true;
  dom.logList.hidden = false;

  const occurrences = new Map();
  const keyed = list.map((entry) => {
    const item = entry && typeof entry === "object" ? entry : {};
    const base = `${item.t ?? ""}\u0000${item.level ?? ""}\u0000${item.msg ?? ""}`;
    const occurrence = occurrences.get(base) || 0;
    occurrences.set(base, occurrence + 1);
    return { item, key: `${base}\u0000${occurrence}` };
  });

  const seen = new Set();
  const nodes = [];

  keyed
    .slice()
    .reverse()
    .forEach(({ item, key }) => {
      seen.add(key);
      let node = app.logNodes.get(key);
      if (!node) {
        node = createLogEntry();
        app.logNodes.set(key, node);
      }
      updateLogEntry(node, item);
      nodes.push(node);
    });

  for (const [key, node] of Array.from(app.logNodes)) {
    if (!seen.has(key)) {
      node.remove();
      app.logNodes.delete(key);
    }
  }
  orderNodes(dom.logList, nodes);
}

function createLogEntry() {
  const node = el("li", "log-entry");
  const time = el("time", "log-time");
  const dot = el("span", "log-dot");
  const level = el("span", "sr-only");
  dot.append(level);
  const message = el("span", "log-message");
  node.append(time, dot, message);
  node.__refs = { time, dot, level, message };
  return node;
}

function updateLogEntry(node, entry) {
  const refs = node.__refs;
  const rawLevel = String(entry.level || "info").toLowerCase();
  const level =
    rawLevel === "warn" || rawLevel === "warning"
      ? "warning"
      : rawLevel === "error"
        ? "error"
        : rawLevel === "debug"
          ? "debug"
          : "info";

  setText(refs.time, formatTimeOfDay(entry.t));
  const date = new Date(Number(entry.t) * 1000);
  if (!Number.isNaN(date.getTime())) {
    refs.time.dateTime = date.toISOString();
  } else {
    refs.time.removeAttribute("datetime");
  }

  refs.dot.className = `log-dot log-level-${level}`;
  setText(refs.level, t(`level${level.charAt(0).toUpperCase()}${level.slice(1)}`));
  setText(refs.message, entry.msg || "");
}

/* ------------------------------------------------------------------ */
/* Security                                                            */
/* ------------------------------------------------------------------ */

function renderSecurity(server) {
  const cert = server && server.cert_sha256 ? String(server.cert_sha256) : "";
  const ca = server && server.ca_sha256 ? String(server.ca_sha256) : "";

  setText(dom.certFingerprint, cert || "—");
  setText(dom.caFingerprint, ca || "—");
  dom.certFingerprint.dataset.copyValue = cert;
  dom.caFingerprint.dataset.copyValue = ca;
  dom.copyCertFingerprint.disabled = !cert;
  dom.copyCaFingerprint.disabled = !ca;
}

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */

function setSelectValue(select, value, fallbackLabel) {
  const raw =
    value === null || value === undefined || value === "" ? "" : String(value);
  if (!raw) {
    if (select.options.length) select.selectedIndex = 0;
    return;
  }

  let option = Array.from(select.options).find(
    (candidate) => candidate.value === raw
  );
  if (!option) {
    option = el("option", "", fallbackLabel || raw);
    option.value = raw;
    select.append(option);
  }
  select.value = raw;
}

function populateSettings(settingsValue) {
  const settings =
    settingsValue && typeof settingsValue === "object" ? settingsValue : {};

  setSelectValue(dom.fps, settings.fps ?? 30);
  const maxWidth = Number(settings.max_width);
  const maxHeight = Number(settings.max_height);
  if (Number.isFinite(maxWidth) && Number.isFinite(maxHeight)) {
    setSelectValue(
      dom.resolution,
      `${maxWidth}x${maxHeight}`,
      `${maxWidth}×${maxHeight}`
    );
  } else {
    dom.resolution.value = "1920x1080";
  }

  const kbps = Number(settings.bitrate_kbps);
  const mbps = Number.isFinite(kbps) ? Math.round(kbps / 100) / 10 : 10;
  dom.bitrate.value = String(Math.min(200, Math.max(1, mbps)));

  const quality = Number(settings.jpeg_quality);
  dom.jpegQuality.value = String(
    Number.isFinite(quality) ? Math.min(95, Math.max(30, quality)) : 70
  );
  syncJpegQualityOutput();

  setSelectValue(dom.jpegFps, settings.jpeg_fps ?? 15);
  dom.approval.checked = Boolean(settings.approval);
  dom.controlEnabled.checked = settings.control_enabled !== false;
  dom.viewerQuality.checked = settings.viewer_quality !== false;
  hideSettingsError();
}

function syncJpegQualityOutput() {
  dom.jpegQualityValue.value = dom.jpegQuality.value;
  setText(dom.jpegQualityValue, dom.jpegQuality.value);
}

function updateSettingsControls() {
  const ready = Boolean(app.data) && app.settingsInitialized && !app.authFailed;
  const busy = app.busy.has("settings");
  dom.saveSettings.disabled = !ready || busy;
  dom.resetSettings.disabled = !ready || busy;
  setText(dom.saveSettingsLabel, t(busy ? "saving" : "save"));
}

function showSettingsError(message) {
  setText(dom.settingsError, message);
  dom.settingsError.hidden = !message;
}

function hideSettingsError() {
  if (!dom.settingsError.hidden) {
    dom.settingsError.hidden = true;
    dom.settingsError.textContent = "";
  }
}

async function saveSettings() {
  if (!app.data || app.authFailed || app.busy.has("settings")) return;

  const fps = Number(dom.fps.value);
  const match = /^(\d+)x(\d+)$/.exec(dom.resolution.value);
  const mbps = Number(dom.bitrate.value);
  const quality = Number(dom.jpegQuality.value);
  const jpegFps = Number(dom.jpegFps.value);

  if (!Number.isFinite(mbps) || mbps < 1 || mbps > 200) {
    showSettingsError(t("invalidBitrate"));
    dom.bitrate.focus();
    return;
  }
  if (!match) {
    showSettingsError(t("resolutionLabel"));
    return;
  }

  const maxWidth = Number(match[1]);
  const maxHeight = Number(match[2]);
  const payload = {
    fps,
    max_width: maxWidth,
    max_height: maxHeight,
    bitrate_kbps: Math.round(mbps * 1000),
    jpeg_quality: quality,
    jpeg_fps: jpegFps,
    approval: dom.approval.checked,
    control_enabled: dom.controlEnabled.checked,
    viewer_quality: dom.viewerQuality.checked
  };

  app.busy.add("settings");
  hideSettingsError();
  updateSettingsControls();

  try {
    await postJson("/admin/api/settings", payload);
    app.data.settings = Object.assign({}, app.data.settings, payload);
    if (!payload.control_enabled) {
      for (const viewer of asArray(app.data.viewers)) {
        viewer.controlling = false;
      }
    }
    hideConnectionBanner();
    showToast(t("applied"));
    renderHeader();
    renderViewers(asArray(app.data.viewers));
  } catch (error) {
    if (error && error.status === 401) {
      showSettingsError(t("authError"));
    } else if (isNetworkError(error)) {
      showConnectionBanner();
      showSettingsError(t("networkError"));
    } else {
      const detail = error && error.message ? error.message : t("unknownError");
      showSettingsError(`${t("saveFailed")} · ${detail}`);
    }
  } finally {
    app.busy.delete("settings");
    updateSettingsControls();
  }
}

function resetSettings() {
  if (!app.data || app.authFailed) return;
  populateSettings(app.data.settings);
  hideSettingsError();
}

/* ------------------------------------------------------------------ */
/* Actions                                                             */
/* ------------------------------------------------------------------ */

async function togglePause() {
  if (!app.data || app.authFailed || app.busy.has("pause")) return;

  const targetPaused = !Boolean(app.data.server && app.data.server.paused);
  app.busy.add("pause");
  renderHeader();

  try {
    await postJson("/admin/api/pause", { paused: targetPaused });
    if (app.data.server) app.data.server.paused = targetPaused;
    hideConnectionBanner();
    refreshNow();
  } catch (error) {
    showActionError(error);
  } finally {
    app.busy.delete("pause");
    renderHeader();
  }
}

async function stopAllControls() {
  if (!app.data || app.authFailed || app.busy.has("stop-controls")) return;
  if (!window.confirm(t("stopAllConfirm"))) return;

  app.busy.add("stop-controls");
  renderHeader();

  try {
    await postJson("/admin/api/settings", { control_enabled: false });
    if (app.data.settings) app.data.settings.control_enabled = false;
    for (const viewer of asArray(app.data.viewers)) {
      viewer.controlling = false;
    }
    if (app.settingsInitialized) {
      dom.controlEnabled.checked = false;
    }
    hideConnectionBanner();
    showToast(t("controlsStopped"));
    renderHeader();
    renderViewers(asArray(app.data.viewers));
    refreshNow();
  } catch (error) {
    showActionError(error);
  } finally {
    app.busy.delete("stop-controls");
    renderHeader();
  }
}

async function rotatePin() {
  if (app.authFailed || app.busy.has("rotate-pin")) return;

  app.busy.add("rotate-pin");
  dom.rotatePin.disabled = true;
  setText(dom.rotatePinLabel, t("rotatingPin"));

  try {
    await postJson("/admin/api/pin/rotate", {});
    hideConnectionBanner();
    showToast(t("pinRotated"));
    refreshNow();
  } catch (error) {
    showActionError(error);
  } finally {
    app.busy.delete("rotate-pin");
    setText(dom.rotatePinLabel, t("rotatePin"));
    dom.rotatePin.disabled = !app.data || app.authFailed;
  }
}

function showActionError(error) {
  if (error && error.status === 401) return;
  const detail = describeError(error);
  showToast(`${t("actionFailed")} · ${detail}`, "error");
  if (isNetworkError(error) || (error && error.status >= 500)) {
    showConnectionBanner();
  }
}

function describeError(error) {
  if (error && error.status === 401) return t("authError");
  if (isNetworkError(error)) return t("networkError");
  if (error && error.message) return String(error.message);
  return t("unknownError");
}

function isNetworkError(error) {
  return Boolean(
    error &&
      (error.status === 0 ||
        error.code === "network" ||
        error.code === "timeout")
  );
}

/* ------------------------------------------------------------------ */
/* Copy and banners                                                    */
/* ------------------------------------------------------------------ */

async function copyFromButton(button, value) {
  if (!value) return;
  const copied = await copyText(value);
  if (!copied) {
    showToast(t("copyFailed"), "error");
    return;
  }

  showToast(t("copied"));
  const slot = button.querySelector(".icon-slot");
  if (slot) {
    if (slot.__restoreTimer) window.clearTimeout(slot.__restoreTimer);
    setButtonIcon(slot, "check");
    slot.__restoreTimer = window.setTimeout(() => {
      slot.__restoreTimer = null;
      setButtonIcon(slot, "copy");
    }, 1200);
  }
}

async function copyText(value) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* fall through to the selection-based fallback */
  }

  let area = null;
  try {
    area = document.createElement("textarea");
    area.value = value;
    area.className = "copy-helper";
    area.setAttribute("readonly", "");
    document.body.append(area);
    area.select();
    area.setSelectionRange(0, area.value.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    if (area) area.remove();
  }
}

function showConnectionBanner() {
  dom.bannerConnection.hidden = false;
}

function hideConnectionBanner() {
  dom.bannerConnection.hidden = true;
}

function showToast(message, kind = "") {
  const node = el(
    "div",
    kind === "error" ? "toast toast-error" : "toast",
    message
  );
  dom.toastRegion.append(node);
  window.setTimeout(() => node.remove(), TOAST_DURATION_MS);
}

/* ------------------------------------------------------------------ */
/* Timers / formatting                                                 */
/* ------------------------------------------------------------------ */

function tickLocal() {
  if (document.hidden) return;
  updatePinCountdown();
  for (const node of app.pendingNodes.values()) {
    updatePendingWaiting(node);
  }
}

function updatePinCountdown() {
  if (app.pinDeadline === null) {
    setText(dom.pinExpiry, "—");
    dom.pinExpiry.classList.remove("is-expired");
    return;
  }

  const seconds = Math.max(
    0,
    Math.ceil((app.pinDeadline - Date.now()) / 1000)
  );
  setText(dom.pinExpiry, formatCountdown(seconds));
  dom.pinExpiry.classList.toggle("is-expired", seconds <= 0);
}

function formatCountdown(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  return `${pad2(minutes)}:${pad2(total % 60)}`;
}

function formatPin(value) {
  if (value === null || value === undefined) return "—";
  const raw = String(value).replace(/\s+/g, "");
  if (!raw) return "—";
  return raw.replace(/(.{3})(?=.)/g, "$1 ");
}

function formatMetric(value, digits = 1, fixed = false) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const factor = 10 ** digits;
  const rounded = Math.round(number * factor) / factor;
  return fixed ? rounded.toFixed(digits) : String(rounded);
}

function formatDuration(value) {
  if (value === null || value === undefined || value === "") return "—";
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";

  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = total % 60;

  if (hours > 0) {
    return `${hours}:${pad2(minutes)}:${pad2(remaining)}`;
  }
  return `${pad2(minutes)}:${pad2(remaining)}`;
}

function formatTimeOfDay(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(
    date.getSeconds()
  )}`;
}

function relativeTime(value) {
  if (value === null || value === undefined || value === "") return "—";
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";

  const age = Math.max(0, Date.now() / 1000 - seconds);
  if (age < 45) return t("justNow");
  if (age < 3600) {
    return t("minutesAgo", { n: Math.max(1, Math.floor(age / 60)) });
  }
  if (age < 86400) {
    return t("hoursAgo", { n: Math.floor(age / 3600) });
  }
  if (age < 7 * 86400) {
    return t("daysAgo", { n: Math.floor(age / 86400) });
  }

  return new Date(seconds * 1000).toLocaleDateString(
    lang === "zh" ? "zh-CN" : "en",
    { year: "numeric", month: "short", day: "numeric" }
  );
}

function shortUserAgent(value) {
  if (value === null || value === undefined) return "—";
  const ua = String(value).trim();
  if (!ua) return "—";

  let device = "";
  if (/iPad/i.test(ua)) device = "iPad";
  else if (/iPhone/i.test(ua)) device = "iPhone";
  else if (/Android/i.test(ua)) device = "Android";
  else if (/Windows/i.test(ua)) device = "Windows";
  else if (/Macintosh|Mac OS X/i.test(ua)) device = "Mac";
  else if (/Linux/i.test(ua)) device = "Linux";

  let browser = "";
  if (/Edg\/|EdgiOS|EdgA/i.test(ua)) browser = "Edge";
  else if (/CriOS|Chrome\//i.test(ua)) browser = "Chrome";
  else if (/FxiOS|Firefox\//i.test(ua)) browser = "Firefox";
  else if (/Safari\//i.test(ua)) browser = "Safari";

  const parts = [device, browser].filter(Boolean);
  if (parts.length) return parts.join(" · ");
  return ua.length > 48 ? `${ua.slice(0, 47)}…` : ua;
}

/* ------------------------------------------------------------------ */
/* Web Audio notification                                              */
/* ------------------------------------------------------------------ */

function unlockAudio() {
  try {
    if (!audioContext) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audioContext = new Ctx();
    }
    if (audioContext.state === "suspended") {
      void audioContext.resume().catch(() => {});
    }
  } catch {
    /* audio is opportunistic */
  }
}

function playBeep() {
  try {
    if (!audioContext) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audioContext = new Ctx();
    }
    if (audioContext.state === "suspended") {
      void audioContext.resume().catch(() => {});
    }

    const start = audioContext.currentTime + 0.01;
    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();

    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(880, start);
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.09, start + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.15);

    oscillator.connect(gain);
    gain.connect(audioContext.destination);
    oscillator.start(start);
    oscillator.stop(start + 0.16);
    oscillator.addEventListener("ended", () => {
      oscillator.disconnect();
      gain.disconnect();
    });
  } catch {
    /* browser may block audio until a user gesture; ignore */
  }
}

function updateDocumentTitle() {
  const count = asArray(app.data && app.data.pending).length;
  document.title =
    count > 0 ? `${t("titlePending", { n: count })}${t("pageTitle")}` : t("pageTitle");
}

/* ------------------------------------------------------------------ */
/* Start                                                               */
/* ------------------------------------------------------------------ */

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
