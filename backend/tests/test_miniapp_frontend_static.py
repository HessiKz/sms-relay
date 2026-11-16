"""Static guards for the device-first Mini App assets.

The Mini App is device-first: the device list, the conversations of one
device and a thread of that device. These assertions keep the shape of
the bundle honest: the routes carry the device, the conversation routes
are per device, the QR scanner is the native one of Telegram and no
user-controlled string is ever treated as markup.
"""

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


def _function_source(source, name):
    """The text of one JavaScript function, braces balanced."""
    marker = "function " + name + "("
    start = source.index(marker)
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("function not found: " + name)


def test_shell_keeps_base_and_telegram_sdk():
    html = _html()
    assert '<base href="/relay/app/">' in html
    assert 'name="viewport"' in html
    assert "https://telegram.org/js/telegram-web-app.js?64" in html
    assert 'rel="stylesheet" href="assets/app.css"' in html
    assert re.search(r'<script[^>]+type="module"[^>]+src="assets/js/app\.js"',
                     html) or re.search(
                         r'<script[^>]+type="module"[^>]+src="assets/app\.js"',
                         html)


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


def test_the_three_screens_of_the_device_first_app_exist():
    html = _html()
    for element_id in ("devices-pane", "device-list", "add-device-button",
                       "chats-pane", "chat-list", "search-form", "search",
                       "thread-pane", "timeline", "composer", "composer-body",
                       "composer-send", "composer-meta", "composer-sim",
                       "composer-sim-row", "composer-paused",
                       "composer-disabled", "composer-error",
                       "composer-retry"):
        assert 'id="' + element_id + '"' in html, element_id
    assert 'data-route="devices"' in html
    assert 'aria-modal="true"' in html
    assert 'maxlength="1600"' in html
    assert 'type="module"' in html


def test_the_pairing_sheet_and_the_device_menu_exist():
    html = _html()
    for element_id in ("pair-sheet", "pair-scan", "pair-form", "pair-code",
                       "pair-preview", "pair-preview-name",
                       "pair-preview-meta", "pair-preview-code",
                       "pair-approve", "pair-cancel", "pair-error",
                       "device-menu", "device-menu-title", "device-menu-sub",
                       "device-rename-name", "device-rename-save",
                       "device-default", "device-revoke",
                       "device-menu-cancel", "device-menu-error"):
        assert 'id="' + element_id + '"' in html, element_id


def test_the_routes_carry_the_device():
    app_js = _js("app.js")
    assert '"#/d/"' in app_js
    assert "deviceHash" in app_js
    assert "threadHash" in app_js
    assert "encodeURIComponent" in app_js
    assert "parseRoute" in app_js
    # The link of the bot names the device and the thread.
    assert 'get("device")' in app_js
    assert 'get("thread")' in app_js
    assert 'startValue("device")' in app_js
    assert 'startValue("thread")' in app_js


def test_the_conversation_routes_are_scoped_to_a_device():
    store_js = _js("store.js")
    # The device-less routes are gone: they answer 410 on the server.
    assert 'apiGet("chats' not in store_js
    assert 'apiPost("chats' not in store_js
    assert 'apiGet("contacts' not in store_js
    assert 'apiGet("chats/' not in store_js
    assert 'apiPost("chats/' not in store_js
    # Every conversation call goes through the device of the route.
    assert '"devices/" + encodeURIComponent' in store_js
    assert '"/chats"' in store_js
    assert '"/messages"' in store_js
    assert '"/read"' in store_js
    assert '"/contacts"' in store_js


def test_the_device_actions_use_the_device_routes():
    store_js = _js("store.js")
    assert '"/revoke"' in store_js
    assert '"/default"' in store_js
    assert "apiPatch" in store_js
    assert "apiPatch" in _js("api.js")


def test_the_qr_scanner_is_the_native_one_of_telegram():
    telegram_js = _js("telegram.js")
    combined = _all_js()
    assert "showScanQrPopup" in telegram_js
    assert "scanQrPopupClosed" in telegram_js
    assert "export function showScanQr" in telegram_js
    assert "scanQrAvailable" in telegram_js
    # The shell sends camera=(), so the app must not ask for the camera.
    assert "getUserMedia" not in combined
    assert "mediaDevices" not in combined


