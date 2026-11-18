"""Calls: the call log, the live call of a phone and remote call actions.

The relay is a fourth stream here, next to messages, commands and
contacts. The phone uploads its call log, reports the state of the one
primary call it has, and executes the actions the admin asks for from the
Mini App or from a Telegram button.

Three rules shape this module:

* **the device comes from the bearer token**, never from a body. A device
  only ever reads, writes or claims the rows of its own device;
* **a call action is never applied to the next call**. The Mini App names
  the phone-generated ``call_ref`` and the backend refuses an action whose
  ref is no longer the live call: a stale ref is a ``409``, never a guess;
* **an emergency call is placed by the phone only**. The backend refuses to
  create a remote ``dial`` action for 110, 112, 115, 125, 911, 999, 000.

The bot mirrors a call, it never carries it: a ringing or missed call is
one message to the admin with buttons that create the same ``call_actions``
row the Mini App would create. The send is bounded and best-effort, so a
slow Telegram can never delay the state report of the phone.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, model_validator

import app.db as db
import app.phones as phones
from app import rev
from app.delivery import _html_escape
from app.miniapp import (_active_device, _known_device, _query_cursor,
                         _query_cursor_ts, _query_int, _query_text)
from app.miniapp_auth import require_miniapp
from app.security import require_device
from app.tg_ui import button, keyboard, web_app_button

log = logging.getLogger(__name__)

MAX_SYNC_ITEMS = 200
# 2000-01-01: no phone call log predates this in practice.
MIN_STARTED_AT = 946684800
FUTURE_GRACE_S = 86400
MAX_CALL_REF_LEN = 64
MAX_PROVIDER_ID_LEN = 64
MAX_NUMBER_LEN = 64
MAX_NAME_LEN = 96
MAX_ACCOUNT_LEN = 64
MAX_SIM_LABEL_LEN = 32
MAX_CLIENT_REF_LEN = 64
MAX_PAYLOAD_LEN = 64
MAX_HISTORY_LIMIT = 200
MAX_WAIT_S = 25
WAIT_STEP_S = 0.5
MAX_ACTIONS = 10
# A disconnected session is still shown for a minute: the Mini App needs a
# moment to render "call ended" before the card disappears.
LIVE_DISCONNECTED_GRACE_S = 60
# A phone that dies mid-call never reports the end, so a session that has
# not been updated for this long is treated as over. A ring lasts seconds;
# a real conversation can last hours, but not a day.
LIVE_RINGING_TTL_S = 120
LIVE_ACTIVE_TTL_S = 12 * 3600
# A notification is a heads-up, not part of the state report: the phone
# gets its 200 within this bound even when Telegram hangs.
NOTIFY_TIMEOUT_S = 2.0
# Telegram rejects a callback_query whose data is longer than this, and a
# rejected ``reply_markup`` fails the WHOLE message, so it is checked.
MAX_CALLBACK_BYTES = 64
# Numbers that may only ever be dialled on the phone itself. The comparison
# is on the digits, with the leading zeros of a local spelling stripped, so
# "0112" cannot slip past the list.
EMERGENCY_NUMBERS = frozenset({"110", "112", "115", "125", "911", "999",
                               "000"})
_ACTIVE_CALL_STATES = ("ringing", "active", "held")


class CallSyncItem(BaseModel):
    """One row of the phone's call log."""

    provider_id: str | int | None = None
    number: str = Field(default="", max_length=MAX_NUMBER_LEN)
    cached_name: str | None = Field(default=None, max_length=MAX_NAME_LEN)
    kind: str
    raw_type: int | None = Field(default=None, ge=0)
    started_at: int
    # A week is far beyond any real call, and generous enough that one odd
    # row can never make the phone's whole batch fail validation.
    duration_s: int = Field(default=0, ge=0, le=604800)
    sub_id: int = Field(default=0, ge=0, le=99)
    phone_account: str | None = Field(default=None,
                                      max_length=MAX_ACCOUNT_LEN)
    is_read: bool = False
    content_hash: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _check_kind(self):
        if self.kind not in db.CALL_KINDS:
            raise ValueError("kind must be one of " + ", ".join(db.CALL_KINDS))
        return self

    @model_validator(mode="after")
    def _check_started_at(self):
        if self.started_at < MIN_STARTED_AT:
            raise ValueError(
                f"started_at must be at or after {MIN_STARTED_AT}")
        if self.started_at > int(time.time()) + FUTURE_GRACE_S:
            raise ValueError("started_at is too far in the future")
        return self

    @model_validator(mode="after")
    def _trim_provider_id(self):
        if self.provider_id is not None:
            object.__setattr__(self, "provider_id",
                               str(self.provider_id)[:MAX_PROVIDER_ID_LEN])
        return self


