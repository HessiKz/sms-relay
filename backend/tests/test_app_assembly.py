import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi.testclient import TestClient

import app.config as config
import app.main as app_main
import app.security as security
from app.main import BodySizeLimitMiddleware, create_app
from app.tg import TGError

DEVICE_HEADERS = {"Authorization": "Bearer device-token"}
ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}
NOW = int(time.time())
LIMIT = 524_288


def _env(database_path):
    return {
        "TELEGRAM_BOT_TOKEN": "test-tg-token",
        "TELEGRAM_CHAT_ID": "test-chat-id",
        "ADMIN_TELEGRAM_USER_ID": "7",
        "PHONE_A_TOKEN": "device-token",
        "ADMIN_TOKEN": "admin-token",
        "DATABASE_PATH": str(database_path),
    }


class _FakeTelegram:
    def __init__(self, poll_error=None, poll_delay=0.05):
        self.sent = []
        self.poll_error = poll_error
        self.poll_delay = poll_delay
        self.polls = 0

    async def send_to_chat(self, chat_id, text, *, reply_to=None, keyboard=None, parse_mode=None):
        self.sent.append((chat_id, text))
        return {"ok": True, "message_id": len(self.sent)}

    async def get_updates(self, offset, timeout):
        self.polls += 1
        if self.poll_error is not None:
            raise self.poll_error
        await asyncio.sleep(self.poll_delay)
        return []


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


class _BlockingTelegram(_FakeTelegram):
    """Fake telegram whose long-poll blocks, mimicking a pending poll."""

    def __init__(self, block_s=10.0):
        super().__init__()
        self.block_s = block_s

    async def get_updates(self, offset, timeout):
        self.polls += 1
        await asyncio.sleep(self.block_s)
        return []


class _RecorderApp:
    def __init__(self):
        self.scopes = []
        self.messages = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        while True:
            message = await receive()
            self.messages.append(message)
            if message["type"] != "http.request" or not message.get("more_body", False):
                break


def _scope(method="POST", headers=(), limit=LIMIT):
    return {
        "type": "http",
        "method": method,
        "path": "/sms",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "query_string": b"",
        "app": SimpleNamespace(state=SimpleNamespace(max_body_bytes=limit)),
    }


def _stream_chunks(chunks):
    remaining = [{"type": "http.request", "body": chunk, "more_body": True}
                 for chunk in chunks]
    remaining.append({"type": "http.request", "body": b"", "more_body": False})

    async def receive():
        if not remaining:
            return {"type": "http.request", "body": b"", "more_body": False}
        return remaining.pop(0)

    return receive, remaining


