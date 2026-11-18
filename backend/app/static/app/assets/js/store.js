// Device-first state, data loading and the revision long poll.
// The backend is the source of truth; this module only decides when to ask.
//
// Every conversation belongs to one device. The state keeps the device
// of the open route and never mixes the rows of two devices: switching
// the device drops the chats, the thread and the pending bubbles of the
// old one before anything is loaded again.

import {
  apiGet,
  apiPost,
  apiPatch,
  AuthError,
  getCalls,
  getLiveCall,
  sendCallAction as postCallAction,
  markCallsRead as postCallsRead,
} from "./api.js?v=20261117-calls";
import * as ui from "./ui.js?v=20261117-calls";

const PAGE_SIZE = 50;
const CHAT_LIMIT = 100;
const CALL_LIMIT = 50;
const BACKOFF_MS = [1000, 2000, 5000, 15000];
// A burst of changes (an import, a busy sender) must not turn into a
// burst of reloads: the refresh waits for the burst to settle.
const REFRESH_DEBOUNCE_MS = 300;

export const state = {
  me: null,
  // Every paired device, active and archived.
  devices: [],
  // The device of the open route. Null on the device screen.
  deviceId: null,
  chats: [],
  // A deep link can open a thread without ever loading the conversations
  // of that device, so the list remembers whether it was loaded.
  chatsLoaded: false,
  query: "",
  chatsHasMore: false,
  chatsCursor: null,
  thread: null,
  threadMeta: null,
  events: [],
  hasMore: false,
  // The call log of the open device. `callsAvailable` is false once the
  // backend answered 404: the UI then shows the "not available" state
  // instead of an empty history.
  calls: [],
  callsHasMore: false,
  callsCursor: null,
  callsQuery: "",
  callsLoaded: false,
  callsAvailable: null,
  liveCall: null,
  callActionPending: null,
  rev: 0,
  // null means "the scope is unknown": everything has to be reloaded.
  // Otherwise a list of {device_id, thread_key}; a null thread_key is a
  // device-wide change.
  changedScopes: [],
  pending: [],
  lastFailed: null,
  sending: false,
  selectedSubId: 0,
  readMarkedTs: 0,
  callsReadMarkedTs: 0,
  status: null,
  loading: { devices: false, chats: false, thread: false, calls: false },
};

let pollTimer = null;
let pollStopped = true;
let pollArmed = false;
let backoffIndex = 0;
let refreshTimer = null;
let refreshRunning = false;
let refreshDirty = false;
let chatsRequest = 0;
let threadRequest = 0;
let devicesRequest = 0;
let callsRequest = 0;

export async function loadMe() {
  const data = await apiGet("me");
  state.me = data;
  ui.renderMe(data);
  return data;
}

export function currentDevice() {
  if (!state.deviceId) return null;
  for (const device of state.devices) {
    if (device && String(device.id) === String(state.deviceId)) return device;
  }
  return null;
}

export function deviceById(deviceId) {
  const target = String(deviceId || "");
  for (const device of state.devices) {
    if (device && String(device.id) === target) return device;
  }
  return null;
}

// The SIMs of the device of the open route. The phone of the status is
// only a fallback for a device that has never reported its SIMs.
export function currentSims() {
  const device = currentDevice();
  if (device && Array.isArray(device.sims)) return device.sims;
  const phone = state.status && state.status.phone ? state.status.phone : {};
  return Array.isArray(phone.sims) ? phone.sims : [];
}

// A device can be read even when it is archived or revoked; it just
// cannot send anything any more.
export function deviceCanSend(device) {
  if (!device) return false;
  if (device.archived || device.revoked) return false;
  if (device.sending === false) return false;
  return true;
}

export function setSelectedSubId(value) {
  const number = Number(value);
  state.selectedSubId = Number.isInteger(number) && number >= 0 ? number : 0;
  ui.setSimSelection(state.selectedSubId);
  return state.selectedSubId;
}

export async function loadStatus() {
  const data = await apiGet("status");
  state.status = data;
  ui.renderPausedState(Boolean(data && data.paused));
  const sims = currentSims();
  const available = sims
    .filter((sim) => sim && Number.isInteger(sim.sub_id))
    .map((sim) => sim.sub_id);
  if (!available.includes(state.selectedSubId)) state.selectedSubId = 0;
  ui.populateSimSelect(sims, state.selectedSubId);
  return data;
}