def _sync_items(payload: dict) -> tuple[list[dict], int]:
    """Validate a sync batch one item at a time.

    A single bad row must not cost the phone the whole page: an overlong
    name, an unknown kind or an impossible timestamp is counted as skipped
    and the rest of the batch is still stored. Only the batch itself is
    fatal, because an answer without any structure cannot be counted.
    """
    if not isinstance(payload, dict):
        raise HTTPException(422, "body must be a JSON object")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise HTTPException(422, "items must be a list")
    if not raw_items:
        raise HTTPException(422, "items must contain at least one call")
    if len(raw_items) > MAX_SYNC_ITEMS:
        raise HTTPException(
            422, f"at most {MAX_SYNC_ITEMS} calls per request")
    items = []
    skipped = 0
    for raw in raw_items:
        try:
            item = CallSyncItem.model_validate(raw)
        except ValidationError:
            skipped += 1
            continue
        except Exception:  # pragma: no cover - defensive, never a 500
            skipped += 1
            continue
        number = (item.number or "").strip()
        number_key = phones.thread_key(number) or None
        items.append({
            "provider_id": item.provider_id,
            "number": number,
            "number_key": number_key,
            "cached_name": item.cached_name,
            "kind": item.kind,
            "raw_type": item.raw_type,
            "started_at": item.started_at,
            "duration_s": item.duration_s,
            "sub_id": item.sub_id,
            "phone_account": item.phone_account,
            "is_read": item.is_read,
            "content_hash": item.content_hash,
        })
    return items, skipped


class CallEventBody(BaseModel):
    """The state of the live call of the authenticated device."""

    call_ref: str = Field(min_length=1, max_length=MAX_CALL_REF_LEN)
    state: str
    direction: str
    number: str | None = Field(default=None, max_length=MAX_NUMBER_LEN)
    sim_label: str | None = Field(default=None,
                                  max_length=MAX_SIM_LABEL_LEN)
    muted: bool | None = None
    speaker: bool | None = None
    started_at: int | None = None
    ended_at: int | None = None

    @model_validator(mode="after")
    def _check_state(self):
        if self.state not in db.CALL_STATES:
            raise ValueError("state must be one of "
                             + ", ".join(db.CALL_STATES))
        if self.direction not in db.CALL_DIRECTIONS:
            raise ValueError("direction must be one of "
                             + ", ".join(db.CALL_DIRECTIONS))
        for name in ("started_at", "ended_at"):
            value = getattr(self, name)
            if value is None:
                continue
            if value < 0 or value > int(time.time()) + FUTURE_GRACE_S:
                raise ValueError(f"{name} is out of range")
        return self

    @model_validator(mode="after")
    def _not_blank(self):
        if not self.call_ref.strip():
            raise ValueError("call_ref must not be blank")
        return self


class CallActionBody(BaseModel):
    """One remote action, created by the Mini App (or by the bot later)."""

    # An empty ref is only valid for a dial, which has no call yet.
    call_ref: str = Field(default="", max_length=MAX_CALL_REF_LEN)
    action: str
    payload: str | None = Field(default=None, max_length=MAX_PAYLOAD_LEN)
    client_ref: str = Field(min_length=1, max_length=MAX_CLIENT_REF_LEN)

    @model_validator(mode="after")
    def _check_action(self):
        if self.action not in db.CALL_ACTIONS:
            raise ValueError("action must be one of "
                             + ", ".join(db.CALL_ACTIONS))
        return self


class CallReadBody(BaseModel):
    until_ts: int = Field(ge=0)


