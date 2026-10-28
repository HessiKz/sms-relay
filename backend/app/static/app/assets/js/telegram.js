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
