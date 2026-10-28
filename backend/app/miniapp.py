"""JSON API for the Telegram Mini App.

Mounted without a prefix: this router owns every ``/app/api/*`` path.
The whole router is guarded by :func:`require_miniapp`, so no handler can
ever be reached without a valid, fresh Telegram initData header.
"""

import asyncio
import math
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, model_validator

import app.db as db
import app.phones as phones
from app import rev
from app.api import VERSION, _phone_shape
from app.miniapp_auth import require_miniapp

MAX_QUERY_CHARS = 100
MAX_PAGE = 200
MAX_EVENTS_TIMEOUT_S = 25
MAX_SEND_BODY_CHARS = 1600
MINUTE_S = 60
DAY_S = 86400
_CLIENT_REF_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SHORT_KEY_RE = re.compile(r"^[0-9]{3,6}$")


class ReadBody(BaseModel):
    until_ts: int = Field(ge=0)


class SendBody(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_SEND_BODY_CHARS)
    client_ref: str = Field(pattern=r"^[A-Za-z0-9._-]{8,64}$")
    sub_id: int = Field(default=0, ge=0, le=99)
    confirm_unknown: bool = False

    @model_validator(mode="after")
    def _body_not_blank(self):
        if not self.body.strip():
            raise ValueError("body must not be blank")
        return self


def _query_text(raw: str | None) -> str:
    value = raw or ""
    if len(value) > MAX_QUERY_CHARS:
        raise HTTPException(
            422, f"q must be at most {MAX_QUERY_CHARS} characters")
    return value


def _query_int(raw: str | None, default: int, lo: int, hi: int,
               name: str) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{name} must be an integer") from None
    if not lo <= value <= hi:
        raise HTTPException(422, f"{name} must be in {lo}..{hi}")
    return value


def _query_timeout(raw: str | None) -> float:
    if raw is None or raw == "":
        return float(MAX_EVENTS_TIMEOUT_S)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "timeout must be a number") from None
    if not math.isfinite(value) or not 0 <= value <= MAX_EVENTS_TIMEOUT_S:
        raise HTTPException(
            422, f"timeout must be in 0..{MAX_EVENTS_TIMEOUT_S}")
    return value


