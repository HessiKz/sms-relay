import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import app.db as db
from app import rev, security

VERSION = "0.1.0"
QUEUE_SCAN_LIMIT = 1000
MAX_STATE_IDS = 50
MAX_COMMANDS = 20
MAX_CONTACTS = 3000
GAP_S_RANGE = (1, 3600)
MAX_PER_HOUR_RANGE = (1, 50)
FILTER_MAX_PATTERNS = 100
FILTER_MAX_PATTERN_LEN = 64
MAX_DEVICE_INFO_BYTES = 2048
MAX_DEVICE_ID_LEN = 64
MAX_VERSION_LEN = 32
MAX_SIMS = 4
MAX_SIM_LABEL_LEN = 32
FALLBACK_DEVICE_ID = "phone-a"
ONLINE_WINDOW_S = 180
_PHONE_PATTERN = re.compile(r"^\+?[0-9]{5,20}$")


def _reject(detail: str) -> None:
    raise HTTPException(422, detail)


def _int_between(value, lo: int, hi: int) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and lo <= value <= hi)


def _parse_sim(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    if not _int_between(item.get("sub_id"), 0, 1000):
        return None
    if not _int_between(item.get("slot"), 0, 10):
        return None
    label = item.get("label")
    if label is not None:
        if not isinstance(label, str) or len(label) > MAX_SIM_LABEL_LEN:
            return None
    return {"sub_id": item["sub_id"], "slot": item["slot"],
            "label": label}


def _parse_device_info(raw: str | None) -> dict:
    """Parse X-Device-Info leniently.

    A malformed header must never break a command poll, so every field
    is validated independently and invalid fields are simply dropped.
    """
    if not raw:
        return {}
    try:
        if len(raw.encode("utf-8")) > MAX_DEVICE_INFO_BYTES:
            return {}
    except (UnicodeEncodeError, AttributeError):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}

    info: dict = {}
    device_id = data.get("id")
    if isinstance(device_id, str) and 0 < len(device_id) <= MAX_DEVICE_ID_LEN:
        info["id"] = device_id
    version = data.get("version")
    if isinstance(version, str) and 0 < len(version) <= MAX_VERSION_LEN:
        info["version"] = version
    sending = data.get("sending")
    if isinstance(sending, bool):
        info["sending"] = sending
    sims = data.get("sims")
    if isinstance(sims, list) and len(sims) <= MAX_SIMS:
        info["sims"] = [parsed for parsed in (_parse_sim(item) for item in sims)
                        if parsed is not None]
    return info


def _phone_shape(conn) -> dict:
    now = int(time.time())
    row = db.device_status(conn)
    if row is None:
        return {"last_seen": None, "online": False, "app_version": None,
                "sending": None, "sims": []}
    sims = []
    if row["sims_json"]:
        try:
            parsed = json.loads(row["sims_json"])
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            sims = parsed
    return {
        "last_seen": row["last_seen_at"],
        "online": row["last_seen_at"] > now - ONLINE_WINDOW_S,
        "app_version": row["app_version"],
        "sending": (None if row["sending_enabled"] is None
                    else bool(row["sending_enabled"])),
        "sims": sims,
    }


def _check_bool_setting(key: str, value) -> bool:
    if not isinstance(value, bool):
        _reject(f"{key} must be a boolean")
    return value


def _check_phone_setting(key: str, value) -> str | None:
    if value is None:
        return value
    if not isinstance(value, str) or not _PHONE_PATTERN.fullmatch(value):
        _reject(f"{key} must be a string matching ^+?[0-9]{{5,20}}$ or null")
    return value


