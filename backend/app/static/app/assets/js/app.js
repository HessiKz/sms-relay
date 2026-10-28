// Bootstrap, routing and the composer for the Mini App.

import * as tg from "./telegram.js";
import * as store from "./store.js";
import * as ui from "./ui.js";
import { AuthError, NetworkError } from "./api.js";

const HASH_PREFIX = "#/t/";
const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search");
const chatList = document.getElementById("chat-list");
const timeline = document.getElementById("timeline");
const backButton = document.getElementById("back-button");
const composerForm = document.getElementById("composer");
const composerBody = document.getElementById("composer-body");
const composerRetry = document.getElementById("composer-retry");
const composerSim = document.getElementById("composer-sim");
const newChatButton = document.getElementById("new-chat-button");
const newChatForm = document.getElementById("new-chat-form");
const newChatNumber = document.getElementById("new-chat-number");
const newChatBody = document.getElementById("new-chat-body");
const newChatSim = document.getElementById("new-chat-sim");
const newChatCancel = document.getElementById("new-chat-cancel");
const newChatSend = document.getElementById("new-chat-send");

let searchTimer = null;
let composerDraft = null;
let dialogDraft = null;
let dialogSending = false;

function keyFromHash() {
  if (!window.location.hash.startsWith(HASH_PREFIX)) return null;
  try {
    return decodeURIComponent(window.location.hash.slice(HASH_PREFIX.length));
  } catch (err) {
    return null;
  }
}

function startParamKey(raw) {
  let value = String(raw || "").trim();
  if (!value) return null;
  if (value.startsWith("thread:")) value = value.slice("thread:".length);
  else if (value.startsWith("thread_")) value = value.slice("thread_".length);
  return value || null;
}

function initialThreadKey() {
  let fromQuery = "";
  try {
    const value = new URLSearchParams(window.location.search).get("thread");
    fromQuery = typeof value === "string" ? value.trim() : "";
  } catch (err) {
    fromQuery = "";
  }
  if (fromQuery) return fromQuery;
  return startParamKey(tg.startParam) || startParamKey(tg.initStartParam);
}

function clearHash() {
  try {
    window.history.replaceState(null, "",
      window.location.pathname + window.location.search);
  } catch (err) {
    // Keep the current hash if the client refuses history updates.
  }
}

function showList() {
  ui.showListPane();
  tg.setBackButton(null);
  if (window.location.hash) clearHash();
}

function handleError(err) {
  if (err instanceof AuthError) {
    store.stopPolling();
    tg.setBackButton(null);
    ui.showSplash("Not signed in",
      "Telegram rejected this session. Reopen the app from the bot.");
    return;
  }
  if (err instanceof NetworkError) {
    ui.setListStatus("Connection problem. Retrying…");
    return;
  }
  if (err && err.status === 404) {
    showList();
    return;
  }
  ui.setListStatus("Could not load the inbox.");
}

