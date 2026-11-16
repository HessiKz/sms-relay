// Bootstrap, routing and the composer of the device-first Mini App.
//
// The routes carry the device, so a conversation can never be shown
// under a device it does not belong to:
//
//   #/                              the devices
//   #/d/<device_id>                 the conversations of that device
//   #/d/<device_id>/t/<thread_key>  a thread of that device

import * as tg from "./telegram.js?v=20261116-scan";
import * as store from "./store.js";
import * as ui from "./ui.js";
import { AuthError, NetworkError } from "./api.js";

const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search");
const deviceList = document.getElementById("device-list");
const chatList = document.getElementById("chat-list");
const timeline = document.getElementById("timeline");
const addDeviceButton = document.getElementById("add-device-button");
const chatsBack = document.getElementById("chats-back");
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
const newChatSuggestions = document.getElementById("new-chat-suggestions");
const deviceMenuCancel = document.getElementById("device-menu-cancel");
const deviceRenameName = document.getElementById("device-rename-name");
const deviceRenameSave = document.getElementById("device-rename-save");
const deviceDefault = document.getElementById("device-default");
const deviceRevoke = document.getElementById("device-revoke");
const pairScan = document.getElementById("pair-scan");
const pairForm = document.getElementById("pair-form");
const pairCancel = document.getElementById("pair-cancel");

let searchTimer = null;
let suggestTimer = null;
let composerDraft = null;
let dialogDraft = null;
let dialogSending = false;
let pairDraft = null;
let deviceTimer = null;

// --- the routes ---------------------------------------------------------

function decodePart(value) {
  try {
    return decodeURIComponent(String(value || ""));
  } catch (err) {
    // A broken escape sequence is not a route.
    return "";
  }
}

function parseRoute() {
  const raw = String(window.location.hash || "");
  if (!raw.startsWith("#/")) return { name: "devices" };
  const parts = raw.slice(2).split("/");
  if (parts[0] !== "d") return { name: "devices" };
  const deviceId = decodePart(parts[1]);
  if (!deviceId) return { name: "devices" };
  if (parts[2] === "t") {
    const threadKey = decodePart(parts.slice(3).join("/"));
    if (threadKey) return { name: "thread", deviceId, threadKey };
  }
  return { name: "chats", deviceId };
}

function deviceHash(deviceId) {
  return "#/d/" + encodeURIComponent(String(deviceId || ""));
}

function threadHash(deviceId, threadKey) {
  return deviceHash(deviceId) + "/t/" + encodeURIComponent(String(threadKey || ""));
}

function navigate(hash) {
  const target = String(hash || "#/");
  if (window.location.hash === target) {
    void applyRoute();
    return;
  }
  window.location.hash = target;
}

// --- the deep links ------------------------------------------------------

// The bot links to a device and a thread with two query parameters.
function deepLink() {
  const result = { deviceId: "", threadKey: "" };
  try {
    const params = new URLSearchParams(window.location.search);
    result.deviceId = String(params.get("device") || "").trim();
    result.threadKey = String(params.get("thread") || "").trim();
  } catch (err) {
    // A malformed query string is no link.
  }
  if (!result.deviceId) result.deviceId = startValue("device");
  if (!result.threadKey) result.threadKey = startValue("thread");
  return result;
}

// A start parameter may carry the device and the thread as well.
function startValue(prefix) {
  for (const value of tg.startParams()) {
    const raw = String(value || "").trim();
    if (raw.startsWith(prefix + "_")) return raw.slice(prefix.length + 1);
    if (raw.startsWith(prefix + ":")) return raw.slice(prefix.length + 1);
  }
  return "";
}

// A pairing code may arrive with the link of the bot.
function pairPayload() {
  for (const value of tg.startParams()) {
    const raw = String(value || "").trim();
    if (raw.indexOf("pair_") === 0) return raw;
  }
  try {
    const value = new URLSearchParams(window.location.search).get("pair");
    if (value && value.trim()) return value.trim();
  } catch (err) {
    // No link, no pairing.
  }
  return "";
}

// --- the screens ---------------------------------------------------------

async function openDevices() {
  ui.setRoute("devices");
  tg.setBackButton(null);
  composerDraft = null;
  if (composerBody) composerBody.value = "";
  store.selectDevice("");
  ui.renderChatsHeader(null);
  ui.renderThreadContext(null);
  refreshComposer();
  try {
    await store.loadDevices();
  } catch (err) {
    handleError(err);
  }
}

