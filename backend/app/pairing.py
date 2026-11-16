"""Device pairing: QR hand-shake, approval and per-device tokens.

The flow has three actors:

* the **phone** calls ``POST /pair/start`` and shows a QR code plus a
  short scan code. It never receives a token in this step;
* the **admin** scans the code in the Mini App and approves it. The
  approval creates (or reactivates) the device, but still no token;
* the **phone** calls ``POST /pair/claim`` with its own claim secret and
  receives the device token exactly once.

The QR code carries the pairing id and the short scan code only. The
claim secret never leaves the phone, so a photographed QR code cannot
collect a token.

Both public endpoints are rate limited per client address and the amount
of open pairings is capped, so nobody can fill the database with them.
"""

import hmac
import json
import re
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import app.db as db
from app import rev
from app.miniapp_auth import require_miniapp
from app.security import RateLimiter

PAIRING_TTL_S = 600
MAX_PENDING_PAIRINGS = 50
MAX_PAIRINGS_PER_IP = 10
PAIRING_WINDOW_S = 60.0
# The phone polls the claim endpoint every couple of seconds while it
# waits for the admin, and behind the reverse proxy every phone shares
# one address. A valid poll must therefore never be rate limited: the
# limit only counts the attempts with a WRONG claim secret.
#
# The failures are counted per pairing, so a photographed QR code cannot
# use up the budget of the phone that owns the pairing: the phone still
# knows the secret and still gets through.
MAX_CLAIM_FAILURES_PER_PAIRING = 10
# Unknown pairings cannot be counted per pairing, so they are counted
# per address. This only bounds the guessing of pairing ids; it never
# touches a claim with the right secret.
MAX_CLAIM_FAILURES_PER_IP = 60
# A safety net for a runaway client. The phone polls every two seconds,
# so this cap is never reached by a normal client.
MAX_VALID_CLAIMS_PER_PAIRING = 600
CLAIM_WINDOW_S = 60.0
MAX_NAME_LEN = 64
MAX_PLATFORM_LEN = 16
MAX_VERSION_LEN = 32
MAX_DEVICE_ID_LEN = 64
MIN_CLAIM_SECRET_LEN = 32
MAX_CLAIM_SECRET_LEN = 128
MAX_SCAN_CODE_LEN = 12
TOKEN_PREFIX_LEN = 11
# The admin types the short code by hand, so a lookup is cheap but should
# not become an oracle for guessing codes: a handful per minute is plenty.
MAX_LOOKUPS_PER_ADMIN = 20
LOOKUP_WINDOW_S = 60.0
# The scan code is read aloud and typed by hand, so it avoids the
# characters that are easy to confuse.
_SCAN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_SCAN_CODE_LEN = 6


class PairStartBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=MAX_DEVICE_ID_LEN,
                           pattern=r"^[A-Za-z0-9._:-]+$")
    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    platform: str = Field(default="android", min_length=1,
                           max_length=MAX_PLATFORM_LEN)
    app_version: str | None = Field(default=None, max_length=MAX_VERSION_LEN)
    claim_secret: str = Field(min_length=MIN_CLAIM_SECRET_LEN,
                               max_length=MAX_CLAIM_SECRET_LEN)


class PairClaimBody(BaseModel):
    pairing_id: str = Field(min_length=1, max_length=64)
    claim_secret: str = Field(min_length=MIN_CLAIM_SECRET_LEN,
                               max_length=MAX_CLAIM_SECRET_LEN)


class PairPreviewBody(BaseModel):
    pairing_id: str = Field(min_length=1, max_length=64)
    scan_code: str = Field(min_length=1, max_length=MAX_SCAN_CODE_LEN)


class PairApproveBody(PairPreviewBody):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LEN)


class PairLookupBody(BaseModel):
    scan_code: str = Field(min_length=1, max_length=32)


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)


def _scan_code() -> str:
    return "".join(secrets.choice(_SCAN_ALPHABET) for _ in range(_SCAN_CODE_LEN))


def _new_token() -> str:
    return "sr_" + secrets.token_urlsafe(32)


def _client_key(request: Request) -> str:
    client = request.client
    host = getattr(client, "host", None) or "unknown"
    return "pair:" + host


def _qr_payload(settings, pairing_id: str, scan_code: str) -> str:
    """What the phone renders as a QR code.

    The phone draws whatever the backend returns, so the format may change
    without an APK update.
    """
    token = f"pair_{pairing_id}_{scan_code}"
    username = (settings.miniapp_bot_username or "").strip()
    if username:
        return f"https://t.me/{username}?startapp={token}"
    url = (settings.miniapp_url or "").strip()
    if url:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}pair={pairing_id}_{scan_code}"
    return token


