// DOM rendering for the device-first Mini App.
// Every user-controlled string goes through textContent or createTextNode;
// nothing here interprets message text as markup or links.
//
// The screens are: the device list, the conversations of one device and
// the thread of one device. A row of the device list belongs to a device,
// a chat row to a device and a thread, and a bubble to a thread. The
// caches are cleared when the device changes, so a row of one device can
// never be reused for another one.

const deviceListEl = document.getElementById("device-list");
const devicesStatusEl = document.getElementById("devices-status");
const chatListEl = document.getElementById("chat-list");
const timelineEl = document.getElementById("timeline");
const listStatusEl = document.getElementById("list-status");
const threadStatusEl = document.getElementById("timeline-status");
const threadNameEl = document.getElementById("thread-name");
const threadNumberEl = document.getElementById("thread-number");
const threadDeviceEl = document.getElementById("thread-device");
const threadBannerEl = document.getElementById("thread-banner");
const chatsDeviceNameEl = document.getElementById("chats-device-name");
const chatsDeviceStatusEl = document.getElementById("chats-device-status");
const chatsBannerEl = document.getElementById("chats-banner");
const meLineEl = document.getElementById("me-line");
const splashEl = document.getElementById("splash");
const splashTitleEl = document.getElementById("splash-title");
const splashDetailEl = document.getElementById("splash-detail");
const appEl = document.getElementById("app");
const pausedIndicatorEl = document.getElementById("paused-indicator");
const composerBodyEl = document.getElementById("composer-body");
const composerMetaEl = document.getElementById("composer-meta");
const composerPausedEl = document.getElementById("composer-paused");
const composerDisabledEl = document.getElementById("composer-disabled");
const composerErrorEl = document.getElementById("composer-error");
const composerRetryEl = document.getElementById("composer-retry");
const composerSendEl = document.getElementById("composer-send");
const composerSimEl = document.getElementById("composer-sim");
const composerSimRowEl = document.getElementById("composer-sim-row");
const deviceMenuEl = document.getElementById("device-menu");
const deviceMenuTitleEl = document.getElementById("device-menu-title");
const deviceMenuSubEl = document.getElementById("device-menu-sub");
const deviceMenuErrorEl = document.getElementById("device-menu-error");
const deviceRenameNameEl = document.getElementById("device-rename-name");
const deviceRenameSaveEl = document.getElementById("device-rename-save");
const deviceDefaultEl = document.getElementById("device-default");
const deviceRevokeEl = document.getElementById("device-revoke");
const pairSheetEl = document.getElementById("pair-sheet");
const pairCodeEl = document.getElementById("pair-code");
const pairErrorEl = document.getElementById("pair-error");
const pairPreviewEl = document.getElementById("pair-preview");
const pairPreviewNameEl = document.getElementById("pair-preview-name");
const pairPreviewMetaEl = document.getElementById("pair-preview-meta");
const pairPreviewCodeEl = document.getElementById("pair-preview-code");
const pairApproveEl = document.getElementById("pair-approve");
const newChatDialogEl = document.getElementById("new-chat-dialog");
const newChatNumberEl = document.getElementById("new-chat-number");
const newChatErrorEl = document.getElementById("new-chat-error");
const newChatSimEl = document.getElementById("new-chat-sim");
const newChatSimRowEl = document.getElementById("new-chat-sim-row");
const newChatSuggestionsEl = document.getElementById("new-chat-suggestions");

// Node caches. A row is built once and reused: a refresh only changes
// the text and the status of the rows that actually changed, so the
// timeline is not rebuilt and the scroll position survives.
const threadNodes = new Map();   // event id -> row
const dayNodes = new Map();      // day key -> separator
const chatNodes = new Map();     // thread key -> row
const deviceNodes = new Map();   // device id -> row
let loadOlderRow = null;
let loadMoreChatsRow = null;
let renderedScope = null;
let menuDeviceId = "";
const NEAR_BOTTOM_PX = 80;

const timeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
});
const shortDateFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
});
const dayFormatter = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
});
const dayWithYearFormatter = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
});

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function secondsToDate(ts) {
  const value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return null;
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatTime(ts) {
  const date = secondsToDate(ts);
  if (!date) return "";
  return timeFormatter.format(date);
}

function formatListTime(ts) {
  const date = secondsToDate(ts);
  if (!date) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return timeFormatter.format(date);
  }
  return shortDateFormatter.format(date);
}

function formatRelative(ts) {
  const date = secondsToDate(ts);
  if (!date) return "never";
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return Math.floor(seconds / 60) + " min ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + " h ago";
  if (seconds < 604800) return Math.floor(seconds / 86400) + " d ago";
  return shortDateFormatter.format(date);
}

function dayKey(ts) {
  const date = secondsToDate(ts);
  if (!date) return "unknown";
  return date.getFullYear() + "-" + date.getMonth() + "-" + date.getDate();
}

function dayLabel(ts) {
  const date = secondsToDate(ts);
  if (!date) return "";
  const now = new Date();
  const formatter = date.getFullYear() === now.getFullYear()
    ? dayFormatter
    : dayWithYearFormatter;
  return formatter.format(date);
}

function initials(name) {
  const text = String(name || "").trim();
  if (!text) return "#";
  const words = text.split(/\s+/).filter(Boolean).slice(0, 2);
  const letters = words.map((word) => Array.from(word)[0] || "");
  const value = letters.join("").toUpperCase();
  return value || "#";
}

function statusName(status) {
  switch (status) {
    case "pending":
    case "sent":
    case "delivered":
    case "failed":
    case "rejected":
      return status;
    default:
      return "pending";
  }
}

function statusLabel(status) {
  switch (status) {
    case "pending":
      return "queued";
    case "sent":
      return "sent";
    case "delivered":
      return "delivered";
    case "failed":
    case "rejected":
      return "failed";
    default:
      return "queued";
  }
}

function statusTick(status) {
  switch (status) {
    case "sent":
      return "✓";
    case "delivered":
      return "✓✓";
    case "failed":
    case "rejected":
      return "✕";
    default:
      return "";
  }
}

// GSM 03.38 basic and extension tables. A basic character costs one
// septet, an extension character two; anything outside the table forces
// the whole message into UCS-2.
const GSM7_BASIC =
  "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r"
  + "\u00c5\u00e5\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a3\u0398\u039e"
  + "\u00c6\u00e6\u00df\u00c9 !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
  + "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7\u00bf"
  + "abcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0";
const GSM7_EXTENSION = "\f^{}\\[~]|\u20ac";

function gsm7Length(text) {
  let total = 0;
  for (const character of Array.from(String(text || ""))) {
    if (GSM7_EXTENSION.indexOf(character) !== -1) {
      total += 2;
    } else if (GSM7_BASIC.indexOf(character) !== -1) {
      total += 1;
    } else {
      return -1;
    }
  }
  return total;
}

// Estimated number of SMS parts, matching the encoder split sizes.
export function messageParts(text) {
  const value = String(text || "");
  if (!value) return 0;
  const septets = gsm7Length(value);
  if (septets >= 0) {
    return septets <= 160 ? 1 : Math.ceil(septets / 153);
  }
  return value.length <= 70 ? 1 : Math.ceil(value.length / 67);
}

// Reconcile the children of a container with the wanted list. Nodes that
// are already in place are left alone, so a poll does not make the list
// flicker or throw away the scroll position. The rows that have to move
// are collected in a fragment and inserted in one go.
function reconcile(parent, desired) {
  const wanted = new Set(desired);
  for (const child of Array.from(parent.children)) {
    if (!wanted.has(child)) parent.removeChild(child);
  }
  let cursor = parent.firstChild;
  let fragment = null;
  for (const node of desired) {
    if (cursor === node) {
      if (fragment) {
        parent.insertBefore(fragment, cursor);
        fragment = null;
      }
      cursor = cursor.nextSibling;
      continue;
    }
    if (!fragment) fragment = document.createDocumentFragment();
    fragment.append(node);
  }
  if (fragment) parent.appendChild(fragment);
}