function newClientRef() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
  } catch (err) {
    // Fall through to a manual random id.
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

function composerRefFor(thread, body) {
  if (!composerDraft || composerDraft.thread !== thread
      || composerDraft.body !== body) {
    composerDraft = { thread, body, clientRef: newClientRef() };
  }
  return composerDraft;
}

function dialogRefFor(thread, body) {
  if (!dialogDraft || dialogDraft.thread !== thread
      || dialogDraft.body !== body) {
    dialogDraft = { thread, body, clientRef: newClientRef() };
  }
  return dialogDraft;
}

function isUnknownNumber(err) {
  return Boolean(err && err.status === 409 && err.payload
    && err.payload.detail
    && err.payload.detail.error === "unknown_number");
}

function errorMessage(err) {
  const detail = err && err.payload && err.payload.detail;
  if (detail && typeof detail === "object") {
    switch (detail.error) {
      case "unknown_number":
        return "Confirm this new number before sending.";
      case "rate_limited":
        return detail.window === "day"
          ? "Daily send limit reached. Try again tomorrow."
          : "Too many messages. Wait a minute and retry.";
      case "validation_error": {
        const fields = Array.isArray(detail.fields) ? detail.fields : [];
        return fields.length ? fields.join("; ")
          : "Check the number and the message.";
      }
      case "unsendable_thread":
        return "This sender cannot receive SMS.";
      case "invalid_destination":
        return "That does not look like a phone number.";
      case "client_ref_conflict":
        return "This send conflicts with another message.";
      default:
        if (typeof detail.error === "string") return detail.error;
    }
  }
  if (typeof detail === "string") return detail;
  if (err && err.status === 429) {
    return "Too many messages. Wait a minute and retry.";
  }
  if (err instanceof NetworkError) {
    return "Connection problem. Retry when you are back online.";
  }
  return "The message could not be sent.";
}

function refreshComposer(options) {
  const opts = options || {};
  const text = composerBody ? composerBody.value : "";
  const failed = store.state.lastFailed;
  ui.renderComposerState({
    sending: opts.sending === undefined
      ? Boolean(store.state.sending)
      : Boolean(opts.sending),
    bodyLength: text.length,
    parts: ui.messageParts(text),
    sims: store.currentSims(),
    selectedSubId: store.state.selectedSubId,
    paused: Boolean(store.state.status && store.state.status.paused),
    error: opts.error || "",
    canRetry: Boolean(failed && failed.thread === store.state.thread
      && failed.body === text),
  });
}

function renderPending() {
  if (!store.state.threadMeta) return;
  ui.renderThread({
    meta: store.state.threadMeta,
    events: store.state.events,
    hasMore: store.state.hasMore,
    pending: store.state.pending,
    append: false,
  });
}

function addPending(thread, body, clientRef) {
  // Newest-first, matching the store contract for renderThread().
  store.state.pending = store.state.pending.filter(
    (event) => event.client_ref !== clientRef);
  store.state.pending.unshift({
    id: "local:" + clientRef,
    thread_key: thread,
    direction: "out",
    body,
    client_ref: clientRef,
    status: "pending",
    ts: Math.floor(Date.now() / 1000),
    local: true,
  });
}

function updateClosingConfirmation() {
  const hasDraft = Boolean(composerBody && composerBody.value.trim());
  tg.setClosingConfirmation(hasDraft || ui.newChatDialogOpen());
}

// One send attempt with the unknown-number confirmation loop. The caller
// owns the client_ref; every retry passes the exact same one in.
async function deliver(params) {
  try {
    return {
      ok: true,
      data: await store.sendMessage({
        thread: params.thread,
        body: params.body,
        clientRef: params.clientRef,
        subId: params.subId,
        confirmUnknown: params.confirmUnknown,
      }),
    };
  } catch (err) {
    if (err instanceof AuthError) throw err;
    if (!isUnknownNumber(err)) {
      return { ok: false, error: errorMessage(err) };
    }
    const detail = err.payload.detail;
    const label = detail.display || detail.thread_key || params.thread;
    const confirmed = await tg.confirmPopup(
      "New number",
      "Send this SMS to " + String(label) + "?",
      "Send");
    if (!confirmed) {
      return { ok: false, error: "Not sent: the number was not confirmed." };
    }
    try {
      return {
        ok: true,
        data: await store.sendMessage({
          thread: params.thread,
          body: params.body,
          clientRef: params.clientRef,
          subId: params.subId,
          confirmUnknown: true,
        }),
      };
    } catch (retryErr) {
      if (retryErr instanceof AuthError) throw retryErr;
      return { ok: false, error: errorMessage(retryErr) };
    }
  }
}

async function sendFromComposer(event) {
  if (event) event.preventDefault();
  const thread = store.state.thread;
  const body = composerBody ? composerBody.value : "";
  if (!thread || store.state.sending) return;
  if (!body.trim()) {
    refreshComposer({ error: "Type a message first." });
    return;
  }
  const draft = composerRefFor(thread, body);
  addPending(thread, body, draft.clientRef);
  renderPending();
  refreshComposer({ sending: true, error: "" });
  tg.impact("light");

  let result;
  try {
    result = await deliver({
      thread,
      body,
      clientRef: draft.clientRef,
      subId: store.state.selectedSubId,
      confirmUnknown: false,
    });
  } catch (err) {
    handleError(err);
    result = { ok: false, error: "" };
  }

  if (result.ok) {
    composerDraft = null;
    if (composerBody) composerBody.value = "";
    tg.notify("success");
  } else {
    tg.notify("error");
  }
  refreshComposer({ error: result.error || "" });
  updateClosingConfirmation();
}

async function retryComposerSend() {
  const failed = store.state.lastFailed;
  if (!failed || store.state.sending) return;
  const pending = store.state.pending.find(
    (event) => event.client_ref === failed.clientRef);
  if (pending) {
    pending.status = "pending";
    renderPending();
  }
  refreshComposer({ sending: true, error: "" });
  tg.impact("light");

  let result;
  try {
    result = await deliver({
      thread: failed.thread,
      body: failed.body,
      clientRef: failed.clientRef,
      subId: failed.subId,
      confirmUnknown: failed.confirmUnknown,
    });
  } catch (err) {
    handleError(err);
    result = { ok: false, error: "" };
  }

  if (result.ok) {
    composerDraft = null;
    if (composerBody && composerBody.value === failed.body) {
      composerBody.value = "";
    }
    tg.notify("success");
  } else {
    tg.notify("error");
  }
  refreshComposer({ error: result.error || "" });
  updateClosingConfirmation();
}

function closeNewChat() {
  ui.hideNewChatDialog();
  ui.setNewChatError("");
  if (newChatNumber) newChatNumber.value = "";
  if (newChatBody) newChatBody.value = "";
  dialogDraft = null;
  updateClosingConfirmation();
}

function openNewChat() {
  ui.showNewChatDialog();
  ui.populateSimSelect(store.currentSims(), store.state.selectedSubId);
  updateClosingConfirmation();
}

async function sendFromNewChat(event) {
  if (event) event.preventDefault();
  if (dialogSending) return;
  const typed = newChatNumber ? newChatNumber.value.trim() : "";
  const body = newChatBody ? newChatBody.value : "";
  if (!typed) {
    ui.setNewChatError("Enter a phone number.");
    return;
  }
  if (!body.trim()) {
    ui.setNewChatError("Type a message first.");
    return;
  }
  const draft = dialogRefFor(typed, body);
  dialogSending = true;
  if (newChatSend) newChatSend.disabled = true;
  ui.setNewChatError("");
  tg.impact("light");

  let result;
  try {
    result = await deliver({
      thread: typed,
      body,
      clientRef: draft.clientRef,
      subId: store.state.selectedSubId,
      confirmUnknown: false,
    });
  } catch (err) {
    handleError(err);
    result = { ok: false, error: "" };
  } finally {
    dialogSending = false;
    if (newChatSend) newChatSend.disabled = false;
  }

  if (!result.ok) {
    tg.notify("error");
    ui.setNewChatError(result.error || "The message could not be sent.");
    return;
  }

  const threadKey = result.data && result.data.thread_key
    ? String(result.data.thread_key)
    : typed;
  tg.notify("success");
  closeNewChat();
  try {
    await store.loadChats(store.state.query);
  } catch (err) {
    // The conversation itself is already queued; the list can catch up.
  }
  openThread(threadKey);
}

async function handleThread(key) {
  try {
    await store.loadThread(key);
    ui.showThreadPane();
    tg.setBackButton(showList);
    refreshComposer();
    const newest = store.state.events[0];
    if (newest) {
      try {
        await store.markRead(newest.ts);
      } catch (err) {
        if (err instanceof AuthError) throw err;
        // A failed read receipt is not worth hiding the conversation.
      }
    }
  } catch (err) {
    if (err && err.status === 404) {
      showList();
      return;
    }
    throw err;
  }
}

async function route() {
  const key = keyFromHash();
  if (!key) {
    showList();
    return;
  }
  try {
    await handleThread(key);
  } catch (err) {
    handleError(err);
  }
}

function openThread(key) {
  if (!key) return;
  const target = HASH_PREFIX + encodeURIComponent(key);
  tg.hapticSelection();
  if (window.location.hash === target) {
    void route();
  } else {
    window.location.hash = target;
  }
}

async function runSearch() {
  try {
    await store.loadChats(searchInput ? searchInput.value : "");
  } catch (err) {
    handleError(err);
  }
}

if (searchForm) {
  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (searchTimer) {
      clearTimeout(searchTimer);
      searchTimer = null;
    }
    void runSearch();
  });
}

