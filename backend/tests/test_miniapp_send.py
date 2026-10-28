"""Phase 4: sending SMS from the Telegram Mini App."""

import time
from urllib.parse import quote

import pytest

import app.db as db
from tests.miniapp_support import headers

DEVICE_HEADERS = {"Authorization": "Bearer device-token"}
ADMIN = headers()
THREAD = "989121234567"  # 09121234567
OTHER_THREAD = "989120000003"  # 09120000003


def _now():
    return int(time.time())


def _upload_contacts(client, contacts):
    response = client.post("/contacts", json={"contacts": contacts},
                           headers=DEVICE_HEADERS)
    assert response.status_code == 200, response.text


def _send(client, thread, body="hello", ref="ref-00000001", sub_id=0,
          confirm_unknown=False):
    return client.post(
        "/app/api/chats/" + quote(thread, safe="") + "/messages",
        json={
            "body": body,
            "client_ref": ref,
            "sub_id": sub_id,
            "confirm_unknown": confirm_unknown,
        },
        headers=ADMIN,
    )


def _rev(client):
    return client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                      headers=ADMIN).json()["rev"]


def _rows(client):
    return client.app.state.db_conn.execute(
        "SELECT * FROM sms_commands ORDER BY created_at, id").fetchall()


def _known(client):
    _upload_contacts(client, [{"number": "09121234567", "name": "Alice"},
                              {"number": "09120000003", "name": "Bob"}])


def test_send_to_contact_queues_a_miniapp_command(client):
    _known(client)
    before = _rev(client)

    response = _send(client, THREAD, body="hello from the app",
                     ref="ref-contact-1", sub_id=1)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "pending"
    assert data["duplicate"] is False
    assert data["client_ref"] == "ref-contact-1"
    assert data["thread_key"] == THREAD
    assert data["target"] == "+" + THREAD
    assert data["display"] == "+" + THREAD
    assert data["name"] == "Alice"
    assert isinstance(data["id"], str) and len(data["id"]) == 32
    assert _rev(client) == before + 1

    stored = db.command_by_client_ref(client.app.state.db_conn, "ref-contact-1")
    assert stored["id"] == data["id"]
    assert stored["source"] == "miniapp"
    assert stored["thread_key"] == THREAD
    assert stored["target"] == "+" + THREAD
    assert stored["sub_id"] == 1
    assert stored["status"] == "pending"
    assert stored["created_at"] == data["ts"]


def test_unknown_number_requires_confirmation_and_contacts_make_it_known(client):
    response = _send(client, OTHER_THREAD, ref="ref-unknown-1")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_number"
    assert detail["thread_key"] == OTHER_THREAD
    assert detail["display"] == "+" + OTHER_THREAD
    assert _rows(client) == []

    confirmed = _send(client, OTHER_THREAD, ref="ref-unknown-2",
                      confirm_unknown=True)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "pending"

    _upload_contacts(client, [{"number": "09121112222", "name": "New"}])
    known = _send(client, "989121112222", ref="ref-unknown-3")
    assert known.status_code == 200
    assert known.json()["name"] == "New"


def test_same_client_ref_is_one_row_and_returns_current_status(client):
    _known(client)
    first = _send(client, THREAD, ref="ref-idem-0001", sub_id=1)
    assert first.status_code == 200
    command_id = first.json()["id"]
    rev_after_insert = _rev(client)

    again = _send(client, THREAD, ref="ref-idem-0001", sub_id=1)
    assert again.status_code == 200
    repeated = again.json()
    assert repeated["duplicate"] is True
    assert repeated["id"] == command_id
    assert repeated["status"] == "pending"
    assert repeated["thread_key"] == THREAD
    assert _rev(client) == rev_after_insert  # duplicates never bump rev
    assert len(_rows(client)) == 1

    conn = client.app.state.db_conn
    accepted, current = db.finish_command(conn, command_id, "sent")
    assert accepted and current == "sent"
    after_sent = _send(client, THREAD, ref="ref-idem-0001", sub_id=1)
    assert after_sent.status_code == 200
    assert after_sent.json()["duplicate"] is True
    assert after_sent.json()["status"] == "sent"
    assert len(_rows(client)) == 1

    conflict = _send(client, OTHER_THREAD, ref="ref-idem-0001")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "client_ref_conflict"
    assert len(_rows(client)) == 1