def _check_int_setting(key: str, value, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        _reject(f"{key} must be an integer in {lo}..{hi}")
    return value


def _check_patterns(key: str, value) -> list[str]:
    try:
        return db.clean_filter_patterns(value)
    except ValueError as exc:
        _reject(f"{key}: {exc}")
        raise  # pragma: no cover - _reject always raises


_SETTING_CHECKS = {
    "paused": _check_bool_setting,
    "fallback_enabled": _check_bool_setting,
    "phone_b": _check_phone_setting,
    "fallback_min_gap_s": lambda key, value: _check_int_setting(key, value,
                                                               *GAP_S_RANGE),
    "fallback_max_per_hour": lambda key, value: _check_int_setting(
        key, value, *MAX_PER_HOUR_RANGE),
}


def _validate_settings(body: dict) -> None:
    for key, value in body.items():
        check = _SETTING_CHECKS.get(key)
        if check is None:
            _reject(f"unknown key: {key}")
        check(key, value)


def _persist_settings(conn, body: dict) -> None:
    for key, value in body.items():
        if isinstance(value, bool):
            stored = "1" if value else "0"
        elif value is None:
            stored = ""
        else:
            stored = str(value)
        db.settings_set(conn, key, stored)


def _settings_shape(conn) -> dict:
    def int_or_default(key: str, default: int) -> int:
        raw = db.settings_get(conn, key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    return {
        "paused": db.settings_get(conn, "paused") == "1",
        "fallback_enabled": db.settings_get(conn, "fallback_enabled") == "1",
        "phone_b": db.settings_get(conn, "phone_b") or None,
        "fallback_min_gap_s": int_or_default("fallback_min_gap_s", 30),
        "fallback_max_per_hour": int_or_default("fallback_max_per_hour", 10),
    }


def _filters_shape(conn) -> dict:
    filters = db.filters_all(conn)
    return {kind: sorted(filters[kind]) for kind in db._FILTER_KINDS}


def _validate_filters_body(body: dict) -> dict:
    provided = {}
    for kind, value in body.items():
        if kind not in db._FILTER_KINDS:
            _reject(f"unknown filter kind: {kind}")
        provided[kind] = _check_patterns(kind, value)
    return provided


def build_router(settings) -> APIRouter:
    admin_required = security.require_admin(settings)
    any_token = security.require_any(settings)
    device_required = security.require_device(settings)
    router = APIRouter()

    @router.get("/messages", dependencies=[Depends(admin_required)])
    async def list_messages(
        request: Request,
        limit: int = Query(default=50, gt=0, le=200),
        offset: int = Query(default=0, ge=0),
        state: str | None = Query(default=None),
    ):
        conn = request.app.state.db_conn
        if state is not None and state not in db.STATES:
            raise HTTPException(422, "invalid state")
        items = db.page_messages(conn, limit, offset, state)
        next_offset = offset + len(items) if len(items) == limit else None
        return {"items": items, "next_offset": next_offset}

    @router.get("/sms-state", dependencies=[Depends(any_token)])
    async def sms_state(request: Request, ids: str = Query(...)):
        """Delivery state for a bounded list of message ids.

        The Android app polls this to learn that the backend confirmed a
        failure for a specific message, which is the only condition that
        authorises a direct-SIM fallback SMS. A timeout is never
        reported here as a failure.
        """
        wanted = [part.strip() for part in ids.split(",") if part.strip()]
        if not wanted:
            raise HTTPException(422, "ids must contain at least one id")
        if len(wanted) > MAX_STATE_IDS:
            raise HTTPException(422, f"at most {MAX_STATE_IDS} ids per request")
        if any(len(part) > 64 for part in wanted):
            raise HTTPException(422, "id too long")
        conn = request.app.state.db_conn
        return {"states": db.states_for_ids(conn, wanted)}

    @router.get("/commands", dependencies=[Depends(device_required)])
    async def list_commands(request: Request):
        """SMS commands the phone should execute with its own SIM.

        Device token only: the app polls this and sends the SMS. The
        optional X-Device-Info header is advisory: a malformed or missing
        header never rejects the poll, it only leaves fields unknown.
        """
        conn = request.app.state.db_conn
        info = _parse_device_info(request.headers.get("x-device-info"))
        sims_json = (json.dumps(info["sims"], separators=(",", ":"))
                     if "sims" in info else None)
        db.device_seen(
            conn,
            info.get("id") or FALLBACK_DEVICE_ID,
            info.get("version"),
            info.get("sending"),
            sims_json,
            int(time.time()),
        )
        return {"commands": db.pending_commands(conn, limit=MAX_COMMANDS)}

    @router.post("/commands/{command_id}/result",
                 dependencies=[Depends(device_required)])
    async def command_result(request: Request, command_id: str,
                             payload: dict):
        """Report the outcome of a command back to the backend.

        A duplicate or late report is answered with 200 and
        ``accepted: false``: a 4xx would make the phone retry forever.
        """
        conn = request.app.state.db_conn
        status = payload.get("status")
        if status not in ("sent", "delivered", "failed", "rejected"):
            raise HTTPException(
                422, "status must be sent, delivered, failed or rejected")
        detail = payload.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise HTTPException(422, "detail must be a string")
        existing = db.command(conn, command_id)
        if existing is None:
            raise HTTPException(404, "unknown command")
        accepted, current = db.finish_command(
            conn, command_id, status, (detail or "")[:200] or None)
        if not accepted:
            if current is None:  # pragma: no cover - row vanished mid-request
                raise HTTPException(404, "unknown command")
            return {"accepted": False, "status": current}
        # An accepted transition changes the outbound bubble in the Mini App.
        rev.bump_rev(request.app)
        # Tell the admin in Telegram what happened to their request. Only
        # the first accepted sent/failed/rejected gets a note; a delivered
        # report is silent by design.
        telegram = getattr(request.app.state, "tg", None)
        chat_id = request.app.state.settings.telegram_chat_id
        if telegram is not None and status != "delivered":
            note = {
                "sent": f"\u2705 SMS sent to {existing['target']}",
                "failed": f"\u274c SMS to {existing['target']} failed"
                          + (f": {detail}" if detail else ""),
                "rejected": f"\u26d4 sending is disabled in the app"
                            + (f" ({detail})" if detail else ""),
            }.get(status, status)
            try:
                await telegram.send_to_chat(chat_id, note)
            except Exception:
                pass
        return {"accepted": True, "status": status}

    @router.post("/contacts", dependencies=[Depends(device_required)])
    async def upload_contacts(request: Request, payload: dict):
        """The phone uploads its contact list so the bot can offer names.

        The whole list is replaced on every upload; only name and number
        are stored, never anything else from the contacts database.
        """
        contacts = payload.get("contacts")
        if not isinstance(contacts, list):
            raise HTTPException(422, "contacts must be a list")
        if len(contacts) > MAX_CONTACTS:
            raise HTTPException(422, f"at most {MAX_CONTACTS} contacts")
        cleaned = []
        for entry in contacts:
            if not isinstance(entry, dict):
                continue
            number = (entry.get("number") or "").strip()
            name = (entry.get("name") or "").strip()
            if not number or not name:
                continue
            if len(number) > 32 or len(name) > 96:
                continue
            cleaned.append({"number": number, "name": name})
        conn = request.app.state.db_conn
        stored = db.replace_contacts(conn, cleaned)
        return {"stored": stored}

    @router.get("/contacts", dependencies=[Depends(admin_required)])
    async def list_contacts(request: Request):
        conn = request.app.state.db_conn
        return {"count": db.contact_count(conn),
                "contacts": db.list_contacts(conn, limit=200)}

    @router.get("/status", dependencies=[Depends(any_token)])
    async def status(request: Request):
        conn = request.app.state.db_conn
        return {
            "paused": db.settings_get(conn, "paused") == "1",
            "counts_by_state": db.counts_by_state(conn),
            "queue": db.count_jobs(conn),
            "version": VERSION,
            "phone": _phone_shape(conn),
        }

    @router.post("/admin/pause", dependencies=[Depends(admin_required)])
    async def pause(request: Request):
        conn = request.app.state.db_conn
        db.settings_set(conn, "paused", "1")
        return {"paused": True}

    @router.post("/admin/resume", dependencies=[Depends(admin_required)])
    async def resume(request: Request):
        conn = request.app.state.db_conn
        db.settings_set(conn, "paused", "0")
        resumed = db.resume_received(conn)
        return {"paused": False, "resumed": resumed}

    @router.get("/admin/settings", dependencies=[Depends(admin_required)])
    async def get_admin_settings(request: Request):
        conn = request.app.state.db_conn
        return _settings_shape(conn)

    @router.put("/admin/settings", dependencies=[Depends(admin_required)])
    async def put_admin_settings(request: Request, body: dict):
        _validate_settings(body)
        conn = request.app.state.db_conn
        _persist_settings(conn, body)
        return _settings_shape(conn)

    @router.get("/admin/filters", dependencies=[Depends(admin_required)])
    async def get_admin_filters(request: Request):
        conn = request.app.state.db_conn
        return _filters_shape(conn)

    @router.put("/admin/filters", dependencies=[Depends(admin_required)])
    async def put_admin_filters(request: Request, body: dict):
        provided = _validate_filters_body(body)
        conn = request.app.state.db_conn
        for kind, patterns in provided.items():
            db.filters_replace(conn, kind, patterns)
        return _filters_shape(conn)

    return router