if (searchInput) {
  searchInput.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      searchTimer = null;
      void runSearch();
    }, 250);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (searchTimer) {
      clearTimeout(searchTimer);
      searchTimer = null;
    }
    const first = store.state.chats[0];
    if (first) openThread(first.thread_key);
  });
}

if (chatList) {
  chatList.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const button = target ? target.closest("[data-thread-key]") : null;
    if (!button) return;
    const key = button.getAttribute("data-thread-key");
    if (key) openThread(key);
  });
}

if (composerForm) {
  composerForm.addEventListener("submit", (event) => {
    void sendFromComposer(event);
  });
}

if (composerBody) {
  composerBody.addEventListener("input", () => {
    if (!composerBody.value) composerDraft = null;
    refreshComposer();
    updateClosingConfirmation();
  });
}

if (composerRetry) {
  composerRetry.addEventListener("click", () => {
    void retryComposerSend();
  });
}

for (const select of [composerSim, newChatSim]) {
  if (!select) continue;
  select.addEventListener("change", () => {
    store.setSelectedSubId(select.value);
    refreshComposer();
  });
}

if (newChatButton) {
  newChatButton.addEventListener("click", () => openNewChat());
}

if (newChatCancel) {
  newChatCancel.addEventListener("click", () => closeNewChat());
}