def _device_public(row: dict) -> dict:
    """The device fields the Mini App may see."""
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "platform": row.get("platform"),
        "app_version": row.get("app_version"),
        "archived": bool(row.get("archived")),
        "revoked": row.get("revoked_at") is not None,
        "paired_at": row.get("paired_at"),
        "last_seen_at": row.get("last_seen_at"),
        "created_at": row.get("created_at"),
    }


def _device_summary(conn, row: dict) -> dict:
    """A device with the state the Mini App device list shows."""
    now = int(time.time())
    summary = _device_public(row)
    last_seen = row.get("last_seen_at")
    summary["online"] = bool(last_seen and last_seen > now - 180)
    summary["last_seen"] = last_seen
    summary["sending"] = (None if row.get("sending_enabled") is None
                          else bool(row.get("sending_enabled")))
    summary["sims"] = _sims(row.get("sims_json"))
    summary["unread"] = db.device_unread_count(conn, row["id"])
    summary["preview"] = db.device_last_preview(conn, row["id"])
    summary["pending_commands"] = len(
        db.pending_commands(conn, row["id"], limit=100))
    summary["default"] = row.get("id") == db.settings_get(
        conn, "default_device_id")
    return summary


def _sims(sims_json) -> list:
    if not sims_json:
        return []
    try:
        parsed = json.loads(sims_json)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _normalize_scan_code(value: str | None) -> str:
    """The code as the admin may type it: case, spaces and hyphens ignored."""
    text = re.sub(r"[\s\-]+", "", str(value or ""))
    return text.upper()


def _unknown_pairing() -> HTTPException:
    """A generic answer: it does not tell whether the pairing exists."""
    return HTTPException(404, "unknown pairing")


def _check_scan_code(pairing: dict, scan_code: str) -> bool:
    """Compare the scan code with the stored digest, in constant time."""
    digest = db.token_hash(scan_code)
    stored = pairing.get("scan_code_hash")
    if not digest or not stored:
        return False
    return hmac.compare_digest(digest, stored)


def _check_claim_secret(pairing: dict, claim_secret: str) -> bool:
    """Compare the claim secret with the stored digest, in constant time."""
    digest = db.token_hash(claim_secret)
    stored = pairing.get("claim_secret_hash")
    if not digest or not stored:
        return False
    return hmac.compare_digest(digest, stored)


def build_router(settings) -> APIRouter:
    """The public pairing endpoints (no token, rate limited per IP)."""
    limiter = RateLimiter(max_events=MAX_PAIRINGS_PER_IP,
                           window_s=PAIRING_WINDOW_S)
    # Failures with a wrong claim secret, counted per pairing. The phone
    # that owns the pairing is never affected by them.
    claim_failures = RateLimiter(max_events=MAX_CLAIM_FAILURES_PER_PAIRING,
                                  window_s=CLAIM_WINDOW_S)
    # Failures with an unknown pairing id, counted per address.
    claim_failures_by_ip = RateLimiter(
        max_events=MAX_CLAIM_FAILURES_PER_IP, window_s=CLAIM_WINDOW_S)
    # A valid poll is cheap and rare (the phone waits for the admin), so
    # this cap is only a safety net against a runaway client.
    claim_polls = RateLimiter(max_events=MAX_VALID_CLAIMS_PER_PAIRING,
                               window_s=CLAIM_WINDOW_S)
    router = APIRouter()

    @router.post("/pair/start")
    async def pair_start(request: Request, payload: PairStartBody):
        if not limiter.allow(_client_key(request)):
            raise HTTPException(429, "rate limited")
        conn = request.app.state.db_conn
        now = int(time.time())
        db.pairing_cleanup(conn, now)
        if db.pairing_pending_count(conn, now) >= MAX_PENDING_PAIRINGS:
            raise HTTPException(429, "too many pending pairings")
        pairing_id = uuid.uuid4().hex
        scan_code = _scan_code()
        db.pairing_create(
            conn,
            pairing_id,
            db.token_hash(scan_code),
            db.token_hash(payload.claim_secret),
            payload.device_id,
            payload.name.strip(),
            payload.platform.strip(),
            (payload.app_version or "").strip() or None,
            now,
            now + PAIRING_TTL_S,
        )
        return {
            "pairing_id": pairing_id,
            "scan_code": scan_code,
            "expires_at": now + PAIRING_TTL_S,
            "qr_payload": _qr_payload(settings, pairing_id, scan_code),
        }

    @router.post("/pair/claim")
    async def pair_claim(request: Request, payload: PairClaimBody):
        conn = request.app.state.db_conn
        now = int(time.time())
        pairing = db.pairing_by_id(conn, payload.pairing_id)
        if pairing is None:
            # An unknown pairing is a failed attempt as well. It cannot
            # be counted per pairing, so it is counted per address.
            if not claim_failures_by_ip.allow(_client_key(request)):
                raise HTTPException(429, "rate limited")
            raise _unknown_pairing()
        if not _check_claim_secret(pairing, payload.claim_secret):
            # The secret is wrong. The answer stays the generic one until
            # the failure budget of this pairing is used up; after that
            # the wrong attempts are refused for a minute. The phone that
            # owns the pairing keeps its own budget: it knows the secret.
            if not claim_failures.allow("pairing:" + pairing["id"]):
                raise HTTPException(429, "rate limited")
            raise _unknown_pairing()
        # The secret is right, so this is the phone that owns the pairing.
        # It may poll as often as it likes: the cap is only a safety net.
        if not claim_polls.allow("pairing:" + pairing["id"]):
            raise HTTPException(429, "rate limited")
        status = pairing.get("status")
        if status == "claimed":
            raise HTTPException(409, "pairing already claimed")
        if pairing.get("expires_at", 0) <= now:
            db.pairing_expire(conn, pairing["id"])
            raise HTTPException(410, "pairing expired")
        if status in ("cancelled", "expired"):
            raise HTTPException(410, "pairing expired or replaced")
        if status == "pending":
            # Not approved yet: the phone keeps waiting.
            return JSONResponse(status_code=202,
                                content={"status": "pending",
                                         "expires_at": pairing["expires_at"]})
        if status != "approved":
            raise _unknown_pairing()

        token = _new_token()
        device = db.claim_pairing(conn, pairing["id"], db.token_hash(token),
                                  token[:TOKEN_PREFIX_LEN], now)
        if device is None:
            raise HTTPException(409, "pairing already claimed")
        rev.bump_rev(request.app, device["id"], None)
        return {
            "status": "claimed",
            "token": token,
            "device": _device_public(device),
        }

    return router