// The device summary carries the live call and the missed count already:
// the card and the tab badge are current before the live endpoint is even
// asked. The explicit live call still wins once it is loaded.
function adoptSummaryCall() {
  const device = currentDevice();
  if (!device) {
    if (!state.liveCall) ui.renderLiveCall(null);
    return;
  }
  if (state.liveCall) return;
  const call = device.call || null;
  ui.renderLiveCall(call);
  ui.renderChatsHeader(device);
}

// The device list is reloaded from several places. The hook lets the
// shell follow a rename or a revoke without polling for it.
let devicesChanged = null;

export function onDevicesChanged(handler) {
  devicesChanged = handler;
}

// The device list of the Mini App: every paired device, active and
// archived. The list is replaced, but the rows are reused, so a refresh
// does not make the cards flicker.
export async function loadDevices() {
  const request = ++devicesRequest;
  state.loading.devices = true;
  ui.setDevicesStatus("Loading devices…");
  try {
    const data = await apiGet("devices");
    if (request !== devicesRequest) return state.devices;
    const list = Array.isArray(data && data.devices) ? data.devices : [];
    state.devices = list;
    ui.renderDevices(list);
    ui.renderChatsHeader(currentDevice());
    ui.renderThreadContext(currentDevice());
    ui.setDevicesStatus("");
    adoptSummaryCall();
    if (devicesChanged) devicesChanged(list);
    return list;
  } finally {
    state.loading.devices = false;
  }
}

// Forget everything of the device that was open. The rows of the old
// device must not survive the switch.
export function selectDevice(deviceId) {
  const target = String(deviceId || "");
  if (target === state.deviceId) return;
  state.deviceId = target;
  state.chats = [];
  state.chatsLoaded = false;
  state.chatsHasMore = false;
  state.chatsCursor = null;
  state.query = "";
  state.thread = null;
  state.threadMeta = null;
  state.events = [];
  state.hasMore = false;
  state.pending = [];
  state.lastFailed = null;
  state.readMarkedTs = 0;
  state.callsReadMarkedTs = 0;
  state.calls = [];
  state.callsHasMore = false;
  state.callsCursor = null;
  state.callsQuery = "";
  state.callsLoaded = false;
  state.callsAvailable = null;
  state.liveCall = null;
  state.callActionPending = null;
  lastCallAttempt = null;
  callsRequest += 1;
  ui.resetScope();
}

// The server names the event id `id` for a message and `event_id` for a
// call. The store keeps one shape: `id` is the key of the row.
function normalizeEvent(event) {
  if (!event) return event;
  if ((event.id === undefined || event.id === null)
      && event.event_id !== undefined && event.event_id !== null) {
    return Object.assign({}, event, { id: String(event.event_id) });
  }
  return event;
}

// The server orders the events of a thread by timestamp and then by the
// event id, newest first. The same order is used here so a merged list
// stays sorted.
function compareEvents(a, b) {
  const left = Number(a && a.ts) || 0;
  const right = Number(b && b.ts) || 0;
  if (left !== right) return right - left;
  const leftId = String((a && a.id) || "");
  const rightId = String((b && b.id) || "");
  if (leftId === rightId) return 0;
  return leftId < rightId ? 1 : -1;
}

// Merge two newest-first event lists. The fresh copy of an event wins so
// an outbound status change is picked up, and the result keeps the order
// of the server.
function mergeEvents(current, incoming) {
  const result = [];
  const seen = new Set();
  let left = 0;
  let right = 0;
  const take = (event) => {
    if (!event || event.id === undefined || event.id === null) return;
    const key = String(event.id);
    if (seen.has(key)) return;
    seen.add(key);
    result.push(event);
  };
  while (left < current.length && right < incoming.length) {
    const order = compareEvents(current[left], incoming[right]);
    if (order < 0) {
      take(current[left]);
      left += 1;
    } else if (order > 0) {
      take(incoming[right]);
      right += 1;
    } else {
      // The same event: keep the fresh copy.
      take(incoming[right]);
      left += 1;
      right += 1;
    }
  }
  while (left < current.length) {
    take(current[left]);
    left += 1;
  }
  while (right < incoming.length) {
    take(incoming[right]);
    right += 1;
  }
  return result;
}

// The conversation list is ordered by the last message, newest first, and
// then by the thread key.
function compareChats(a, b) {
  const left = Number(a && a.last_ts) || 0;
  const right = Number(b && b.last_ts) || 0;
  if (left !== right) return right - left;
  const leftKey = String((a && a.thread_key) || "");
  const rightKey = String((b && b.thread_key) || "");
  if (leftKey === rightKey) return 0;
  return leftKey < rightKey ? -1 : 1;
}

