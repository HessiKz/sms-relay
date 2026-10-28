// Inbox state, data loading and the revision long poll.
// The backend is the source of truth; this module only decides when to ask.

import { apiGet, apiPost, AuthError } from "./api.js";
import * as ui from "./ui.js";

const PAGE_SIZE = 50;
const CHAT_LIMIT = 100;
const BACKOFF_MS = [1000, 2000, 5000, 15000];

export const state = {
  me: null,
  chats: [],
  query: "",
  thread: null,
  threadMeta: null,
  events: [],
  hasMore: false,
  rev: 0,
  loading: { chats: false, thread: false },
  status: null,
  pending: [],
  lastFailed: null,
  sending: false,
  selectedSubId: 0,
};

let pollTimer = null;
let pollStopped = true;
let pollArmed = false;
let backoffIndex = 0;

export async function loadMe() {
  const data = await apiGet("me");
  state.me = data;
  ui.renderMe(data);
  return data;
}

export function currentSims() {
  const phone = state.status && state.status.phone ? state.status.phone : {};
  return Array.isArray(phone.sims) ? phone.sims : [];
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
  const sims = currentSims();
  const available = sims
    .filter((sim) => sim && Number.isInteger(sim.sub_id))
    .map((sim) => sim.sub_id);
  if (!available.includes(state.selectedSubId)) state.selectedSubId = 0;
  ui.populateSimSelect(sims, state.selectedSubId);
  ui.renderPausedState(Boolean(data && data.paused));
  return data;
}

export async function loadChats(query) {
  const text = typeof query === "string" ? query : state.query;
  state.query = text;
  state.loading.chats = true;
  ui.setListStatus("Loading conversations…");
  try {
    const params = { limit: CHAT_LIMIT };
    const trimmed = text.trim();
    if (trimmed) params.q = trimmed;
    const data = await apiGet("chats", params);
    state.chats = Array.isArray(data.chats) ? data.chats : [];
    ui.renderChatList(state.chats, {
      query: trimmed,
      activeKey: state.thread,
    });
    if (state.chats.length) {
      ui.setListStatus("");
    } else if (trimmed) {
      ui.setListStatus("No conversations match your search.");
    } else {
      ui.setListStatus("No conversations yet.");
    }
    return state.chats;
  } finally {
    state.loading.chats = false;
  }
}

export async function loadThread(key, options) {
  const opts = options || {};
  const append = Boolean(opts.append);
  const beforeTs = opts.before_ts === undefined ? null : opts.before_ts;
  if (!key) throw new Error("missing thread key");
  state.loading.thread = true;
  if (!append) {
    state.thread = key;
    state.threadMeta = null;
    state.events = [];
    state.hasMore = false;
    ui.renderThreadLoading(key);
    ui.showThreadPane();
  }
  const params = { limit: PAGE_SIZE };
  if (beforeTs !== null && beforeTs !== undefined) params.before_ts = beforeTs;
  try {
    const data = await apiGet(
      "chats/" + encodeURIComponent(key) + "/messages", params);
    state.thread = data.thread_key || key;
    state.threadMeta = {
      thread_key: data.thread_key,
      display: data.display,
      name: data.name,
    };
    state.hasMore = Boolean(data.has_more);
    const page = Array.isArray(data.events) ? data.events : [];
    state.events = append ? state.events.concat(page) : page;
    ui.renderThread({
      meta: state.threadMeta,
      events: state.events,
      hasMore: state.hasMore,
      pending: state.pending,
      append,
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

export async function markRead(untilTs) {
  const key = state.thread;
  const value = Number(untilTs);
  if (!key || !Number.isFinite(value) || value < 0) return null;
  const data = await apiPost(
    "chats/" + encodeURIComponent(key) + "/read",
    { until_ts: Math.floor(value) });
  const chat = state.chats.find((item) => item.thread_key === key);
  if (chat) {
    chat.unread = 0;
    ui.renderChatList(state.chats, {
      query: state.query,
      activeKey: state.thread,
    });
  }
  return data;
}

async function refreshAfterSend() {
  try {
    await loadChats(state.query);
  } catch (err) {
    // The send itself succeeded; a stale list can wait for the next poll.
  }
  if (state.thread && !document.hidden) {
    try {
      await loadThread(state.thread);
    } catch (err) {
      // Same rule: never turn a successful send into a retry because the
      // refresh failed.
    }
  }
}

export async function sendMessage(options) {
  const data = options || {};
  const thread = String(data.thread || "");
  const body = String(data.body || "");
  const clientRef = String(data.clientRef || "");
  const subId = Number.isInteger(data.subId) ? data.subId : 0;
  const confirmUnknown = Boolean(data.confirmUnknown);
  state.sending = true;
  try {
    const result = await apiPost(
      "chats/" + encodeURIComponent(thread) + "/messages",
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
        append: false,
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
    thread: failed.thread,
    body: failed.body,
    clientRef: failed.clientRef,
    subId: failed.subId,
    confirmUnknown: failed.confirmUnknown,
  });
}

function schedule(delay) {
  if (pollStopped) return;
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(() => {
    pollTimer = null;
    void pollOnce();
  }, delay);
}

async function refreshFromChange() {
  await loadChats(state.query);
  if (!state.thread || document.hidden) return;
  try {
    await loadThread(state.thread);
  } catch (err) {
    if (err && err.status === 404) return;
    throw err;
  }
  const newest = state.events[0];
  if (newest) await markRead(newest.ts);
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
      // Refresh first: if it fails, keep the old revision so the next
      // poll retries the same change instead of silently skipping it.
      await refreshFromChange();
    }
    if (typeof data.rev === "number" && data.rev >= 0) {
      state.rev = data.rev;
    }
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
}

function pauseForVisibility() {
  if (!pollArmed) return;
  pollStopped = true;
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    pauseForVisibility();
  } else if (pollArmed) {
    startPolling();
  }
});