class CallActionResultBody(BaseModel):
    """The phone's report about one action."""

    status: Literal["done", "failed", "rejected"]
    detail: str | None = Field(default=None, max_length=200)


def _call_digits(value) -> str:
    """The digits of a typed number, Persian and Arabic forms included."""
    text = phones.to_ascii_digits(str(value or ""))
    return "".join(character for character in text if character.isdigit())


def is_emergency_number(value) -> bool:
    """True when a typed number reaches an emergency service.

    Mirrors ``isEmergencyNumber`` in the Mini App: the digits alone decide,
    and a local spelling with leading zeros ("0112") resolves to the same
    emergency number. The phone refuses these as well; the check here only
    makes sure no remote action can ever ask for one.
    """
    digits = _call_digits(value)
    if not digits:
        return False
    if digits in EMERGENCY_NUMBERS:
        return True
    stripped = digits.lstrip("0")
    return bool(stripped) and stripped in EMERGENCY_NUMBERS


def _display_for(number_key: str | None, number: str | None) -> str:
    if number_key:
        return phones.display_number(number_key)
    return str(number or "")


def _call_shape(conn, device_id: str, row: dict) -> dict:
    """One call-log row in the shape the Mini App reads."""
    key = row.get("number_key") or None
    name = db.call_name_for_key(conn, device_id, key)
    return {
        "id": row["id"],
        "number": row.get("number") or "",
        "display": _display_for(key, row.get("number")),
        "name": name or row.get("cached_name") or None,
        "kind": row.get("kind") or "unknown",
        "started_at": row.get("started_at"),
        "duration_s": int(row.get("duration_s") or 0),
        "sub_id": int(row.get("sub_id") or 0),
        "phone_account": row.get("phone_account"),
        "is_read": bool(row.get("is_read")),
        "thread_key": key,
    }


def session_is_live(row: dict | None, now: int | None = None) -> bool:
    """True while the reported session can still describe a real call.

    A phone can die or reboot mid-call: the session row then stays
    ``ringing`` or ``active`` for ever, and the Mini App would offer
    Answer/Hang up for a call that ended long ago. The freshness window is
    the only guard, so it lives in one place and both the live card and the
    action check use it:

    * ``ringing`` / ``dialing`` die after two minutes without an update,
      because a ring is over in seconds and the phone reports again on
      every transition;
    * ``active`` / ``held`` survive twelve hours, which covers any real
      call but not a phone left on a desk for a day;
    * ``disconnected`` is shown for a minute, so the Mini App can render
      "call ended" before the card disappears.
    """
    if row is None:
        return False
    stamp = int(now) if now is not None else int(time.time())
    state = row.get("state")
    if state in ("ringing", "dialing"):
        ttl = LIVE_RINGING_TTL_S
    elif state in ("active", "held"):
        ttl = LIVE_ACTIVE_TTL_S
    elif state == "disconnected":
        ended = row.get("ended_at") or row.get("updated_at") or 0
        return int(ended) > stamp - LIVE_DISCONNECTED_GRACE_S
    else:  # pragma: no cover - the column is checked on write
        return False
    updated = row.get("updated_at") or 0
    return int(updated) > stamp - ttl


def live_call_shape(conn, device_id: str) -> dict | None:
    """The live call of one device, or None when it is idle.

    A session that is over (or that went quiet for so long that the phone
    cannot still be on the call) is idle: the caller must not keep a card
    for a call that has ended. ``session_is_live`` is the one rule.
    """
    row = db.call_session_get(conn, device_id)
    if not session_is_live(row):
        return None
    key = row.get("number_key") or None
    return {
        "device_id": row["device_id"],
        "call_ref": row["call_ref"],
        "number": row.get("number") or "",
        "number_key": key,
        "display": _display_for(key, row.get("number")),
        "name": db.call_name_for_key(conn, device_id, key),
        "direction": row["direction"],
        "state": row["state"],
        "started_at": row["started_at"],
        "answered_at": row.get("answered_at"),
        "ended_at": row.get("ended_at"),
        "muted": bool(row.get("muted")),
        "speaker": bool(row.get("speaker")),
        "sim_label": row.get("sim_label"),
        "updated_at": row.get("updated_at"),
    }