async function openChats(deviceId) {
  const target = String(deviceId || "");
  const device = store.deviceById(target);
  ui.setRoute("chats");
  ui.renderChatsHeader(device);
  tg.setBackButton(() => navigate("#/"));
  const changed = store.state.deviceId !== target;
  if (changed) {
    store.selectDevice(target);
    if (searchInput) searchInput.value = "";
    try {
      await store.loadChats(target, "");
    } catch (err) {
      handleError(err);
    }
  } else if (!store.state.chatsLoaded) {
    // A deep link can open a thread without ever loading the
    // conversations of that device.
    try {
      await store.loadChats(target, store.state.query);
    } catch (err) {
      handleError(err);
    }
  } else {
    ui.renderChatList(store.state.chats, {
      query: store.state.query,
      activeKey: store.state.thread,
      hasMore: store.state.chatsHasMore,
    });
  }
  refreshComposer();
}

async function openThread(deviceId, threadKey) {
  const target = String(deviceId || "");
  const key = String(threadKey || "");
  ui.setRoute("thread");
  ui.renderThreadContext(store.deviceById(target));
  tg.setBackButton(() => navigate(deviceHash(target)));
  composerDraft = null;
  if (composerBody) composerBody.value = "";
  try {
    await store.loadThread(target, key);
    ui.renderThreadContext(store.currentDevice());
    refreshComposer();
    const newest = store.state.events[0];
    if (newest) {
      try {
        await store.markRead(target, key, newest.ts);
      } catch (err) {
        if (err instanceof AuthError) throw err;
        // A failed read receipt is not worth hiding the conversation.
      }
    }
  } catch (err) {
    if (err && err.status === 404) {
      navigate(deviceHash(target));
      return;
    }
    throw err;
  }
}

async function applyRoute() {
  const target = parseRoute();
  try {
    if (target.name === "thread") {
      await openThread(target.deviceId, target.threadKey);
    } else if (target.name === "chats") {
      await openChats(target.deviceId);
    } else {
      await openDevices();
    }
  } catch (err) {
    handleError(err);
  }
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
    navigate("#/");
    return;
  }
  ui.setListStatus("Could not load the conversations.");
}

// --- the composer --------------------------------------------------------

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

function composerRefFor(deviceId, thread, body) {
  if (!composerDraft || composerDraft.deviceId !== deviceId
      || composerDraft.thread !== thread
      || composerDraft.body !== body) {
    composerDraft = { deviceId, thread, body, clientRef: newClientRef() };
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
      case "device_not_active":
        return "This device cannot send messages any more.";
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
  const device = store.currentDevice();
  const reason = ui.readOnlyReason(device);
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
    canRetry: Boolean(failed && failed.deviceId === store.state.deviceId
      && failed.thread === store.state.thread && failed.body === text),
    disabled: Boolean(reason),
    disabledReason: reason,
    inputDisabled: Boolean(device && (device.archived || device.revoked)),
  });
}

function renderPending() {
  if (!store.state.threadMeta) return;
  ui.renderThread({
    meta: store.state.threadMeta,
    events: store.state.events,
    hasMore: store.state.hasMore,
    pending: store.state.pending,
    deviceId: store.state.deviceId,
    mode: "merge",
  });
}