function mergeChats(current, incoming) {
  const byKey = new Map();
  for (const chat of current) {
    if (chat && chat.thread_key) byKey.set(String(chat.thread_key), chat);
  }
  for (const chat of incoming) {
    if (!chat || !chat.thread_key) continue;
    const key = String(chat.thread_key);
    const old = byKey.get(key);
    if (!old) {
      byKey.set(key, chat);
      continue;
    }
    // Keep the newest preview, but take the unread count of the fresh
    // page: the reader may have marked the conversation as read.
    const merged = Object.assign({}, old, chat);
    if (Number(old.last_ts) > Number(chat.last_ts)) {
      merged.last_ts = old.last_ts;
      merged.last_body = old.last_body;
      merged.last_direction = old.last_direction;
    }
    byKey.set(key, merged);
  }
  const result = Array.from(byKey.values());
  result.sort(compareChats);
  return result;
}

// The conversations of ONE device.
export async function loadChats(deviceId, query, options) {
  const opts = options || {};
  const target = String(deviceId || state.deviceId || "");
  if (!target) return state.chats;
  if (target !== state.deviceId) selectDevice(target);
  const append = Boolean(opts.append);
  const text = typeof query === "string" ? query : state.query;
  const trimmed = text.trim();
  const queryChanged = trimmed !== (state.query || "").trim();
  if (queryChanged) {
    // A new search starts a new list; the pages of the old one do not
    // belong to it.
    state.chats = [];
    state.chatsHasMore = false;
    state.chatsCursor = null;
  }
  state.query = text;
  state.loading.chats = true;
  if (!state.chats.length) ui.setListStatus("Loading conversations…");
  const request = ++chatsRequest;
  const hadChats = state.chats.length > 0;
  try {
    const params = { limit: CHAT_LIMIT };
    if (trimmed) params.q = trimmed;
    if (append && state.chatsCursor) {
      params.before_ts = state.chatsCursor.ts;
      params.before_key = state.chatsCursor.key;
    }
    const data = await apiGet(
      "devices/" + encodeURIComponent(target) + "/chats", params);
    if (request !== chatsRequest) return state.chats;
    if (state.deviceId !== target) return state.chats;
    const page = Array.isArray(data.chats) ? data.chats : [];
    state.chats = mergeChats(state.chats, page);
    state.chatsLoaded = true;
    // Only the page that walks backwards knows whether the list ends.
    // A refresh of the newest page must not resurrect the button.
    if (append || queryChanged || !hadChats) {
      state.chatsHasMore = Boolean(data.has_more);
    }
    const oldest = state.chats[state.chats.length - 1];
    state.chatsCursor = oldest
      ? { ts: oldest.last_ts, key: String(oldest.thread_key) }
      : null;
    ui.renderChatList(state.chats, {
      query: trimmed,
      activeKey: state.thread,
      hasMore: state.chatsHasMore,
    });
    if (state.chats.length) {
      ui.setListStatus("");
    } else if (trimmed) {
      ui.setListStatus("No conversations match your search.");
    } else {
      ui.setListStatus("No messages on this device yet.");
    }
    return state.chats;
  } finally {
    state.loading.chats = false;
  }
}

// The history of one thread of ONE device.
//
//   append: true   an older page, added in front of the loaded history
//   otherwise:      the newest page, merged into the loaded history
//
// The timeline is only reset when a different conversation is opened.
export async function loadThread(deviceId, threadKey, options) {
  const opts = options || {};
  const target = String(deviceId || state.deviceId || "");
  const key = String(threadKey || "");
  if (!key) throw new Error("missing thread key");
  if (target !== state.deviceId) selectDevice(target);
  const append = Boolean(opts.append);
  const switching = key !== state.thread;
  if (switching) {
    state.thread = key;
    state.threadMeta = null;
    state.events = [];
    state.hasMore = false;
    state.readMarkedTs = 0;
    ui.renderThreadLoading(key);
  }
  const params = { limit: PAGE_SIZE };
  if (opts.before_ts !== undefined && opts.before_ts !== null) {
    params.before_ts = opts.before_ts;
  }
  if (opts.before_id !== undefined && opts.before_id !== null) {
    params.before_id = opts.before_id;
  }
  state.loading.thread = true;
  const request = ++threadRequest;
  try {
    const data = await apiGet(
      "devices/" + encodeURIComponent(target) + "/chats/"
      + encodeURIComponent(key) + "/messages", params);
    if (request !== threadRequest) return data;
    if (state.deviceId !== target || state.thread !== key) return data;
    state.thread = data.thread_key || key;
    state.threadMeta = {
      thread_key: data.thread_key,
      display: data.display,
      name: data.name,
    };
    const page = Array.isArray(data.events)
      ? data.events.map(normalizeEvent) : [];
    if (switching) {
      state.events = page;
      state.hasMore = Boolean(data.has_more);
    } else if (append) {
      state.events = mergeEvents(state.events, page);
      state.hasMore = Boolean(data.has_more);
    } else {
      // A refresh of the open conversation: merge, never reset. The
      // history that was already loaded stays loaded.
      state.events = mergeEvents(state.events, page);
    }
    ui.renderThread({
      meta: state.threadMeta,
      events: state.events,
      hasMore: state.hasMore,
      pending: state.pending,
      deviceId: target,
      mode: append ? "append" : (switching ? "reset" : "merge"),
    });
    ui.setThreadStatus("");
    return data;
  } catch (err) {
    if (err && err.status === 404) {
      state.thread = null;
      state.threadMeta = null;
      state.events = [];
      state.hasMore = false;
    }
    throw err;
  } finally {
    state.loading.thread = false;
  }
}