def session_actionable(row: dict | None) -> bool:
    """True when a live session can still take an answer or a reject.

    ``session_is_live`` keeps a phone that died mid-call from offering an
    action for ever; the state check keeps an answered (or an already
    ended) call from being answered again. A ``disconnected`` session is
    still "live" for a minute so the Mini App can render "call ended", but
    it is not actionable.
    """
    return bool(row) and session_is_live(row) and row.get("state") in (
        _ACTIVE_CALL_STATES)


def _utc_stamp(value) -> str:
    try:
        ts = datetime.fromtimestamp(int(value or 0), tz=timezone.utc)
    except (TypeError, ValueError, OSError):  # pragma: no cover - odd row
        ts = datetime.now(tz=timezone.utc)
    return ts.strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def _device_label(conn, device_id: str) -> str:
    """The name of the phone, or its id when it has none."""
    try:
        found = db.device_by_id(conn, device_id)
    except Exception:  # pragma: no cover - a lookup never fails a report
        found = None
    name = (found or {}).get("name")
    if name and str(name).strip():
        return str(name).strip()
    return device_id


def _caller_label(conn, device_id: str, session: dict) -> str:
    """The contact name of the caller, otherwise the readable number.

    A private number has no key and no display, and it must never be shown
    as a blank line: the message says "Unknown number" instead.
    """
    key = session.get("number_key") or None
    name = db.call_name_for_key(conn, device_id, key)
    if name and str(name).strip():
        return str(name).strip()
    return _display_for(key, session.get("number")) or "Unknown number"


def _calls_url(base: str | None, device_id: str) -> str | None:
    """The Mini App link of one device, or None when it is not configured."""
    base = (base or "").strip()
    if not base:
        return None
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}device={quote(str(device_id), safe='')}"


def _call_keyboard(device_id: str, actions,
                   miniapp_url: str | None) -> dict | None:
    """The inline keyboard of a call message, or None when it cannot exist.

    Telegram caps ``callback_data`` at 64 bytes. A device id long enough to
    break that cap must not produce a keyboard Telegram rejects (the whole
    message would fail): the message goes out without buttons instead, and
    the admin still has the Mini App.
    """
    row = []
    for label, verb in actions:
        data = f"call:{verb}:{device_id}"
        if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
            return None
        row.append(button(label, data))
    rows = [row] if row else []
    url = _calls_url(miniapp_url, device_id)
    if url:
        rows.append([web_app_button("Open calls", url)])
    return keyboard(*rows) if rows else None


def _call_text(header: str, device: str, caller: str, stamp) -> str:
    return (f"\U0001F4DE <b>{header}</b>\n"
            f"\U0001F4F1 <b>{_html_escape(device)}</b>\n"
            f"<b>{_html_escape(caller)}</b>\n"
            f"<i>{_utc_stamp(stamp)}</i>")


