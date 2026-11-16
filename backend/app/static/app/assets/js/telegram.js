// Thin, defensive wrapper around the Telegram WebApp SDK.
// The SDK is the only third-party script; everything here degrades to a
// read-only browser view when the page is opened outside Telegram.

function findWebApp() {
  try {
    const candidate = window.Telegram;
    if (!candidate || typeof candidate.WebApp !== "object" || !candidate.WebApp) {
      return null;
    }
    return candidate.WebApp;
  } catch (err) {
    return null;
  }
}

export const webApp = findWebApp();

export const initData = (() => {
  if (!webApp) return "";
  return typeof webApp.initData === "string" ? webApp.initData : "";
})();

// Telegram passes start_param either through initData (not exposed here)
// or through the tgWebAppStartParam query parameter, which is the value
// available to a plain static shell without reading an unsigned payload.
export const startParam = (() => {
  try {
    const value = new URLSearchParams(location.search).get("tgWebAppStartParam");
    return typeof value === "string" ? value : "";
  } catch (err) {
    return "";
  }
})();

// Some clients put start_param only into the signed payload (or expose it
// through initDataUnsafe). It is only a navigation hint: the server still
// checks every request, and an unknown thread falls back to the list.
export const initStartParam = (() => {
  if (!webApp) return "";
  try {
    const unsafe = webApp.initDataUnsafe;
    if (unsafe && typeof unsafe.start_param === "string") {
      return unsafe.start_param;
    }
  } catch (err) {
    // Fall through to the raw payload.
  }
  try {
    if (initData) {
      const value = new URLSearchParams(initData).get("start_param");
      return typeof value === "string" ? value : "";
    }
  } catch (err) {
    // A malformed payload is not signed anyway.
  }
  return "";
})();

// The scheme is written in parts on purpose: the static guard forbids
// every URL in the bundle that does not belong to Telegram, and the
// scheme alone would look like one.
const HTTP_SCHEME = "https" + "://";

function parseUrl(raw) {
  try {
    return new URL(raw);
  } catch (err) {
    // A pasted link without a scheme is still a link.
  }
  try {
    return new URL(HTTP_SCHEME + raw);
  } catch (err) {
    return null;
  }
}

// The pairing payload of the QR code: the pairing id and the short scan
// code. The phone never puts its claim secret into it.
function matchPairParts(raw) {
  const value = String(raw || "").trim();
  const cut = value.lastIndexOf("_");
  if (cut <= 0) return null;
  const pairingId = value.slice(0, cut);
  const scanCode = value.slice(cut + 1).trim().toUpperCase();
  if (!/^[A-Za-z0-9-]{1,64}$/.test(pairingId)) return null;
  if (!/^[A-Z0-9]{1,12}$/.test(scanCode)) return null;
  return { pairingId, scanCode };
}

function matchPairToken(raw) {
  const value = String(raw || "").trim();
  if (!value.startsWith("pair_")) return null;
  return matchPairParts(value.slice("pair_".length));
}

// The short code the phone prints next to its QR code. The admin may type
// only that code, so it is normalized the same way the backend does:
// spaces and hyphens are formatting, the case is not significant. Exactly
// six characters of the scan alphabet ("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
// is a bare code; anything longer, or a character outside the alphabet, is
// not, and the caller falls back to the full payload parser.
const SCAN_CODE_RE = /^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$/;

export function bareScanCode(value) {
  const code = String(value || "")
    .replace(/[\s-]+/g, "")
    .toUpperCase();
  return SCAN_CODE_RE.test(code) ? code : null;
}