def build_router(settings) -> APIRouter:
    guard = require_miniapp(settings)
    router = APIRouter(dependencies=[Depends(guard)])

    @router.get("/app/api/me")
    async def me(request: Request):
        user = request.state.tg_user
        miniapp = getattr(request.state, "miniapp", {}) or {}
        return {
            "user": {
                "id": user.get("id"),
                "first_name": user.get("first_name"),
                "username": user.get("username"),
            },
            "admin": True,
            "auth_date": miniapp.get("auth_date"),
        }

    @router.get("/app/api/chats")
    async def chats(
        request: Request,
        q: str | None = None,
        limit: str | None = None,
    ):
        query = _query_text(q)
        page = _query_int(limit, 100, 1, MAX_PAGE, "limit")
        conn = request.app.state.db_conn
        return {"chats": db.chat_summaries(conn, query=query, limit=page)}

    @router.get("/app/api/chats/{thread}/messages")
    async def thread_messages(
        request: Request,
        thread: str,
        before_ts: str | None = None,
        limit: str | None = None,
    ):
        cursor = None
        if before_ts is not None and before_ts != "":
            cursor = _query_int(before_ts, 0, 0, 2 ** 63 - 1, "before_ts")
        page = _query_int(limit, 50, 1, MAX_PAGE, "limit")
        conn = request.app.state.db_conn
        if not db.thread_exists(conn, thread):
            raise HTTPException(404, "unknown thread")
        meta = db.thread_meta(conn, thread)
        # Fetch one extra row to tell whether an older page exists. The
        # underlying helper caps a page at 200 rows, so at the maximum
        # page size has_more stays false even if more history exists.
        events = db.thread_timeline(conn, thread, before_ts=cursor,
                                    limit=page + 1)
        has_more = len(events) > page
        if has_more:
            events = events[:page]
        return {
            "thread_key": thread,
            "display": meta["display"],
            "name": meta["name"],
            "events": events,
            "has_more": has_more,
        }

    @router.post("/app/api/chats/{thread}/messages")
    async def send_message(request: Request, thread: str, payload: SendBody):
        conn = request.app.state.db_conn

        # Resolve the path value to a canonical sendable thread key. The
        # browser may send the canonical key, a local spelling or a number
        # typed into the new-conversation dialog; all of them must land in
        # the same thread. Alphanumeric senders have no dialable address.
        if thread.startswith("raw:"):
            raise HTTPException(422, {"error": "unsendable_thread"})
        if thread.startswith("short:"):
            digits = phones.to_ascii_digits(thread[len("short:"):])
            if not _SHORT_KEY_RE.fullmatch(digits):
                raise HTTPException(422, {"error": "invalid_destination"})
            key = "short:" + digits
            target = digits
        else:
            key = phones.normalize_target(thread)
            if key is None or key.startswith("raw:"):
                raise HTTPException(422, {"error": "invalid_destination"})
            if key.startswith("short:"):
                target = key[len("short:"):]
            else:
                target = "+" + key

        # Idempotency first: a retry after a timeout must succeed even when
        # the command has already moved on or the caps have filled up.
        existing = db.command_by_client_ref(conn, payload.client_ref)
        if existing is not None:
            if existing["thread_key"] != key:
                raise HTTPException(409, {"error": "client_ref_conflict"})
            duplicate_meta = db.thread_meta(conn, key)
            return {
                "id": existing["id"],
                "status": existing["status"],
                "client_ref": payload.client_ref,
                "duplicate": True,
                "thread_key": key,
                "ts": existing["created_at"],
                "display": duplicate_meta["display"],
                "name": duplicate_meta["name"],
            }

        known = (db.contact_name_for_key(conn, key) is not None
                 or db.thread_exists(conn, key))
        if not known and not payload.confirm_unknown:
            raise HTTPException(409, {
                "error": "unknown_number",
                "thread_key": key,
                "display": phones.display_number(key),
            })

        # The caps are the security control; the confirmation flag above is
        # only UX. They apply to every send, known number or not, and they
        # are checked before the insert so a burst cannot slip through.
        now = int(time.time())
        if (db.count_miniapp_sends(conn, now - MINUTE_S)
                >= settings.miniapp_send_per_minute):
            raise HTTPException(429, {"error": "rate_limited",
                                      "window": "minute"})
        if (db.count_miniapp_sends(conn, now - DAY_S)
                >= settings.miniapp_send_daily_cap):
            raise HTTPException(429, {"error": "rate_limited",
                                      "window": "day"})

        command_id = uuid.uuid4().hex
        db.create_command(conn, command_id, target, payload.body, "miniapp",
                          thread_key=key, sub_id=payload.sub_id,
                          client_ref=payload.client_ref)

        # A concurrent retry with the same ref can win the INSERT OR IGNORE.
        # In that case the stored row is the truth and this request is a
        # duplicate: never bump rev for a row this request did not create.
        stored = db.command_by_client_ref(conn, payload.client_ref)
        if stored is not None and stored["id"] != command_id:
            duplicate_meta = db.thread_meta(conn, key)
            return {
                "id": stored["id"],
                "status": stored["status"],
                "client_ref": payload.client_ref,
                "duplicate": True,
                "thread_key": key,
                "ts": stored["created_at"],
                "display": duplicate_meta["display"],
                "name": duplicate_meta["name"],
            }

        rev.bump_rev(request.app)
        meta = db.thread_meta(conn, key)
        return {
            "id": command_id,
            "status": "pending",
            "client_ref": payload.client_ref,
            "duplicate": False,
            "thread_key": key,
            "ts": stored["created_at"] if stored is not None else now,
            "display": meta["display"],
            "name": meta["name"],
            "target": target,
        }

    @router.post("/app/api/chats/{thread}/read")
    async def thread_read(request: Request, thread: str, payload: dict):
        try:
            body = ReadBody.model_validate(payload)
        except ValidationError:
            raise HTTPException(
                422, "until_ts must be an integer >= 0") from None
        conn = request.app.state.db_conn
        if not db.thread_exists(conn, thread):
            raise HTTPException(404, "unknown thread")
        # A client clock can be ahead; the cursor never moves into the
        # future, otherwise later inbound messages would start read.
        until_ts = min(int(body.until_ts), int(time.time()))
        last_read_at = db.mark_thread_read(conn, thread, until_ts)
        return {"thread_key": thread, "last_read_at": last_read_at}

    @router.get("/app/api/contacts")
    async def contacts(
        request: Request,
        q: str | None = None,
        limit: str | None = None,
    ):
        query = _query_text(q)
        page = _query_int(limit, 50, 1, 100, "limit")
        conn = request.app.state.db_conn
        rows = db.miniapp_contacts(conn, query=query, limit=page)
        result = []
        for row in rows:
            thread_key = row.get("number_key")
            if not thread_key:
                thread_key = (phones.thread_key(row["number"])
                              or phones.raw_key(row["number"]))
            result.append({
                "number": row["number"],
                "name": row["name"],
                "number_key": row.get("number_key"),
                "thread_key": thread_key,
                "display": phones.display_number(thread_key),
            })
        return {"contacts": result}

    @router.get("/app/api/status")
    async def status(request: Request):
        conn = request.app.state.db_conn
        return {
            "paused": db.settings_get(conn, "paused") == "1",
            "queue": db.count_jobs(conn),
            "counts_by_state": db.counts_by_state(conn),
            "phone": _phone_shape(conn),
            "version": VERSION,
        }

    @router.get("/app/api/events")
    async def events(
        request: Request,
        rev: str | None = None,
        timeout: str | None = None,
    ):
        requested = _query_int(rev, 0, 0, 2 ** 63 - 1, "rev")
        wait_s = _query_timeout(timeout)
        app = request.app
        current = int(getattr(app.state, "rev", 0) or 0)
        if current > requested or wait_s <= 0:
            return {"rev": current, "changed": current > requested}
        wait_event = getattr(app.state, "rev_event", None)
        if wait_event is not None:
            try:
                await asyncio.wait_for(wait_event.wait(), timeout=wait_s)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        current = int(getattr(app.state, "rev", 0) or 0)
        return {"rev": current, "changed": current > requested}

    return router
