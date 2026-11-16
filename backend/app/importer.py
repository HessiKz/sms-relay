"""Historical SMS import from the phone's inbox and sent folder.

The endpoint is opt-in from the Android app and deliberately separate from
``/sms``. An inbound item (``direction='in'``, the default) is stored with
``state='received'`` and ``source='import'``, so it never enters the
Telegram delivery queue. An outbound item (``direction='out'``) is stored
as an already-sent command in ``sms_commands`` with ``status='sent'``, so
it is history too: it is never pending, never handed to a phone and never
forwarded to Telegram. The only effect is that old conversations become
visible in the Mini App, in both directions.

Timestamps are not held to the +/-24 h drift rule of live ingestion; an
import exists to carry years of history. The bounds below only reject
values that cannot be SMS timestamps at all.
"""

import hashlib
import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator

import app.db as db
import app.phones as phones
from app import rev

MAX_IMPORT_ITEMS = 200
MAX_IMPORT_BODY = 16000
MAX_PROVIDER_ID = 64
# 2000-01-01: no SMS inbox predates this in practice.
MIN_RECEIVED_AT = 946684800
FUTURE_GRACE_S = 86400

router = APIRouter()


class ImportItem(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    sender: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=MAX_IMPORT_BODY)
    received_at: int | str
    sub_id: int = Field(default=0, ge=0, le=99)
    provider_id: str | int | None = None
    # 'in' is the inbox and the default, so an older client keeps working.
    # 'out' is the phone's Sent folder: sender is then the recipient.
    direction: Literal["in", "out"] = "in"

    @model_validator(mode="after")
    def _validate_received_at(self):
        raw = self.received_at
        if isinstance(raw, str):
            try:
                epoch = int(raw.strip())
            except ValueError:
                raise ValueError(
                    "received_at must be epoch-seconds (int or numeric string)"
                ) from None
        else:
            epoch = raw
        if epoch < MIN_RECEIVED_AT:
            raise ValueError(f"received_at must be at or after {MIN_RECEIVED_AT}")
        if epoch > int(time.time()) + FUTURE_GRACE_S:
            raise ValueError("received_at is too far in the future")
        object.__setattr__(self, "received_at", epoch)
        return self

    @model_validator(mode="after")
    def _trim_provider_id(self):
        if self.provider_id is not None:
            object.__setattr__(self, "provider_id", str(self.provider_id)[:MAX_PROVIDER_ID])
        return self


class ImportBatch(BaseModel):
    items: list[ImportItem] = Field(min_length=1, max_length=MAX_IMPORT_ITEMS)


def _item_id(item: ImportItem, device_id: str | None) -> str:
    """Deterministic message id.

    The phone's inbox row id is the strongest identity: replaying the same
    batch after a crash produces the same ``imp-<hash>`` values, and
    ``INSERT OR IGNORE`` makes the replay a no-op. Messages without a
    provider id fall back to sender, timestamp and text.

    The device and the direction are part of the basis: two phones may use
    the same provider id for different rows, and an inbound and an outbound
    message from the same conversation must not collapse into one id. The
    id stays deterministic, so a replay is still a no-op.
    """
    provider = item.provider_id or item.id
    if provider:
        basis = "pid:" + provider
    else:
        basis = "body:" + "\x00".join(
            (item.sender, str(item.received_at), item.body))
    basis = "\x00".join(
        (str(device_id or ""), item.direction, basis))
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    return "imp-" + digest


@router.post("/import")
async def import_sms(request: Request, payload: ImportBatch):
    conn = request.app.state.db_conn
    # The device comes from the bearer token, never from the body: the
    # imported history belongs to the authenticated device only.
    device = request.state.device
    device_id = device["id"] if device is not None else None
    imported = 0
    duplicates = 0
    skipped = 0

    for item in payload.items:
        key = phones.thread_key(item.sender) or phones.raw_key(item.sender)
        if key is None:
            skipped += 1
            continue
        item_id = _item_id(item, device_id)
        if item.direction == "out":
            # The Sent folder is history as well, but it must never be
            # treated as work: the row is written as an already-sent
            # command, so no phone polls it and Telegram never sees it.
            if db.find_import_command_duplicate(
                    conn, device_id, key, item.body, item.received_at):
                duplicates += 1
                continue
            inserted = db.record_imported_command(conn, {
                "id": item_id,
                "created_at": item.received_at,
                "target": item.sender,
                "body": item.body,
                "source": "import",
                "status": "sent",
                "thread_key": key,
                "sub_id": item.sub_id,
                "device_id": device_id,
            })
        else:
            if db.find_import_duplicate(conn, key, item.body, item.received_at,
                                       device_id=device_id):
                duplicates += 1
                continue
            inserted = db.record_message(conn, {
                "id": item_id,
                "sender": item.sender,
                "body": item.body,
                "received_at": item.received_at,
                "source": "import",
                "sub_id": item.sub_id,
                "state": "received",
                "device_id": device_id,
            })
        if inserted:
            imported += 1
        else:
            duplicates += 1

    if imported:
        # An import touches many conversations of one device, so the scope
        # is the device without a thread.
        rev.bump_rev(request.app, device_id, None)

    return {
        "accepted": True,
        "imported": imported,
        "duplicates": duplicates,
        "skipped": skipped,
    }