function prune(cache, live) {
  for (const key of Array.from(cache.keys())) {
    if (!live.has(key)) cache.delete(key);
  }
}

function nearBottom() {
  if (!timelineEl) return true;
  const distance = timelineEl.scrollHeight - timelineEl.scrollTop
    - timelineEl.clientHeight;
  return distance <= NEAR_BOTTOM_PX;
}

// --- the device list ---------------------------------------------------

function deviceStatusLine(device) {
  if (!device) return "";
  const parts = [];
  parts.push(device.online ? "online" : "offline");
  const platform = String(device.platform || "");
  const version = device.app_version ? " " + String(device.app_version) : "";
  if (platform) parts.push(platform + version);
  const seen = device.last_seen
    ? "last seen " + formatRelative(device.last_seen)
    : "never seen";
  parts.push(seen);
  return parts.join(" · ");
}

// Why the device cannot send anything, in the words of the user.
export function readOnlyReason(device) {
  if (!device) return "";
  if (device.archived) {
    return "This device is archived. Its messages can be read, but it "
      + "cannot send anything.";
  }
  if (device.revoked) {
    return "This device was revoked. Pair the phone again to use it.";
  }
  if (device.sending === false) {
    return "Sending is disabled on this device. Turn it on in the phone "
      + "app, then send.";
  }
  return "";
}

function buildDeviceRow(device) {
  const row = create("li", "device-row");
  const button = create("button", "device-item");
  button.type = "button";
  button.dataset.deviceId = String(device.id);
  const dot = create("span", "status-dot");
  dot.setAttribute("aria-hidden", "true");
  const main = create("span", "device-main");
  const title = create("span", "device-title");
  const badges = create("span", "device-badges");
  const defaultChip = create("span", "chip chip-default", "Default");
  const archivedChip = create("span", "chip chip-archived", "Archived");
  const revokedChip = create("span", "chip chip-revoked", "Revoked");
  const offChip = create("span", "chip chip-off", "Sending off");
  badges.append(defaultChip, archivedChip, revokedChip, offChip);
  const meta = create("span", "device-meta");
  const preview = create("span", "device-preview");
  main.append(title, badges, meta, preview);
  const side = create("span", "device-side");
  const when = create("span", "device-time");
  const badge = create("span", "badge");
  side.append(when, badge);
  button.append(dot, main, side);
  const menu = create("button", "device-menu-button", "⋯");
  menu.type = "button";
  menu.dataset.deviceMenu = String(device.id);
  menu.setAttribute("aria-label", "Device actions");
  row.append(button, menu);
  row.parts = {
    button,
    dot,
    title,
    badges,
    defaultChip,
    archivedChip,
    revokedChip,
    offChip,
    meta,
    preview,
    side,
    time: when,
    badge,
    menu,
  };
  return row;
}

function updateDeviceRow(node, device) {
  const parts = node.parts;
  if (!parts) return;
  const name = String(device.name || device.id || "");
  if (parts.title.textContent !== name) parts.title.textContent = name;
  const dotClass = "status-dot " + (device.online ? "online" : "offline");
  if (parts.dot.className !== dotClass) parts.dot.className = dotClass;
  parts.defaultChip.hidden = !device.default;
  parts.archivedChip.hidden = !device.archived;
  parts.revokedChip.hidden = !device.revoked;
  parts.offChip.hidden = device.sending !== false;
  // An empty row of badges takes no space.
  parts.badges.hidden = parts.defaultChip.hidden && parts.archivedChip.hidden
    && parts.revokedChip.hidden && parts.offChip.hidden;
  const meta = deviceStatusLine(device);
  if (parts.meta.textContent !== meta) parts.meta.textContent = meta;
  const preview = device.preview
    ? String(device.preview) : "No messages yet";
  if (parts.preview.textContent !== preview) parts.preview.textContent = preview;
  const when = device.last_seen ? formatRelative(device.last_seen) : "never";
  if (parts.time.textContent !== when) parts.time.textContent = when;
  const unread = Number(device.unread) || 0;
  if (unread > 0) {
    const text = unread > 99 ? "99+" : String(unread);
    if (parts.badge.textContent !== text) parts.badge.textContent = text;
    const badgeLabel = String(unread) + " unread";
    if (parts.badge.getAttribute("aria-label") !== badgeLabel) {
      parts.badge.setAttribute("aria-label", badgeLabel);
    }
    if (parts.badge.parentNode !== parts.side) parts.side.append(parts.badge);
    parts.button.classList.add("has-unread");
  } else if (parts.badge.parentNode) {
    parts.side.removeChild(parts.badge);
    parts.button.classList.remove("has-unread");
  }
  const label = name + ", " + deviceStatusLine(device);
  if (parts.button.getAttribute("aria-label") !== label) {
    parts.button.setAttribute("aria-label", label);
  }
}

function deviceRow(device) {
  const key = String(device.id);
  let node = deviceNodes.get(key);
  if (!node) {
    node = buildDeviceRow(device);
    deviceNodes.set(key, node);
  }
  updateDeviceRow(node, device);
  return node;
}

let emptyDevicesRow = null;

function emptyDevicesNode() {
  if (!emptyDevicesRow) {
    emptyDevicesRow = create("li", "empty-row");
    emptyDevicesRow.append(
      create("p", "empty-title", "No device paired yet"),
      create("p", "muted", "Open the relay app on the phone, choose "
        + "“Pair this phone” and scan the QR code here."),
    );
  }
  return emptyDevicesRow;
}

export function renderDevices(devices) {
  if (!deviceListEl) return;
  const list = Array.isArray(devices) ? devices : [];
  const desired = [];
  const live = new Set();
  if (!list.length) {
    desired.push(emptyDevicesNode());
  } else {
    for (const device of list) {
      if (!device || !device.id) continue;
      live.add(String(device.id));
      desired.push(deviceRow(device));
    }
  }
  reconcile(deviceListEl, desired);
  prune(deviceNodes, live);
}

export function setDevicesStatus(text) {
  if (devicesStatusEl) devicesStatusEl.textContent = text ? String(text) : "";
}

export function renderChatsHeader(device) {
  ensureCallsSurface();
  renderCallsTabBadge(device);
  if (!device) {
    if (chatsDeviceNameEl) chatsDeviceNameEl.textContent = "Conversations";
    if (chatsDeviceStatusEl) chatsDeviceStatusEl.textContent = "";
    if (chatsBannerEl) chatsBannerEl.hidden = true;
    return;
  }
  if (chatsDeviceNameEl) {
    chatsDeviceNameEl.textContent = String(device.name || device.id || "");
  }
  if (chatsDeviceStatusEl) {
    chatsDeviceStatusEl.textContent = deviceStatusLine(device);
  }
  if (chatsBannerEl) {
    const reason = readOnlyReason(device);
    chatsBannerEl.textContent = reason;
    chatsBannerEl.hidden = !reason;
  }
}

export function renderThreadContext(device) {
  if (threadDeviceEl) {
    threadDeviceEl.textContent = device
      ? String(device.name || device.id || "") : "";
  }
  if (threadBannerEl) {
    const reason = readOnlyReason(device);
    threadBannerEl.textContent = reason;
    threadBannerEl.hidden = !reason;
  }
}

// --- the conversation list ---------------------------------------------

function buildChatRow(chat) {
  const row = create("li", "chat-row");
  const button = create("button", "chat-item");
  button.type = "button";
  button.dataset.threadKey = String(chat.thread_key);
  const avatar = create("span", "avatar");
  avatar.setAttribute("aria-hidden", "true");
  const main = create("span", "chat-main");
  const name = create("span", "chat-name");
  const preview = create("span", "chat-preview");
  main.append(name, preview);
  const side = create("span", "chat-side");
  const when = create("span", "chat-time");
  side.append(when);
  const badge = create("span", "badge");
  button.append(avatar, main, side);
  row.append(button);
  row.parts = {
    button: button,
    avatar: avatar,
    name: name,
    preview: preview,
    time: when,
    side: side,
    badge: badge,
  };
  return row;
}