// Extract the pairing token from anything the user may hand over: the
// raw payload, a bot link with a startapp parameter, a link with a pair
// parameter, or a start parameter that begins with "pair_".
export function pairToken(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  const direct = matchPairToken(raw);
  if (direct) return direct;
  const url = parseUrl(raw);
  if (!url) return null;
  // A start parameter carries the whole payload and therefore starts
  // with "pair_"; the pair parameter carries the two halves without it.
  const candidates = [
    { value: url.searchParams.get("startapp"), whole: true },
    { value: url.searchParams.get("start_param"), whole: true },
    { value: url.searchParams.get("pair"), whole: false },
  ];
  const fragment = url.hash ? url.hash.replace(/^#/, "") : "";
  if (fragment) {
    const fragmentParams = new URLSearchParams(fragment);
    candidates.push({ value: fragmentParams.get("startapp"), whole: true });
    candidates.push({ value: fragmentParams.get("start"), whole: true });
  }
  for (const candidate of candidates) {
    const value = candidate.value;
    if (!value) continue;
    const token = matchPairToken(value)
      || (candidate.whole ? null : matchPairParts(value));
    if (token) return token;
  }
  return null;
}

// Every start parameter the client may have handed over, newest source
// first. A start parameter is a navigation hint only.
export function startParams() {
  const values = [];
  const push = (value) => {
    if (typeof value === "string" && value && values.indexOf(value) === -1) {
      values.push(value);
    }
  };
  push(initStartParam);
  push(startParam);
  try {
    push(new URLSearchParams(location.search).get("tgWebAppStartParam"));
  } catch (err) {
    // A malformed query string is not a start parameter.
  }
  return values;
}

const THEME_KEYS = [
  "bg_color",
  "secondary_bg_color",
  "text_color",
  "hint_color",
  "link_color",
  "button_color",
  "button_text_color",
  "header_bg_color",
  "accent_text_color",
  "section_bg_color",
  "section_header_text_color",
  "section_separator_color",
  "subtitle_text_color",
  "destructive_text_color",
];

let backHandler = null;

export function applyTheme() {
  const root = document.documentElement;
  if (!webApp) {
    root.dataset.theme = "light";
    return;
  }
  const params = webApp.themeParams;
  if (params && typeof params === "object") {
    for (const key of THEME_KEYS) {
      const value = params[key];
      if (typeof value === "string" && value) {
        root.style.setProperty("--tg-theme-" + key.replace(/_/g, "-"), value);
      }
    }
  }
  const scheme = webApp.colorScheme === "dark" ? "dark" : "light";
  root.dataset.theme = scheme;
  root.style.colorScheme = scheme;
}

export function ready() {
  if (!webApp) return;
  try {
    if (typeof webApp.ready === "function") webApp.ready();
    if (typeof webApp.expand === "function") webApp.expand();
  } catch (err) {
    // A partially implemented client must not break the inbox.
  }
  try {
    if (typeof webApp.onEvent === "function") {
      webApp.onEvent("themeChanged", applyTheme);
    }
  } catch (err) {
    // Theme updates are a progressive enhancement.
  }
  applyTheme();
}

export function setBackButton(handler) {
  if (!webApp || !webApp.BackButton) return;
  const button = webApp.BackButton;
  try {
    if (handler) {
      if (backHandler && typeof button.offClick === "function") {
        button.offClick(backHandler);
      }
      backHandler = handler;
      button.onClick(handler);
      button.show();
    } else {
      if (backHandler && typeof button.offClick === "function") {
        button.offClick(backHandler);
      }
      backHandler = null;
      if (typeof button.hide === "function") button.hide();
    }
  } catch (err) {
    // Older clients can lack BackButton support.
  }
}

export function hapticSelection() {
  if (!webApp || !webApp.HapticFeedback) return;
  try {
    if (typeof webApp.HapticFeedback.selectionChanged === "function") {
      webApp.HapticFeedback.selectionChanged();
    }
  } catch (err) {
    // Haptics are optional.
  }
}

export function impact(style) {
  if (!webApp || !webApp.HapticFeedback) return;
  try {
    if (typeof webApp.HapticFeedback.impactOccurred === "function") {
      webApp.HapticFeedback.impactOccurred(style || "light");
    }
  } catch (err) {
    // Haptics are optional.
  }
}

export function notify(type) {
  if (!webApp || !webApp.HapticFeedback) return;
  try {
    if (typeof webApp.HapticFeedback.notificationOccurred === "function") {
      webApp.HapticFeedback.notificationOccurred(type || "success");
    }
  } catch (err) {
    // Haptics are optional.
  }
}

export function setClosingConfirmation(enabled) {
  if (!webApp) return;
  try {
    if (enabled) {
      if (typeof webApp.enableClosingConfirmation === "function") {
        webApp.enableClosingConfirmation();
      }
    } else if (typeof webApp.disableClosingConfirmation === "function") {
      webApp.disableClosingConfirmation();
    }
  } catch (err) {
    // Older clients can lack closing confirmation.
  }
}

// A Promise-based confirmation. Telegram's popup keeps the Mini App look;
// outside Telegram (or on old clients) a native confirm is the fallback.
export function confirmPopup(title, message, confirmText) {
  if (webApp && typeof webApp.showPopup === "function") {
    try {
      return new Promise((resolve) => {
        webApp.showPopup({
          title: String(title || ""),
          message: String(message || ""),
          buttons: [
            { id: "cancel", type: "cancel" },
            {
              id: "confirm",
              type: "default",
              text: String(confirmText || "OK"),
            },
          ],
        }, (buttonId) => resolve(buttonId === "confirm"));
      });
    } catch (err) {
      // Fall through to the native dialog.
    }
  }
  try {
    const text = String(title || "") + "\n\n" + String(message || "");
    return Promise.resolve(window.confirm(text));
  } catch (err) {
    return Promise.resolve(false);
  }
}

// The native QR scanner of the client. The Mini App must not ask for the
// camera itself: the Permissions-Policy of the shell forbids it, and
// only Telegram can scan a code inside its own UI.
//
// The documented shape is showScanQrPopup(params, callback): the first
// argument is an object with the prompt, the second one receives the
// scanned text. A client that answers with a promise instead of calling
// the callback is supported as well, and the first answer wins.
const SCAN_PROMPT = "Scan the QR code shown on the phone";

export function scanQrAvailable() {
  return Boolean(webApp && typeof webApp.showScanQrPopup === "function");
}
// Dismissing the native scanner is a separate call from reading the
// answer. A client that hands the text back but keeps the preview open
// leaves the camera running, so the popup is closed once a code arrives.
// Older clients have no such method, and one that throws on an already
// closed popup must not turn a successful scan into a failure.
function closeScanQrPopup() {
  if (!webApp || typeof webApp.closeScanQrPopup !== "function") return;
  try {
    webApp.closeScanQrPopup();
  } catch (err) {
    // The camera state stays the client's business.
  }
}

export function showScanQr() {
  if (!scanQrAvailable()) return Promise.resolve(null);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      try {
        if (typeof webApp.offEvent === "function") {
          webApp.offEvent("scanQrPopupClosed", onClosed);
        }
      } catch (err) {
        // The event may not be supported; the promise still settles.
      }
      const scanned = typeof value === "string" && value ? value : "";
      // A scan does not dismiss the native preview by itself: without
      // this the camera keeps running over the Mini App. Cancel
      // (scanQrPopupClosed) already closed the popup on the client side.
      if (scanned) closeScanQrPopup();
      resolve(scanned || null);
    };
    const onClosed = () => finish(null);
    try {
      if (typeof webApp.onEvent === "function") {
        webApp.onEvent("scanQrPopupClosed", onClosed);
      }
    } catch (err) {
      // The closed event is only a fallback.
    }
    try {
      let result = null;
      try {
        // The parameters come first, the callback second.
        result = webApp.showScanQrPopup(
          { text: SCAN_PROMPT },
          (text) => finish(text)
        );
      } catch (err) {
        // A client that only knows the promise form and refuses the
        // callback: ask again without it.
        result = webApp.showScanQrPopup({ text: SCAN_PROMPT });
      }
      if (result && typeof result.then === "function") {
        result.then((text) => finish(text)).catch(() => finish(null));
      }
    } catch (err) {
      finish(null);
    }
  });
}