// The read marker of one thread of ONE device.
export async function markRead(deviceId, threadKey, untilTs) {
  const target = String(deviceId || state.deviceId || "");
  const key = String(threadKey || "");
  const value = Number(untilTs);
  if (!target || !key || !Number.isFinite(value) || value < 0) return null;
  if (value <= state.readMarkedTs) return null;
  const data = await apiPost(
    "devices/" + encodeURIComponent(target) + "/chats/"
    + encodeURIComponent(key) + "/read",
    { until_ts: Math.floor(value) });
  state.readMarkedTs = value;
  const chat = state.chats.find((item) => item.thread_key === key);
  if (chat) {
    chat.unread = 0;
    ui.renderChatList(state.chats, {
      query: state.query,
      activeKey: state.thread,
      hasMore: state.chatsHasMore,
    });
  }
  return data;
}

// The contacts of ONE device, for a new conversation on that device.
export async function loadContacts(deviceId, query) {
  const target = String(deviceId || state.deviceId || "");
  if (!target) return [];
  const params = { limit: 20 };
  const text = String(query || "").trim();
  if (text) params.q = text;
  const data = await apiGet(
    "devices/" + encodeURIComponent(target) + "/contacts", params);
  if (state.deviceId !== target) return [];
  return Array.isArray(data.contacts) ? data.contacts : [];
}

// --- calls ------------------------------------------------------------

// One ref per attempt, reused by every retry of the same action so the
// unique `client_ref` of the backend turns a lost answer into a duplicate
// instead of a second radio command.
function newActionRef() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
  } catch (err) {
    // Fall through to the manual random id.
  }
  const bytes = new Uint8Array(16);
  try {
    window.crypto.getRandomValues(bytes);
  } catch (err) {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  let value = "";
  for (const byte of bytes) value += byte.toString(16).padStart(2, "0");
  return value;
}

// Newest first by the call time, then by the id: the same order the
// server pages with, so the merged list stays sorted.
function compareCalls(a, b) {
  const left = Number(a && a.started_at) || 0;
  const right = Number(b && b.started_at) || 0;
  if (left !== right) return right - left;
  const leftId = String((a && a.id) || "");
  const rightId = String((b && b.id) || "");
  if (leftId === rightId) return 0;
  return leftId < rightId ? 1 : -1;
}

function mergeCalls(current, incoming) {  const byId = new Map();
  for (const call of current) {
    if (call && call.id) byId.set(String(call.id), call);
  }
  for (const call of incoming) {
    if (!call || !call.id) continue;
    byId.set(String(call.id), call);
  }
  const result = Array.from(byId.values());
  result.sort(compareCalls);
  return result;
}

function renderCallsList() {
  ui.renderCalls(state.calls, {
    query: state.callsQuery,
    hasMore: state.callsHasMore,
    available: state.callsAvailable !== false,
  });
}

function callsUnreadUntil() {
  let newest = 0;
  for (const call of state.calls) {
    const ts = Number(call && call.started_at) || 0;
    if (ts > newest) newest = ts;
  }
  return newest;
}

