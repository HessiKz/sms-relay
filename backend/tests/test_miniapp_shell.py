import time

from tests.test_miniapp_auth import API, HEADER, signed_init_data

DEVICE_HEADERS = {"Authorization": "Bearer device-token"}
CSP_HEADER = "content-security-policy"


def _csp(response):
    return response.headers.get(CSP_HEADER)


def test_shell_root_serves_html_with_csp(client):
    r = client.get("/app")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    csp = _csp(r)
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'self' https://web.telegram.org" in csp
    assert "https://*.telegram.org" in csp
    assert "base-uri 'self'" in csp
    assert "base-uri 'none'" not in csp
    assert "x-frame-options" not in r.headers
    assert r.headers["cache-control"] == "no-store"


def test_shell_trailing_slash_is_200(client):
    r = client.get("/app/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert _csp(r) is not None


def test_shell_document_references_assets_and_telegram_sdk(client):
    body = client.get("/app").text
    assert '<base href="/relay/app/">' in body
    assert "https://telegram.org/js/telegram-web-app.js?64" in body
    assert "assets/app.css" in body
    assert "assets/app.js" in body
    assert "<script>" not in body
    assert " style=" not in body
    assert "onclick=" not in body


def test_shell_security_headers(client):
    r = client.get("/app")
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=()")


def test_assets_are_served_with_no_cache(client):
    r = client.get("/app/assets/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"
    assert _csp(r) is not None

    css = client.get("/app/assets/app.css")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "no-cache"


def test_missing_asset_still_carries_csp(client):
    r = client.get("/app/assets/nope.js")
    assert r.status_code == 404
    assert _csp(r) is not None
    assert r.headers["cache-control"] == "no-cache"


def test_unauthorized_api_response_still_carries_csp_and_no_store(client):
    r = client.get(API)
    assert r.status_code == 401
    assert _csp(r) is not None
    assert r.headers["cache-control"] == "no-store"
    assert "x-frame-options" not in r.headers


def test_authorized_api_response_carries_csp(client):
    raw = signed_init_data(auth_date=int(time.time()))
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 200
    assert _csp(r) is not None
    assert r.headers["cache-control"] == "no-store"


def test_health_has_no_miniapp_csp(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert _csp(r) is None


def test_legacy_status_has_no_miniapp_csp(client):
    r = client.get("/status", headers=DEVICE_HEADERS)
    assert r.status_code == 200
    assert _csp(r) is None


def test_lookalike_path_has_no_miniapp_csp(client):
    # /application is not under /app/ and must stay untouched.
    r = client.get("/application")
    assert r.status_code == 404
    assert _csp(r) is None


def test_shell_api_route_is_registered_in_router(client):
    paths = {route.path for route in client.app.routes}
    assert "/app" in paths
    assert "/app/" in paths
    assert "/app/api/me" in paths