function updateChatRow(node, chat, activeKey) {
  const parts = node.parts;
  if (!parts) return;
  const name = String(chat.name || "");
  if (parts.name.textContent !== name) parts.name.textContent = name;
  const preview = (chat.last_direction === "out" ? "You: " : "")
    + String(chat.last_body || "");
  if (parts.preview.textContent !== preview) {
    parts.preview.textContent = preview;
  }
  const when = formatListTime(chat.last_ts);
  if (parts.time.textContent !== when) parts.time.textContent = when;
  const avatarText = initials(chat.name);
  if (parts.avatar.textContent !== avatarText) {
    parts.avatar.textContent = avatarText;
  }
  const label = name + ", " + String(chat.display || "");
  if (parts.button.getAttribute("aria-label") !== label) {
    parts.button.setAttribute("aria-label", label);
  }
  const active = Boolean(activeKey) && chat.thread_key === activeKey;
  parts.button.classList.toggle("active", active);
  const unread = Number(chat.unread) || 0;
  if (unread > 0) {
    const text = unread > 99 ? "99+" : String(unread);
    if (parts.badge.textContent !== text) parts.badge.textContent = text;
    const badgeLabel = String(unread) + " unread";
    if (parts.badge.getAttribute("aria-label") !== badgeLabel) {
      parts.badge.setAttribute("aria-label", badgeLabel);
    }
    if (parts.badge.parentNode !== parts.side) parts.side.append(parts.badge);
    parts.button.classList.add("has-unread");
  } else if (parts.badge.parentNode) {
    parts.side.removeChild(parts.badge);
    parts.button.classList.remove("has-unread");
  }
}

function chatRow(chat, activeKey) {
  const key = String(chat.thread_key);
  let node = chatNodes.get(key);
  if (!node) {
    node = buildChatRow(chat);
    chatNodes.set(key, node);
  }
  updateChatRow(node, chat, activeKey);
  return node;
}

function loadMoreChatsButton() {
  if (!loadMoreChatsRow) {
    loadMoreChatsRow = create("li", "load-row");
    const button = create("button", "load-older", "Load more conversations");
    button.type = "button";
    button.id = "load-more-chats";
    loadMoreChatsRow.append(button);
  }
  return loadMoreChatsRow;
}

// The empty rows are cached as well: a poll must not replace them.
let emptyChatsRow = null;
let emptyChatsKey = null;

function emptyChatsNode(query) {
  const key = query ? "search" : "plain";
  if (!emptyChatsRow || emptyChatsKey !== key) {
    emptyChatsRow = create("li", "empty-row", key === "search"
      ? "No conversations match your search."
      : "No messages on this device yet.");
    emptyChatsKey = key;
  }
  return emptyChatsRow;
}

let emptyThreadRow = null;

export function renderChatList(chats, options) {
  if (!chatListEl) return;
  const opts = options || {};
  const activeKey = opts.activeKey || null;
  const list = Array.isArray(chats) ? chats : [];
  const desired = [];
  const live = new Set();
  if (!list.length) {
    desired.push(emptyChatsNode(opts.query));
  } else {
    for (const chat of list) {
      if (!chat || !chat.thread_key) continue;
      live.add(String(chat.thread_key));
      desired.push(chatRow(chat, activeKey));
    }
    if (opts.hasMore) desired.push(loadMoreChatsButton());
  }
  reconcile(chatListEl, desired);
  prune(chatNodes, live);
}

// --- calls -------------------------------------------------------------

// The numbers the backend refuses to dial remotely. They are never shown
// in the "Call via…" sheet and never sent as a dial action: an emergency
// call is placed on the phone itself.
const EMERGENCY_NUMBERS = ["110", "112", "115", "125", "911", "999", "000"];

let tabBarEl = null;
let tabChatsEl = null;
let tabCallsEl = null;
let liveCardEl = null;
let liveNodes = null;
let callsSectionEl = null;
let callListEl = null;
let callsStatusEl = null;
let activeDeviceTab = "chats";
let liveTimer = null;
let currentLiveCall = null;
let liveCallError = "";
const callNodes = new Map();   // call id -> row
let emptyCallsRow = null;
let emptyCallsKey = null;
let loadMoreCallsRow = null;

let callSheetEl = null;
let callSheetParts = null;
let callSheetCall = null;
let viaSheetEl = null;
let viaListEl = null;
let viaSubEl = null;
let keypadEl = null;

export function isEmergencyNumber(number) {
  const digits = String(number || "").replace(/[^0-9]/g, "");
  if (!digits) return false;
  if (EMERGENCY_NUMBERS.indexOf(digits) !== -1) return true;
  const stripped = digits.replace(/^0+/, "");
  return Boolean(stripped) && EMERGENCY_NUMBERS.indexOf(stripped) !== -1;
}

function callKindLabel(kind) {
  switch (kind) {
    case "incoming":
      return "Incoming call";
    case "outgoing":
      return "Outgoing call";
    case "missed":
      return "Missed call";
    case "rejected":
      return "Declined call";
    case "blocked":
      return "Blocked call";
    case "voicemail":
      return "Voicemail";
    default:
      return "Call";
  }
}

// A text glyph, never an image: the CSP only allows 'self' assets and the
// row has to work without any download.
function callGlyph(kind) {
  switch (kind) {
    case "outgoing":
      return "↗";
    case "missed":
      return "✖";
    case "rejected":
      return "⊘";
    case "blocked":
      return "⊗";
    case "voicemail":
      return "▣";
    case "incoming":
    case "unknown":
      return "↙";
    default:
      return "•";
  }
}

function callStateLabel(state) {
  switch (state) {
    case "ringing":
      return "Ringing";
    case "dialing":
      return "Dialing";
    case "active":
      return "On call";
    case "held":
      return "Held";
    case "disconnected":
      return "Call ended";
    default:
      return "Call";
  }
}

function formatClock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  if (hours > 0) return hours + ":" + pad(minutes) + ":" + pad(secs);
  return pad(minutes) + ":" + pad(secs);
}

// The phone sends a phone account label that is often already human text
// ("SIM 1", or the carrier name). Add the prefix only when it is missing.
function simText(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return /^sim\b/i.test(text) ? text : "SIM " + text;
}

function callTitle(call) {
  return String(call.name || call.display || call.number || "Unknown number");
}

// --- the tab header of the device screen -------------------------------

// The calls surface is built here, not in the shell: index.html only
// carries the version query. The nodes are created once and then reused,
// so switching tabs never rebuilds the list.
function ensureCallsSurface() {
  if (tabBarEl || !chatListEl) return Boolean(tabBarEl);
  const pane = chatListEl.parentNode;
  if (!pane) return false;

  liveNodes = buildLiveCard();
  liveCardEl = liveNodes.card;

  tabBarEl = create("div", "tab-bar");
  tabBarEl.id = "tab-bar";
  tabBarEl.setAttribute("role", "tablist");
  tabBarEl.setAttribute("aria-label", "Device sections");
  tabChatsEl = create("button", "tab-button active", "Chats");
  tabChatsEl.type = "button";
  tabChatsEl.id = "tab-chats";
  tabChatsEl.dataset.tab = "chats";
  tabChatsEl.setAttribute("role", "tab");
  tabChatsEl.setAttribute("aria-selected", "true");
  tabChatsEl.setAttribute("aria-controls", "chat-list");
  tabCallsEl = create("button", "tab-button", "Calls");
  tabCallsEl.type = "button";
  tabCallsEl.id = "tab-calls";
  tabCallsEl.dataset.tab = "calls";
  tabCallsEl.setAttribute("role", "tab");
  tabCallsEl.setAttribute("aria-selected", "false");
  tabCallsEl.setAttribute("aria-controls", "call-list");
  tabBarEl.append(tabChatsEl, tabCallsEl);

  callsSectionEl = create("div", "calls-section");
  callsSectionEl.id = "calls-section";
  callsSectionEl.hidden = true;
  callsStatusEl = create("p", "status-line");
  callsStatusEl.id = "calls-status";
  callsStatusEl.setAttribute("role", "status");
  callsStatusEl.setAttribute("aria-live", "polite");
  callListEl = create("ul", "call-list");
  callListEl.id = "call-list";
  callListEl.setAttribute("aria-label", "Call history");
  callsSectionEl.append(callsStatusEl, callListEl);

  // The header keeps the device, then the live card, the tabs, the chat
  // list and the call list of the same device.
  pane.insertBefore(tabBarEl, listStatusEl || chatListEl);
  pane.insertBefore(liveCardEl, tabBarEl);
  pane.insertBefore(callsSectionEl, chatListEl.nextSibling);
  return true;
}

