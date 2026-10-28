import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.dependencies.utils import get_dependant
from fastapi.testclient import TestClient

from app import security
from app.config import Settings
from app.security import RateLimiter, token_equal

SETTINGS = Settings(
    telegram_bot_token="t", telegram_chat_id="c", admin_telegram_user_id=1,
    phone_a_token="pat", admin_token="adm", database_path="/tmp/x.db",
)


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers if headers is not None else {}


def bearer(value):
    if value is None:
        return FakeRequest()
    return FakeRequest({"authorization": value})


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
async def test_require_device_accepts_correct_token():
    dep = security.require_device(SETTINGS)
    resp = await dep(bearer("Bearer pat"))
    assert resp is None


@pytest.mark.anyio
async def test_require_device_rejects_wrong_token():
    dep = security.require_device(SETTINGS)
    with pytest.raises(HTTPException) as exc:
        await dep(bearer("Bearer adm"))
    assert exc.value.status_code == 401
    assert exc.value.detail == "unauthorized"


@pytest.mark.anyio
async def test_require_device_rejects_missing_and_malformed():
    for req in (bearer(None), bearer(""), bearer("pat"), bearer("Token pat")):
        dep = security.require_device(SETTINGS)
        with pytest.raises(HTTPException) as exc:
            await dep(req)
        assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_require_admin_accepts_admin_token_only():
    assert await security.require_admin(SETTINGS)(bearer("Bearer adm")) is None
    with pytest.raises(HTTPException) as exc:
        await security.require_admin(SETTINGS)(bearer("Bearer pat"))
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_require_any_accepts_device_or_admin_token():
    assert await security.require_any(SETTINGS)(bearer("Bearer pat")) is None
    assert await security.require_any(SETTINGS)(bearer("Bearer adm")) is None
    with pytest.raises(HTTPException) as exc:
        await security.require_any(SETTINGS)(bearer("Bearer nope"))
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_dep_switches_to_429_after_failure_threshold():
    dep = security.require_device(SETTINGS)
    req = bearer("Bearer bad")
    for _ in range(10):
        with pytest.raises(HTTPException) as exc:
            await dep(req)
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        await dep(req)
    assert exc.value.status_code == 429


def test_dep_request_param_resolves_as_request_and_enforces_auth():
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
    assert client.get("/x", headers={"Authorization": "Bearer pat"}).status_code == 200