if (newChatForm) {
  newChatForm.addEventListener("submit", (event) => {
    void sendFromNewChat(event);
  });
}

for (const input of [newChatNumber, newChatBody]) {
  if (!input) continue;
  input.addEventListener("input", () => ui.setNewChatError(""));
}

if (timeline) {
  timeline.addEventListener("click", async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const button = target ? target.closest("#load-older") : null;
    if (!button || store.state.loading.thread) return;
    const events = store.state.events;
    if (!events.length) return;
    const oldest = events[events.length - 1];
    ui.setThreadStatus("Loading older messages…");
    try {
      await store.loadThread(store.state.thread, {
        before_ts: oldest.ts,
        append: true,
      });
    } catch (err) {
      ui.setThreadStatus("Could not load older messages.");
      handleError(err);
    }
  });
}

if (backButton) {
  backButton.addEventListener("click", () => showList());
}

window.addEventListener("hashchange", () => {
  void route();
});

async function boot() {
  tg.ready();
  if (!tg.initData) {
    ui.showSplash("Open from Telegram",
      "This inbox only works inside the Telegram app.");
    return;
  }
  ui.showSplash("Loading inbox…", "Checking your Telegram session.");
  try {
    await store.loadMe();
    try {
      // Status drives the SIM pickers and the paused indicator. It is
      // optional: a phone that has never polled still has an inbox.
      await store.loadStatus();
    } catch (err) {
      if (err instanceof AuthError) throw err;
    }
    refreshComposer();
    await store.loadChats("");
    ui.hideSplash();
    const initial = keyFromHash();
    const deepLink = initialThreadKey();
    if (initial) {
      await route();
    } else if (deepLink) {
      window.location.hash = HASH_PREFIX + encodeURIComponent(deepLink);
    } else {
      showList();
    }
    store.startPolling();
  } catch (err) {
    if (err instanceof AuthError) {
      handleError(err);
    } else if (err instanceof NetworkError) {
      ui.showSplash("Connection problem",
        "Could not reach the relay. Check your connection and try again.");
    } else {
      ui.showSplash("Something went wrong", "The inbox could not be loaded.");
    }
  }
}

void boot();