def build_miniapp_router(settings) -> APIRouter:
    """The pairing and device endpoints of the Mini App (admin only)."""
    guard = require_miniapp(settings)
    router = APIRouter(dependencies=[Depends(guard)])
    # The admin already holds a Mini App credential; this only bounds a
    # runaway client and makes guessing the short code pointless.
    lookups = RateLimiter(max_events=MAX_LOOKUPS_PER_ADMIN,
                          window_s=LOOKUP_WINDOW_S)

    @router.post("/app/api/pair/lookup")
    async def pair_lookup(request: Request, payload: PairLookupBody):
        """Resolve the short code the phone shows to the pairing behind it.

        The phone draws a QR code and a six-character code. The admin may
        type only the code, so this endpoint finds the one live pairing
        that the code belongs to. The answer never says whether a code
        exists: a miss is the same generic 404 as a wrong code.
        """
        admin = getattr(request.state, "tg_user", {}) or {}
        if not lookups.allow("lookup:" + str(admin.get("id") or "unknown")):
            raise HTTPException(429, "rate limited")
        code = _normalize_scan_code(payload.scan_code)
        if not 1 <= len(code) <= MAX_SCAN_CODE_LEN:
            raise _unknown_pairing()
        conn = request.app.state.db_conn
        now = int(time.time())
        matches = db.pairing_by_scan_code(conn, code, now)
        if not matches:
            raise _unknown_pairing()
        if len(matches) > 1:
            # A six-character code can collide. Guessing would approve
            # the wrong phone, so the admin has to hand over the whole
            # payload instead.
            raise HTTPException(409, "ambiguous code")
        pairing = matches[0]
        return {
            "pairing_id": pairing["id"],
            "device_id": pairing["device_id"],
            "name": pairing["device_name"],
            "platform": pairing["platform"],
            "app_version": pairing.get("app_version"),
            "expires_at": pairing["expires_at"],
            "status": pairing["status"],
        }

    @router.post("/app/api/pair/preview")
    async def pair_preview(request: Request, payload: PairPreviewBody):
        conn = request.app.state.db_conn
        now = int(time.time())
        pairing = db.pairing_by_id(conn, payload.pairing_id)
        if pairing is None or not _check_scan_code(pairing, payload.scan_code):
            raise _unknown_pairing()
        if pairing.get("expires_at", 0) <= now:
            db.pairing_expire(conn, pairing["id"])
            raise HTTPException(410, "pairing expired")
        if pairing.get("status") in ("cancelled", "expired"):
            raise HTTPException(410, "pairing expired or replaced")
        return {
            "pairing_id": pairing["id"],
            "device_id": pairing["device_id"],
            "name": pairing["device_name"],
            "platform": pairing["platform"],
            "app_version": pairing.get("app_version"),
            "expires_at": pairing["expires_at"],
            "status": pairing["status"],
        }

    @router.post("/app/api/pair/approve")
    async def pair_approve(request: Request, payload: PairApproveBody):
        conn = request.app.state.db_conn
        now = int(time.time())
        pairing = db.pairing_by_id(conn, payload.pairing_id)
        if pairing is None or not _check_scan_code(pairing, payload.scan_code):
            raise _unknown_pairing()
        if pairing.get("expires_at", 0) <= now:
            db.pairing_expire(conn, pairing["id"])
            raise HTTPException(410, "pairing expired")
        if pairing.get("status") in ("cancelled", "expired"):
            raise HTTPException(410, "pairing expired or replaced")
        if pairing.get("status") == "claimed":
            raise HTTPException(409, "pairing already claimed")
        device_id = pairing["device_id"]
        existing = db.device_by_id(conn, device_id)
        if existing is not None and existing.get("archived"):
            raise HTTPException(409, "the archived device cannot be paired")
        if (existing is not None
                and existing.get("revoked_at") is None
                and existing.get("token_hash")):
            # A device that already holds a token is actively paired.
            # An unclaimed row (no token) is not: the previous approval
            # may have expired before the phone could claim it, so a new
            # approval must be able to finish the pairing.
            raise HTTPException(409, "device is already paired")
        admin = getattr(request.state, "tg_user", {}) or {}
        approved = db.pairing_approve(conn, pairing["id"], now,
                                      admin.get("id"),
                                      (payload.name or "").strip() or None)
        if not approved and pairing.get("status") != "approved":
            raise HTTPException(409, "pairing is not pending")
        name = (payload.name or "").strip() or pairing["device_name"]
        device = db.activate_device(conn, device_id, name, pairing["platform"],
                                    pairing.get("app_version"), now)
        rev.bump_rev(request.app, device_id, None)
        return {"device": _device_public(device)}

    @router.post("/app/api/pair/cancel")
    async def pair_cancel(request: Request, payload: PairPreviewBody):
        conn = request.app.state.db_conn
        pairing = db.pairing_by_id(conn, payload.pairing_id)
        if pairing is None or not _check_scan_code(pairing, payload.scan_code):
            raise _unknown_pairing()
        db.pairing_cancel(conn, pairing["id"])
        rev.bump_rev(request.app, pairing.get("device_id"), None)
        return {"status": "cancelled"}

    @router.get("/app/api/devices")
    async def list_devices(request: Request):
        conn = request.app.state.db_conn
        rows = db.list_devices(conn)
        return {"devices": [_device_summary(conn, row) for row in rows]}

    @router.patch("/app/api/devices/{device_id}")
    async def rename_device(request: Request, device_id: str, payload: RenameBody):
        conn = request.app.state.db_conn
        if db.device_by_id(conn, device_id) is None:
            raise HTTPException(404, "unknown device")
        db.rename_device(conn, device_id, payload.name.strip(), int(time.time()))
        rev.bump_rev(request.app, device_id, None)
        device = db.device_by_id(conn, device_id)
        return {"device": _device_public(device)}

    @router.post("/app/api/devices/{device_id}/revoke")
    async def revoke_device(request: Request, device_id: str):
        conn = request.app.state.db_conn
        device = db.device_by_id(conn, device_id)
        if device is None:
            raise HTTPException(404, "unknown device")
        if device.get("archived"):
            raise HTTPException(409, "the archived device cannot be revoked")
        now = int(time.time())
        if not db.revoke_device(conn, device_id, now):
            raise HTTPException(409, "device is not active")
        # The commands of a revoked device can never run: they fail now
        # instead of waiting for a phone that will not pick them up.
        db.fail_device_commands(conn, device_id, "device revoked", now)
        rev.bump_rev(request.app, device_id, None)
        return {"device": _device_public(db.device_by_id(conn, device_id))}

    @router.post("/app/api/devices/{device_id}/default")
    async def set_default_device(request: Request, device_id: str):
        """Choose the device the Telegram /sms command sends with.

        The bot never broadcasts: when several devices are paired, one of
        them is the default. Only a device that can actually send may be
        the default.
        """
        conn = request.app.state.db_conn
        device = db.device_by_id(conn, device_id)
        if device is None:
            raise HTTPException(404, "unknown device")
        if device.get("archived") or device.get("revoked_at") is not None:
            raise HTTPException(409, {"error": "device_not_active"})
        db.settings_set(conn, "default_device_id", device_id)
        rev.bump_rev(request.app, device_id, None)
        summary = _device_public(db.device_by_id(conn, device_id))
        summary["default"] = True
        return {"device": summary}

    return router