function addPending(deviceId, thread, body, clientRef) {
  // Newest-first, matching the store contract for renderThread().
  store.state.pending = store.state.pending.filter(
    (event) => event.client_ref !== clientRef);
  store.state.pending.unshift({
    id: "local:" + clientRef,
    device_id: deviceId,
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
        deviceId: params.deviceId,
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
          deviceId: params.deviceId,
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
  const deviceId = store.state.deviceId;
  const thread = store.state.thread;
  const body = composerBody ? composerBody.value : "";
  if (!deviceId || !thread || store.state.sending) return;
  const device = store.currentDevice();
  if (!store.deviceCanSend(device)) {
    refreshComposer({ error: ui.readOnlyReason(device) });
    return;
  }
  if (!body.trim()) {
    refreshComposer({ error: "Type a message first." });
    return;
  }
  const draft = composerRefFor(deviceId, thread, body);
  addPending(deviceId, thread, body, draft.clientRef);
  renderPending();
  refreshComposer({ sending: true, error: "" });
  tg.impact("light");

  let result;
  try {
    result = await deliver({
      deviceId,
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
      deviceId: failed.deviceId,
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

// --- the new conversation ------------------------------------------------

function closeNewChat() {
  ui.hideNewChatDialog();
  ui.setNewChatError("");
  ui.clearSuggestions();
  if (newChatNumber) newChatNumber.value = "";
  if (newChatBody) newChatBody.value = "";
  dialogDraft = null;
  updateClosingConfirmation();
}

async function openNewChat() {
  const deviceId = store.state.deviceId;
  if (!deviceId) return;
  if (!store.deviceCanSend(store.currentDevice())) {
    ui.setListStatus(ui.readOnlyReason(store.currentDevice()));
    return;
  }
  ui.showNewChatDialog();
  ui.populateSimSelect(store.currentSims(), store.state.selectedSubId);
  updateClosingConfirmation();
  try {
    const contacts = await store.loadContacts(deviceId, "");
    ui.renderSuggestions(contacts);
  } catch (err) {
    // The suggestions are optional: the number can be typed.
  }
}

async function sendFromNewChat(event) {
  if (event) event.preventDefault();
  if (dialogSending) return;
  const deviceId = store.state.deviceId;
  const typed = newChatNumber ? newChatNumber.value.trim() : "";
  const body = newChatBody ? newChatBody.value : "";
  if (!deviceId) return;
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
      deviceId,
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
  navigate(threadHash(deviceId, threadKey));
}

// --- the device menu -----------------------------------------------------

function openDeviceMenu(device) {
  ui.showDeviceMenu(device);
}

async function renameSelectedDevice() {
  const deviceId = ui.deviceMenuDeviceId();
  const device = store.deviceById(deviceId);
  const name = deviceRenameName ? deviceRenameName.value.trim() : "";
  if (!device) return;
  if (!name) {
    ui.setDeviceMenuError("The name must not be empty.");
    return;
  }
  ui.setDeviceMenuBusy(true);
  ui.setDeviceMenuError("");
  try {
    await store.renameDevice(device.id, name);
    ui.setDeviceMenuError("");
    ui.hideDeviceMenu();
    tg.notify("success");
    syncDeviceContext();
  } catch (err) {
    ui.setDeviceMenuError(errorMessage(err));
    tg.notify("error");
  } finally {
    ui.setDeviceMenuBusy(false);
  }
}

async function setDefaultDevice() {
  const deviceId = ui.deviceMenuDeviceId();
  if (!deviceId) return;
  ui.setDeviceMenuBusy(true);
  ui.setDeviceMenuError("");
  try {
    await store.setDefaultDevice(deviceId);
    ui.hideDeviceMenu();
    tg.notify("success");
    syncDeviceContext();
  } catch (err) {
    ui.setDeviceMenuError(errorMessage(err));
    tg.notify("error");
  } finally {
    ui.setDeviceMenuBusy(false);
  }
}

async function revokeSelectedDevice() {
  const deviceId = ui.deviceMenuDeviceId();
  if (!deviceId) return;
  const device = store.deviceById(deviceId);
  const name = device ? String(device.name || device.id) : "this device";
  const confirmed = await tg.confirmPopup(
    "Revoke " + name + "?",
    "The phone stops sending and receiving for this device, and the "
    + "messages it still has to send fail. You have to pair the phone "
    + "again to use it.",
    "Revoke");
  if (!confirmed) return;
  ui.setDeviceMenuBusy(true);
  ui.setDeviceMenuError("");
  try {
    await store.revokeDevice(deviceId);
    ui.hideDeviceMenu();
    tg.notify("success");
    syncDeviceContext();
  } catch (err) {
    ui.setDeviceMenuError(errorMessage(err));
    tg.notify("error");
  } finally {
    ui.setDeviceMenuBusy(false);
  }
}

// --- the pairing sheet ---------------------------------------------------

function pairErrorMessage(err) {
  const detail = err && err.payload ? err.payload.detail : null;
  const text = typeof detail === "string" ? detail : "";
  if (err instanceof AuthError) {
    return "Telegram rejected this session. Reopen the app from the bot.";
  }
  if (err instanceof NetworkError) {
    return "Connection problem. Try again when you are back online.";
  }
  if (err && err.status === 404) {
    return "This pairing code is not known. Scan the code of the phone "
      + "again.";
  }
  if (err && err.status === 410) {
    return "This pairing code has expired. Start the pairing on the "
      + "phone again.";
  }
  if (err && err.status === 409) {
    if (/claimed/i.test(text)) {
      return "This pairing code was already used. Start the pairing on "
        + "the phone again.";
    }
    if (/already paired/i.test(text)) {
      return "This device is already paired. Revoke it first.";
    }
    if (/archived/i.test(text)) {
      return "This device is archived and cannot be paired again.";
    }
    return "This pairing code cannot be approved. Start the pairing on "
      + "the phone again.";
  }
  if (text) return text;
  return "The pairing code could not be checked.";
}

async function previewPairing(payload) {
  const token = tg.pairToken(payload);
  if (!token) {
    pairDraft = null;
    ui.hidePairPreview();
    ui.setPairError("That is not a pairing code. Scan the QR code of the "
      + "phone, or paste the whole code (pair_…).");
    return;
  }
  ui.setPairError("");
  ui.setPairBusy(true);
  try {
    const preview = await store.pairPreview(token.pairingId, token.scanCode);
    pairDraft = {
      pairingId: token.pairingId,
      scanCode: token.scanCode,
    };
    ui.renderPairPreview({
      name: preview.name,
      platform: preview.platform,
      app_version: preview.app_version,
      expires_at: preview.expires_at,
      scan_code: token.scanCode,
    });
  } catch (err) {
    pairDraft = null;
    ui.hidePairPreview();
    ui.setPairError(pairErrorMessage(err));
  } finally {
    ui.setPairBusy(false);
  }
}

function openPairSheet(payload) {
  pairDraft = null;
  ui.showPairSheet();
  if (payload) {
    ui.setPairCode(payload);
    void previewPairing(payload);
  }
}

async function scanPairing() {
  if (!tg.scanQrAvailable()) {
    ui.setPairError("This Telegram client cannot scan a code. Paste the "
      + "pairing code instead.");
    return;
  }
  const text = await tg.showScanQr();
  if (!text) return;
  ui.setPairCode(text);
  await previewPairing(text);
}

async function approvePairing() {
  if (!pairDraft) return;
  ui.setPairBusy(true);
  ui.setPairError("");
  try {
    const result = await store.pairApprove(
      pairDraft.pairingId, pairDraft.scanCode);
    pairDraft = null;
    ui.hidePairSheet();
    ui.hidePairPreview();
    tg.notify("success");
    const device = result && result.device ? result.device : null;
    if (device && device.id) navigate(deviceHash(device.id));
  } catch (err) {
    ui.setPairError(pairErrorMessage(err));
    tg.notify("error");
  } finally {
    ui.setPairBusy(false);
  }
}

// --- the device context --------------------------------------------------

function syncDeviceContext() {
  const device = store.currentDevice();
  ui.renderChatsHeader(device);
  ui.renderThreadContext(device);
  refreshComposer();
}

// --- the events ----------------------------------------------------------

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
    if (first && store.state.deviceId) {
      navigate(threadHash(store.state.deviceId, first.thread_key));
    }
  });
}

async function runSearch() {
  const deviceId = store.state.deviceId;
  if (!deviceId) return;
  try {
    // The search never leaves the device of the route.
    await store.loadChats(deviceId, searchInput ? searchInput.value : "");
  } catch (err) {
    handleError(err);
  }
}

async function loadMoreChats() {
  if (store.state.loading.chats || !store.state.chatsHasMore) return;
  const deviceId = store.state.deviceId;
  if (!deviceId) return;
  try {
    await store.loadChats(deviceId, store.state.query, { append: true });
  } catch (err) {
    handleError(err);
  }
}

if (chatList) {
  chatList.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const more = target ? target.closest("#load-more-chats") : null;
    if (more) {
      void loadMoreChats();
      return;
    }
    const button = target ? target.closest("[data-thread-key]") : null;
    if (!button) return;
    const key = button.getAttribute("data-thread-key");
    if (key && store.state.deviceId) {
      navigate(threadHash(store.state.deviceId, key));
    }
  });
}

if (deviceList) {
  deviceList.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const menu = target.closest("[data-device-menu]");
    if (menu) {
      const id = menu.getAttribute("data-device-menu");
      const device = store.deviceById(id);
      if (device) openDeviceMenu(device);
      return;
    }
    const button = target.closest("[data-device-id]");
    if (!button) return;
    const id = button.getAttribute("data-device-id");
    if (id) navigate(deviceHash(id));
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
  newChatButton.addEventListener("click", () => {
    void openNewChat();
  });
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

if (newChatNumber) {
  newChatNumber.addEventListener("input", () => {
    const deviceId = store.state.deviceId;
    if (!deviceId) return;
    if (suggestTimer) clearTimeout(suggestTimer);
    suggestTimer = setTimeout(() => {
      suggestTimer = null;
      void store.loadContacts(deviceId, newChatNumber.value.trim())
        .then((contacts) => ui.renderSuggestions(contacts))
        .catch(() => {});
    }, 250);
  });
}

if (newChatSuggestions) {
  newChatSuggestions.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const button = target ? target.closest("[data-suggestion]") : null;
    if (!button) return;
    const number = button.getAttribute("data-suggestion");
    if (number && newChatNumber) newChatNumber.value = number;
  });
}

