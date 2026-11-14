/*
 * Small fetch wrapper for the local admin API.
 *
 * - Reads the admin token from location.hash#token=... on startup and keeps a
 *   copy in sessionStorage so a reload works.
 * - Sends X-Admin-Token on every request.
 * - Sends JSON for POST requests.
 * - Calls the registered unauthorized handler on HTTP 401.
 */

const TOKEN_KEY = "lancast.admin";
const REQUEST_TIMEOUT_MS = 8000;

let adminToken = "";
let unauthorizedHandler = null;

function readTokenFromHash() {
  const raw = location.hash.startsWith("#")
    ? location.hash.slice(1)
    : location.hash;
  try {
    return new URLSearchParams(raw).get("token") || "";
  } catch {
    return "";
  }
}

function sessionGet() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function sessionSet(value) {
  try {
    sessionStorage.setItem(TOKEN_KEY, value);
  } catch {
    /* sessionStorage can be unavailable in private/embedded contexts */
  }
}

/**
 * Resolve the token for this page load. A token in the hash always wins and is
 * persisted; otherwise the session copy is reused.
 * @returns {string}
 */
export function initToken() {
  const fromHash = readTokenFromHash();
  if (fromHash) {
    adminToken = fromHash;
    sessionSet(fromHash);
  } else {
    adminToken = sessionGet();
  }
  return adminToken;
}

export function getToken() {
  return adminToken;
}

/**
 * Register the single 401 callback used by the admin page.
 * @param {(() => void) | null} handler
 */
export function onUnauthorized(handler) {
  unauthorizedHandler = typeof handler === "function" ? handler : null;
}

export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {{status?: number, code?: string}} [options]
   */
  constructor(message, { status = 0, code = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function assertApiPath(path) {
  if (typeof path !== "string" || !path.startsWith("/admin/api/")) {
    throw new ApiError("invalid API path", { code: "bad_path" });
  }
}

async function request(path, { method = "GET", body } = {}) {
  assertApiPath(path);

  const headers = new Headers();
  headers.set("Accept", "application/json");
  headers.set("X-Admin-Token", adminToken);

  let payload;
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
    payload = JSON.stringify(body);
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: payload,
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal
    });
  } catch {
    throw new ApiError(
      controller.signal.aborted ? "request timed out" : "network request failed",
      { status: 0, code: controller.signal.aborted ? "timeout" : "network" }
    );
  } finally {
    clearTimeout(timeout);
  }

  let data = null;
  try {
    const text = await response.text();
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }

  if (response.status === 401) {
    if (unauthorizedHandler) {
      try {
        unauthorizedHandler();
      } catch {
        /* never mask the 401 with a UI callback error */
      }
    }
    throw new ApiError("unauthorized", {
      status: 401,
      code: "unauthorized"
    });
  }

  if (!response.ok) {
    const message =
      data && typeof data.error === "string"
        ? data.error
        : `HTTP ${response.status}`;
    throw new ApiError(message, { status: response.status, code: "http" });
  }

  if (data && data.ok === false) {
    const message =
      typeof data.error === "string" ? data.error : "request failed";
    throw new ApiError(message, { status: response.status, code: "api" });
  }

  return data;
}

/**
 * GET /admin/api/state
 * @returns {Promise<any>}
 */
export function getState() {
  return request("/admin/api/state");
}

/**
 * POST a JSON body to an admin API endpoint.
 * @param {string} path
 * @param {Record<string, unknown>} [body]
 * @returns {Promise<any>}
 */
export function postJson(path, body = {}) {
  return request(path, { method: "POST", body });
}
