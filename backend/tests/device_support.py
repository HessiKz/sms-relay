"""Helpers for the per-device tests.

A phone has no shared token any more: it pairs through the Mini App and
gets a token of its own. The helpers here do the same thing, so the
tests exercise the real flow instead of poking the database.
"""

import secrets
import time

import app.db as db
from tests.miniapp_support import headers as admin_headers

TOKEN_PREFIX = "sr_"
# The device every test module uses as "the phone".
DEVICE_ID = "device-a"
DEVICE_TOKEN = "sr_test-device-token-device-a"
SECOND_DEVICE_ID = "device-b"


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def device_headers(token: str = DEVICE_TOKEN) -> dict:
    return {"Authorization": "Bearer " + token}


def seed_device(conn, device_id: str = DEVICE_ID, token: str = DEVICE_TOKEN,
                name: str = "Phone A", platform: str = "android",
                app_version: str | None = None) -> dict:
    """Create a paired device with a known token."""
    now = int(time.time())
    db.activate_device(conn, device_id, name, platform, app_version, now)
    db.set_device_token(conn, device_id, db.token_hash(token),
                        token[:11], now)
    return db.device_by_id(conn, device_id)


def start_pairing(client, device_id: str = SECOND_DEVICE_ID,
                  name: str = "Phone B", platform: str = "android",
                  app_version: str = "2.0.0",
                  claim_secret: str | None = None) -> dict:
    """Ask for a pairing code, the way the phone does."""
    secret = claim_secret if claim_secret is not None else ("c" * 40)
    response = client.post("/pair/start", json={
        "device_id": device_id,
        "name": name,
        "platform": platform,
        "app_version": app_version,
        "claim_secret": secret,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    data["claim_secret"] = secret
    return data


def approve_pairing(client, pairing: dict, name: str | None = None) -> dict:
    """Approve a pairing in the Mini App."""
    body = {"pairing_id": pairing["pairing_id"],
            "scan_code": pairing["scan_code"]}
    if name is not None:
        body["name"] = name
    response = client.post("/app/api/pair/approve", json=body,
                            headers=admin_headers())
    assert response.status_code == 200, response.text
    return response.json()


def claim_pairing(client, pairing: dict,
                  claim_secret: str | None = None) -> dict:
    """Claim an approved pairing and get the device token."""
    secret = claim_secret if claim_secret is not None else pairing["claim_secret"]
    response = client.post("/pair/claim", json={
        "pairing_id": pairing["pairing_id"],
        "claim_secret": secret,
    })
    return {"status_code": response.status_code, "json": response.json()}


def pair_device(client, device_id: str = SECOND_DEVICE_ID,
                name: str = "Phone B", platform: str = "android",
                app_version: str = "2.0.0") -> str:
    """Run start -> approve -> claim and return the device token."""
    pairing = start_pairing(client, device_id=device_id, name=name,
                             platform=platform, app_version=app_version)
    approve_pairing(client, pairing, name=name)
    claimed = claim_pairing(client, pairing)
    assert claimed["status_code"] == 200, claimed
    return claimed["json"]["token"]
