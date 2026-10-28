"""Telegram Mini App initData validation and the request dependency.

The HMAC scheme follows the official Bot API documentation: the secret is
``HMAC_SHA256("WebAppData", bot_token)`` and the message is the URL-decoded,
alphabetically sorted ``key=value`` pairs joined by newlines, excluding
``hash``. The ``signature`` field is a normal received field and therefore
part of that message.

The raw initData is never logged and never embedded in an exception: it is a
short-lived credential, but it still authenticates its holder.
"""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request

from app import security

INIT_DATA_HEADER = "X-Telegram-Init-Data"
MAX_INIT_DATA_BYTES = 8 * 1024
MAX_INIT_DATA_FIELDS = 64
MAX_FUTURE_SKEW_S = 300
MAX_FAILURES = 20
FAILURE_WINDOW_S = 60.0

_MISSING = "unauthorized"
_INVALID = "invalid init data"
_FORBIDDEN = "forbidden"
_RATE_LIMITED = "rate limited"
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class MiniAppAuthError(Exception):
    """Raised when initData is absent, malformed, stale or badly signed."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _parse(raw: str) -> dict:
    if not isinstance(raw, str) or not raw:
        raise MiniAppAuthError("missing init data")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=False,
                          max_num_fields=MAX_INIT_DATA_FIELDS)
    except ValueError:
        raise MiniAppAuthError("malformed init data") from None
    if not pairs:
        raise MiniAppAuthError("empty init data")
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise MiniAppAuthError("duplicate init data fields")
    return dict(pairs)


def _check_hash(init_data: dict, bot_token: str) -> None:
    received = init_data.get("hash", "")
    if not received or not _HEX_RE.match(received):
        raise MiniAppAuthError("missing or malformed hash")
    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(init_data.items())
        if key != "hash"
    )
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"),
                      hashlib.sha256).digest()
    expected = hmac.new(secret, data_check_string.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        raise MiniAppAuthError("invalid hash")


def _check_freshness(init_data: dict, max_age: int, now: float) -> int:
    raw_date = init_data.get("auth_date")
    if not raw_date:
        raise MiniAppAuthError("missing auth_date")
    try:
        auth_date = int(raw_date)
    except (TypeError, ValueError):
        raise MiniAppAuthError("malformed auth_date") from None
    if auth_date < now - max_age:
        raise MiniAppAuthError("stale init data")
    if auth_date > now + MAX_FUTURE_SKEW_S:
        raise MiniAppAuthError("init data from the future")
    return auth_date


def _check_user(init_data: dict) -> dict:
    raw_user = init_data.get("user")
    if not raw_user:
        raise MiniAppAuthError("missing user")
    try:
        user = json.loads(raw_user)
    except (TypeError, ValueError):
        raise MiniAppAuthError("malformed user") from None
    if not isinstance(user, dict):
        raise MiniAppAuthError("malformed user")
    user_id = user.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise MiniAppAuthError("malformed user id")
    return user


def validate_init_data(raw: str, bot_token: str, max_age: int,
                       now: float | None = None) -> dict:
    """Validate Telegram Mini App initData and return its trusted fields.

    Raises MiniAppAuthError on any failure. The message describes the reason
    only; the caller's raw value is never included.
    """
    if now is None:
        now = time.time()
    init_data = _parse(raw)
    _check_hash(init_data, bot_token)
    auth_date = _check_freshness(init_data, max_age, now)
    user = _check_user(init_data)
    return {
        "user": user,
        "auth_date": auth_date,
        "start_param": init_data.get("start_param") or None,
        "query_id": init_data.get("query_id") or None,
    }


def _client_key(request: Request) -> str:
    client = request.client
    host = getattr(client, "host", None) or "unknown"
    return "miniapp:" + host


def _reject(limiter: security.RateLimiter, request: Request,
            status_code: int, detail: str) -> None:
    """Count an auth failure and reject; trips 429 once the window is full."""
    if not limiter.allow(_client_key(request)):
        raise HTTPException(429, _RATE_LIMITED)
    raise HTTPException(status_code, detail)


def require_miniapp(settings):
    """FastAPI dependency guarding /app/api/* routes."""
    limiter = security.RateLimiter(max_events=MAX_FAILURES,
                                   window_s=FAILURE_WINDOW_S)

    async def dep(request: Request) -> dict:
        raw = request.headers.get(INIT_DATA_HEADER)
        if raw is None or not raw.strip():
            raise HTTPException(401, _MISSING)
        if len(raw.encode("utf-8")) > MAX_INIT_DATA_BYTES:
            _reject(limiter, request, 401, _INVALID)
        try:
            validated = validate_init_data(
                raw, settings.telegram_bot_token,
                settings.miniapp_auth_max_age)
        except MiniAppAuthError:
            _reject(limiter, request, 401, _INVALID)
        user = validated["user"]
        if user.get("id") != settings.admin_telegram_user_id:
            raise HTTPException(403, _FORBIDDEN)
        request.state.tg_user = user
        request.state.miniapp = {
            "auth_date": validated["auth_date"],
            "start_param": validated["start_param"],
        }
        return validated

    return dep
