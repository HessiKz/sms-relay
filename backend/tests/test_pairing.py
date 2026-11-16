"""Device pairing: the phone asks, the admin approves, the phone claims."""

import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.db as db
from app.main import create_app
from tests.device_support import (DEVICE_ID, approve_pairing, claim_pairing,
                                  device_headers, start_pairing)
from tests.miniapp_support import headers as admin_headers

SECRET = "s" * 40
OTHER_SECRET = "x" * 40


def _body(**overrides):
    body = {"device_id": "new-phone", "name": "New Phone",
            "platform": "android", "app_version": "1.4.0",
            "claim_secret": SECRET}
    body.update(overrides)
    return body


def _start(client, **kwargs):
    kwargs.setdefault("claim_secret", SECRET)
    return start_pairing(client, **kwargs)


def _client(settings, **overrides):
    app = create_app(replace(settings, **overrides), run_workers=False)
    with TestClient(app) as test_client:
        yield test_client


def _expire(client, pairing_id):
    conn = client.app.state.db_conn
    conn.execute("UPDATE pairings SET expires_at = ? WHERE id = ?",
                 (int(time.time()) - 1, pairing_id))


# --- the phone asks -------------------------------------------------------


def test_pair_start_returns_a_code_and_a_qr_payload(client):
    response = client.post("/pair/start", json=_body())
    assert response.status_code == 200
    data = response.json()
    assert data["pairing_id"]
    assert len(data["scan_code"]) == 6
    assert data["expires_at"] > int(time.time())
    assert data["qr_payload"] == f"pair_{data['pairing_id']}_{data['scan_code']}"


def test_pair_start_stores_only_hashes(client):
    data = client.post("/pair/start", json=_body()).json()
    conn = client.app.state.db_conn
    row = conn.execute("SELECT * FROM pairings WHERE id = ?",
                       (data["pairing_id"],)).fetchone()
    assert row["scan_code_hash"] != data["scan_code"]
    assert row["claim_secret_hash"] != SECRET
    assert row["scan_code_hash"] == db.token_hash(data["scan_code"])
    assert row["claim_secret_hash"] == db.token_hash(SECRET)
    assert row["status"] == "pending"
    assert row["device_id"] == "new-phone"
    assert row["device_name"] == "New Phone"
    assert row["platform"] == "android"
    assert row["app_version"] == "1.4.0"


def test_pair_start_needs_no_token(client):
    # The phone has no token yet: the endpoint is public.
    assert client.post("/pair/start", json=_body()).status_code == 200


@pytest.mark.parametrize("overrides", [
    {"device_id": ""},
    {"device_id": "a" * 65},
    {"device_id": "bad id!"},
    {"name": ""},
    {"name": "a" * 65},
    {"platform": ""},
    {"app_version": "a" * 33},
    {"claim_secret": "too short"},
    {"claim_secret": "x" * 129},
])
def test_pair_start_validates_the_input(client, overrides):
    response = client.post("/pair/start", json=_body(**overrides))
    assert response.status_code == 422, overrides


def test_pair_start_rate_limits_one_address(client):
    for _ in range(10):
        assert client.post("/pair/start", json=_body()).status_code == 200
    assert client.post("/pair/start", json=_body()).status_code == 429


def test_pair_start_caps_the_pending_pairings(client, monkeypatch):
    import app.pairing as pairing
    monkeypatch.setattr(pairing, "MAX_PENDING_PAIRINGS", 2)
    assert client.post("/pair/start",
                       json=_body(device_id="cap-phone-1")).status_code == 200
    assert client.post("/pair/start",
                       json=_body(device_id="cap-phone-2")).status_code == 200
    assert client.post("/pair/start",
                       json=_body(device_id="cap-phone-3")).status_code == 429


def test_qr_payload_uses_the_bot_username(settings):
    for client in _client(settings, miniapp_bot_username="my_bot"):
        data = client.post("/pair/start", json=_body()).json()
        assert data["qr_payload"] == (
            f"https://t.me/my_bot?startapp=pair_{data['pairing_id']}"
            f"_{data['scan_code']}")


def test_qr_payload_uses_the_miniapp_url(settings):
    for client in _client(settings, miniapp_url="https://example.org/app/"):
        data = client.post("/pair/start", json=_body()).json()
        assert data["qr_payload"] == (
            f"https://example.org/app/?pair={data['pairing_id']}"
            f"_{data['scan_code']}")


# --- the admin approves ---------------------------------------------------


