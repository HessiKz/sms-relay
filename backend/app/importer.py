"""Historical SMS import from the phone's inbox.

The endpoint is opt-in from the Android app and deliberately separate from
``/sms``: imported rows are stored with ``state='received'`` and
``source='import'``, so they never enter the Telegram delivery queue. The
only effect is that old conversations become visible in the Mini App.

Timestamps are not held to the +/-24 h drift rule of live ingestion; an
import exists to carry years of history. The bounds below only reject
values that cannot be SMS timestamps at all.
"""

import hashlib
import time

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


def _item_id(item: ImportItem) -> str:
    """Deterministic message id.

    The phone's inbox row id is the strongest identity: replaying the same
    batch after a crash produces the same ``imp-<hash>`` values, and
    ``INSERT OR IGNORE`` makes the replay a no-op. Messages without a
    provider id fall back to sender, timestamp and text.
    """
    provider = item.provider_id or item.id
    if provider:
        basis = "pid:" + provider
    else:
        basis = "body:" + "\x00".join(
            (item.sender, str(item.received_at), item.body))
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    return "imp-" + digest


@router.post("/import")
async def import_sms(request: Request, payload: ImportBatch):
    conn = request.app.state.db_conn
    imported = 0
    duplicates = 0
    skipped = 0

    for item in payload.items:
        key = phones.thread_key(item.sender) or phones.raw_key(item.sender)
        if key is None:
            skipped += 1
            continue
        if db.find_import_duplicate(conn, key, item.body, item.received_at):
            duplicates += 1
            continue
        inserted = db.record_message(conn, {
            "id": _item_id(item),
            "sender": item.sender,
            "body": item.body,
            "received_at": item.received_at,
            "source": "import",
            "sub_id": item.sub_id,
            "state": "received",
        })
        if inserted:
            imported += 1
        else:
            duplicates += 1

    if imported:
        rev.bump_rev(request.app)

    return {
        "accepted": True,
        "imported": imported,
        "duplicates": duplicates,
        "skipped": skipped,
    }
