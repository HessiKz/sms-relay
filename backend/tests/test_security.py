import time

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.dependencies.utils import get_dependant
from fastapi.testclient import TestClient
from types import SimpleNamespace

import app.db as db
from app import security
from app.config import Settings
from app.security import RateLimiter, token_equal

SETTINGS = Settings(
    telegram_bot_token="t", telegram_chat_id="c", admin_telegram_user_id=1,
    admin_token="adm", database_path="/tmp/x.db",
)
DEVICE_TOKEN = "sr_test-token"
OLD_PHONE_TOKEN = "pat"


class FakeRequest:
    def __init__(self, headers=None, conn=None):
        self.headers = headers if headers is not None else {}
        self.state = SimpleNamespace()
        if conn is not None:
            self.app = SimpleNamespace(state=SimpleNamespace(db_conn=conn))


def bearer(value, conn=None):
    if value is None:
        return FakeRequest(conn=conn)
    return FakeRequest({"authorization": value}, conn=conn)


@pytest.fixture
def conn():
    connection = db.open_db(":memory:")
    now = int(time.time())
    db.activate_device(connection, "device-a", "Phone A", "android", "1.0.0", now)
    db.set_device_token(connection, "device-a", db.token_hash(DEVICE_TOKEN),
                        DEVICE_TOKEN[:11], now)
    yield connection
    connection.close()


def test_token_equal_true_for_equal():
    assert token_equal("pat", "pat") is True


def test_token_equal_false_same_length_diff_value():
    assert token_equal("pat", "pax") is False


@pytest.mark.parametrize("bad", ["", "p", "pat ", "patx", "x" * 64])
def test_token_equal_false_for_other_lengths(bad):
    assert token_equal("pat", bad) is False


@pytest.mark.parametrize("bad", [None, 123, b"pat", ["pat"], True])
def test_token_equal_false_for_invalid_types(bad):
    assert token_equal("pat", bad) is False


def test_token_equal_symmetric_and_self():
    assert token_equal(None, None) is False
    assert token_equal("adm", "adm") is True


def test_limiter_allows_first_n_then_false():
    rl = RateLimiter(max_events=3, window_s=60)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False
    assert rl.allow("k") is False


def test_limiter_keys_independent():
    rl = RateLimiter(max_events=1, window_s=60)
    assert rl.allow("a") is True
    assert rl.allow("b") is True
    assert rl.allow("a") is False
    assert rl.allow("b") is False


def test_limiter_frees_again_after_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(security.time, "time", lambda: now[0])
    rl = RateLimiter(max_events=2, window_s=60)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False
    now[0] += 59.0
    assert rl.allow("k") is False
    now[0] += 1.0
    assert rl.allow("k") is True


def test_limiter_sliding_window_partial_expiry(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(security.time, "time", lambda: now[0])
    rl = RateLimiter(max_events=2, window_s=60)
    assert rl.allow("k") is True
    now[0] = 45.0
    assert rl.allow("k") is True
    assert rl.allow("k") is False
    now[0] = 61.0
    assert rl.allow("k") is True


@pytest.mark.anyio
async def test_require_device_accepts_a_paired_device_token(conn):
    dep = security.require_device(SETTINGS)
    request = bearer("Bearer " + DEVICE_TOKEN, conn)
    await dep(request)
    assert request.state.device["id"] == "device-a"


@pytest.mark.anyio
async def test_require_device_rejects_the_old_shared_token(conn):
    # The shared phone token is gone: only a paired device gets in.
    dep = security.require_device(SETTINGS)
    with pytest.raises(HTTPException) as exc:
        await dep(bearer("Bearer " + OLD_PHONE_TOKEN, conn))
    assert exc.value.status_code == 401
    assert exc.value.detail == "unauthorized"


@pytest.mark.anyio
async def test_require_device_rejects_wrong_token(conn):
    dep = security.require_device(SETTINGS)
    with pytest.raises(HTTPException) as exc:
        await dep(bearer("Bearer adm", conn))
    assert exc.value.status_code == 401
    assert exc.value.detail == "unauthorized"


@pytest.mark.anyio
async def test_require_device_rejects_missing_and_malformed(conn):
    for req in (bearer(None, conn), bearer("", conn), bearer("pat", conn),
                bearer("Token " + DEVICE_TOKEN, conn)):
        dep = security.require_device(SETTINGS)
        with pytest.raises(HTTPException) as exc:
            await dep(req)
        assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_require_device_rejects_a_revoked_device(conn):
    now = int(time.time())
    db.revoke_device(conn, "device-a", now)
    dep = security.require_device(SETTINGS)
    with pytest.raises(HTTPException) as exc:
        await dep(bearer("Bearer " + DEVICE_TOKEN, conn))
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_require_admin_accepts_admin_token_only():
    assert await security.require_admin(SETTINGS)(bearer("Bearer adm")) is None
    with pytest.raises(HTTPException) as exc:
        await security.require_admin(SETTINGS)(bearer("Bearer pat"))
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_require_any_accepts_device_or_admin_token(conn):
    request = bearer("Bearer " + DEVICE_TOKEN, conn)
    assert await security.require_any(SETTINGS)(request) is None
    assert request.state.device["id"] == "device-a"
    admin_request = bearer("Bearer adm", conn)
    assert await security.require_any(SETTINGS)(admin_request) is None
    assert admin_request.state.device is None
    with pytest.raises(HTTPException) as exc:
        await security.require_any(SETTINGS)(bearer("Bearer nope", conn))
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_dep_switches_to_429_after_failure_threshold(conn):
    dep = security.require_device(SETTINGS)
    req = bearer("Bearer bad", conn)
    for _ in range(10):
        with pytest.raises(HTTPException) as exc:
            await dep(req)
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        await dep(req)
    assert exc.value.status_code == 429


def test_dep_request_param_resolves_as_request_and_enforces_auth(conn):
    dep = security.require_device(SETTINGS)
    app = FastAPI()

    @app.get("/x")
    async def guarded_route(_principal=Depends(dep)):
        return {"ok": True}

    dependant = get_dependant(path="/x", call=dep)
    assert dependant.request_param_name == "request"
    assert dependant.query_params == []

    client = TestClient(app)
    assert client.get("/x").status_code == 401
    assert client.get("/x", headers={"Authorization": "Bearer wrong"}).status_code == 401