def test_minute_cap_returns_429_and_counts_only_miniapp_rows(client):
    _known(client)
    for index in range(6):
        response = _send(client, THREAD, body=f"m{index}",
                         ref=f"ref-minute-{index:02d}")
        assert response.status_code == 200, response.text

    blocked = _send(client, THREAD, body="seven", ref="ref-minute-07")
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == {
        "error": "rate_limited", "window": "minute"}
    assert len(_rows(client)) == 6

    # Bot / Telegram rows never consume the Mini App quota: this row is
    # inside the minute window but was not created by the Mini App.
    db.create_command(client.app.state.db_conn, "bot-row", "+15551230000",
                      "bot", "telegram_reply")
    assert len(_rows(client)) == 7


def test_daily_cap_returns_429(client):
    _known(client)
    conn = client.app.state.db_conn
    old = _now() - 3600
    for index in range(50):
        command_id = f"seed-{index:03d}"
        db.create_command(conn, command_id, "+15551230000", "seed", "miniapp")
        conn.execute("UPDATE sms_commands SET created_at = ? WHERE id = ?",
                     (old, command_id))

    blocked = _send(client, THREAD, ref="ref-daily-001")
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == {
        "error": "rate_limited", "window": "day"}
    assert len(_rows(client)) == 50


def test_sub_id_bounds_and_storage(client):
    _known(client)
    too_big = _send(client, THREAD, ref="ref-subid-100", sub_id=100)
    assert too_big.status_code == 422
    assert too_big.json()["detail"]["error"] == "validation_error"

    default = _send(client, THREAD, ref="ref-subid-000", sub_id=0)
    explicit = _send(client, THREAD, ref="ref-subid-001", sub_id=1)
    assert default.status_code == 200 and explicit.status_code == 200
    conn = client.app.state.db_conn
    assert db.command_by_client_ref(conn, "ref-subid-000")["sub_id"] == 0
    assert db.command_by_client_ref(conn, "ref-subid-001")["sub_id"] == 1


def test_destination_normalisation_and_unsendable_keys(client):
    _known(client)
    forms = ["09120000003", "00989120000003", "+989120000003",
             "۰۹۱۲۰۰۰۰۰۰۳"]
    for index, form in enumerate(forms):
        response = _send(client, form, ref=f"ref-form-{index:04d}")
        assert response.status_code == 200, (form, response.text)
        data = response.json()
        assert data["thread_key"] == OTHER_THREAD
        assert data["target"] == "+" + OTHER_THREAD

    shortcode = _send(client, "short:110", ref="ref-short-001",
                      confirm_unknown=True)
    assert shortcode.status_code == 200
    assert shortcode.json()["thread_key"] == "short:110"
    assert shortcode.json()["target"] == "110"

    raw = _send(client, "raw:ad-bank", ref="ref-raw-0001",
                confirm_unknown=True)
    assert raw.status_code == 422
    assert raw.json()["detail"]["error"] == "unsendable_thread"

    for index, form in enumerate(("12", "not-a-number")):
        invalid = _send(client, form, ref=f"ref-bad-{index:04d}",
                        confirm_unknown=True)
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["error"] == "invalid_destination"


def test_paused_relay_still_queues_and_body_limits(client):
    _known(client)
    client.app.state.db_conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('paused', '1')")

    long_body = "x" * 500
    response = _send(client, THREAD, body=long_body, ref="ref-paused-01")
    assert response.status_code == 200
    stored = db.command_by_client_ref(client.app.state.db_conn,
                                      "ref-paused-01")
    assert stored["body"] == long_body
    assert stored["status"] == "pending"
    queued = db.pending_commands(client.app.state.db_conn)
    assert [row["id"] for row in queued] == [stored["id"]]

    maxed = _send(client, THREAD, body="y" * 1600, ref="ref-long-1600")
    assert maxed.status_code == 200
    too_long = _send(client, THREAD, body="y" * 1601, ref="ref-long-1601")
    assert too_long.status_code == 422


@pytest.mark.parametrize("body,ref", [
    ("", "ref-empty-01"),
    ("   \n\t ", "ref-blank-01"),
    ("hello", "short"),
    ("hello", "has space ref"),
    ("hello", "bad/ref/char"),
    ("hello", "x" * 65),
])
def test_invalid_body_and_client_ref_are_422(client, body, ref):
    _known(client)
    response = _send(client, THREAD, body=body, ref=ref)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "validation_error"
    assert _rows(client) == []


def test_send_endpoint_requires_init_data(client):
    response = client.post("/app/api/chats/" + THREAD + "/messages", json={
        "body": "hi", "client_ref": "ref-noauth-1"})
    assert response.status_code == 401
    assert _rows(client) == []