def test_the_scanner_is_called_with_the_documented_signature():
    telegram_js = _js("telegram.js")
    # showScanQrPopup(params, callback): the first argument is an object
    # with the prompt, the second one receives the scanned text. A call
    # that passes the callback first never delivers the scan.
    match = re.search(
        r"showScanQrPopup\(\s*\{[^{}]*text\s*:[^{}]*\}\s*,\s*\([^()]*\)\s*=>",
        telegram_js, re.S)
    assert match, "showScanQrPopup must be called with (params, callback)"
    # A client that answers with a promise instead of a callback works too.
    assert "result.then(" in telegram_js
    # The cancellation path of the popup stays.
    assert "scanQrPopupClosed" in telegram_js


def test_the_pairing_payload_forms_are_accepted():
    telegram_js = _js("telegram.js")
    store_js = _js("store.js")
    app_js = _js("app.js")
    assert '"pair_"' in telegram_js
    assert "startapp" in telegram_js
    assert "pairToken" in telegram_js
    assert "pairToken" in app_js
    assert "pair/preview" in store_js
    assert "pair/approve" in store_js
    assert "pair/cancel" in store_js


def test_the_event_scope_names_the_device_and_the_thread():
    store_js = _js("store.js")
    assert "changedScopes" in store_js
    assert "device_id" in store_js
    assert "thread_key" in store_js
    # A change of another device never reloads the open thread.
    assert "changedDevice !== deviceId" in store_js


def test_the_row_caches_are_scoped_to_the_device():
    ui_js = _js("ui.js")
    assert "resetScope" in ui_js
    assert "renderedScope" in ui_js
    assert "device_id" in ui_js
    assert "deviceNodes" in ui_js


def test_deep_link_prefixes_and_polling_contract():
    app_js = _js("app.js")
    store_js = _js("store.js")
    assert '"tgWebAppStartParam"' in _js("telegram.js")
    assert "events?rev=" in store_js
    assert "&timeout=25" in store_js
    assert "visibilitychange" in store_js
    assert "BACKOFF_MS = [1000, 2000, 5000, 15000]" in store_js


def test_send_contract():
    store_js = _js("store.js")
    app_js = _js("app.js")
    combined = _all_js()
    assert "encodeURIComponent(thread)" in store_js
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
    # The app reads the start parameters through the Telegram wrapper.
    assert "startParams" in app_js
    assert "initStartParam" in telegram_js
    assert "initDataUnsafe" in telegram_js
    assert "start_param" in telegram_js


def test_relative_paths_and_no_markup_injection():
    combined = _all_js()
    assert "/app/api" not in combined
    assert '"/relay' not in combined
    for helper in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                   "document.write"):
        assert helper not in combined
        assert helper not in _html()


def test_history_pagination_uses_the_composite_cursor():
    store_js = _js("store.js")
    app_js = _js("app.js")
    # The cursor has two halves, so events of the same second survive.
    assert "before_id" in store_js
    assert "before_id" in app_js
    assert "before_id: oldest.id" in app_js
    assert "before_ts: oldest.ts" in app_js
    # The conversation list pages with the same idea.
    assert "before_key" in store_js
    assert "before_ts = state.chatsCursor.ts" in store_js
    assert "before_key = state.chatsCursor.key" in store_js


def test_refresh_merges_instead_of_resetting_the_thread():
    store_js = _js("store.js")
    ui_js = _js("ui.js")
    assert "mergeEvents" in store_js
    assert "mergeChats" in store_js
    assert 'mode: append ? "append" : (switching ? "reset" : "merge")' \
        in store_js
    # A revision-driven refresh must not clear the timeline or the list.
    assert "replaceChildren" not in _function_source(ui_js, "renderThread")
    assert "replaceChildren" not in _function_source(ui_js, "renderChatList")
    assert "reconcile(" in ui_js
    # The rows are cached, so a refresh does not rebuild every bubble.
    assert "threadNodes" in ui_js
    assert "chatNodes" in ui_js


def test_refresh_is_debounced_and_uses_the_change_scope():
    store_js = _js("store.js")
    assert "REFRESH_DEBOUNCE_MS" in store_js
    assert "scheduleRefresh" in store_js
    assert "refreshDirty" in store_js
    assert "changedScopes" in store_js


def test_older_history_and_more_conversations_are_reachable():
    ui_js = _js("ui.js")
    app_js = _js("app.js")
    store_js = _js("store.js")
    assert "load-older" in ui_js
    assert "load-more-chats" in ui_js
    assert "load-more-chats" in app_js
    assert "loadMoreChats" in app_js
    assert "hasMore: state.chatsHasMore" in store_js