// The call log of ONE device. A search starts a new list; a plain load
// merges the newest page so a refresh never throws away the older pages
// the reader has already walked to.
export async function loadCalls(deviceId, options) {
  const opts = options || {};
  const target = String(deviceId || state.deviceId || "");
  if (!target) return state.calls;
  if (target !== state.deviceId) selectDevice(target);
  const append = Boolean(opts.append);
  const text = typeof opts.query === "string" ? opts.query : state.callsQuery;
  const trimmed = text.trim();
  const queryChanged = trimmed !== (state.callsQuery || "").trim();
  if (opts.reset || queryChanged) {
    state.calls = [];
    state.callsHasMore = false;
    state.callsCursor = null;
  }
  state.callsQuery = text;
  const hadCalls = state.calls.length > 0;
  if (!hadCalls) ui.setCallsStatus("Loading calls…");
  const request = ++callsRequest;
  state.loading.calls = true;
  try {
    const params = { limit: CALL_LIMIT };
    if (trimmed) params.q = trimmed;
    if (append && state.callsCursor) {
      params.beforeTs = state.callsCursor.ts;
      params.beforeKey = state.callsCursor.id;
    }
    const data = await getCalls(target, params);
    if (request !== callsRequest) return state.calls;
    if (state.deviceId !== target) return state.calls;
    state.callsAvailable = true;
    const page = Array.isArray(data && data.calls) ? data.calls : [];
    state.calls = append ? mergeCalls(state.calls, page) : mergeCalls(
      queryChanged || opts.reset ? [] : state.calls, page);
    state.callsLoaded = true;
    if (append || queryChanged || opts.reset || !hadCalls) {
      state.callsHasMore = Boolean(data && data.has_more);
    }
    const oldest = state.calls[state.calls.length - 1];
    state.callsCursor = oldest
      ? { ts: oldest.started_at, id: String(oldest.id) }
      : null;
    renderCallsList();
    ui.setCallsStatus("");
    return state.calls;
  } catch (err) {
    if (request !== callsRequest) return state.calls;
    // A backend without the calls feature answers 404. That is a device
    // the relay cannot show calls for, not a crash.
    if (err && err.status === 404) {
      state.callsAvailable = false;
      state.calls = [];
      state.callsHasMore = false;
      state.callsCursor = null;
      state.callsLoaded = true;
      state.liveCall = null;
      ui.renderLiveCall(null);
      renderCallsList();
      ui.setCallsStatus("");
      return state.calls;
    }
    ui.setCallsStatus("Could not load the calls.");
    throw err;
  } finally {
    state.loading.calls = false;
  }
}

export async function loadMoreCalls(deviceId) {
  if (state.loading.calls || !state.callsHasMore) return state.calls;
  return loadCalls(deviceId || state.deviceId, { append: true });
}

// The live call of the device. An idle device answers a null call, and a
// backend without the feature answers 404: both hide the card.
export async function loadLiveCall(deviceId) {
  const target = String(deviceId || state.deviceId || "");
  if (!target) {
    state.liveCall = null;
    ui.renderLiveCall(null);
    return null;
  }
  try {
    const data = await getLiveCall(target);
    if (state.deviceId !== target) return state.liveCall;
    const call = data && data.call ? data.call : null;
    state.liveCall = call;
    state.callsAvailable = true;
    ui.renderLiveCall(call);
    return call;
  } catch (err) {
    if (state.deviceId !== target) return state.liveCall;
    if (err && err.status === 404) {
      // The live endpoint is missing: only the card is hidden. The log
      // has its own flag, so a missing live route never turns a working
      // history into "not available".
      state.liveCall = null;
      ui.renderLiveCall(null);
      return null;
    }
    throw err;
  }
}

// The closest a call can be answered or hung up: the request is
// idempotent through the client_ref, so a retry never runs twice.
// The retry of a failed attempt must reuse the client_ref of that
// attempt, or the server would create a second action and the phone
// would run it twice. It is kept apart from the in-flight guard so a
// different action is never blocked by a failed one.
let lastCallAttempt = null;

function sameCallAttempt(pending, target, name, callRef, payload) {
  if (!pending) return false;
  return pending.deviceId === target && pending.action === name
    && pending.callRef === callRef
    && String(pending.payload === null ? "" : pending.payload)
    === String(payload === null ? "" : payload);
}