async def _send_call_message(app, text: str,
                             keyboard_value: dict | None) -> None:
    """Send one call message, bounded, and never let it break the report.

    The phone reports a ringing call and must get its ``200`` at once: a
    Telegram that hangs or fails is a lost notification, never a failed
    state transition. Only the error kind is logged; the device, the
    number and the name never reach the log.
    """
    tg = getattr(getattr(app, "state", None), "tg", None)
    settings = getattr(getattr(app, "state", None), "settings", None)
    chat_id = getattr(settings, "telegram_chat_id", None)
    if tg is None or chat_id is None or chat_id == "":
        return
    try:
        await asyncio.wait_for(
            tg.send_to_chat(chat_id, text, keyboard=keyboard_value,
                            parse_mode="HTML"),
            timeout=NOTIFY_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("sms-relay call notify failed kind=timeout")
    except Exception as exc:
        kind = getattr(exc, "kind", None) or type(exc).__name__
        log.warning("sms-relay call notify failed kind=%s", kind)


async def _notify_ringing(request: Request, device_id: str,
                         session: dict) -> None:
    """Tell the admin that a phone is ringing, with Answer / Decline.

    The buttons carry the device id, never the call: the bot resolves the
    live session when the button is tapped, so a message that was kept for
    a while can never answer the next call. ``call_ref`` is not part of the
    callback data because a Telegram callback is capped at 64 bytes and a
    phone-generated ref plus a device id can exceed that.
    """
    try:
        conn = request.app.state.db_conn
        device = _device_label(conn, device_id)
        caller = _caller_label(conn, device_id, session)
        text = _call_text("Incoming call", device, caller,
                          session.get("started_at"))
        kb = _call_keyboard(device_id,
                            (("Answer", "answer"), ("Decline", "reject")),
                            getattr(request.app.state.settings,
                                    "miniapp_url", None))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("sms-relay call notify failed kind=%s",
                    type(exc).__name__)
        return
    await _send_call_message(request.app, text, kb)


def _is_missed(previous: dict | None, session: dict) -> bool:
    """True when this event completes a call that nobody answered.

    A missed call is the transition into ``disconnected`` of a session we
    saw ringing or active, for an incoming call, without an answer. An
    outgoing call, an answered one, a replayed disconnected event and a
    call we never saw ring (an import or a legacy row) are all silent.
    """
    if not previous or not session:
        return False
    if session.get("state") != "disconnected":
        return False
    if previous.get("state") == "disconnected":
        return False
    if previous.get("call_ref") != session.get("call_ref"):
        return False
    if session.get("direction") != "incoming":
        return False
    return not session.get("answered_at")


async def _notify_missed(request: Request, device_id: str,
                        session: dict) -> None:
    """Tell the admin about a missed call, with a Call back button."""
    try:
        conn = request.app.state.db_conn
        device = _device_label(conn, device_id)
        caller = _caller_label(conn, device_id, session)
        text = _call_text("Missed call", device, caller,
                          session.get("ended_at") or session.get("updated_at"))
        kb = _call_keyboard(device_id, (("Call back", "callback"),),
                            getattr(request.app.state.settings,
                                    "miniapp_url", None))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("sms-relay call notify failed kind=%s",
                    type(exc).__name__)
        return
    await _send_call_message(request.app, text, kb)


def _bump_change(app, device_id: str, scopes: set) -> None:
    """Announce one sync to the Mini App, once per request.

    The scopes are deduplicated first. One affected conversation is named
    as exactly that thread; several of them (or a call with a private
    number, which has no thread at all) collapse into one device-wide
    change, which still covers every row the request touched.
    """
    keys = {key for key in scopes if key}
    if len(keys) == 1 and None not in scopes:
        rev.bump_rev(app, device_id, next(iter(keys)))
    else:
        rev.bump_rev(app, device_id, None)


def build_router(settings) -> APIRouter:
    """The phone routes: sync the call log, report the live call, run
    actions."""
    router = APIRouter(dependencies=[Depends(require_device(settings))])

    @router.post("/calls/sync")
    async def calls_sync(request: Request, payload: dict):
        """Upsert a batch of the call log of the authenticated device.

        The body is validated one call at a time: one unusable row is
        counted as skipped instead of failing the whole page, because the
        phone would otherwise lose every call of that batch.
        """
        conn = request.app.state.db_conn
        device = request.state.device
        device_id = device["id"]
        items, rejected = _sync_items(payload)
        scopes = {item["number_key"] for item in items}
        counts = db.record_call_items(conn, device_id, items)
        counts["skipped"] += rejected
        if counts["inserted"] or counts["updated"]:
            _bump_change(request.app, device_id, scopes)
        return counts

    @router.post("/calls/events")
    async def calls_events(request: Request, payload: CallEventBody):
        """Upsert the live call of the authenticated device.

        The phone reports a state transition; the backend keeps one primary
        call per device. A disconnected event stamps ``ended_at`` when the
        phone did not send one. The state change is committed before any
        notification is attempted, and the notification is bounded, so the
        phone always gets its ``200`` quickly.

        Two transitions mirror a call in Telegram: the first ``ringing``
        event of a call (Answer / Decline) and the ``disconnected`` event
        of an incoming call nobody answered (Call back).
        """
        conn = request.app.state.db_conn
        device = request.state.device
        device_id = device["id"]
        number = (payload.number or "").strip()
        previous = db.call_session_get(conn, device_id)
        session = db.call_session_set(
            conn, device_id, payload.call_ref, payload.state,
            direction=payload.direction,
            number=number if payload.number is not None else None,
            started_at=payload.started_at,
            ended_at=payload.ended_at,
            muted=payload.muted,
            speaker=payload.speaker,
            sim_label=payload.sim_label,
        )
        rev.bump_rev(request.app, device_id, None)
        # A ringing retry (the phone reconnected, an offline queue drained)
        # repeats a call we already announced: one ring, one message. The
        # ref is what identifies the call, so a replay after the call went
        # active is deduplicated too. Only an incoming call rings: there is
        # nothing to answer on an outgoing one.
        ringing_is_new = (
            payload.state == "ringing"
            and session.get("direction") == "incoming"
            and not (previous is not None
                     and previous.get("call_ref") == payload.call_ref))
        if ringing_is_new:
            await _notify_ringing(request, device_id, session)
        if _is_missed(previous, session):
            await _notify_missed(request, device_id, session)
        return {"ok": True}

    @router.get("/calls/actions")
    async def calls_actions(request: Request, wait: str | None = None):
        """Hand the phone its pending call actions, at most ten.

        ``wait`` holds the request open for a few seconds while a call is
        live, so the phone reacts to a Mini App tap immediately. The claim
        and the ``delivered`` transition happen in one transaction: two
        consumers (the command poll and the live call poll) never run the
        same action.
        """
        conn = request.app.state.db_conn
        device = request.state.device
        device_id = device["id"]
        wait_s = _query_int(wait, 0, 0, MAX_WAIT_S, "wait")
        deadline = time.monotonic() + wait_s
        while True:
            rows = db.claim_call_actions(conn, device_id, limit=MAX_ACTIONS)
            if rows:
                return {"actions": [
                    {"id": row["id"], "call_ref": row["call_ref"],
                     "action": row["action"], "payload": row["payload"],
                     "expires_at": row["expires_at"]}
                    for row in rows
                ]}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"actions": []}
            await asyncio.sleep(min(WAIT_STEP_S, remaining))

    @router.post("/calls/actions/{action_id}/result")
    async def call_action_result(request: Request, action_id: str,
                                 payload: CallActionResultBody):
        """Report the outcome of an action.

        A duplicate or late report answers ``accepted: false`` with the
        current status, so the phone stops retrying. An action of another
        device is unknown: the answer is 404, never a hint that it exists.
        """
        conn = request.app.state.db_conn
        device = request.state.device
        existing = db.call_action(conn, action_id)
        if existing is None or existing.get("device_id") != device["id"]:
            raise HTTPException(404, "unknown call action")
        accepted, current = db.finish_call_action(
            conn, action_id, payload.status, payload.detail)
        if not accepted:
            if current is None:  # pragma: no cover - row vanished mid-request
                raise HTTPException(404, "unknown call action")
            return {"accepted": False, "status": current}
        rev.bump_rev(request.app, existing.get("device_id"), None)
        return {"accepted": True, "status": payload.status}

    return router