// The missed count of the device summary is the badge of the Calls tab.
function renderCallsTabBadge(device) {
  if (!tabCallsEl) return;
  let badge = tabCallsEl.querySelector(".tab-badge");
  const count = device ? Number(device.missed) || 0 : 0;
  if (count <= 0) {
    if (badge) badge.remove();
    return;
  }
  if (!badge) {
    badge = create("span", "tab-badge");
    tabCallsEl.append(badge);
  }
  const text = count > 99 ? "99+" : String(count);
  if (badge.textContent !== text) badge.textContent = text;
  const label = String(count) + " missed calls";
  if (badge.getAttribute("aria-label") !== label) {
    badge.setAttribute("aria-label", label);
  }
}

// Switch the visible tab of the device screen. The route owns the value,
// so a back/forward gesture always shows the matching tab.
export function setDeviceTab(name) {
  ensureCallsSurface();
  if (!tabBarEl) return;
  const tab = name === "calls" ? "calls" : "chats";
  activeDeviceTab = tab;
  const selected = tab === "calls";
  tabChatsEl.setAttribute("aria-selected", selected ? "false" : "true");
  tabCallsEl.setAttribute("aria-selected", selected ? "true" : "false");
  tabChatsEl.classList.toggle("active", !selected);
  tabCallsEl.classList.toggle("active", selected);
  if (chatListEl) chatListEl.hidden = selected;
  if (listStatusEl) listStatusEl.hidden = selected;
  if (callsSectionEl) callsSectionEl.hidden = !selected;
  const newChat = document.getElementById("new-chat-button");
  if (newChat) newChat.hidden = selected;
  const input = document.getElementById("search");
  if (input) {
    input.placeholder = selected ? "Search calls" : "Search this device";
    input.setAttribute("aria-label",
      selected ? "Search calls" : "Search conversations");
  }
}

export function deviceTab() {
  return activeDeviceTab;
}

// --- the call list -----------------------------------------------------

function buildCallRow(call) {
  const row = create("li", "call-row");
  const button = create("button", "call-item");
  button.type = "button";
  const wrap = create("span", "call-icon-wrap");
  const icon = create("span", "call-icon");
  icon.setAttribute("aria-hidden", "true");
  const dot = create("span", "call-dot");
  dot.setAttribute("aria-hidden", "true");
  wrap.append(icon, dot);
  const main = create("span", "call-main");
  const title = create("span", "call-title");
  const meta = create("span", "call-meta");
  main.append(title, meta);
  const side = create("span", "call-side");
  const when = create("span", "call-time");
  side.append(when);
  button.append(wrap, main, side);
  const more = create("button", "call-more", "⋯");
  more.type = "button";
  more.setAttribute("aria-label", "Call options");
  row.append(button, more);
  row.parts = {
    button,
    icon,
    dot,
    title,
    meta,
    time: when,
    more,
  };
  return row;
}

function updateCallRow(node, call) {
  const parts = node.parts;
  if (!parts) return;
  const kind = String(call.kind || "unknown");
  const title = callTitle(call);
  if (parts.title.textContent !== title) parts.title.textContent = title;
  const iconClass = "call-icon dir-" + kind;
  if (parts.icon.className !== iconClass) parts.icon.className = iconClass;
  const glyph = callGlyph(kind);
  if (parts.icon.textContent !== glyph) parts.icon.textContent = glyph;
  const metaParts = [callKindLabel(kind)];
  const number = String(call.number || "");
  const display = String(call.display || "");
  if (number && number !== title && display !== title) metaParts.push(number);
  const duration = Number(call.duration_s) || 0;
  if (duration > 0) metaParts.push(formatClock(duration));
  // The log row carries `phone_account`; a live session carries
  // `sim_label`. Either one names the line that handled the call.
  const simLine = simText(call.sim_label) || simText(call.phone_account);
  if (simLine) metaParts.push(simLine);
  const meta = metaParts.filter(Boolean).join(" · ");
  if (parts.meta.textContent !== meta) parts.meta.textContent = meta;
  const when = formatRelative(call.started_at);
  if (parts.time.textContent !== when) parts.time.textContent = when;
  const unread = kind === "missed" && !call.is_read;
  parts.dot.hidden = !unread;
  parts.button.classList.toggle("has-unread", unread);
  const label = [callKindLabel(kind), title, when].filter(Boolean).join(", ");
  if (parts.button.getAttribute("aria-label") !== label) {
    parts.button.setAttribute("aria-label", label);
  }
  parts.button.dataset.callId = String(call.id || "");
  const threadKey = call.thread_key ? String(call.thread_key) : "";
  if (threadKey) parts.button.dataset.threadKey = threadKey;
  else delete parts.button.dataset.threadKey;
  parts.more.dataset.callMenu = String(call.id || "");
  parts.more.setAttribute("aria-label", "Options for " + title);
}

function callRow(call) {
  const key = String(call.id);
  let node = callNodes.get(key);
  if (!node) {
    node = buildCallRow(call);
    callNodes.set(key, node);
  }
  updateCallRow(node, call);
  return node;
}

function loadMoreCallsButton() {
  if (!loadMoreCallsRow) {
    loadMoreCallsRow = create("li", "load-row");
    const button = create("button", "load-older", "Load more calls");
    button.type = "button";
    button.id = "load-more-calls";
    loadMoreCallsRow.append(button);
  }
  return loadMoreCallsRow;
}

function emptyCallsNode(key) {
  if (!emptyCallsRow || emptyCallsKey !== key) {
    let text = "No calls yet";
    if (key === "search") text = "No calls match your search.";
    if (key === "unavailable") {
      text = "Calls are not available on this device yet";
    }
    emptyCallsRow = create("li", "empty-row", text);
    emptyCallsKey = key;
  }
  return emptyCallsRow;
}

export function renderCalls(calls, options) {
  ensureCallsSurface();
  if (!callListEl) return;
  const opts = options || {};
  const list = Array.isArray(calls) ? calls : [];
  const desired = [];
  const live = new Set();
  if (opts.available === false) {
    desired.push(emptyCallsNode("unavailable"));
  } else if (!list.length) {
    desired.push(emptyCallsNode(opts.query ? "search" : "plain"));
  } else {
    for (const call of list) {
      if (!call || !call.id) continue;
      live.add(String(call.id));
      desired.push(callRow(call));
    }
    if (opts.hasMore) desired.push(loadMoreCallsButton());
  }
  reconcile(callListEl, desired);
  prune(callNodes, live);
}

export function setCallsStatus(text) {
  ensureCallsSurface();
  if (callsStatusEl) callsStatusEl.textContent = text ? String(text) : "";
}

// --- the live call card ------------------------------------------------

function buildLiveCard() {
  const card = create("section", "live-call");
  card.id = "live-call-card";
  card.setAttribute("aria-live", "polite");
  card.hidden = true;
  const top = create("div", "live-top");
  const icon = create("span", "live-icon");
  icon.setAttribute("aria-hidden", "true");
  const main = create("div", "live-main");
  const title = create("p", "live-title");
  const stateLine = create("p", "live-state");
  main.append(title, stateLine);
  const elapsed = create("p", "live-elapsed");
  top.append(icon, main, elapsed);
  const error = create("p", "live-error");
  error.setAttribute("role", "alert");
  error.hidden = true;
  const actions = create("div", "live-actions");
  const definitions = [
    ["answer", "Answer"],
    ["reject", "Decline"],
    ["hangup", "Hang up"],
    ["mute", "Mute"],
    ["speaker_on", "Speaker on"],
    ["dtmf", "Keypad"],
    ["hold", "Hold"],
  ];
  const byAction = {};
  for (const definition of definitions) {
    const button = create("button", "live-button action-" + definition[0],
      definition[1]);
    button.type = "button";
    button.dataset.callAction = definition[0];
    actions.append(button);
    byAction[definition[0]] = button;
  }
  card.append(top, error, actions);
  return {
    card,
    icon,
    title,
    stateLine,
    elapsed,
    error,
    actions,
    byAction,
  };
}