export async function sendCallAction(deviceId, action, options) {
  const opts = options || {};
  const target = String(deviceId || state.deviceId || "");
  const name = String(action || "");
  if (!target || !name) throw new Error("missing call action");
  const callRef = String(opts.callRef || "");
  const payload = opts.payload === undefined ? null : opts.payload;
  let clientRef = opts.clientRef ? String(opts.clientRef) : "";
  if (!clientRef) {
    clientRef = sameCallAttempt(lastCallAttempt, target, name, callRef, payload)
      ? lastCallAttempt.clientRef : newActionRef();
  }
  lastCallAttempt = {
    deviceId: target,
    action: name,
    callRef,
    payload,
    clientRef,
  };
  state.callActionPending = {
    deviceId: target,
    action: name,
    callRef,
    payload,
    clientRef,
  };
  ui.setCallActionsBusy(true);
  ui.setCallError("");
  try {
    const data = await postCallAction(target, {
      callRef,
      action: name,
      payload,
      clientRef,
    });
    // The action was accepted: the next tap of the same control is a new
    // command with a new ref.
    lastCallAttempt = null;
    return data;
  } catch (err) {
    if (err && err.status === 409) {
      // The call ended between the tap and the delivery: the ref is
      // stale and never applies to the next call.
      lastCallAttempt = null;
      ui.setCallError("That call has already ended.");
      await loadLiveCall(target).catch(() => {});
      throw err;
    }
    if (err && err.status === 410) {
      lastCallAttempt = null;
      ui.setCallError("This call action expired. Try again.");
      await loadLiveCall(target).catch(() => {});
      throw err;
    }
    if (err && err.payload && err.payload.detail
        && err.payload.detail.error === "emergency_number") {
      lastCallAttempt = null;
      ui.setCallError("Emergency numbers cannot be dialled remotely.");
      throw err;
    }
    if (err && err.status === 429) {
      ui.setCallError("Too many call actions. Wait a moment and retry.");
      throw err;
    }
    // A network or server failure keeps the attempt: retrying the same
    // control reuses its ref and can never run the action twice.
    throw err;
  } finally {
    state.callActionPending = null;
    ui.setCallActionsBusy(false);
  }
}

// Opening the Calls tab clears the missed badge of that device. The
// newest timestamp already marked is remembered, so re-entering the tab
// is a no-op instead of a write on every navigation.
export async function markCallsRead(deviceId, untilTs) {
  const target = String(deviceId || state.deviceId || "");
  if (!target) return null;
  const value = Number(untilTs);
  const until = Number.isFinite(value) && value > 0
    ? Math.floor(value) : callsUnreadUntil();
  if (!until) return null;
  if (until <= state.callsReadMarkedTs) return null;
  try {
    const data = await postCallsRead(target, until);
    if (state.deviceId !== target) return data;
    state.callsReadMarkedTs = until;
    for (const call of state.calls) {
      if (call && !call.is_read && Number(call.started_at) <= until) {
        call.is_read = true;
      }
    }
    renderCallsList();
    const device = currentDevice();
    if (device && Number(device.missed)) {
      device.missed = 0;
      ui.renderChatsHeader(device);
    }
    return data;
  } catch (err) {
    if (err instanceof AuthError) throw err;
    // A backend without the calls feature answers 404 here. Nothing to
    // clear, and the badge of the device is the next refresh's business.
    return null;
  }
}

async function refreshAfterSend() {
  const deviceId = state.deviceId;
  try {
    await loadChats(deviceId, state.query);
  } catch (err) {
    // The send itself succeeded; a stale list can wait for the next poll.
  }
  if (deviceId && state.thread && !document.hidden) {
    try {
      await loadThread(deviceId, state.thread);
    } catch (err) {
      // Same rule: never turn a successful send into a retry because the
      // refresh failed.
    }
  }
}

export async function sendMessage(options) {
  const data = options || {};
  const deviceId = String(data.deviceId || state.deviceId || "");
  const thread = String(data.thread || "");
  const body = String(data.body || "");
  const clientRef = String(data.clientRef || "");
  const subId = Number.isInteger(data.subId) ? data.subId : 0;
  const confirmUnknown = Boolean(data.confirmUnknown);
  if (!deviceId) throw new Error("missing device");
  state.sending = true;
  try {
    const result = await apiPost(
      "devices/" + encodeURIComponent(deviceId) + "/chats/"
      + encodeURIComponent(thread) + "/messages",
      {
        body,
        client_ref: clientRef,
        sub_id: subId,
        confirm_unknown: confirmUnknown,
      });
    state.pending = state.pending.filter(
      (event) => event.client_ref !== clientRef);
    if (state.lastFailed && state.lastFailed.clientRef === clientRef) {
      state.lastFailed = null;
    }
    await refreshAfterSend();
    return result;
  } catch (err) {
    const pending = state.pending.find(
      (event) => event.client_ref === clientRef);
    if (pending) pending.status = "failed";
    // Keep the exact client_ref and body: a retry must reuse both, or the
    // server would create a second SMS.
    state.lastFailed = {
      deviceId,
      thread,
      body,
      clientRef,
      subId,
      confirmUnknown,
      status: err && typeof err.status === "number" ? err.status : 0,
    };
    if (state.threadMeta) {
      ui.renderThread({
        meta: state.threadMeta,
        events: state.events,
        hasMore: state.hasMore,
        pending: state.pending,
        deviceId,
        mode: "merge",
      });
    }
    throw err;
  } finally {
    state.sending = false;
  }
}

