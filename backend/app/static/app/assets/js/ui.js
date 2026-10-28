// DOM rendering for the read-only inbox.
// Every user-controlled string goes through textContent or createTextNode;
// nothing here interprets message text as markup or links.

const chatListEl = document.getElementById("chat-list");
const timelineEl = document.getElementById("timeline");
const listStatusEl = document.getElementById("list-status");
const threadStatusEl = document.getElementById("timeline-status");
const threadNameEl = document.getElementById("thread-name");
const threadNumberEl = document.getElementById("thread-number");
const meLineEl = document.getElementById("me-line");
const splashEl = document.getElementById("splash");
const splashTitleEl = document.getElementById("splash-title");
const splashDetailEl = document.getElementById("splash-detail");
const appEl = document.getElementById("app");
const threadPaneEl = document.getElementById("thread-pane");
const pausedIndicatorEl = document.getElementById("paused-indicator");
const composerMetaEl = document.getElementById("composer-meta");
const composerPausedEl = document.getElementById("composer-paused");
const composerErrorEl = document.getElementById("composer-error");
const composerRetryEl = document.getElementById("composer-retry");
const composerSendEl = document.getElementById("composer-send");
const composerSimEl = document.getElementById("composer-sim");
const composerSimRowEl = document.getElementById("composer-sim-row");
const newChatDialogEl = document.getElementById("new-chat-dialog");
const newChatNumberEl = document.getElementById("new-chat-number");
const newChatErrorEl = document.getElementById("new-chat-error");
const newChatSimEl = document.getElementById("new-chat-sim");
const newChatSimRowEl = document.getElementById("new-chat-sim-row");

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
  + "\u00c5\u00e5\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e"
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

export function renderChatList(chats, options) {
  if (!chatListEl) return;
  const opts = options || {};
  const activeKey = opts.activeKey || null;
  const list = Array.isArray(chats) ? chats : [];
  chatListEl.replaceChildren();
  if (!list.length) {
    const item = create("li", "empty-row",
      opts.query ? "No conversations match your search." : "No conversations yet.");
    chatListEl.append(item);
    return;
  }
  for (const chat of list) {
    const row = create("li", "chat-row");
    const button = create("button", "chat-item");
    button.type = "button";
    button.dataset.threadKey = chat.thread_key;
    if (activeKey && chat.thread_key === activeKey) {
      button.classList.add("active");
    }
    button.setAttribute("aria-label",
      String(chat.name || "") + ", " + String(chat.display || ""));

    const avatar = create("span", "avatar", initials(chat.name));
    avatar.setAttribute("aria-hidden", "true");

    const main = create("span", "chat-main");
    const name = create("span", "chat-name", chat.name);
    const preview = create("span", "chat-preview",
      (chat.last_direction === "out" ? "You: " : "") + String(chat.last_body || ""));
    main.append(name, preview);

    const side = create("span", "chat-side");
    const when = create("span", "chat-time", formatListTime(chat.last_ts));
    side.append(when);
    const unread = Number(chat.unread) || 0;
    if (unread > 0) {
      const badge = create("span", "badge", unread > 99 ? "99+" : String(unread));
      badge.setAttribute("aria-label", String(unread) + " unread");
      side.append(badge);
      button.classList.add("has-unread");
    }

    button.append(avatar, main, side);
    row.append(button);
    chatListEl.append(row);
  }
}

export function renderThread(state) {
  if (!timelineEl) return;
  const data = state || {};
  const meta = data.meta || {};
  const events = Array.isArray(data.events) ? data.events : [];
  if (threadNameEl) threadNameEl.textContent = String(meta.name || "");
  if (threadNumberEl) threadNumberEl.textContent = String(meta.display || "");

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
    if (meta.thread_key && event.thread_key !== meta.thread_key) return false;
    return !serverRefs.has(event.client_ref);
  });

  const previousHeight = timelineEl.scrollHeight;
  const previousTop = timelineEl.scrollTop;
  timelineEl.replaceChildren();

  if (data.hasMore) {
    const row = create("li", "load-row");
    const button = create("button", "load-older", "Load older messages");
    button.type = "button";
    button.id = "load-older";
    row.append(button);
    timelineEl.append(row);
  }

  if (!events.length && !pending.length) {
    timelineEl.append(create("li", "empty-row", "No messages yet."));
    return;
  }

  const ordered = pending.concat(events).slice().reverse();
  let lastDay = "";
  for (const event of ordered) {
    const key = dayKey(event.ts);
    if (key !== lastDay) {
      const separator = create("li", "day-separator", dayLabel(event.ts));
      separator.setAttribute("role", "presentation");
      timelineEl.append(separator);
      lastDay = key;
    }
    const outbound = event.direction === "out";
    const row = create("li", outbound ? "row out" : "row in");
    if (event.local && event.id) row.dataset.localId = String(event.id);
    const bubble = create("div", "bubble");
    bubble.append(create("p", "bubble-body", event.body));

    const metaRow = create("p", "bubble-meta");
    metaRow.append(create("span", "bubble-time", formatTime(event.ts)));
    if (outbound) {
      const name = statusName(event.status);
      const tick = statusTick(event.status);
      if (tick) {
        const tickEl = create("span", "bubble-tick", tick);
        tickEl.setAttribute("aria-hidden", "true");
        metaRow.append(tickEl);
      }
      const label = event.status === "failed" ? "failed" : statusLabel(event.status);
      metaRow.append(create("span", "bubble-status status-" + name, label));
    }
    bubble.append(metaRow);
    row.append(bubble);
    timelineEl.append(row);
  }

  if (data.append) {
    timelineEl.scrollTop = previousTop + (timelineEl.scrollHeight - previousHeight);
  } else {
    timelineEl.scrollTop = timelineEl.scrollHeight;
  }
}

export function renderThreadLoading(key) {
  if (threadNameEl) threadNameEl.textContent = String(key || "");
  if (threadNumberEl) threadNumberEl.textContent = "";
  if (timelineEl) timelineEl.replaceChildren();
  setThreadStatus("Loading messages…");
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

export function showListPane() {
  if (appEl) appEl.classList.remove("thread-open");
  if (threadPaneEl) threadPaneEl.hidden = true;
}

export function showThreadPane() {
  if (appEl) appEl.classList.add("thread-open");
  if (threadPaneEl) threadPaneEl.hidden = false;
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
  if (composerSendEl) composerSendEl.disabled = Boolean(data.sending);
  if (composerMetaEl) {
    const length = Math.max(0, Number(data.bodyLength) || 0);
    const parts = Math.max(0, Number(data.parts) || 0);
    composerMetaEl.textContent = String(length) + " chars · "
      + String(parts) + (parts === 1 ? " part" : " parts");
  }
  if (composerPausedEl) composerPausedEl.hidden = !data.paused;
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
