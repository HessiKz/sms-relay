import json
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "https://api.telegram.org"
MAX_TEXT_LEN = 4000
KINDS = frozenset({"timeout", "unavailable", "rate_limited", "api_error", "bad_request", "network"})


class TGError(Exception):
    kind: str
    retry_after: Optional[int]
    detail: Optional[str]

    def __init__(self, kind: str, *, retry_after: Optional[int] = None, detail: Optional[str] = None):
        if kind not in KINDS:
            raise ValueError(f"unknown kind: {kind}")
        self.kind = kind
        self.retry_after = retry_after
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


def _json_body(response: httpx.Response) -> Optional[dict]:
    try:
        body = response.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        http: httpx.AsyncClient,
        *,
        base_url: str = DEFAULT_BASE_URL,
    ):
        if not bot_token:
            raise ValueError("bot_token is required")
        self.bot_token = bot_token
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def send_to_chat(self, chat_id: Any, text: str, *, reply_to: Optional[int] = None,
                           keyboard: Optional[dict] = None,
                           parse_mode: Optional[str] = None) -> dict:
        if chat_id is None or chat_id == "":
            raise ValueError("chat_id is required")
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT_LEN:
            raise ValueError(f"text must be a str of length 1..{MAX_TEXT_LEN}")
        if not text.strip():
            raise ValueError("text must not be blank")
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
        if keyboard:
            payload["reply_markup"] = keyboard
        if parse_mode:
            payload["parse_mode"] = parse_mode
        url = f"{self.base_url}/bot{self.bot_token}/sendMessage"
        try:
            response = await self.http.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise TGError("timeout", detail=f"timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise TGError("network", detail=f"transport failure ({type(exc).__name__})") from exc
        return self._interpret(response)

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        """Long-poll Telegram getUpdates; param types come from the API spec."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        url = f"{self.base_url}/bot{self.bot_token}/getUpdates"
        try:
            response = await self.http.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise TGError("timeout", detail=f"timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise TGError("network", detail=f"transport failure ({type(exc).__name__})") from exc
        return self._interpret_updates(response)

    def _interpret_updates(self, response: httpx.Response) -> list[dict]:
        status = response.status_code
        if status == 429:
            body = _json_body(response)
            parameters = body.get("parameters") if body is not None else None
            if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int):
                retry_after = parameters["retry_after"]
            else:
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
            raise TGError("rate_limited", retry_after=retry_after, detail="rate limited (429)")
        if status >= 500:
            raise TGError("api_error", detail=f"telegram server error (status {status})")
        if 200 <= status < 300:
            body = _json_body(response)
            if body is not None and body.get("ok") is True:
                result = body.get("result")
                if isinstance(result, list):
                    return result
            raise TGError("api_error", detail="malformed getUpdates response")
        body = _json_body(response)
        if body is not None and body.get("ok") is False:
            description = body.get("description")
            detail = str(description)[:200] if description is not None else ""
            raise TGError("bad_request", detail=f"telegram rejected request (status {status}): {detail}".strip())
        raise TGError("api_error", detail=f"unexpected response (status {status})")

    async def edit_message_text(self, chat_id: Any, message_id: int, text: str, *,
                               keyboard: Optional[dict] = None,
                               parse_mode: Optional[str] = None) -> dict:
        if chat_id is None or chat_id == "":
            raise ValueError("chat_id is required")
        if not isinstance(message_id, int):
            raise ValueError("message_id must be an int")
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT_LEN:
            raise ValueError(f"text must be a str of length 1..{MAX_TEXT_LEN}")
        if not text.strip():
            raise ValueError("text must not be blank")
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id,
                                   "text": text}
        if keyboard:
            payload["reply_markup"] = keyboard
        if parse_mode:
            payload["parse_mode"] = parse_mode
        url = f"{self.base_url}/bot{self.bot_token}/editMessageText"
        try:
            response = await self.http.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise TGError("timeout", detail=f"timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise TGError("network", detail=f"transport failure ({type(exc).__name__})") from exc
        return self._interpret(response)

    async def answer_callback_query(self, callback_query_id: str,
                                    text: Optional[str] = None) -> None:
        if not callback_query_id:
            raise ValueError("callback_query_id is required")
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        url = f"{self.base_url}/bot{self.bot_token}/answerCallbackQuery"
        try:
            await self.http.post(url, json=payload)
        except (httpx.TimeoutException, httpx.TransportError):
            pass

    def _interpret(self, response: httpx.Response) -> dict:
        status = response.status_code
        if status == 429:
            body = _json_body(response)
            parameters = body.get("parameters") if body is not None else None
            if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int):
                retry_after = parameters["retry_after"]
            else:
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
            raise TGError("rate_limited", retry_after=retry_after, detail="rate limited (429)")
        if 200 <= status < 300:
            body = _json_body(response)
            if body is not None and body.get("ok") is True:
                result = body.get("result")
                if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                    return {"ok": True, "message_id": result["message_id"]}
                raise TGError("api_error", detail="response missing message_id")
            raise TGError("api_error", detail="malformed response body")
        if status >= 500:
            raise TGError("api_error", detail=f"telegram server error (status {status})")
        body = _json_body(response)
        if body is not None and body.get("ok") is False:
            description = body.get("description")
            detail = str(description)[:200] if description is not None else ""
            raise TGError("bad_request", detail=f"telegram rejected request (status {status}): {detail}".strip())
        raise TGError("api_error", detail=f"unexpected response (status {status})")
