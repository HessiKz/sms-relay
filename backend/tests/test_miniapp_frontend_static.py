"""Static guards for the Phase 3 Mini App assets."""

import re
from pathlib import Path

STATIC_ROOT = Path(__file__).resolve().parent.parent / "app" / "static" / "app"
INDEX = STATIC_ROOT / "index.html"
JS_DIR = STATIC_ROOT / "assets" / "js"
JS_FILES = ("telegram.js", "api.js", "store.js", "ui.js", "app.js")


def _html():
    return INDEX.read_text(encoding="utf-8")


def _js(name):
    return (JS_DIR / name).read_text(encoding="utf-8")


def _all_js():
    return "\n".join(_js(name) for name in JS_FILES)


def test_shell_keeps_base_and_telegram_sdk():
    html = _html()
    assert '<base href="/relay/app/">' in html
    assert 'name="viewport"' in html
    assert "https://telegram.org/js/telegram-web-app.js?64" in html
    assert 'rel="stylesheet" href="assets/app.css"' in html
    assert re.search(r'<script[^>]+type="module"[^>]+src="assets/app\.js"', html)


def test_shell_has_no_inline_handlers_or_styles():
    html = _html()
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)
    assert " style=" not in html
    assert "<script>" not in html
    script_tags = re.findall(r"<script\b([^>]*)>", html, re.IGNORECASE)
    assert len(script_tags) == 2
    assert all("src=" in attrs for attrs in script_tags)


def test_javascript_never_uses_markup_injection_helpers():
    combined = _all_js()
    for helper in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                   "document.write"):
        assert helper not in combined
    assert ("eval" + "(") not in combined


def test_api_calls_use_relative_paths_only():
    api_js = _js("api.js")
    combined = _all_js()
    assert 'const API_ROOT = "api/"' in api_js
    assert 'const AUTH_HEADER = "X-Telegram-Init-Data"' in api_js
    assert 'credentials: "omit"' in api_js
    assert 'cache: "no-store"' in api_js
    assert "/app/api" not in combined
    assert '"/relay' not in combined


def test_thread_keys_are_url_encoded():
    assert "encodeURIComponent" in _js("app.js")
    assert "encodeURIComponent" in _js("store.js")


def test_no_third_party_urls_outside_telegram():
    combined = _html() + "\n" + _all_js()
    urls = re.findall(r"https?://[^\s\"'<>)]+", combined)
    assert urls
    for url in urls:
        assert url.startswith("https://telegram.org/"), url


def test_entry_module_loads_the_real_app():
    entry = (STATIC_ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert './js/app.js' in entry


def test_nested_modules_are_served(client):
    for name in JS_FILES:
        response = client.get("/app/assets/js/" + name)
        assert response.status_code == 200, name
        assert "javascript" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-cache"


def test_deep_link_prefixes_and_polling_contract():
    app_js = _js("app.js")
    store_js = _js("store.js")
    assert '"thread:"' in app_js
    assert '"thread_"' in app_js
    assert '"tgWebAppStartParam"' in _js("telegram.js")
    assert "events?rev=" in store_js
    assert "&timeout=25" in store_js
    assert "visibilitychange" in store_js
    assert "BACKOFF_MS = [1000, 2000, 5000, 15000]" in store_js


def test_phase_four_composer_and_new_conversation_dialog_exist():
    html = _html()
    for element_id in ("composer", "composer-body", "composer-send",
                       "composer-meta", "composer-sim", "composer-error",
                       "composer-retry", "new-chat-button", "new-chat-dialog",
                       "new-chat-form", "new-chat-number", "new-chat-body",
                       "new-chat-sim", "new-chat-cancel", "new-chat-send",
                       "new-chat-error"):
        assert 'id="' + element_id + '"' in html, element_id
    assert 'aria-modal="true"' in html
    assert 'maxlength="1600"' in html
    assert 'type="module"' in html


def test_phase_four_send_contract():
    store_js = _js("store.js")
    app_js = _js("app.js")
    combined = _all_js()
    assert '"chats/" + encodeURIComponent(thread) + "/messages"' in store_js
    assert "apiPost(" in store_js
    assert "confirm_unknown" in combined
    assert "crypto.randomUUID()" in app_js
    assert "enableClosingConfirmation" in _js("telegram.js")
    assert "showPopup" in _js("telegram.js")
    assert "retryLastSend" in store_js
    assert "lastFailed" in store_js
    assert "messageParts" in _js("ui.js")
    assert "160" in _js("ui.js") and "153" in _js("ui.js")
    assert "70" in _js("ui.js") and "67" in _js("ui.js")


def test_phase_six_deep_link_sources():
    app_js = _js("app.js")
    telegram_js = _js("telegram.js")
    assert 'get("thread")' in app_js
    assert "initStartParam" in app_js
    assert "initStartParam" in telegram_js
    assert "initDataUnsafe" in telegram_js
    assert "start_param" in telegram_js


def test_phase_four_relative_paths_and_no_markup_injection():
    combined = _all_js()
    assert "/app/api" not in combined
    assert '"/relay' not in combined
    for helper in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                   "document.write"):
        assert helper not in combined
        assert helper not in _html()
