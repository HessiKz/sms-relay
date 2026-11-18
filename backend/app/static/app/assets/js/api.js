// Minimal fetch wrapper for the Mini App JSON API.
// The initData credential is sent only through the request header, never
// in a URL, a cache key or a log line.

import { initData } from "./telegram.js?v=20261117-calls";

const API_ROOT = "api/";
const AUTH_HEADER = "X-Telegram-Init-Data";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export class AuthError extends ApiError {
  constructor(status) {
    super("not authorized", status);
    this.name = "AuthError";
  }
}

export class RateLimitError extends ApiError {
  constructor() {
    super("rate limited", 429);
    this.name = "RateLimitError";
  }
}

export class NetworkError extends ApiError {
  constructor() {
    super("network error", 0);
    this.name = "NetworkError";
  }
}

function buildUrl(path, params) {
  const clean = String(path || "").replace(/^\/+/, "");
  let url = API_ROOT + clean;
  if (!params) return url;
  const query = new URLSearchParams();
  for (const key of Object.keys(params)) {
    const value = params[key];
    if (value === undefined || value === null) continue;
    query.set(key, String(value));
  }
  const serialized = query.toString();
  if (serialized) url += "?" + serialized;
  return url;
}

async function request(method, path, options) {
  const opts = options || {};
  const headers = { Accept: "application/json" };
  if (initData) headers[AUTH_HEADER] = initData;
  let body;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  let response;
  try {
    response = await fetch(buildUrl(path, opts.params), {
      method,
      headers,
      body,
      credentials: "omit",
      cache: "no-store",
    });
  } catch (err) {
    throw new NetworkError();
  }
  if (!response.ok) {
    // Read the JSON error body once so the UI can inspect the structured
    // detail ({error: "unknown_number"}, {error: "rate_limited"}, ...).
    let payload = null;
    try {
      payload = await response.json();
    } catch (err) {
      payload = null;
    }
    if (response.status === 401 || response.status === 403) {
      const authError = new AuthError(response.status);
      authError.payload = payload;
      throw authError;
    }
    if (response.status === 429) {
      const rateError = new RateLimitError();
      rateError.payload = payload;
      throw rateError;
    }
    const apiError = new ApiError("request failed", response.status);
    apiError.payload = payload;
    throw apiError;
  }
  try {
    return await response.json();
  } catch (err) {
    throw new ApiError("invalid response", response.status);
  }
}

export function apiGet(path, params) {
  return request("GET", path, { params });
}

export function apiPost(path, body) {
  return request("POST", path, { body });
}

export function apiPatch(path, body) {
  return request("PATCH", path, { body });
}

// --- calls ------------------------------------------------------------

// The call log of ONE device, newest first. The cursor has two halves
// (`before_ts` and `before_id`), exactly like the thread timeline, so two
// calls of the same second are never skipped or repeated.
export function getCalls(deviceId, options) {
  const opts = options || {};
  const params = { limit: opts.limit };
  if (opts.beforeTs !== undefined && opts.beforeTs !== null) {
    params.before_ts = opts.beforeTs;
  }
  if (opts.beforeKey !== undefined && opts.beforeKey !== null) {
    params.before_id = opts.beforeKey;
  }
  if (opts.q) params.q = opts.q;
  return apiGet(
    "devices/" + encodeURIComponent(String(deviceId || "")) + "/calls",
    params);
}

// The current call of the device, or a null call when it is idle.
export function getLiveCall(deviceId) {
  return apiGet(
    "devices/" + encodeURIComponent(String(deviceId || "")) + "/calls/live");
}

// Ask the phone to answer, decline, mute, dial, ... The client_ref makes
// the request idempotent: a retry with the same one is not a second call.
export function sendCallAction(deviceId, options) {
  const opts = options || {};
  const body = {
    call_ref: String(opts.callRef || ""),
    action: String(opts.action || ""),
    client_ref: String(opts.clientRef || ""),
  };
  if (opts.payload !== undefined && opts.payload !== null) {
    body.payload = opts.payload;
  }
  return apiPost(
    "devices/" + encodeURIComponent(String(deviceId || ""))
    + "/calls/actions",
    body);
}

// Clear the unread missed badge of ONE device up to a timestamp.
export function markCallsRead(deviceId, untilTs) {
  return apiPost(
    "devices/" + encodeURIComponent(String(deviceId || "")) + "/calls/read",
    { until_ts: Math.floor(Number(untilTs) || 0) });
}