if (timeline) {
  timeline.addEventListener("click", async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const button = target ? target.closest("#load-older") : null;
    if (!button || store.state.loading.thread) return;
    const events = store.state.events;
    if (!events.length) return;
    // The cursor has two halves: the timestamp and the id of the oldest
    // loaded event. Events that share that second are not skipped.
    const oldest = events[events.length - 1];
    ui.setThreadStatus("Loading older messages…");
    try {
      await store.loadThread(store.state.deviceId, store.state.thread, {
        before_ts: oldest.ts,
        before_id: oldest.id,
        append: true,
      });
    } catch (err) {
      ui.setThreadStatus("Could not load older messages.");
      handleError(err);
    }
  });
}

if (addDeviceButton) {
  addDeviceButton.addEventListener("click", () => openPairSheet(""));
}

if (chatsBack) {
  chatsBack.addEventListener("click", () => navigate("#/"));
}

if (backButton) {
  backButton.addEventListener("click", () => {
    const deviceId = store.state.deviceId;
    if (deviceId) navigate(deviceHash(deviceId));
    else navigate("#/");
  });
}

if (deviceMenuCancel) {
  deviceMenuCancel.addEventListener("click", () => ui.hideDeviceMenu());
}

if (deviceRenameSave) {
  deviceRenameSave.addEventListener("click", () => {
    void renameSelectedDevice();
  });
}

