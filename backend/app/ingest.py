import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator

import app.db as db
from app import rev

MAX_RECEIVED_AT_DRIFT_S = 86400

router = APIRouter()


class SmsIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    sender: str = Field(min_length=1, max_length=64)
    sender_name: str | None = Field(default=None, max_length=64)
    body: str = Field(min_length=1, max_length=16000)
    received_at: int | str
    parts: int | None = Field(default=None, ge=1)
    part_index: int | None = Field(default=None, ge=0)
    thread_id: str | None = Field(default=None, max_length=64)
    sub_id: int = Field(default=0, ge=0, le=99)
    source: str = Field(default="sms", max_length=16)

    @model_validator(mode="after")
    def _validate_received_at(self):
        raw = self.received_at
        if isinstance(raw, str):
            text = raw.strip()
            try:
                epoch = int(text)
            except ValueError:
                raise ValueError("received_at must be epoch-seconds (int or numeric string)") from None
        else:
            epoch = raw
        if abs(epoch - int(time.time())) > MAX_RECEIVED_AT_DRIFT_S:
            raise ValueError(f"received_at must be within +/-{MAX_RECEIVED_AT_DRIFT_S}s of now")
        object.__setattr__(self, "received_at", epoch)
        return self


def _current_state(conn, msg_id):
    with db._WRITE_LOCK:
        row = conn.execute("SELECT state FROM messages WHERE id = ?", (msg_id,)).fetchone()
    return row["state"] if row is not None else None


def _is_blocked(conn, sender: str, body: str) -> bool:
    filters = db.filters_all(conn)
    if sender in filters["block"]:
        return True
    if filters["allow"] and sender not in filters["allow"]:
        return True
    return any(keyword in body for keyword in filters["keyword"])


@router.post("/sms")
async def ingest_sms(request: Request, payload: SmsIn):
    conn = request.app.state.db_conn

    if _is_blocked(conn, payload.sender, payload.body):
        return {"accepted": False, "reason": "blocked"}

    msg = {"id": payload.id, "sender": payload.sender, "body": payload.body,
           "received_at": payload.received_at, "source": payload.source,
           "sub_id": payload.sub_id,
           "parts": payload.parts if payload.parts is not None else 1,
           "part_index": payload.part_index if payload.part_index is not None else 0}
    if payload.thread_id is not None:
        msg["thread_id"] = payload.thread_id
    if payload.sender_name is not None:
        msg["sender_name"] = payload.sender_name

    paused = db.settings_get(conn, "paused") == "1"

    # The same SMS can reach the phone twice (broadcast + notification
    # capture). Identical sender, text and timestamp from ANOTHER capture
    # path means it is the same message: accept it, but do not queue a
    # second Telegram delivery. Two genuine repeats from the same path
    # are separate messages and both are delivered.
    twin = db.find_recent_duplicate(conn, payload.sender, payload.body,
                                    payload.received_at,
                                    source=payload.source)
    if twin is not None:
        return {"accepted": True, "duplicate": True,
                "state": twin.get("state", "pending")}

    if paused:
        inserted = db.record_message(conn, {**msg, "state": "received"})
        state = "received" if inserted else _current_state(conn, payload.id)
    else:
        inserted = db.record_message(conn, msg)
        state = "pending" if inserted else _current_state(conn, payload.id)

    if inserted and not paused:
        # Wake the delivery worker instead of letting the message wait
        # for the next idle cycle (up to two minutes).
        poke = getattr(request.app.state, "poke_event", None)
        if poke is not None:
            poke.set()
    if inserted:
        # A genuine new message is inbox-visible even while forwarding is
        # paused. Duplicates and blocked messages never get this far.
        rev.bump_rev(request.app)

    response = {"accepted": True, "duplicate": not inserted, "state": state}
    if paused:
        response["paused"] = True
    return response
