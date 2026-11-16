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

// --- the timeline -------------------------------------------------------

function buildEventRow(event) {
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
  const key = String(event.id);
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
  renderedScope = null;
  if (chatListEl) chatListEl.replaceChildren();
  if (timelineEl) timelineEl.replaceChildren();
  if (listStatusEl) listStatusEl.textContent = "";
  if (threadStatusEl) threadStatusEl.textContent = "";
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

// The route of the shell: devices, chats or thread. The CSS decides
// which pane is visible.
export function setRoute(name) {
  if (!appEl) return;
  const route = name === "chats" || name === "thread" ? name : "devices";
  appEl.dataset.route = route;
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