function setLiveToggle(button, action, label) {
  if (!button) return;
  button.dataset.callAction = action;
  if (button.textContent !== label) button.textContent = label;
}

function liveElapsedSeconds(call) {
  const start = Number(call && call.started_at) || 0;
  if (!start) return 0;
  return Math.max(0, Math.floor(Date.now() / 1000) - start);
}

function updateLiveElapsed() {
  if (!liveNodes) return;
  const call = currentLiveCall;
  const state = call ? String(call.state || "") : "";
  const show = state === "active" || state === "held";
  const text = show ? formatClock(liveElapsedSeconds(call)) : "";
  if (liveNodes.elapsed.textContent !== text) {
    liveNodes.elapsed.textContent = text;
  }
}

function liveTimerWanted() {
  if (!currentLiveCall) return false;
  if (document.hidden) return false;
  const route = appEl ? String(appEl.dataset.route || "") : "";
  if (route !== "chats" && route !== "calls") return false;
  const state = String(currentLiveCall.state || "");
  return state === "active" || state === "held";
}

function syncLiveTimer() {
  if (liveTimerWanted()) {
    if (!liveTimer) liveTimer = setInterval(updateLiveElapsed, 1000);
    updateLiveElapsed();
    return;
  }
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = null;
  }
}

export function stopLiveTimer() {
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = null;
  }
}

export function renderLiveCall(call) {
  ensureCallsSurface();
  currentLiveCall = call && call.call_ref ? call : null;
  if (!liveCardEl || !liveNodes) {
    syncLiveTimer();
    return;
  }
  if (!currentLiveCall) {
    liveCardEl.hidden = true;
    liveNodes.error.hidden = true;
    liveNodes.error.textContent = "";
    liveCallError = "";
    syncLiveTimer();
    return;
  }
  const live = currentLiveCall;
  liveCardEl.hidden = false;
  const state = String(live.state || "");
  const outgoing = live.direction === "outgoing";
  const kind = outgoing ? "outgoing" : "incoming";
  const iconClass = "live-icon dir-" + kind;
  if (liveNodes.icon.className !== iconClass) liveNodes.icon.className = iconClass;
  const glyph = callGlyph(kind);
  if (liveNodes.icon.textContent !== glyph) liveNodes.icon.textContent = glyph;

  const title = callTitle(live);
  if (liveNodes.title.textContent !== title) liveNodes.title.textContent = title;

  const parts = [callStateLabel(state), outgoing ? "outgoing" : "incoming"];
  const sim = simText(live.sim_label);
  if (sim) parts.push(sim);
  const number = String(live.display || live.number || "");
  if (number && number !== title) parts.push(number);
  const stateText = parts.filter(Boolean).join(" · ");
  if (liveNodes.stateLine.textContent !== stateText) {
    liveNodes.stateLine.textContent = stateText;
  }

  const ringing = state === "ringing";
  const onCall = state === "active" || state === "held";
  liveNodes.byAction.answer.hidden = !(ringing && !outgoing);
  liveNodes.byAction.reject.hidden = !ringing;
  liveNodes.byAction.hangup.hidden = !(onCall || state === "dialing");
  liveNodes.byAction.mute.hidden = !onCall;
  liveNodes.byAction.speaker_on.hidden = !onCall;
  liveNodes.byAction.hold.hidden = !onCall;
  liveNodes.byAction.dtmf.hidden = !onCall;
  setLiveToggle(liveNodes.byAction.mute,
    live.muted ? "unmute" : "mute", live.muted ? "Unmute" : "Mute");
  setLiveToggle(liveNodes.byAction.speaker_on,
    live.speaker ? "speaker_off" : "speaker_on",
    live.speaker ? "Speaker off" : "Speaker on");
  setLiveToggle(liveNodes.byAction.hold,
    state === "held" ? "unhold" : "hold",
    state === "held" ? "Unhold" : "Hold");

  const message = liveCallError;
  liveNodes.error.textContent = message;
  liveNodes.error.hidden = !message;
  updateLiveElapsed();
  syncLiveTimer();
}

// Only the buttons that send an action are disabled; the Close/Cancel
// buttons of the sheets stay usable so a slow answer never traps the
// reader.
export function setCallActionsBusy(busy) {
  const value = Boolean(busy);
  for (const root of [liveCardEl, keypadEl]) {
    if (!root) continue;
    for (const button of root.querySelectorAll("button")) {
      button.disabled = value;
    }
  }
}

export function setCallError(text) {
  liveCallError = text ? String(text) : "";
  if (!liveNodes || liveCardEl.hidden) return;
  liveNodes.error.textContent = liveCallError;
  liveNodes.error.hidden = !liveCallError;
}

// --- the call sheets ---------------------------------------------------

