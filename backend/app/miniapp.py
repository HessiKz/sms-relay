"""JSON API for the Telegram Mini App.

Mounted without a prefix: this router owns every ``/app/api/*`` path.
The whole router is guarded by :func:`require_miniapp`, so no handler can
ever be reached without a valid, fresh Telegram initData header.

Every conversation belongs to a device, so every chat, thread, contact
and read route is scoped to the device in the path. Nothing is ever
mixed: a device never sees the history, the contacts or the unread
counters of another device.
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
MAX_CURSOR_KEY_CHARS = 128
MINUTE_S = 60
DAY_S = 86400
_CLIENT_REF_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SHORT_KEY_RE = re.compile(r"^[0-9]{3,6}$")
# The old, device-less routes are gone. They are answered with 410 so an
# old Mini App build fails loudly instead of reading a mixed inbox.
_MOVED = {
    "error": "moved",
    "message": ("this endpoint is gone: use "
                "/app/api/devices/{device_id}/chats and its siblings"),
}


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


def _query_cursor(raw: str | None, name: str) -> str | None:
    """A cursor string: a thread key or an event id."""
    if raw is None or raw == "":
        return None
    value = str(raw)
    if len(value) > MAX_CURSOR_KEY_CHARS:
        raise HTTPException(
            422, f"{name} must be at most {MAX_CURSOR_KEY_CHARS} characters")
    return value


def _query_cursor_ts(raw: str | None, name: str) -> int | None:
    if raw is None or raw == "":
        return None
    return _query_int(raw, 0, 0, 2 ** 63 - 1, name)


def _known_device(conn, device_id: str) -> dict:
    """The device of a route, or 404.

    A revoked or archived device may still be read: its history is part
    of the Mini App, it simply cannot send anything any more.
    """
    row = db.device_by_id(conn, device_id)
    if row is None:
        raise HTTPException(404, "unknown device")
    return row


def _active_device(conn, device_id: str) -> dict:
    """The device of a route, but only one that can still send.

    Sending needs a paired device: an archived or revoked one is
    refused, because nobody would ever run the command.
    """
    row = _known_device(conn, device_id)
    if row.get("archived") or row.get("revoked_at") is not None:
        raise HTTPException(409, {"error": "device_not_active"})
    return row


def _events_payload(app, current: int, requested: int) -> dict:
    """The long-poll answer.

    ``changed`` is true whenever the counter moved. A client that is ahead
    of the server (the counter is in memory and resets on a restart) also
    gets ``changed`` so it reloads instead of waiting forever.

    ``threads`` names the conversations that changed. Every entry has a
    ``device_id`` and a ``thread_key`` (``None`` for a device-wide
    change). ``None`` means the scope is unknown and the client must
    reload everything.
    """
    changed = current != requested
    threads = rev.changed_threads(app, requested) if changed else []
    return {"rev": current, "changed": changed, "threads": threads}


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

    @router.get("/app/api/devices/{device_id}/chats")
    async def device_chats(
        request: Request,
        device_id: str,
        q: str | None = None,
        limit: str | None = None,
        before_ts: str | None = None,
        before_key: str | None = None,
    ):
        """The conversations of ONE device."""
        conn = request.app.state.db_conn
        _known_device(conn, device_id)
        query = _query_text(q)
        page = _query_int(limit, 100, 1, MAX_PAGE, "limit")
        cursor_ts = _query_cursor_ts(before_ts, "before_ts")
        cursor_key = _query_cursor(before_key, "before_key")
        if cursor_key is not None and cursor_ts is None:
            raise HTTPException(422, "before_key requires before_ts")
        # One extra row tells the client whether older conversations exist.
        rows = db.chat_summaries(conn, device_id, query=query, limit=page + 1,
                                 before_ts=cursor_ts, before_key=cursor_key)
        has_more = len(rows) > page
        if has_more:
            rows = rows[:page]
        return {"device_id": device_id, "chats": rows, "has_more": has_more}

    @router.get("/app/api/devices/{device_id}/chats/{thread}/messages")
    async def device_thread_messages(
        request: Request,
        device_id: str,
        thread: str,
        before_ts: str | None = None,
        before_id: str | None = None,
        limit: str | None = None,
    ):
        """The history of one thread of ONE device."""
        conn = request.app.state.db_conn
        _known_device(conn, device_id)
        cursor = _query_cursor_ts(before_ts, "before_ts")
        event_id = _query_cursor(before_id, "before_id")
        if event_id is not None and cursor is None:
            raise HTTPException(422, "before_id requires before_ts")
        page = _query_int(limit, 50, 1, MAX_PAGE, "limit")
        if not db.thread_exists(conn, device_id, thread):
            raise HTTPException(404, "unknown thread")
        meta = db.thread_meta(conn, device_id, thread)
        # Fetch one extra row to tell whether an older page exists. The
        # cursor has two halves, so events that share a second with the
        # last row of the previous page are not skipped.
        events = db.thread_timeline(conn, device_id, thread, before_ts=cursor,
                                    before_id=event_id, limit=page + 1)
        has_more = len(events) > page
        if has_more:
            events = events[:page]
        return {
            "device_id": device_id,
            "thread_key": thread,
            "display": meta["display"],
            "name": meta["name"],
            "events": events,
            "has_more": has_more,
        }

    @router.post("/app/api/devices/{device_id}/chats/{thread}/messages")
    async def device_send_message(request: Request, device_id: str,
                                  thread: str, payload: SendBody):
        """Queue an SMS that the given device has to send with its SIM."""
        conn = request.app.state.db_conn
        _active_device(conn, device_id)

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
        # the command has already moved on or the caps have filled up. The
        # key belongs to one device and one thread: the same ref in another
        # device or another thread is a conflict, never a second SMS.
        existing = db.command_by_client_ref(conn, payload.client_ref)
        if existing is not None:
            if (existing["thread_key"] != key
                    or existing["device_id"] != device_id):
                raise HTTPException(409, {"error": "client_ref_conflict"})
            duplicate_meta = db.thread_meta(conn, device_id, key)
            return {
                "id": existing["id"],
                "status": existing["status"],
                "client_ref": payload.client_ref,
                "duplicate": True,
                "device_id": device_id,
                "thread_key": key,
                "ts": existing["created_at"],
                "display": duplicate_meta["display"],
                "name": duplicate_meta["name"],
            }

        # The known-number check only knows this device: its contacts and
        # its own history. Another device's contact list must not make a
        # number known here.
        known = (db.device_contact_name_for_key(conn, device_id, key)
                 is not None
                 or db.thread_exists(conn, device_id, key))
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
                          client_ref=payload.client_ref, device_id=device_id)

        # A concurrent retry with the same ref can win the INSERT OR IGNORE.
        # In that case the stored row is the truth and this request is a
        # duplicate: never bump rev for a row this request did not create.
        stored = db.command_by_client_ref(conn, payload.client_ref)
        if stored is not None and stored["id"] != command_id:
            duplicate_meta = db.thread_meta(conn, device_id, key)
            return {
                "id": stored["id"],
                "status": stored["status"],
                "client_ref": payload.client_ref,
                "duplicate": True,
                "device_id": device_id,
                "thread_key": key,
                "ts": stored["created_at"],
                "display": duplicate_meta["display"],
                "name": duplicate_meta["name"],
            }

        rev.bump_rev(request.app, device_id, key)
        meta = db.thread_meta(conn, device_id, key)
        return {
            "id": command_id,
            "status": "pending",
            "client_ref": payload.client_ref,
            "duplicate": False,
            "device_id": device_id,
            "thread_key": key,
            "ts": stored["created_at"] if stored is not None else now,
            "display": meta["display"],
            "name": meta["name"],
            "target": target,
        }

    @router.post("/app/api/devices/{device_id}/chats/{thread}/read")
    async def device_thread_read(request: Request, device_id: str,
                                 thread: str, payload: dict):
        """Move the read marker of one thread of ONE device."""
        try:
            body = ReadBody.model_validate(payload)
        except ValidationError:
            raise HTTPException(
                422, "until_ts must be an integer >= 0") from None
        conn = request.app.state.db_conn
        _known_device(conn, device_id)
        if not db.thread_exists(conn, device_id, thread):
            raise HTTPException(404, "unknown thread")
        # A client clock can be ahead; the cursor never moves into the
        # future, otherwise later inbound messages would start read.
        until_ts = min(int(body.until_ts), int(time.time()))
        last_read_at = db.mark_thread_read(conn, device_id, thread, until_ts)
        return {"device_id": device_id, "thread_key": thread,
                "last_read_at": last_read_at}

    @router.get("/app/api/devices/{device_id}/contacts")
    async def device_contacts(
        request: Request,
        device_id: str,
        q: str | None = None,
        limit: str | None = None,
    ):
        """The contacts of ONE device."""
        conn = request.app.state.db_conn
        _known_device(conn, device_id)
        query = _query_text(q)
        page = _query_int(limit, 50, 1, 100, "limit")
        rows = db.miniapp_contacts(conn, device_id, query=query, limit=page)
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
        return {"device_id": device_id, "contacts": result}

    # --- the device-less routes are gone ---------------------------------

    @router.get("/app/api/chats")
    async def removed_chats():
        raise HTTPException(410, _MOVED)

    @router.get("/app/api/chats/{thread}/messages")
    async def removed_thread_messages(thread: str):
        raise HTTPException(410, _MOVED)

    @router.post("/app/api/chats/{thread}/messages")
    async def removed_send_message(thread: str):
        raise HTTPException(410, _MOVED)

    @router.post("/app/api/chats/{thread}/read")
    async def removed_thread_read(thread: str):
        raise HTTPException(410, _MOVED)

    @router.get("/app/api/contacts")
    async def removed_contacts():
        raise HTTPException(410, _MOVED)

    @router.get("/app/api/status")
    async def status(request: Request):
        """The global state of the relay and the list of devices.

        No conversation, contact or message of any device is part of this
        answer: it is the header of the Mini App, not an inbox.
        """
        conn = request.app.state.db_conn
        return {
            "paused": db.settings_get(conn, "paused") == "1",
            "queue": db.count_jobs(conn),
            "counts_by_state": db.counts_by_state(conn),
            "phone": _phone_shape(conn),
            "devices": db.device_summaries(conn),
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
        if current != requested or wait_s <= 0:
            # A counter that moved backwards was reset by a restart: the
            # client is ahead of the server and has to reload at once.
            return _events_payload(app, current, requested)
        wait_event = getattr(app.state, "rev_event", None)
        if wait_event is not None:
            try:
                await asyncio.wait_for(wait_event.wait(), timeout=wait_s)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        current = int(getattr(app.state, "rev", 0) or 0)
        return _events_payload(app, current, requested)

    return router