export async function retryLastSend() {
  const failed = state.lastFailed;
  if (!failed) return null;
  return sendMessage({
    deviceId: failed.deviceId,
    thread: failed.thread,
    body: failed.body,
    clientRef: failed.clientRef,
    subId: failed.subId,
    confirmUnknown: failed.confirmUnknown,
  });
}

// --- the device itself ------------------------------------------------

export async function renameDevice(deviceId, name) {
  const target = String(deviceId || "");
  const data = await apiPatch("devices/" + encodeURIComponent(target),
    { name: String(name || "") });
  await loadDevices();
  return data;
}

export async function revokeDevice(deviceId) {
  const target = String(deviceId || "");
  const data = await apiPost(
    "devices/" + encodeURIComponent(target) + "/revoke", {});
  await loadDevices();
  return data;
}

export async function setDefaultDevice(deviceId) {
  const target = String(deviceId || "");
  const data = await apiPost(
    "devices/" + encodeURIComponent(target) + "/default", {});
  await loadDevices();
  return data;
}

// --- pairing ----------------------------------------------------------

// Resolve the short code the phone shows to the pairing behind it. The
// backend answers the pairing id, so a typed code works exactly like a
// scan without the admin having to copy the whole payload.
export async function pairLookup(scanCode) {
  return apiPost("pair/lookup", {
    scan_code: String(scanCode || ""),
  });
}

export async function pairPreview(pairingId, scanCode) {
  return apiPost("pair/preview", {
    pairing_id: String(pairingId || ""),
    scan_code: String(scanCode || ""),
  });
}

export async function pairApprove(pairingId, scanCode, name) {
  const body = {
    pairing_id: String(pairingId || ""),
    scan_code: String(scanCode || ""),
  };
  const label = String(name || "").trim();
  if (label) body.name = label;
  const data = await apiPost("pair/approve", body);
  await loadDevices();
  return data;
}

export async function pairCancel(pairingId, scanCode) {
  return apiPost("pair/cancel", {
    pairing_id: String(pairingId || ""),
    scan_code: String(scanCode || ""),
  });
}

// --- the revision long poll -------------------------------------------

function schedule(delay) {
  if (pollStopped) return;
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(() => {
    pollTimer = null;
    void pollOnce();
  }, delay);
}

// One refresh per burst. The timer is not restarted for every change, so
// a busy conversation costs one reload instead of one per message.
function scheduleRefresh(delay) {
  if (pollStopped) return;
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    void runRefresh();
  }, delay);
}

async function runRefresh() {
  if (refreshRunning) {
    // A change arrived while the refresh was running; run it again once.
    refreshDirty = true;
    return;
  }
  refreshRunning = true;
  try {
    do {
      refreshDirty = false;
      await refreshFromChange();
    } while (refreshDirty && !pollStopped);
  } catch (err) {
    // Keep the flag: the next poll retries the same refresh.
    refreshDirty = true;
  } finally {
    refreshRunning = false;
  }
}

