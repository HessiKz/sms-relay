import json

import httpx
import pytest

from app.tg import TGError, TelegramClient

BOT_TOKEN = "123:abc"


def _handler_factory(respond=None, raise_error=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if raise_error is not None:
            raise raise_error
        if callable(respond):
            return respond(request)
        return respond

    return handler, calls


def _body(request: httpx.Request):
    return json.loads(request.read())


def _tg_response(status: int, body=None, *, headers=None) -> httpx.Response:
    if body is None and status == 429:
        return httpx.Response(status, content=b"", headers=headers or {})
    return httpx.Response(status, json=body, headers=headers or {})


def _make_client(handler, *, base_url: str = "https://api.telegram.org") -> TelegramClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramClient(BOT_TOKEN, http, base_url=base_url)


@pytest.mark.anyio
async def test_success_parses_message_id():
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": {"message_id": 42}}))
    telegram = _make_client(handler)

    result = await telegram.send_to_chat("-100", "hello")

    assert result == {"ok": True, "message_id": 42}
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == f"/bot{BOT_TOKEN}/sendMessage"
    assert _body(request) == {"chat_id": "-100", "text": "hello"}


@pytest.mark.anyio
async def test_base_url_configurable():
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": {"message_id": 1}}))
    telegram = _make_client(handler, base_url="http://tg.test/")

    result = await telegram.send_to_chat(123, "hello")

    assert result == {"ok": True, "message_id": 1}
    assert calls[0].url.path == f"/bot{BOT_TOKEN}/sendMessage"


@pytest.mark.anyio
async def test_400_ok_false_maps_to_bad_request():
    handler, calls = _handler_factory(
        _tg_response(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
    )
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    error = exc_info.value
    assert error.kind == "bad_request"
    assert error.retry_after is None
    assert "chat not found" in error.detail
    assert "hello" not in (error.detail or "")
    assert len(calls) == 1


@pytest.mark.anyio
async def test_400_description_truncated():
    long_description = "Bad Request: " + "x" * 300
    handler, _ = _handler_factory(_tg_response(400, {"ok": False, "description": long_description}))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    detail = exc_info.value.detail
    assert len(detail) <= len("telegram rejected request (status 400): ") + 200
    assert long_description[:200] in detail


@pytest.mark.anyio
async def test_429_with_parameters_retry_after():
    handler, _ = _handler_factory(
        _tg_response(429, {"ok": False, "parameters": {"retry_after": 5}})
    )
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    error = exc_info.value
    assert error.kind == "rate_limited"
    assert error.retry_after == 5


@pytest.mark.anyio
async def test_429_retry_after_header():
    handler, _ = _handler_factory(_tg_response(429, headers={"Retry-After": "7"}))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    error = exc_info.value
    assert error.kind == "rate_limited"
    assert error.retry_after == 7


@pytest.mark.anyio
async def test_429_without_retry_after_sources():
    handler, _ = _handler_factory(
        _tg_response(429, {"ok": False, "description": "Too Many Requests: retry after 9"})
    )
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    error = exc_info.value
    assert error.kind == "rate_limited"
    assert error.retry_after is None


@pytest.mark.anyio
async def test_500_maps_to_api_error():
    handler, _ = _handler_factory(_tg_response(500, {"ok": False, "description": "internal"}))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    error = exc_info.value
    assert error.kind == "api_error"
    assert error.retry_after is None
    assert "500" in error.detail


@pytest.mark.anyio
async def test_2xx_ok_false_maps_to_api_error():
    handler, _ = _handler_factory(_tg_response(200, {"ok": False, "description": "weird"}))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    assert exc_info.value.kind == "api_error"


@pytest.mark.anyio
async def test_2xx_malformed_json_maps_to_api_error():
    handler, _ = _handler_factory(httpx.Response(200, content=b"<html>oops</html>"))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    assert exc_info.value.kind == "api_error"


@pytest.mark.anyio
async def test_2xx_missing_message_id_maps_to_api_error():
    handler, _ = _handler_factory(_tg_response(200, {"ok": True, "result": {}}))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    assert exc_info.value.kind == "api_error"


@pytest.mark.anyio
async def test_read_timeout_maps_to_timeout():
    handler, _ = _handler_factory(raise_error=httpx.ReadTimeout("read timed out"))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    error = exc_info.value
    assert error.kind == "timeout"
    assert error.retry_after is None


@pytest.mark.anyio
async def test_connect_error_maps_to_network():
    handler, _ = _handler_factory(raise_error=httpx.ConnectError("no route"))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.send_to_chat("-100", "hello")

    error = exc_info.value
    assert error.kind == "network"
    assert error.retry_after is None


@pytest.mark.anyio
@pytest.mark.parametrize("chat_id", [None, ""])
async def test_invalid_chat_id_raises_value_error(chat_id):
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": {"message_id": 1}}))
    telegram = _make_client(handler)

    with pytest.raises(ValueError):
        await telegram.send_to_chat(chat_id, "hello")

    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_text_raises_value_error(text):
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": {"message_id": 1}}))
    telegram = _make_client(handler)

    with pytest.raises(ValueError):
        await telegram.send_to_chat("-100", text)

    assert calls == []


@pytest.mark.anyio
async def test_oversized_text_raises_value_error():
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": {"message_id": 1}}))
    telegram = _make_client(handler)

    with pytest.raises(ValueError):
        await telegram.send_to_chat("-100", "x" * 4001)

    assert calls == []


@pytest.mark.anyio
async def test_text_at_limit_is_accepted():
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": {"message_id": 9}}))
    telegram = _make_client(handler)

    result = await telegram.send_to_chat("-100", "x" * 4000)

    assert result == {"ok": True, "message_id": 9}
    assert _body(calls[0])["text"] == "x" * 4000


@pytest.mark.anyio
async def test_reply_to_included_in_payload():
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": {"message_id": 3}}))
    telegram = _make_client(handler)

    result = await telegram.send_to_chat("-100", "hello", reply_to=77)

    assert result == {"ok": True, "message_id": 3}
    assert _body(calls[0]) == {"chat_id": "-100", "text": "hello", "reply_to_message_id": 77}


@pytest.mark.parametrize("bot_token", [None, ""])
def test_invalid_bot_token_raises_value_error(bot_token):
    with pytest.raises(ValueError):
        TelegramClient(bot_token, httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))))