def _write_env_file(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "sample.env"
    text = fixture.read_text(encoding="utf-8").replace(
        "fixture-relay.db", str(tmp_path / "relay.db"))
    env_file = tmp_path / "sample.env"
    env_file.write_text(text, encoding="utf-8")
    return env_file


def test_assembly_starts_and_stops_workers(settings, monkeypatch):
    app = create_app(settings, run_workers=True)
    fake = _FakeTelegram()
    monkeypatch.setattr(app.state, "tg", fake)
    monkeypatch.setattr(app.state, "poll_tg", fake)

    with TestClient(app) as test_client:
        assert app.state.workers_enabled is True
        assert len(app.state.worker_tasks) == 2
        assert app.state.delivery_worker.poke_event is app.state.poke_event
        assert app.state.updater.poke_event is app.state.poke_event
        assert app.state.stop_event is not None
        _wait_for(lambda: fake.polls >= 1)
        r = test_client.get("/status", headers=DEVICE_HEADERS)
        assert r.status_code == 200
        assert r.json()["counts_by_state"] == {}
    assert app.state.stop_event.is_set()
    assert all(task.done() for task in app.state.worker_tasks)


def test_assembly_run_workers_false_disables_workers(settings):
    app = create_app(settings, run_workers=False)
    with TestClient(app) as test_client:
        assert app.state.workers_enabled is False
        assert app.state.worker_tasks == []
        assert app.state.http is not None
        assert app.state.tg is not None
        r = test_client.get("/status", headers=DEVICE_HEADERS)
        assert r.status_code == 200


def test_assembly_updater_failure_tolerated(settings, monkeypatch):
    app = create_app(settings, run_workers=True)
    fake = _FakeTelegram(poll_error=TGError("network"))
    monkeypatch.setattr(app.state, "tg", fake)
    monkeypatch.setattr(app.state, "poll_tg", fake)
    with TestClient(app):
        assert len(app.state.worker_tasks) == 2
    assert all(task.done() for task in app.state.worker_tasks)


def test_assembly_env_var_disables_updater(settings, monkeypatch):
    monkeypatch.setenv("RELAY_DISABLE_UPDATER", "1")
    app = create_app(settings, run_workers=True)
    with TestClient(app):
        assert len(app.state.worker_tasks) == 1
    assert app.state.worker_tasks[0].done()


def test_413_rejects_declared_oversize_payload(client):
    r = client.post("/sms", content=b"x" * (LIMIT + 1))
    assert r.status_code == 413
    assert r.json() == {"detail": "payload too large"}


def test_413_boundary_exactly_at_limit_passes(client):
    r = client.post("/sms", content=b"x" * LIMIT, headers=DEVICE_HEADERS)
    assert r.status_code == 422


def test_stream_over_limit_413_without_content_length():
    recorder = _RecorderApp()
    middleware = BodySizeLimitMiddleware(recorder)
    sent = []
    leftover = {}

    async def scenario():
        receive, remaining = _stream_chunks([b"a" * 300_000, b"b" * 300_000])

        async def send(message):
            sent.append(message)

        await middleware(_scope(), receive, send)
        leftover["messages"] = remaining

    asyncio.run(scenario())
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [413]
    assert recorder.scopes == []
    assert len(leftover["messages"]) == 1


def test_stream_under_limit_passes_body_through():
    recorder = _RecorderApp()
    middleware = BodySizeLimitMiddleware(recorder)
    sent = []

    async def scenario():
        receive, _ = _stream_chunks([b"hello ", b"world"])

        async def send(message):
            sent.append(message)

        await middleware(_scope(), receive, send)

    asyncio.run(scenario())
    bodies = [m.get("body", b"") for m in recorder.messages
              if m["type"] == "http.request"]
    assert b"".join(bodies) == b"hello world"


def test_stream_guard_applies_only_to_post_put():
    recorder = _RecorderApp()
    middleware = BodySizeLimitMiddleware(recorder)

    async def scenario():
        receive, _ = _stream_chunks([b"x" * (LIMIT + 1)])

        async def send(message):
            pass

        await middleware(_scope(method="GET",
                                headers=[("content-length", str(LIMIT + 1))]),
                         receive, send)

    asyncio.run(scenario())
    assert len(recorder.scopes) == 1


def test_validation_422_reports_no_offending_values(client):
    payload = {"id": "evt-1", "sender": "x" * 65, "body": "hi", "received_at": NOW}
    r = client.post("/sms", json=payload, headers=DEVICE_HEADERS)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "validation_error"
    assert isinstance(detail["fields"], list) and detail["fields"]
    assert all(isinstance(field, str) for field in detail["fields"])
    assert any("sender" in field for field in detail["fields"])
    assert "x" * 65 not in r.text
    assert "errors.pydantic.dev" not in r.text


def test_401_beats_422(client):
    payload = {"id": "evt-1", "sender": "x" * 65, "body": "hi", "received_at": NOW}
    r = client.post("/sms", json=payload, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


MALFORMED = b'{"id": "evt-1", "sender":'
MALFORMED_HEADERS = {"Content-Type": "application/json"}


def test_malformed_json_with_bad_token_returns_401(client):
    r = client.post("/sms", content=MALFORMED,
                    headers={**MALFORMED_HEADERS,
                             "Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}
    assert "fields" not in r.text
    assert "validation_error" not in r.text
    assert "JSON decode error" not in r.text


def test_malformed_json_with_unknown_token_returns_401(client):
    r = client.post("/sms", content=MALFORMED,
                    headers={**MALFORMED_HEADERS,
                             "Authorization": "Bearer admin-token-wrong"})
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


def test_malformed_json_with_good_token_stays_422_without_echo(client):
    r = client.post("/sms", content=MALFORMED,
                    headers={**MALFORMED_HEADERS, **DEVICE_HEADERS})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "validation_error"
    assert isinstance(detail["fields"], list) and detail["fields"]
    assert '{"id"' not in r.text
    assert "evt-1" not in r.text


def test_malformed_json_with_admin_good_token_stays_422(client):
    r = client.post("/sms", content=MALFORMED,
                    headers={**MALFORMED_HEADERS, **ADMIN_HEADERS})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "validation_error"


def test_malformed_json_without_auth_header_is_401(client):
    # No credential at all: the caller must not learn the request schema.
    r = client.post("/sms", content=MALFORMED, headers=MALFORMED_HEADERS)
    assert r.status_code == 401
    assert "validation_error" not in r.text


def test_poll_client_outlives_the_long_poll_timeout(settings):
    # The Telegram long poll holds the connection for poll_timeout seconds,
    # so the polling client needs a longer read timeout than the sender.
    app = create_app(settings, run_workers=False)
    assert app.state.poll_http is not app.state.http
    assert app.state.poll_http.timeout.read is not None
    assert app.state.poll_http.timeout.read >= 30
    assert app.state.http.timeout.read < app.state.poll_http.timeout.read


def test_sms_state_reports_per_id_delivery_state(client):
    payload = {"id": "state-check-1", "sender": "+15550001111",
               "body": "hello", "received_at": int(time.time())}
    assert client.post("/sms", json=payload,
                       headers=DEVICE_HEADERS).status_code == 200
    r = client.get("/sms-state?ids=state-check-1", headers=DEVICE_HEADERS)
    assert r.status_code == 200
    states = r.json()["states"]
    assert states["state-check-1"]["state"] == "pending"


def test_sms_state_rejects_too_many_ids(client):
    ids = ",".join(f"id-{index}" for index in range(60))
    r = client.get(f"/sms-state?ids={ids}", headers=DEVICE_HEADERS)
    assert r.status_code == 422


def test_rate_limiter_caps_tracked_keys():
    limiter = security.RateLimiter(max_events=10, window_s=60)
    for index in range(5000):
        limiter.allow(f"fail:{index}")
    assert len(limiter._events) <= security.MAX_TRACKED_KEYS


def test_shutdown_is_prompt_when_updater_blocked_in_long_poll(settings,
                                                               monkeypatch):
    app = create_app(settings, run_workers=True)
    fake = _BlockingTelegram(block_s=10.0)
    monkeypatch.setattr(app.state, "tg", fake)
    monkeypatch.setattr(app.state, "poll_tg", fake)
    monkeypatch.setattr(app.state, "poll_tg", fake)
    with TestClient(app):
        _wait_for(lambda: fake.polls >= 1)
        started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"shutdown took {elapsed:.2f}s"


def test_restart_persistence_across_apps(tmp_path, monkeypatch):
    settings = config.load_settings(env=_env(tmp_path / "relay.db"))
    headers = DEVICE_HEADERS
    payload = {"id": "evt-r1", "sender": "+15550100", "body": "persist me",
               "received_at": NOW}

    first = create_app(settings, run_workers=False)
    with TestClient(first) as client_a:
        r = client_a.post("/sms", json=payload, headers=headers)
        assert r.status_code == 200
        assert r.json() == {"accepted": True, "duplicate": False,
                            "state": "pending"}
        counts = client_a.get("/status", headers=headers).json()["counts_by_state"]
        assert counts == {"pending": 1}

    second = create_app(settings, run_workers=True)
    fake = _FakeTelegram()
    monkeypatch.setattr(second.state, "tg", fake)
    with TestClient(second) as client_b:
        def delivered():
            counts = client_b.get("/status", headers=headers).json()["counts_by_state"]
            return counts.get("delivered") == 1

        _wait_for(delivered)
        body = client_b.get("/status", headers=headers).json()
        assert body["counts_by_state"] == {"delivered": 1}
        assert body["queue"] == 0


def test_main_builds_app_from_env_file(tmp_path, monkeypatch):
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    env_file = _write_env_file(tmp_path)

    app_main.main(["--env-file", str(env_file)])

    settings = captured["app"].state.settings
    assert settings.telegram_bot_token == "fixture-tg-token"
    assert settings.telegram_chat_id == "fixture-chat-id"
    assert settings.admin_telegram_user_id == 7
    assert settings.phone_a_token == "fixture-device-token"
    assert settings.admin_token == "fixture-admin-token"
    assert settings.database_path == str(tmp_path / "relay.db")
    assert captured.get("host") == "127.0.0.1"
    assert captured.get("port") == 8090
    assert captured.get("log_level") == "warning"
    asyncio.run(captured["app"].state.http.aclose())


def test_main_cli_overrides(tmp_path, monkeypatch):
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    env_file = _write_env_file(tmp_path)

    app_main.main(["--env-file", str(env_file), "--host", "0.0.0.0",
                   "--port", "9443", "--log-level", "debug"])

    assert captured.get("host") == "0.0.0.0"
    assert captured.get("port") == 9443
    assert captured.get("log_level") == "debug"
    asyncio.run(captured["app"].state.http.aclose())