// Apply the scope of the changes.
//
// A null scope means "cannot tell": everything is reloaded. Otherwise
// only the device of the scope is touched, and a thread is only reloaded
// when the change belongs to the device that is open.
async function refreshFromChange() {
  const scope = state.changedScopes;
  state.changedScopes = [];
  const deviceId = state.deviceId;
  const threadKey = state.thread;

  if (scope === null) {
    await loadDevices();
    if (state.deviceId !== deviceId) return;
    if (deviceId) await loadChats(deviceId, state.query);
    if (state.deviceId !== deviceId) return;
    if (deviceId) {
      await loadLiveCall(deviceId).catch(() => {});
      if (state.deviceId !== deviceId) return;
      if (state.callsLoaded) {
        await loadCalls(deviceId, { query: state.callsQuery }).catch(() => {});
        if (state.deviceId !== deviceId) return;
      }
    }
    if (deviceId && threadKey) {
      await loadThread(deviceId, threadKey);
      const newest = state.events[0];
      if (newest) await markRead(deviceId, threadKey, newest.ts);
    }
    return;
  }

  let devicesDirty = false;
  let chatsDirty = false;
  let threadDirty = false;
  let callsDirty = false;
  for (const item of scope) {
    if (!item) continue;
    const changedDevice = item.device_id ? String(item.device_id) : "";
    const changedThread = item.thread_key ? String(item.thread_key) : null;
    if (!changedDevice) {
      // A global change without a device: everything may have moved.
      devicesDirty = true;
      chatsDirty = true;
      threadDirty = true;
      callsDirty = true;
      break;
    }
    if (changedThread === null) {
      // A device-wide change: the card of that device changed, and a
      // call change has no thread key at all.
      devicesDirty = true;
      if (changedDevice === deviceId) {
        chatsDirty = true;
        threadDirty = true;
        callsDirty = true;
      }
      continue;
    }
    if (changedDevice !== deviceId) {
      // Another device: its thread is never loaded here.
      continue;
    }
    if (threadKey && changedThread === threadKey) {
      threadDirty = true;
    } else {
      chatsDirty = true;
    }
  }

  if (devicesDirty) await loadDevices();
  if (state.deviceId !== deviceId) return;
  if (chatsDirty && deviceId) await loadChats(deviceId, state.query);
  if (state.deviceId !== deviceId) return;
  if (callsDirty && deviceId) {
    // The live card is visible on both tabs, so it always refreshes. The
    // history is only reloaded once it has been opened: a device-wide
    // change of another feature must not cost a request otherwise.
    await loadLiveCall(deviceId).catch(() => {});
    if (state.deviceId !== deviceId) return;
    if (state.callsLoaded) {
      await loadCalls(deviceId, { query: state.callsQuery }).catch(() => {});
    }
  }
  if (state.deviceId !== deviceId) return;
  if (threadDirty && deviceId && threadKey) {
    await loadThread(deviceId, threadKey);
    const newest = state.events[0];
    if (newest) await markRead(deviceId, threadKey, newest.ts);
  }
}

// Remember which conversations changed. The scopes of several polls are
// merged until the next refresh, because every poll only reports the
// changes since the previous one.
function rememberScope(scope) {
  if (!Array.isArray(scope)) {
    // The scope is unknown: the next refresh has to reload everything.
    state.changedScopes = null;
    return;
  }
  if (!Array.isArray(state.changedScopes)) {
    state.changedScopes = scope.slice();
    return;
  }
  for (const item of scope) {
    if (!item) continue;
    const deviceId = item.device_id ? String(item.device_id) : "";
    const threadKey = item.thread_key ? String(item.thread_key) : null;
    const known = state.changedScopes.some((entry) => entry
      && String(entry.device_id) === deviceId
      && (entry.thread_key ? String(entry.thread_key) : null) === threadKey);
    if (!known) {
      state.changedScopes.push({ device_id: deviceId, thread_key: threadKey });
    }
  }
}

async function pollOnce() {
  if (pollStopped) return;
  if (document.hidden) {
    pollStopped = true;
    return;
  }
  try {
    const data = await apiGet(
      "events?rev=" + encodeURIComponent(state.rev) + "&timeout=25");
    backoffIndex = 0;
    if (data.changed) {
      // The refresh reads the current state, not a diff, so the revision
      // can be adopted right away and a lost notification cannot lose
      // data. The scope says which device and conversation changed.
      rememberScope(data.threads);
      refreshDirty = true;
    }
    if (typeof data.rev === "number" && data.rev >= 0) {
      state.rev = data.rev;
    }
    if (refreshDirty) scheduleRefresh(REFRESH_DEBOUNCE_MS);
  } catch (err) {
    if (err instanceof AuthError) {
      pollStopped = true;
      return;
    }
    const delay = BACKOFF_MS[Math.min(backoffIndex, BACKOFF_MS.length - 1)];
    backoffIndex += 1;
    schedule(delay);
    return;
  }
  if (document.hidden) {
    pollStopped = true;
    return;
  }
  schedule(0);
}

export function startPolling() {
  pollArmed = true;
  if (!pollStopped) return;
  pollStopped = false;
  backoffIndex = 0;
  schedule(0);
}

export function stopPolling() {
  pollArmed = false;
  pollStopped = true;
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function pauseForVisibility() {
  if (!pollArmed) return;
  pollStopped = true;
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    pauseForVisibility();
  } else if (pollArmed) {
    startPolling();
  }
});