@pytest.mark.anyio
async def test_get_updates_parses_result_list():
    handler, calls = _handler_factory(
        _tg_response(200, {"ok": True, "result": [{"update_id": 1}, {"update_id": 2}]})
    )
    telegram = _make_client(handler)

    updates = await telegram.get_updates(None, 30)

    assert updates == [{"update_id": 1}, {"update_id": 2}]
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "GET"
    assert request.url.path == f"/bot{BOT_TOKEN}/getUpdates"
    assert request.url.params["timeout"] == "30"
    assert "offset" not in request.url.params


@pytest.mark.anyio
async def test_get_updates_includes_offset_when_given():
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": []}))
    telegram = _make_client(handler)

    updates = await telegram.get_updates(11, 0)

    assert updates == []
    assert calls[0].url.params["offset"] == "11"
    assert calls[0].url.params["timeout"] == "0"


@pytest.mark.anyio
async def test_get_updates_empty_result_returns_empty_list():
    handler, calls = _handler_factory(_tg_response(200, {"ok": True, "result": []}))
    telegram = _make_client(handler)

    updates = await telegram.get_updates(None, 30)

    assert updates == []
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("body", [{"ok": True}, {"ok": True, "result": {}},
                                  {"ok": True, "result": None}, None])
async def test_get_updates_malformed_body_maps_to_api_error(body):
    handler, _ = _handler_factory(_tg_response(200, body))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.get_updates(None, 30)

    assert exc_info.value.kind == "api_error"


@pytest.mark.anyio
async def test_get_updates_429_maps_to_rate_limited():
    handler, _ = _handler_factory(
        _tg_response(429, {"ok": False, "parameters": {"retry_after": 5}})
    )
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.get_updates(None, 30)

    error = exc_info.value
    assert error.kind == "rate_limited"
    assert error.retry_after == 5


@pytest.mark.anyio
async def test_get_updates_400_ok_false_maps_to_bad_request():
    handler, calls = _handler_factory(
        _tg_response(400, {"ok": False, "description": "Bad Request: conflict terminated"})
    )
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.get_updates(None, 30)

    error = exc_info.value
    assert error.kind == "bad_request"
    assert "conflict terminated" in error.detail
    assert len(calls) == 1


@pytest.mark.anyio
async def test_get_updates_5xx_maps_to_api_error():
    handler, _ = _handler_factory(_tg_response(502, {"ok": False, "description": "bad gateway"}))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.get_updates(None, 30)

    error = exc_info.value
    assert error.kind == "api_error"
    assert "502" in error.detail


@pytest.mark.anyio
async def test_get_updates_read_timeout_maps_to_timeout():
    handler, _ = _handler_factory(raise_error=httpx.ReadTimeout("poll timed out"))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.get_updates(None, 30)

    assert exc_info.value.kind == "timeout"


@pytest.mark.anyio
async def test_get_updates_connect_error_maps_to_network():
    handler, _ = _handler_factory(raise_error=httpx.ConnectError("no route"))
    telegram = _make_client(handler)

    with pytest.raises(TGError) as exc_info:
        await telegram.get_updates(None, 30)

    assert exc_info.value.kind == "network"