if (deviceDefault) {
  deviceDefault.addEventListener("click", () => {
    void setDefaultDevice();
  });
}

if (deviceRevoke) {
  deviceRevoke.addEventListener("click", () => {
    void revokeSelectedDevice();
  });
}

if (pairScan) {
  pairScan.addEventListener("click", () => {
    void scanPairing();
  });
}

if (pairForm) {
  pairForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void previewPairing(ui.pairCodeValue());
  });
}

if (pairCancel) {
  pairCancel.addEventListener("click", () => {
    pairDraft = null;
    ui.hidePairSheet();
  });
}

const pairApproveButton = document.getElementById("pair-approve");
if (pairApproveButton) {
  pairApproveButton.addEventListener("click", () => {
    void approvePairing();
  });
}

window.addEventListener("hashchange", () => {
  void applyRoute();
});

// The device list is refreshed on its own: the online dot, the unread
// count and the sending switch of a phone change without a revision of
// the inbox.
function startDeviceTimer() {
  if (deviceTimer) return;
  deviceTimer = setInterval(() => {
    if (document.hidden) return;
    void store.loadDevices().catch(() => {});
  }, 30000);
}

store.onDevicesChanged(() => {
  syncDeviceContext();
});

async function boot() {
  tg.ready();
  if (!tg.initData) {
    ui.showSplash("Open from Telegram",
      "This app only works inside the Telegram app.");
    return;
  }
  ui.showSplash("Loading devices…", "Checking your Telegram session.");
  try {
    await store.loadMe();
    try {
      // Status drives the paused banner. It is optional: a phone that
      // has never polled still has its conversations.
      await store.loadStatus();
    } catch (err) {
      if (err instanceof AuthError) throw err;
    }
    await store.loadDevices();
    ui.hideSplash();
    startDeviceTimer();

    const payload = pairPayload();
    if (payload) openPairSheet(payload);

    const route = parseRoute();
    const link = deepLink();
    if (route.name !== "devices") {
      await applyRoute();
    } else if (link.deviceId) {
      navigate(link.threadKey
        ? threadHash(link.deviceId, link.threadKey)
        : deviceHash(link.deviceId));
    } else {
      await openDevices();
    }
    store.startPolling();
  } catch (err) {
    if (err instanceof AuthError) {
      handleError(err);
    } else if (err instanceof NetworkError) {
      ui.showSplash("Connection problem",
        "Could not reach the relay. Check your connection and try again.");
    } else {
      ui.showSplash("Something went wrong", "The app could not be loaded.");
    }
  }
}

void boot();