// The three sheets are built once, lazily, and appended to the app root:
// the same pattern as the live card, so the shell stays markup-only.
function ensureSheets() {
  if (callSheetEl && viaSheetEl && keypadEl) return;
  const root = appEl || document.body;

  if (!callSheetEl) {
    callSheetEl = create("div", "dialog-backdrop");
    callSheetEl.id = "call-sheet";
    callSheetEl.hidden = true;
    const card = create("div", "dialog-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "call-sheet-title");
    const title = create("h2", "dialog-title");
    title.id = "call-sheet-title";
    const sub = create("p", "muted");
    sub.id = "call-sheet-sub";
    const note = create("p", "dialog-error");
    note.id = "call-sheet-note";
    note.setAttribute("role", "alert");
    note.hidden = true;
    const actions = create("div", "dialog-form");
    const callback = create("button", "secondary-button", "Call back");
    callback.type = "button";
    callback.id = "call-sheet-callback";
    const via = create("button", "secondary-button", "Call via…");
    via.type = "button";
    via.id = "call-sheet-via";
    actions.append(callback, via);
    const footer = create("div", "dialog-actions");
    const cancel = create("button", "secondary-button", "Cancel");
    cancel.type = "button";
    cancel.id = "call-sheet-cancel";
    footer.append(cancel);
    card.append(title, sub, note, actions, footer);
    callSheetEl.append(card);
    root.append(callSheetEl);
    callSheetParts = { title, sub, note, callback, via, cancel };
  }

  if (!viaSheetEl) {
    viaSheetEl = create("div", "dialog-backdrop");
    viaSheetEl.id = "call-via-sheet";
    viaSheetEl.hidden = true;
    const card = create("div", "dialog-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "call-via-title");
    const title = create("h2", "dialog-title", "Call via…");
    title.id = "call-via-title";
    viaSubEl = create("p", "muted");
    viaSubEl.id = "call-via-sub";
    viaListEl = create("div", "call-via-list");
    viaListEl.id = "call-via-list";
    const footer = create("div", "dialog-actions");
    const cancel = create("button", "secondary-button", "Cancel");
    cancel.type = "button";
    cancel.id = "call-via-cancel";
    footer.append(cancel);
    card.append(title, viaSubEl, viaListEl, footer);
    viaSheetEl.append(card);
    root.append(viaSheetEl);
  }

  if (!keypadEl) {
    keypadEl = create("div", "dialog-backdrop");
    keypadEl.id = "keypad-sheet";
    keypadEl.hidden = true;
    const card = create("div", "dialog-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "keypad-title");
    const title = create("h2", "dialog-title", "Keypad");
    title.id = "keypad-title";
    const grid = create("div", "keypad-grid");
    grid.id = "keypad-grid";
    const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9",
      "*", "0", "#"];
    for (const key of keys) {
      const button = create("button", "keypad-key", key);
      button.type = "button";
      button.dataset.dtmf = key;
      grid.append(button);
    }
    const footer = create("div", "dialog-actions");
    const close = create("button", "secondary-button", "Close");
    close.type = "button";
    close.id = "keypad-close";
    footer.append(close);
    card.append(title, grid, footer);
    keypadEl.append(card);
    root.append(keypadEl);
  }
}

export function openCallSheet(call) {
  ensureSheets();
  if (!callSheetEl) return;
  callSheetCall = call || {};
  const number = String(callSheetCall.number || "");
  const title = callTitle(callSheetCall);
  callSheetParts.title.textContent = title;
  const meta = [];
  const display = String(callSheetCall.display || "");
  if (display && display !== title) meta.push(display);
  const kind = String(callSheetCall.kind || "");
  if (kind) meta.push(callKindLabel(kind));
  const when = formatRelative(callSheetCall.started_at);
  if (when) meta.push(when);
  callSheetParts.sub.textContent = meta.filter(Boolean).join(" · ");
  const emergency = isEmergencyNumber(number);
  callSheetParts.callback.hidden = !number || emergency;
  callSheetParts.via.hidden = !number || emergency;
  callSheetParts.note.hidden = !emergency;
  callSheetParts.note.textContent = emergency
    ? "Emergency numbers can only be dialled on the phone itself."
    : "";
  callSheetEl.hidden = false;
}

export function closeCallSheet() {
  if (callSheetEl) callSheetEl.hidden = true;
}

export function callSheetNumber() {
  return String((callSheetCall && callSheetCall.number) || "");
}

function e164Digits(number) {
  const digits = String(number || "").replace(/[^0-9]/g, "");
  return digits.length >= 5 ? digits : "";
}

// Every place a number can be opened. The `https` ones are the reliable
// path; `tel`, `viber`, `skype` and `facetime` are best effort and only
// work when the client has an app for them.
function callViaEntries(number) {
  const entries = [{
    key: "tel",
    label: "Phone (SIM)",
    hint: "the phone places the call",
    url: "",
  }];
  const digits = e164Digits(number);
  if (digits) {
    entries.push({
      key: "whatsapp",
      label: "WhatsApp",
      hint: "",
      url: "https://wa.me/" + digits,
    });
    entries.push({
      key: "telegram",
      label: "Telegram",
      hint: "",
      url: "https://t.me/+" + digits,
    });
    entries.push({
      key: "signal",
      label: "Signal",
      hint: "",
      url: "https://signal.me/#p/+" + digits,
    });
    entries.push({
      key: "viber",
      label: "Viber",
      hint: "",
      url: "viber://chat?number=%2B" + digits,
    });
    entries.push({
      key: "skype",
      label: "Skype",
      hint: "",
      url: "skype:" + digits + "?call",
    });
    entries.push({
      key: "facetime",
      label: "FaceTime",
      hint: "iOS only",
      url: "facetime://+" + digits,
    });
  }
  entries.push({ key: "bale", label: "Bale", hint: "", url: "https://ble.ir/" });
  entries.push({
    key: "rubika",
    label: "Rubika",
    hint: "",
    url: "https://rubika.ir/",
  });
  return entries;
}

export function openCallViaSheet(options) {
  ensureSheets();
  if (!viaSheetEl) return;
  const data = options || {};
  const call = callSheetCall || {};
  const number = String(data.number || call.number || "");
  // An emergency number never reaches this sheet: the local dialer is
  // the only place that may place it.
  if (!number || isEmergencyNumber(number)) {
    closeCallVia();
    return;
  }
  const name = String(data.name || call.name || call.display || "");
  viaSubEl.textContent = [name, number].filter(Boolean).join(" · ");
  const desired = [];
  for (const entry of callViaEntries(number)) {
    const button = create("button", "call-via-item");
    button.type = "button";
    button.dataset.callVia = entry.key;
    if (entry.url) button.dataset.callViaUrl = entry.url;
    const label = create("span", "call-via-label", entry.label);
    button.append(label);
    if (entry.hint) {
      button.append(create("span", "call-via-hint", entry.hint));
    }
    desired.push(button);
  }
  viaListEl.replaceChildren();
  for (const button of desired) viaListEl.append(button);
  // One dialog at a time; the call of the sheet is kept so "Call back"
  // and the tel entry still know the number.
  if (callSheetEl) callSheetEl.hidden = true;
  viaSheetEl.hidden = false;
}

export function closeCallVia() {
  if (viaSheetEl) viaSheetEl.hidden = true;
}

export function openKeypad() {
  ensureSheets();
  if (keypadEl) keypadEl.hidden = false;
}

export function closeKeypad() {
  if (keypadEl) keypadEl.hidden = true;
}

// --- the timeline -------------------------------------------------------

function eventKey(event) {
  if (!event) return "";
  const id = event.id !== undefined && event.id !== null
    ? event.id : event.event_id;
  return id === undefined || id === null ? "" : String(id);
}

function isOutboundDirection(direction) {
  const value = String(direction || "");
  return value === "out" || value === "outgoing";
}

function buildCallBubble(event) {
  const outbound = isOutboundDirection(event.direction);
  const row = create("li", outbound ? "row out" : "row in");
  const bubble = create("div", "bubble bubble-call");
  const head = create("p", "bubble-call-head");
  const icon = create("span", "call-icon");
  icon.setAttribute("aria-hidden", "true");
  const text = create("span", "bubble-call-text");
  head.append(icon, text);
  const metaRow = create("p", "bubble-meta");
  const time = create("span", "bubble-time", formatTime(event.ts));
  metaRow.append(time);
  const callback = create("button", "bubble-call-back", "Call back");
  callback.type = "button";
  bubble.append(head, metaRow, callback);
  row.append(bubble);
  row.parts = { icon, text, time, callback };
  return row;
}

function callEventText(event) {
  const kind = String(event.call_kind || "unknown");
  const duration = Number(event.duration_s) || 0;
  const parts = [callKindLabel(kind)];
  if (duration > 0) parts.push(formatClock(duration));
  return parts.join(" · ");
}

function updateCallBubble(node, event) {
  const parts = node.parts;
  if (!parts) return;
  const kind = String(event.call_kind || "unknown");
  const iconClass = "call-icon dir-" + kind;
  if (parts.icon.className !== iconClass) parts.icon.className = iconClass;
  const glyph = callGlyph(kind);
  if (parts.icon.textContent !== glyph) parts.icon.textContent = glyph;
  const text = callEventText(event);
  if (parts.text.textContent !== text) parts.text.textContent = text;
  const when = formatTime(event.ts);
  if (parts.time.textContent !== when) parts.time.textContent = when;
  const number = String(event.number || "");
  // The attribute is the hook of the delegated listener in app.js; the
  // element itself never carries the number as markup.
  parts.callback.setAttribute("data-call-back", number);
  const title = String(event.number || "");
  const label = "Call back" + (title ? " " + title : "");
  if (parts.callback.getAttribute("aria-label") !== label) {
    parts.callback.setAttribute("aria-label", label);
  }
  parts.callback.hidden = !number || isEmergencyNumber(number);
}

function buildEventRow(event) {
  if (event.kind === "call") return buildCallBubble(event);
  const outbound = event.direction === "out";
  const row = create("li", outbound ? "row out" : "row in");
  if (event.local && event.id) row.dataset.localId = String(event.id);
  const bubble = create("div", "bubble");
  const body = create("p", "bubble-body", String(event.body || ""));
  bubble.append(body);
  const metaRow = create("p", "bubble-meta");
  const time = create("span", "bubble-time", formatTime(event.ts));
  metaRow.append(time);
  const parts = { body: body, time: time, tick: null, status: null };
  if (outbound) {
    const tick = create("span", "bubble-tick");
    tick.setAttribute("aria-hidden", "true");
    metaRow.append(tick);
    const status = create("span", "bubble-status");
    metaRow.append(status);
    parts.tick = tick;
    parts.status = status;
  }
  bubble.append(metaRow);
  row.append(bubble);
  row.parts = parts;
  return row;
}

function updateEventRow(node, event) {
  const parts = node.parts;
  if (!parts) return;
  if (event.kind === "call") {
    updateCallBubble(node, event);
    return;
  }
  const text = String(event.body === undefined || event.body === null
    ? "" : event.body);
  if (parts.body.textContent !== text) parts.body.textContent = text;
  const when = formatTime(event.ts);
  if (parts.time.textContent !== when) parts.time.textContent = when;
  if (!parts.status) return;
  const label = event.status === "failed" ? "failed" : statusLabel(event.status);
  if (parts.status.textContent !== label) parts.status.textContent = label;
  const className = "bubble-status status-" + statusName(event.status);
  if (parts.status.className !== className) parts.status.className = className;
  const tick = statusTick(event.status);
  if (parts.tick.textContent !== tick) parts.tick.textContent = tick;
}

function eventRow(event) {
  const key = eventKey(event);
  let node = threadNodes.get(key);
  if (!node) {
    node = buildEventRow(event);
    threadNodes.set(key, node);
  }
  updateEventRow(node, event);
  return node;
}

function daySeparator(key, ts) {
  let node = dayNodes.get(key);
  if (!node) {
    node = create("li", "day-separator", dayLabel(ts));
    node.setAttribute("role", "presentation");
    dayNodes.set(key, node);
  }
  return node;
}

function loadOlderButton() {
  if (!loadOlderRow) {
    loadOlderRow = create("li", "load-row");
    const button = create("button", "load-older", "Load older messages");
    button.type = "button";
    button.id = "load-older";
    loadOlderRow.append(button);
  }
  return loadOlderRow;
}

export function renderThread(state) {
  if (!timelineEl) return;
  const data = state || {};
  const meta = data.meta || {};
  const events = Array.isArray(data.events) ? data.events : [];
  const mode = data.mode || "reset";
  const deviceId = data.deviceId ? String(data.deviceId) : "";
  if (threadNameEl) threadNameEl.textContent = String(meta.name || "");
  if (threadNumberEl) threadNumberEl.textContent = String(meta.display || "");

  const threadKey = meta.thread_key || null;
  // A row belongs to a device and to a thread: the scope of the cache is
  // both, so the rows of one device are never reused for another.
  const scope = deviceId + "|" + String(threadKey || "");
  if (scope !== renderedScope) {
    threadNodes.clear();
    dayNodes.clear();
    renderedScope = scope;
  }

  // Optimistic outbound bubbles live in the store until the server has
  // accepted them. They are newest-first there, so putting them before
  // the newest-first events and reversing the lot keeps them at the end
  // of the timeline. A server event with the same client_ref wins, which
  // avoids showing a bubble twice when a long poll lands mid-send.
  const serverRefs = new Set();
  for (const event of events) {
    if (event && event.client_ref) serverRefs.add(event.client_ref);
  }
  const pendingRaw = Array.isArray(data.pending) ? data.pending : [];
  const pending = pendingRaw.filter((event) => {
    if (!event) return false;
    if (deviceId && event.device_id && String(event.device_id) !== deviceId) {
      return false;
    }
    if (threadKey && event.thread_key !== threadKey) return false;
    return !serverRefs.has(event.client_ref);
  });

  const ordered = pending.concat(events).slice().reverse();
  const desired = [];
  const live = new Set();
  const liveDays = new Set();
  if (data.hasMore) desired.push(loadOlderButton());
  let lastDay = "";
  for (const event of ordered) {
    const key = dayKey(event.ts);
    if (key !== lastDay) {
      desired.push(daySeparator(key, event.ts));
      liveDays.add(key);
      lastDay = key;
    }
    live.add(String(event.id));
    desired.push(eventRow(event));
  }
  if (!ordered.length) {
    if (!emptyThreadRow) {
      emptyThreadRow = create("li", "empty-row", "No messages yet.");
    }
    desired.push(emptyThreadRow);
  }

  const wasNearBottom = nearBottom();
  const previousHeight = timelineEl.scrollHeight;
  const previousTop = timelineEl.scrollTop;
  reconcile(timelineEl, desired);
  prune(threadNodes, live);
  prune(dayNodes, liveDays);

  if (mode === "append") {
    timelineEl.scrollTop = previousTop
      + (timelineEl.scrollHeight - previousHeight);
  } else if (mode === "merge") {
    // A refresh keeps the reader in place unless the reader was already
    // at the bottom, where new messages belong.
    timelineEl.scrollTop = wasNearBottom
      ? timelineEl.scrollHeight : previousTop;
  } else {
    timelineEl.scrollTop = timelineEl.scrollHeight;
  }
}

export function renderThreadLoading(key) {
  if (threadNameEl) threadNameEl.textContent = String(key || "");
  if (threadNumberEl) threadNumberEl.textContent = "";
  if (timelineEl) timelineEl.replaceChildren();
  threadNodes.clear();
  dayNodes.clear();
  renderedScope = null;
  setThreadStatus("Loading messages…");
}

// Forget the rows of the device that was open: they must not survive a
// switch to another device.
export function resetScope() {
  chatNodes.clear();
  threadNodes.clear();
  dayNodes.clear();
  callNodes.clear();
  renderedScope = null;
  currentLiveCall = null;
  liveCallError = "";
  stopLiveTimer();
  if (chatListEl) chatListEl.replaceChildren();
  if (timelineEl) timelineEl.replaceChildren();
  if (callListEl) callListEl.replaceChildren();
  if (liveCardEl) liveCardEl.hidden = true;
  if (listStatusEl) listStatusEl.textContent = "";
  if (threadStatusEl) threadStatusEl.textContent = "";
  if (callsStatusEl) callsStatusEl.textContent = "";
}

export function renderMe(me) {
  if (!meLineEl) return;
  const user = me && me.user ? me.user : {};
  const name = user.first_name || user.username || user.id;
  meLineEl.textContent = name ? "Signed in as " + String(name) : "";
}

export function setListStatus(text) {
  if (listStatusEl) listStatusEl.textContent = text ? String(text) : "";
}

export function setThreadStatus(text) {
  if (threadStatusEl) threadStatusEl.textContent = text ? String(text) : "";
}

export function showSplash(title, detail) {
  if (splashTitleEl) splashTitleEl.textContent = String(title || "");
  if (splashDetailEl) splashDetailEl.textContent = String(detail || "");
  if (splashEl) splashEl.hidden = false;
}

export function renderError(title, detail) {
  showSplash(title || "Something went wrong", detail || "");
}

export function hideSplash() {
  if (splashEl) splashEl.hidden = true;
}

// The route of the shell: devices, chats, calls or thread. The CSS
// decides which pane is visible.
export function setRoute(name) {
  if (!appEl) return;
  const route = name === "chats" || name === "thread" || name === "calls"
    ? name : "devices";
  // The "calls" route reuses the chats pane: same header, same tab bar.
  appEl.dataset.route = route === "calls" ? "chats" : route;
  // The route owns the tab, so back/forward always restores it. Any other
  // route (the device list, a thread) returns the pane to Chats.
  setDeviceTab(route === "calls" ? "calls" : "chats");
  syncLiveTimer();
}

export function renderPausedState(paused) {
  if (pausedIndicatorEl) pausedIndicatorEl.hidden = !paused;
}

function simOptionLabel(sim) {
  if (typeof sim.label === "string" && sim.label.trim()) return sim.label.trim();
  const slot = Number(sim.slot);
  if (Number.isInteger(slot) && slot >= 0) return "SIM " + String(slot + 1);
  return "SIM " + String(sim.sub_id);
}

// SIM pickers are only useful with more than one SIM. The value is the
// subscription id; 0 means "let the phone use its default SIM".
export function populateSimSelect(sims, selectedSubId) {
  const list = (Array.isArray(sims) ? sims : []).filter(
    (sim) => sim && Number.isInteger(sim.sub_id));
  const visible = list.length > 1;
  const pairs = [
    [composerSimEl, composerSimRowEl],
    [newChatSimEl, newChatSimRowEl],
  ];
  for (const [select, row] of pairs) {
    if (!select) continue;
    select.replaceChildren();
    if (!visible) {
      if (row) row.hidden = true;
      continue;
    }
    const fallback = create("option", null, "Phone default");
    fallback.value = "0";
    select.append(fallback);
    for (const sim of list) {
      const option = create("option", null, simOptionLabel(sim));
      option.value = String(sim.sub_id);
      select.append(option);
    }
    const wanted = String(Number(selectedSubId) || 0);
    const hasWanted = Array.from(select.options).some(
      (option) => option.value === wanted);
    select.value = hasWanted ? wanted : "0";
    if (row) row.hidden = false;
  }
  return visible;
}

export function setSimSelection(subId) {
  const wanted = String(Number(subId) || 0);
  for (const select of [composerSimEl, newChatSimEl]) {
    if (!select) continue;
    const hasWanted = Array.from(select.options).some(
      (option) => option.value === wanted);
    if (hasWanted) select.value = wanted;
  }
}

export function renderComposerState(options) {
  const data = options || {};
  const disabled = Boolean(data.disabled);
  if (composerSendEl) {
    composerSendEl.disabled = disabled || Boolean(data.sending);
  }
  if (composerBodyEl) composerBodyEl.disabled = Boolean(data.inputDisabled);
  if (composerMetaEl) {
    const length = Math.max(0, Number(data.bodyLength) || 0);
    const parts = Math.max(0, Number(data.parts) || 0);
    composerMetaEl.textContent = String(length) + " chars · "
      + String(parts) + (parts === 1 ? " part" : " parts");
  }
  if (composerPausedEl) composerPausedEl.hidden = !data.paused;
  if (composerDisabledEl) {
    const reason = data.disabledReason ? String(data.disabledReason) : "";
    composerDisabledEl.textContent = reason;
    composerDisabledEl.hidden = !reason;
  }
  if (composerErrorEl) {
    const error = data.error ? String(data.error) : "";
    composerErrorEl.textContent = error;
    composerErrorEl.hidden = !error;
  }
  if (composerRetryEl) composerRetryEl.hidden = !data.canRetry;
  if (Array.isArray(data.sims)) {
    populateSimSelect(data.sims, data.selectedSubId);
  }
}

// --- the new conversation dialog ----------------------------------------

export function setNewChatError(text) {
  if (!newChatErrorEl) return;
  const error = text ? String(text) : "";
  newChatErrorEl.textContent = error;
  newChatErrorEl.hidden = !error;
}

export function newChatDialogOpen() {
  return Boolean(newChatDialogEl && !newChatDialogEl.hidden);
}

export function showNewChatDialog() {
  if (!newChatDialogEl) return;
  newChatDialogEl.hidden = false;
  setNewChatError("");
  if (newChatNumberEl) {
    try {
      newChatNumberEl.focus();
    } catch (err) {
      // Focus is a convenience; the input is still usable.
    }
  }
}

export function hideNewChatDialog() {
  if (newChatDialogEl) newChatDialogEl.hidden = true;
}

// The contacts of the device: a shortcut for a number that is already
// known to that phone.
export function renderSuggestions(contacts) {
  if (!newChatSuggestionsEl) return;
  const list = Array.isArray(contacts) ? contacts : [];
  const desired = [];
  for (const contact of list) {
    if (!contact) continue;
    const number = String(contact.number || "");
    if (!number) continue;
    const row = create("li", "suggestion-row");
    const button = create("button", "suggestion-item");
    button.type = "button";
    button.dataset.suggestion = number;
    button.append(
      create("span", "suggestion-name",
        String(contact.name || contact.display || number)),
      create("span", "suggestion-number",
        String(contact.display || number)),
    );
    row.append(button);
    desired.push(row);
  }
  reconcile(newChatSuggestionsEl, desired);
}

export function clearSuggestions() {
  if (newChatSuggestionsEl) newChatSuggestionsEl.replaceChildren();
}

// --- the device menu ----------------------------------------------------

// The device the menu belongs to. The menu is opened from the device
// list, so it always names one device.
export function deviceMenuDeviceId() {
  return menuDeviceId;
}

export function showDeviceMenu(device) {
  if (!deviceMenuEl || !device) return;
  menuDeviceId = String(device.id || "");
  if (deviceMenuTitleEl) {
    deviceMenuTitleEl.textContent = String(device.name || device.id || "");
  }
  if (deviceMenuSubEl) {
    deviceMenuSubEl.textContent = deviceStatusLine(device);
  }
  if (deviceRenameNameEl) {
    deviceRenameNameEl.value = String(device.name || "");
  }
  const readOnly = Boolean(device.archived || device.revoked);
  // A read-only device can still be renamed, but it can neither be
  // revoked nor made the default device.
  if (deviceDefaultEl) {
    deviceDefaultEl.hidden = readOnly || device.default === true;
  }
  if (deviceRevokeEl) deviceRevokeEl.hidden = readOnly;
  if (deviceMenuErrorEl) {
    deviceMenuErrorEl.textContent = "";
    deviceMenuErrorEl.hidden = true;
  }
  deviceMenuEl.hidden = false;
}

export function hideDeviceMenu() {
  if (deviceMenuEl) deviceMenuEl.hidden = true;
}

export function setDeviceMenuError(text) {
  if (!deviceMenuErrorEl) return;
  const error = text ? String(text) : "";
  deviceMenuErrorEl.textContent = error;
  deviceMenuErrorEl.hidden = !error;
}

export function setDeviceMenuBusy(busy) {
  const disabled = Boolean(busy);
  if (deviceRenameSaveEl) deviceRenameSaveEl.disabled = disabled;
  if (deviceDefaultEl) deviceDefaultEl.disabled = disabled;
  if (deviceRevokeEl) deviceRevokeEl.disabled = disabled;
}

// --- the pairing sheet ---------------------------------------------------

export function showPairSheet() {
  if (!pairSheetEl) return;
  pairSheetEl.hidden = false;
  setPairError("");
  hidePairPreview();
}

export function hidePairSheet() {
  if (pairSheetEl) pairSheetEl.hidden = true;
}

export function setPairError(text) {
  if (!pairErrorEl) return;
  const error = text ? String(text) : "";
  pairErrorEl.textContent = error;
  pairErrorEl.hidden = !error;
}

export function setPairCode(value) {
  if (pairCodeEl) pairCodeEl.value = value ? String(value) : "";
}

export function pairCodeValue() {
  return pairCodeEl ? String(pairCodeEl.value || "") : "";
}

// The preview of the device that is waiting to be paired.
export function renderPairPreview(preview) {
  if (!pairPreviewEl || !preview) return;
  const name = String(preview.name || "");
  if (pairPreviewNameEl) {
    pairPreviewNameEl.textContent = name || "Unnamed device";
  }
  if (pairPreviewMetaEl) {
    const parts = [];
    const platform = String(preview.platform || "");
    if (platform) parts.push(platform);
    const version = String(preview.app_version || "");
    if (version) parts.push("version " + version);
    const expires = Number(preview.expires_at);
    if (Number.isFinite(expires) && expires > 0) {
      parts.push("expires " + formatTime(expires));
    }
    pairPreviewMetaEl.textContent = parts.join(" · ");
  }
  if (pairPreviewCodeEl) {
    const code = String(preview.scan_code || "");
    pairPreviewCodeEl.textContent = code ? "Code " + code : "";
  }
  pairPreviewEl.hidden = false;
  if (pairApproveEl) pairApproveEl.hidden = false;
}

export function hidePairPreview() {
  if (pairPreviewEl) pairPreviewEl.hidden = true;
  if (pairApproveEl) pairApproveEl.hidden = true;
}

export function setPairBusy(busy) {
  const disabled = Boolean(busy);
  if (pairApproveEl) pairApproveEl.disabled = disabled;
}