def build_miniapp_router(settings) -> APIRouter:
    """The Mini App routes for the calls of one device."""
    router = APIRouter(dependencies=[Depends(require_miniapp(settings))])

    @router.get("/app/api/devices/{device_id}/calls")
    async def device_calls(
        request: Request,
        device_id: str,
        q: str | None = None,
        limit: str | None = None,
        before_ts: str | None = None,
        before_id: str | None = None,
    ):
        """The call log of ONE device, newest first."""
        conn = request.app.state.db_conn
        _known_device(conn, device_id)
        query = _query_text(q)
        page = _query_int(limit, 100, 1, MAX_HISTORY_LIMIT, "limit")
        cursor_ts = _query_cursor_ts(before_ts, "before_ts")
        cursor_id = _query_cursor(before_id, "before_id")
        if cursor_id is not None and cursor_ts is None:
            raise HTTPException(422, "before_id requires before_ts")
        # One extra row tells the client whether an older page exists.
        rows = db.page_calls(conn, device_id, limit=page + 1,
                             before_ts=cursor_ts, before_id=cursor_id,
                             query=query)
        has_more = len(rows) > page
        if has_more:
            rows = rows[:page]
        return {
            "device_id": device_id,
            "calls": [_call_shape(conn, device_id, row) for row in rows],
            "has_more": has_more,
        }

    @router.get("/app/api/devices/{device_id}/calls/live")
    async def device_live_call(request: Request, device_id: str):
        """The current call of ONE device, or a null call when it is idle."""
        conn = request.app.state.db_conn
        _known_device(conn, device_id)
        return {"device_id": device_id,
                "call": live_call_shape(conn, device_id)}

    @router.post("/app/api/devices/{device_id}/calls/actions")
    async def device_call_action(request: Request, device_id: str,
                                 payload: CallActionBody):
        """Ask a phone to run one call action.

        ``dial`` has no live call yet: its payload is the target, which is
        normalised here so the phone never receives a raw spelling, and an
        emergency number is refused outright. Every other action names the
        ``call_ref`` of the live call and is refused as stale when that
        call is over, so an action can never land on the next call.
        """
        conn = request.app.state.db_conn
        _active_device(conn, device_id)
        action = payload.action

        if action == "dial":
            if payload.call_ref.strip():
                raise HTTPException(422, {"error": "invalid_call_ref"})
            raw = (payload.payload or "").strip()
            if not raw:
                raise HTTPException(422, {"error": "invalid_destination"})
            if is_emergency_number(raw):
                raise HTTPException(422, {"error": "emergency_number"})
            target = phones.normalize_target(raw)
            if target is None:
                raise HTTPException(422, {"error": "invalid_destination"})
            payload_value = target
            call_ref = ""
        else:
            call_ref = payload.call_ref.strip()
            if not call_ref:
                raise HTTPException(409, {"error": "stale_call"})
            payload_value = payload.payload
            if payload_value is not None:
                payload_value = payload_value.strip() or None

        # Idempotency first: a retry after a timeout has to succeed even
        # when the call has ended since. The ref belongs to one device and
        # one action; the same ref anywhere else is a conflict, never a
        # second action.
        existing = db.call_action_by_client_ref(conn, payload.client_ref)
        if existing is not None:
            if (existing["device_id"] != device_id
                    or existing["action"] != action
                    or existing["call_ref"] != call_ref):
                raise HTTPException(409, {"error": "client_ref_conflict"})
            return _action_payload(existing, duplicate=True)

        if action != "dial":
            session = db.call_session_get(conn, device_id)
            if (not session_actionable(session)
                    or session.get("call_ref") != call_ref):
                raise HTTPException(409, {"error": "stale_call"})

        row = db.call_action_create(conn, device_id, action,
                                    call_ref=call_ref,
                                    payload=payload_value,
                                    client_ref=payload.client_ref)
        if row is None:
            # A concurrent retry won the insert: its row is the truth.
            stored = db.call_action_by_client_ref(conn, payload.client_ref)
            if stored is not None and (stored["device_id"] == device_id
                                       and stored["action"] == action
                                       and stored["call_ref"] == call_ref):
                return _action_payload(stored, duplicate=True)
            raise HTTPException(409, {"error": "client_ref_conflict"})
        rev.bump_rev(request.app, device_id, None)
        return _action_payload(row, duplicate=False)

    @router.post("/app/api/devices/{device_id}/calls/read")
    async def device_calls_read(request: Request, device_id: str,
                                payload: CallReadBody):
        """Clear the missed badge of ONE device up to a timestamp."""
        conn = request.app.state.db_conn
        _known_device(conn, device_id)
        # A client clock can be ahead; a read marker in the future would
        # hide calls that have not arrived yet.
        until_ts = min(int(payload.until_ts), int(time.time()))
        read = db.mark_calls_read(conn, device_id, until_ts)
        if read:
            rev.bump_rev(request.app, device_id, None)
        return {
            "device_id": device_id,
            "read": read,
            "missed": db.count_missed_calls(conn, device_id),
        }

    return router


def _action_payload(row: dict, duplicate: bool) -> dict:
    """One action row in the shape the Mini App reads."""
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "call_ref": row["call_ref"],
        "action": row["action"],
        "payload": row["payload"],
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "client_ref": row["client_ref"],
        "duplicate": duplicate,
    }
