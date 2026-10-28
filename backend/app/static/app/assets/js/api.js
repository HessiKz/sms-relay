// Minimal fetch wrapper for the Mini App JSON API.
// The initData credential is sent only through the request header, never
// in a URL, a cache key or a log line.

import { initData } from "./telegram.js";

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