def test_pair_preview_shows_the_device(client):
    pairing = _start(client, device_id="new-phone", name="New Phone")
    response = client.post("/app/api/pair/preview",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": pairing["scan_code"]},
                           headers=admin_headers())
    assert response.status_code == 200
    data = response.json()
    assert data["device_id"] == "new-phone"
    assert data["name"] == "New Phone"
    assert data["platform"] == "android"
    assert data["app_version"] == "2.0.0"
    assert data["expires_at"] == pairing["expires_at"]
    # the preview does not approve anything
    assert db.pairing_by_id(client.app.state.db_conn,
                            pairing["pairing_id"])["status"] == "pending"


def test_pair_preview_needs_the_admin(client):
    pairing = _start(client, device_id="new-phone")
    response = client.post("/app/api/pair/preview",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": pairing["scan_code"]})
    assert response.status_code == 401


def test_pair_preview_with_a_wrong_code_is_404(client):
    pairing = _start(client, device_id="new-phone")
    response = client.post("/app/api/pair/preview",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": "ZZZZZZ"},
                           headers=admin_headers())
    assert response.status_code == 404


def test_pair_approve_creates_the_device_without_a_token(client):
    pairing = _start(client, device_id="new-phone", name="New Phone")
    response = client.post("/app/api/pair/approve",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": pairing["scan_code"]},
                           headers=admin_headers())
    assert response.status_code == 200
    device = response.json()["device"]
    assert device["id"] == "new-phone"
    assert device["name"] == "New Phone"
    conn = client.app.state.db_conn
    row = db.device_by_id(conn, "new-phone")
    assert row["token_hash"] is None
    assert row["paired_at"] is None
    assert row["revoked_at"] is None
    assert db.pairing_by_id(conn, pairing["pairing_id"])["status"] == "approved"
    assert db.pairing_by_id(conn, pairing["pairing_id"])["approved_by"] == 7


def test_pair_approve_can_rename_the_device(client):
    pairing = _start(client, device_id="new-phone", name="New Phone")
    client.post("/app/api/pair/approve",
                json={"pairing_id": pairing["pairing_id"],
                      "scan_code": pairing["scan_code"], "name": "Kitchen phone"},
                headers=admin_headers())
    assert db.device_by_id(client.app.state.db_conn,
                           "new-phone")["name"] == "Kitchen phone"


def test_pair_approve_rejects_an_active_duplicate(client):
    pairing = _start(client, device_id=DEVICE_ID, name="Phone A")
    response = client.post("/app/api/pair/approve",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": pairing["scan_code"]},
                           headers=admin_headers())
    assert response.status_code == 409


def test_a_new_code_supersedes_the_previous_code(client):
    first = _start(client, device_id="new-phone")
    second = _start(client, device_id="new-phone")
    conn = client.app.state.db_conn
    assert db.pairing_by_id(conn, first["pairing_id"])["status"] == "cancelled"
    assert db.pairing_by_id(conn, second["pairing_id"])["status"] == "pending"

    # The old QR must not be able to approve the device after the phone
    # has generated a newer code.
    old = client.post("/app/api/pair/approve",
                      json={"pairing_id": first["pairing_id"],
                            "scan_code": first["scan_code"]},
                      headers=admin_headers())
    assert old.status_code == 410

    new = client.post("/app/api/pair/approve",
                      json={"pairing_id": second["pairing_id"],
                            "scan_code": second["scan_code"]},
                      headers=admin_headers())
    assert new.status_code == 200


def test_pair_approve_of_an_unclaimed_device_can_be_retried(client):
    first = _start(client, device_id="new-phone")
    approve_pairing(client, first)
    conn = client.app.state.db_conn
    assert db.device_by_id(conn, "new-phone")["token_hash"] is None

    # The approval expired before the phone claimed it. A fresh code and
    # approval must finish the pairing instead of reporting "already
    # paired" on the unclaimed row.
    second = _start(client, device_id="new-phone")
    approve_pairing(client, second)
    claimed = claim_pairing(client, second)
    assert claimed["status_code"] == 200
    assert claimed["json"]["token"]
    assert db.device_by_id(conn, "new-phone")["token_hash"] is not None


def test_pair_approve_reactivates_a_revoked_device(client):
    conn = client.app.state.db_conn
    db.revoke_device(conn, DEVICE_ID, int(time.time()))
    pairing = _start(client, device_id=DEVICE_ID, name="Phone A")
    response = client.post("/app/api/pair/approve",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": pairing["scan_code"]},
                           headers=admin_headers())
    assert response.status_code == 200
    row = db.device_by_id(conn, DEVICE_ID)
    assert row["revoked_at"] is None
    assert row["token_hash"] is None


def test_pair_approve_of_a_revoked_device_kills_the_old_token(client):
    conn = client.app.state.db_conn
    db.revoke_device(conn, DEVICE_ID, int(time.time()))
    pairing = _start(client, device_id=DEVICE_ID, name="Phone A")
    client.post("/app/api/pair/approve",
                json={"pairing_id": pairing["pairing_id"],
                      "scan_code": pairing["scan_code"]},
                headers=admin_headers())
    assert client.get("/status",
                      headers=device_headers()).status_code == 401


# --- the phone claims -----------------------------------------------------


def test_pair_claim_before_the_approval_is_pending(client):
    pairing = _start(client, device_id="new-phone")
    response = client.post("/pair/claim",
                           json={"pairing_id": pairing["pairing_id"],
                                 "claim_secret": SECRET})
    assert response.status_code == 202
    assert response.json() == {"status": "pending",
                               "expires_at": pairing["expires_at"]}


def test_pair_claim_rate_limits_one_address(client):
    """A valid poll is never rate limited.

    Behind the reverse proxy every phone shares one address and polls
    every two seconds, so a blanket limit would block the phones.
    """
    pairing = _start(client, device_id="new-phone")
    body = {"pairing_id": pairing["pairing_id"], "claim_secret": SECRET}
    for _ in range(65):
        response = client.post("/pair/claim", json=body)
        assert response.status_code == 202, response.text


def test_two_phones_behind_one_proxy_can_poll_at_the_same_time(client):
    """The reported case: two phones, one shared proxy address.

    Every phone polls every two seconds, so two phones make 60 requests
    a minute from the same address. They must both keep working.
    """
    first = _start(client, device_id="phone-1")
    second = _start(client, device_id="phone-2")
    for _ in range(35):
        for pairing in (first, second):
            response = client.post("/pair/claim", json={
                "pairing_id": pairing["pairing_id"],
                "claim_secret": SECRET})
            assert response.status_code == 202, response.text


def test_pair_claim_rate_limits_wrong_secrets(client):
    """The claim secret is the only thing that is rate limited."""
    pairing = _start(client, device_id="new-phone")
    wrong = {"pairing_id": pairing["pairing_id"],
             "claim_secret": OTHER_SECRET}
    for _ in range(10):
        response = client.post("/pair/claim", json=wrong)
        assert response.status_code == 404, response.text
    blocked = client.post("/pair/claim", json=wrong)
    assert blocked.status_code == 429


def test_a_correct_claim_still_works_after_wrong_attempts(client):
    """A photographed QR code cannot use up the quota of the phone."""
    pairing = _start(client, device_id="new-phone")
    wrong = {"pairing_id": pairing["pairing_id"],
             "claim_secret": OTHER_SECRET}
    for _ in range(12):
        client.post("/pair/claim", json=wrong)

    # The phone knows the secret, so it is not blocked by the failures
    # of somebody else.
    pending = client.post("/pair/claim", json={
        "pairing_id": pairing["pairing_id"], "claim_secret": SECRET})
    assert pending.status_code == 202

    approve_pairing(client, pairing)
    claimed = claim_pairing(client, pairing)
    assert claimed["status_code"] == 200, claimed
    assert claimed["json"]["token"].startswith("sr_")


def test_pair_claim_rate_limits_unknown_pairings_per_address(client):
    """Guessing pairing ids is bounded per address."""
    for _ in range(60):
        response = client.post("/pair/claim", json={
            "pairing_id": "nope", "claim_secret": SECRET})
        assert response.status_code == 404, response.text
    blocked = client.post("/pair/claim", json={
        "pairing_id": "nope", "claim_secret": SECRET})
    assert blocked.status_code == 429


def test_pair_claim_returns_the_token_once(client):
    pairing = _start(client, device_id="new-phone", name="New Phone")
    approve_pairing(client, pairing)
    claimed = claim_pairing(client, pairing)
    assert claimed["status_code"] == 200
    data = claimed["json"]
    assert data["status"] == "claimed"
    assert data["token"].startswith("sr_")
    assert data["device"]["id"] == "new-phone"

    conn = client.app.state.db_conn
    row = db.device_by_id(conn, "new-phone")
    assert row["token_hash"] == db.token_hash(data["token"])
    assert row["token_hash"] != data["token"]
    assert row["paired_at"] is not None

    # the token works
    assert client.get("/status",
                      headers={"Authorization": "Bearer " + data["token"]}
                      ).status_code == 200


def test_pair_claim_cannot_be_replayed(client):
    pairing = _start(client, device_id="new-phone")
    approve_pairing(client, pairing)
    claim_pairing(client, pairing)
    again = claim_pairing(client, pairing)
    assert again["status_code"] == 409


def test_pair_claim_with_a_wrong_secret_is_404(client):
    pairing = _start(client, device_id="new-phone")
    approve_pairing(client, pairing)
    response = client.post("/pair/claim",
                           json={"pairing_id": pairing["pairing_id"],
                                 "claim_secret": OTHER_SECRET})
    assert response.status_code == 404
    assert db.device_by_id(client.app.state.db_conn,
                           "new-phone")["token_hash"] is None


def test_pair_claim_of_an_unknown_pairing_is_404(client):
    response = client.post("/pair/claim",
                           json={"pairing_id": "nope", "claim_secret": SECRET})
    assert response.status_code == 404


def test_pair_claim_after_the_expiry_is_410(client):
    pairing = _start(client, device_id="new-phone")
    approve_pairing(client, pairing)
    _expire(client, pairing["pairing_id"])
    response = client.post("/pair/claim",
                           json={"pairing_id": pairing["pairing_id"],
                                 "claim_secret": SECRET})
    assert response.status_code == 410


def test_pair_claim_of_an_expired_pairing_is_410(client):
    pairing = _start(client, device_id="new-phone")
    _expire(client, pairing["pairing_id"])
    response = client.post("/pair/claim",
                           json={"pairing_id": pairing["pairing_id"],
                                 "claim_secret": SECRET})
    assert response.status_code == 410


def test_pair_approve_after_the_expiry_is_410(client):
    pairing = _start(client, device_id="new-phone")
    _expire(client, pairing["pairing_id"])
    response = client.post("/app/api/pair/approve",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": pairing["scan_code"]},
                           headers=admin_headers())
    assert response.status_code == 410


def test_pair_start_cleans_up_expired_pairings(client):
    pairing = _start(client, device_id="new-phone")
    _expire(client, pairing["pairing_id"])
    client.post("/pair/start", json=_body())
    conn = client.app.state.db_conn
    assert db.pairing_by_id(conn, pairing["pairing_id"])["status"] == "expired"


def test_the_cleanup_removes_a_device_that_was_never_claimed(client):
    pairing = _start(client, device_id="new-phone")
    approve_pairing(client, pairing)
    _expire(client, pairing["pairing_id"])
    client.post("/pair/start", json=_body())
    conn = client.app.state.db_conn
    assert db.device_by_id(conn, "new-phone") is None


def test_the_archived_device_cannot_be_paired(client):
    conn = client.app.state.db_conn
    db.record_message(conn, {"id": "old", "sender": "+15550100",
                             "body": "old", "received_at": int(time.time())})
    db.backfill_legacy_device(conn)
    pairing = _start(client, device_id="legacy")
    response = client.post("/app/api/pair/approve",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": pairing["scan_code"]},
                           headers=admin_headers())
    assert response.status_code == 409
    assert db.device_by_id(conn, "legacy")["archived"] == 1


def test_pair_cancel_marks_the_pairing_cancelled(client):
    pairing = _start(client, device_id="new-phone")
    response = client.post("/app/api/pair/cancel",
                           json={"pairing_id": pairing["pairing_id"],
                                 "scan_code": pairing["scan_code"]},
                           headers=admin_headers())
    assert response.status_code == 200
    assert db.pairing_by_id(client.app.state.db_conn,
                            pairing["pairing_id"])["status"] == "cancelled"


# --- the old shared token is gone ----------------------------------------


def test_the_old_shared_phone_token_is_rejected(client):
    response = client.get("/commands",
                          headers={"Authorization": "Bearer device-token"})
    assert response.status_code == 401


def test_the_old_shared_phone_token_cannot_upload(client):
    response = client.post("/sms",
                           json={"id": "evt-1", "sender": "+15550100",
                                 "body": "hi", "received_at": int(time.time())},
                           headers={"Authorization": "Bearer device-token"})
    assert response.status_code == 401


def test_a_revoked_device_cannot_poll(client):
    conn = client.app.state.db_conn
    db.revoke_device(conn, DEVICE_ID, int(time.time()))
    assert client.get("/commands", headers=device_headers()).status_code == 401
