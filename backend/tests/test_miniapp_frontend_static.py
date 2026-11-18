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
    # The entry and the modulepreload of the entry carry the cache-buster,
    # so an already installed WebView fetches the updated bundle.
    assert re.search(
        r'<script[^>]+type="module"[^>]+src="assets/app\.js\?v=20261117-calls"',
        html)
    assert 'rel="modulepreload" href="assets/js/app.js?v=20261117-calls"' \
        in html


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
    # The call-via sheet needs the public https links of the apps it
    # offers; nothing else may point off-origin.
    allowed = (
        "https://telegram.org/",
        "https://wa.me/",
        "https://t.me/",
        "https://signal.me/",
        "https://ble.ir/",
        "https://rubika.ir/",
    )
    for url in urls:
        assert url.startswith(allowed), url


def test_entry_module_loads_the_real_app():
    entry = (STATIC_ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert './js/app.js?v=20261117-calls' in entry
    # The entry is reached through the cache-busted URL of index.html.
    assert 'src="assets/app.js?v=20261117-calls"' in _html()


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
    # The popup is dismissed once a code arrives, or the camera of the
    # client keeps running behind the Mini App.
    assert "closeScanQrPopup" in telegram_js
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


def test_the_scanned_code_closes_the_native_scanner():
    telegram_js = _js("telegram.js")
    # Feature-detect: older clients have no closeScanQrPopup.
    assert 'typeof webApp.closeScanQrPopup !== "function"' in telegram_js
    # The call sits in the scanner path, not in an unrelated helper.
    show_scan = _function_source(telegram_js, "showScanQr")
    assert "closeScanQrPopup()" in show_scan
    # The scanned text is still resolved after the popup is dismissed.
    assert "resolve(scanned || null)" in show_scan
    # Dismissing is guarded: a client that throws must not lose the scan.
    close_source = _function_source(telegram_js, "closeScanQrPopup")
    assert "try {" in close_source and "catch (err)" in close_source


def test_the_bundle_uses_one_cache_buster():
    app_js = _js("app.js")
    api_js = _js("api.js")
    store_js = _js("store.js")
    version = "v=20261117-calls"
    # One version for the whole graph, or the bundle ends up with two
    # copies of a module and its state is computed twice.
    for name in ("telegram.js", "store.js", "ui.js", "api.js"):
        assert '"./' + name + "?" + version + '"' in app_js, name
    assert "./telegram.js?" + version in api_js
    assert "./api.js?" + version in store_js
    assert "./ui.js?" + version in store_js
    # No plain specifier is left behind: a bare import would be served
    # from the cached copy and split the module identity.
    for name in ("telegram.js", "store.js", "ui.js", "api.js"):
        assert '"./' + name + '"' not in app_js, name
    assert '"./telegram.js"' not in api_js
    assert '"./api.js"' not in store_js
    assert '"./ui.js"' not in store_js


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


def test_a_bare_six_character_code_is_resolved_by_the_backend():
    telegram_js = _js("telegram.js")
    store_js = _js("store.js")
    app_js = _js("app.js")
    # The helper is separate from the full payload parser: it only
    # accepts a code of the scan alphabet, never a whole payload.
    assert "export function bareScanCode" in telegram_js
    code_source = _function_source(telegram_js, "bareScanCode")
    assert r"/[\s-]+/" in code_source or "/[\\s-]+/" in code_source
    assert "toUpperCase()" in code_source
    helper_re = re.search(r"const SCAN_CODE_RE = (/[^/]+/);", telegram_js)
    assert helper_re, "the scan alphabet must be a literal pattern"
    assert "{6}" in helper_re.group(1)
    # The lookup endpoint carries only the typed code.
    assert 'apiPost("pair/lookup"' in store_js
    assert "scan_code: String(scanCode" in store_js
    # A failed payload parse falls back to the bare code, and the draft
    # uses the pairing id the backend resolved.
    preview_source = _function_source(app_js, "previewPairing")
    assert "tg.pairToken(payload)" in preview_source
    assert "tg.bareScanCode(payload)" in preview_source
    assert "store.pairLookup(" in preview_source
    assert "preview.pairing_id" in preview_source
    # The preview and the approval keep the typed code.
    assert "scan_code: scanCode" in preview_source
    assert "pairDraft.scanCode" in _function_source(app_js, "approvePairing")


def test_the_manual_pairing_entry_documents_the_six_character_code():
    html = _html()
    assert "six-character code" in html
    assert "ABC123" in html
    assert "pair_…" in html
    # The sheet explains both ways of entering a code.
    assert "Type the six characters" in html
    assert "full pairing link works too" in html


def test_the_pairing_error_names_the_bare_code():
    app_js = _js("app.js")
    source = _function_source(app_js, "pairErrorMessage")
    assert "six-character" in source
    assert "ambiguous" in source


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


# --- the calls surface ---------------------------------------------------


def test_the_calls_api_client_uses_the_device_routes():
    api_js = _js("api.js")
    for name in ("getCalls", "getLiveCall", "sendCallAction",
                 "markCallsRead"):
        assert "export function " + name + "(" in api_js, name
    # Every path hangs off the device of the bearer token route.
    assert '"/calls"' in api_js
    assert '"/calls/live"' in api_js
    assert '"/calls/actions"' in api_js
    assert '"/calls/read"' in api_js
    # The cursor has two halves and the search one query parameter.
    assert "params.before_ts = opts.beforeTs" in api_js
    assert "params.before_id = opts.beforeKey" in api_js
    assert "params.q = opts.q" in api_js
    # The action body is idempotent through the client_ref.
    assert "client_ref: String(opts.clientRef" in api_js
    assert "call_ref: String(opts.callRef" in api_js
    assert "until_ts: Math.floor(" in api_js
    # No initData outside the header helper: no call function reads it.
    assert "headers[AUTH_HEADER] = initData" in api_js
    assert "initData" in api_js.split("export function")[0]
    for name in ("getCalls", "getLiveCall", "sendCallAction",
                 "markCallsRead"):
        assert "initData" not in _function_source(api_js, name), name


def test_the_store_holds_the_call_state_and_resets_it_per_device():
    store_js = _js("store.js")
    for field in ("calls:", "callsHasMore:", "callsCursor:", "callsQuery:",
                  "liveCall:", "callActionPending:"):
        assert field in store_js, field
    # The cursor is the pair of the server, never a single number.
    assert "params.beforeTs = state.callsCursor.ts" in store_js
    assert "params.beforeKey = state.callsCursor.id" in store_js
    # A device switch forgets the calls of the old device.
    select_source = _function_source(store_js, "selectDevice")
    assert "state.calls = []" in select_source
    assert "state.callsCursor = null" in select_source
    assert "state.liveCall = null" in select_source
    # Loading is per device and paged.
    for name in ("loadCalls", "loadMoreCalls", "loadLiveCall",
                 "sendCallAction", "markCallsRead"):
        assert "export async function " + name + "(" in store_js, name


def test_a_missing_calls_endpoint_degrades_to_the_empty_state():
    store_js = _js("store.js")
    load_source = _function_source(store_js, "loadCalls")
    live_source = _function_source(store_js, "loadLiveCall")
    # A 404 is the "not available yet" state, not an exception.
    assert "err.status === 404" in load_source
    assert "state.callsAvailable = false" in load_source
    assert "err.status === 404" in live_source
    ui_js = _js("ui.js")
    assert "No calls yet" in ui_js
    assert "Calls are not available on this device yet" in ui_js


def test_a_call_action_is_idempotent_and_refreshes_a_stale_call():
    store_js = _js("store.js")
    source = _function_source(store_js, "sendCallAction")
    # The retry of the same action reuses the client_ref, so the unique
    # index of the backend turns a lost answer into a duplicate.
    assert "lastCallAttempt.clientRef" in source
    assert "sameCallAttempt(" in source
    assert "crypto.randomUUID()" in store_js
    # A stale or expired call is surfaced and the live call is reloaded.
    assert "err.status === 409" in source
    assert "err.status === 410" in source
    assert "await loadLiveCall(target)" in source
    # Emergency numbers are refused with their own message.
    assert "emergency_number" in source
    # The busy guard is published before the request and always cleared,
    # or one failed action would block every later one.
    assert "ui.setCallActionsBusy(true)" in source
    assert "state.callActionPending = null;" in source
    assert "finally {" in source


def test_the_revision_refresh_includes_the_live_call_for_the_open_device():
    store_js = _js("store.js")
    source = _function_source(store_js, "refreshFromChange")
    # A device-wide change (thread_key === null) refreshes the live call
    # and the call list of the open device only.
    assert "callsDirty" in source
    assert "changedThread === null" in source
    assert "await loadLiveCall(deviceId)" in source
    assert "await loadCalls(deviceId" in source
    # Another device never reloads the calls of this one.
    assert "changedDevice !== deviceId" in source


def test_the_calls_route_is_in_the_hash_and_round_trips():
    app_js = _js("app.js")
    assert 'parts[2] === "calls"' in app_js
    assert "function callsHash(" in app_js
    assert '"/calls"' in app_js
    assert 'target.name === "calls"' in app_js
    assert "export" not in app_js or "callsHash" in app_js
    # The existing routes keep working.
    assert 'parts[2] === "t"' in app_js
    assert "deviceHash" in app_js
    assert "threadHash" in app_js
    assert "ui.setRoute(\"calls\")" in app_js
    # The calls screen loads the live call and marks the log as read.
    calls_source = _function_source(app_js, "openCalls")
    assert "loadCallsScreen(" in calls_source
    assert "loadLiveCallQuietly" in calls_source
    # Opening the tab marks the log as read, and the store skips the
    # write when the same marker is already known.
    assert "store.markCallsRead(" in calls_source
    assert "until <= state.callsReadMarkedTs" in _js("store.js")
    screen_source = _function_source(app_js, "loadCallsScreen")
    assert "store.loadCalls(" in screen_source


def test_the_search_scope_follows_the_open_tab():
    app_js = _js("app.js")
    source = _function_source(app_js, "runSearch")
    assert 'ui.deviceTab() === "calls"' in source
    assert "store.loadCalls(deviceId" in source
    assert "store.loadChats(deviceId" in source
    # A call-only search never touches the conversation list.
    assert source.index("store.loadCalls(deviceId") < source.index(
        "store.loadChats(deviceId")


def test_the_tab_header_is_accessible_and_rendered():
    html = _html()
    ui_js = _js("ui.js")
    # The shell carries only the cache-buster; the calls surface is built
    # by ui.js with the same create() helper as every other row.
    assert '<base href="/relay/app/">' in html
    for marker in ('tabBarEl.setAttribute("role", "tablist")',
                   'tabBarEl.id = "tab-bar"',
                   'tabChatsEl.id = "tab-chats"',
                   'tabCallsEl.id = "tab-calls"',
                   'aria-controls", "chat-list"',
                   'aria-controls", "call-list"',
                   'callsSectionEl.id = "calls-section"',
                   'callListEl.id = "call-list"',
                   'callsStatusEl.id = "calls-status"',
                   'card.id = "live-call-card"'):
        assert marker in ui_js, marker
    assert ui_js.count('setAttribute("role", "tab")') == 2
    # The tab is toggled through aria-selected, not only through a class.
    tab_source = _function_source(ui_js, "setDeviceTab")
    assert 'setAttribute("aria-selected"' in tab_source
    assert "export function deviceTab" in ui_js


def test_the_call_sheets_and_the_live_card_exist():
    ui_js = _js("ui.js")
    sheets_source = _function_source(ui_js, "ensureSheets")
    for element_id in ("call-sheet", "call-sheet-callback", "call-sheet-via",
                       "call-sheet-cancel", "call-via-sheet",
                       "call-via-cancel", "keypad-sheet", "keypad-close"):
        assert '"' + element_id + '"' in sheets_source, element_id
    # The live card is built from data and has every control of the
    # contract.
    assert 'card.id = "live-call-card"' in ui_js
    for action in ("answer", "reject", "hangup", "mute", "speaker_on",
                   "dtmf", "hold"):
        assert '"' + action + '"' in ui_js, action
    assert "button.dataset.callAction" in ui_js
    # The delegated listener of app.js matches the attribute it emits.
    assert "data-call-action" in _js("app.js")
    assert "Unmute" in ui_js and "Speaker off" in ui_js and "Unhold" in ui_js


def test_call_rendering_uses_text_content_only():
    ui_js = _js("ui.js")
    combined = _all_js()
    # The global guard already forbids the helpers; the call rows, the
    # live card and the thread bubble must use the create() helper.
    for helper in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert helper not in combined
    for name in ("buildCallRow", "updateCallRow", "buildLiveCard",
                 "buildCallBubble", "updateCallBubble", "openCallViaSheet"):
        source = _function_source(ui_js, name)
        assert "create(" in source or "textContent" in source, name
        for helper in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            assert helper not in source, name
    # Every server string goes through textContent.
    assert "parts.title.textContent = title" in ui_js
    assert "parts.meta.textContent = meta" in ui_js
    assert "liveNodes.title.textContent = title" in ui_js


def test_the_call_via_sheet_uses_https_and_hides_emergency_numbers():
    ui_js = _js("ui.js")
    assert "https://wa.me/" in ui_js
    assert "https://t.me/+" in ui_js
    assert "https://signal.me/#p/+" in ui_js
    assert "https://ble.ir/" in ui_js
    assert "https://rubika.ir/" in ui_js
    assert "facetime://" in ui_js
    assert "iOS only" in ui_js
    assert "viber://chat" in ui_js
    assert "skype:" in ui_js
    # The emergency list matches the backend.
    assert '["110", "112", "115", "125", "911", "999", "000"]' in ui_js
    assert "export function isEmergencyNumber" in ui_js
    # Emergency numbers are hidden from the sheet and from call back.
    sheet_source = _function_source(ui_js, "openCallSheet")
    assert "isEmergencyNumber(number)" in sheet_source
    assert "Emergency numbers can only be dialled" in sheet_source
    bubble_source = _function_source(ui_js, "updateCallBubble")
    assert "isEmergencyNumber(number)" in bubble_source


def test_the_thread_renders_a_call_bubble():
    ui_js = _js("ui.js")
    store_js = _js("store.js")
    assert 'event.kind === "call"' in ui_js
    assert "bubble-call" in ui_js
    assert "data-call-back" in ui_js
    assert '"Call back"' in ui_js
    # The server names the key `event_id` for a call and `id` elsewhere;
    # the store normalizes both into one shape.
    assert "function normalizeEvent(" in store_js
    assert "event.event_id" in store_js
    assert "data.events.map(normalizeEvent)" in store_js


def test_the_live_timer_stops_when_the_screen_is_left():
    ui_js = _js("ui.js")
    assert "export function stopLiveTimer" in ui_js
    assert "setInterval(updateLiveElapsed, 1000)" in ui_js
    assert "clearInterval(liveTimer)" in ui_js
    route_source = _function_source(ui_js, "setRoute")
    assert "syncLiveTimer()" in route_source
    reset_source = _function_source(ui_js, "resetScope")
    assert "stopLiveTimer()" in reset_source
