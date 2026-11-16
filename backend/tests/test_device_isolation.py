"""Strict per-device isolation of the Mini App readers.

A thread belongs to a device, so its identity is the pair
``(device_id, thread_key)``. Every rule of that identity is checked
here: a device never sees the conversations, the history, the contacts,
the unread counters or the read markers of another device, and a command
belongs to exactly one device.
"""

import time

import app.db as db
from tests.device_support import (DEVICE_ID, SECOND_DEVICE_ID, device_headers,
                                  pair_device)
from tests.miniapp_support import headers as admin_headers

ADMIN = admin_headers()
NOW = int(time.time())
THREAD_A = "989121234567"   # 09121234567
THREAD_B = "989120000009"   # 09120000009
SENDER_A = "09121234567"
SENDER_B = "09120000009"


def _second_device(client) -> str:
    """A second phone, paired through the real flow."""
    return pair_device(client, device_id=SECOND_DEVICE_ID, name="Phone B")


def _sms(client, message_id, headers, sender, body="hello", ts=None):
    return client.post("/sms", json={"id": message_id, "sender": sender,
                                     "body": body,
                                     "received_at": NOW if ts is None else ts},
                        headers=headers)


def _chats(client, device_id, **params):
    response = client.get(f"/app/api/devices/{device_id}/chats",
                          params=params, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def _timeline(client, device_id, thread, **params):
    response = client.get(
        f"/app/api/devices/{device_id}/chats/{thread}/messages",
        params=params, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def _contacts(client, device_id, **params):
    response = client.get(f"/app/api/devices/{device_id}/contacts",
                          params=params, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()["contacts"]


def _send(client, device_id, thread, body="hello", ref="ref-00000001",
          confirm_unknown=False):
    return client.post(
        f"/app/api/devices/{device_id}/chats/{thread}/messages",
        json={"body": body, "client_ref": ref,
              "confirm_unknown": confirm_unknown},
        headers=ADMIN,
    )


# --- the inbox of a device ------------------------------------------------


def test_a_device_only_sees_its_own_conversations(client):
    token = _second_device(client)
    _sms(client, "iso-a", device_headers(), SENDER_A, "for a")
    _sms(client, "iso-b", device_headers(token), SENDER_B, "for b")

    chats_a = _chats(client, DEVICE_ID)["chats"]
    assert [chat["thread_key"] for chat in chats_a] == [THREAD_A]

    chats_b = _chats(client, SECOND_DEVICE_ID)["chats"]
    assert [chat["thread_key"] for chat in chats_b] == [THREAD_B]


def test_a_device_only_sees_its_own_timeline(client):
    token = _second_device(client)
    _sms(client, "iso-t-a", device_headers(), SENDER_A, "for a")
    _sms(client, "iso-t-b", device_headers(token), SENDER_B, "for b")

    page = _timeline(client, DEVICE_ID, THREAD_A)
    assert [event["id"] for event in page["events"]] == ["msg:iso-t-a"]
    assert page["events"][0]["body"] == "for a"

    # The thread of the other device is unknown here, not empty.
    missing = client.get(
        f"/app/api/devices/{DEVICE_ID}/chats/{THREAD_B}/messages",
        headers=ADMIN)
    assert missing.status_code == 404


def test_the_unread_counter_is_per_device(client):
    token = _second_device(client)
    _sms(client, "iso-u-a", device_headers(), SENDER_A, "for a")
    _sms(client, "iso-u-b", device_headers(token), SENDER_B, "for b")

    assert _chats(client, DEVICE_ID)["chats"][0]["unread"] == 1
    assert _chats(client, SECOND_DEVICE_ID)["chats"][0]["unread"] == 1


def test_the_read_marker_is_per_device(client):
    token = _second_device(client)
    _sms(client, "iso-r-a", device_headers(), SENDER_A, "for a")
    _sms(client, "iso-r-b", device_headers(token), SENDER_B, "for b")

    read = client.post(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD_A}/read",
                       json={"until_ts": NOW}, headers=ADMIN)
    assert read.status_code == 200
    assert read.json()["last_read_at"] == NOW

    # The marker of the first device says nothing about the second one.
    assert _chats(client, DEVICE_ID)["chats"][0]["unread"] == 0
    assert _chats(client, SECOND_DEVICE_ID)["chats"][0]["unread"] == 1

    # Reading the thread of the other device from the first one is a
    # 404: the thread does not exist for that device.
    other = client.post(
        f"/app/api/devices/{DEVICE_ID}/chats/{THREAD_B}/read",
        json={"until_ts": NOW}, headers=ADMIN)
    assert other.status_code == 404


def test_the_contacts_are_per_device(client):
    token = _second_device(client)
    client.post("/contacts", json={"contacts": [
        {"number": "09121234567", "name": "Alice"}]},
        headers=device_headers())
    client.post("/contacts", json={"contacts": [
        {"number": "09120000009", "name": "Bob"}]},
        headers=device_headers(token))

    assert [row["name"] for row in _contacts(client, DEVICE_ID)] == ["Alice"]
    assert [row["name"] for row in _contacts(client, SECOND_DEVICE_ID)] == [
        "Bob"]


def test_the_search_only_finds_the_own_device(client):
    token = _second_device(client)
    client.post("/contacts", json={"contacts": [
        {"number": "09121234567", "name": "Alice"}]},
        headers=device_headers())
    _sms(client, "iso-s-a", device_headers(), SENDER_A, "the secret word")
    _sms(client, "iso-s-b", device_headers(token), SENDER_B,
         "the secret word")

    found_a = _chats(client, DEVICE_ID, q="secret")
    assert [chat["thread_key"] for chat in found_a["chats"]] == [THREAD_A]

    found_b = _chats(client, SECOND_DEVICE_ID, q="secret")
    assert [chat["thread_key"] for chat in found_b["chats"]] == [THREAD_B]

    # The contact name of one device is not found by the other one.
    assert _chats(client, SECOND_DEVICE_ID, q="Alice")["chats"] == []


def test_the_same_second_cursor_stays_inside_the_device(client):
    token = _second_device(client)
    for index in range(3):
        _sms(client, f"iso-c-a-{index}", device_headers(), SENDER_A,
             f"a {index}", ts=NOW)
        _sms(client, f"iso-c-b-{index}", device_headers(token), SENDER_B,
             f"b {index}", ts=NOW)

    first = _timeline(client, DEVICE_ID, THREAD_A, limit=2)
    assert [event["id"] for event in first["events"]] == [
        "msg:iso-c-a-2", "msg:iso-c-a-1"]
    assert first["has_more"] is True

    cursor = first["events"][-1]
    second = _timeline(client, DEVICE_ID, THREAD_A, limit=2,
                       before_ts=cursor["ts"], before_id=cursor["id"])
    assert [event["id"] for event in second["events"]] == ["msg:iso-c-a-0"]
    assert second["has_more"] is False


# --- commands belong to one device ----------------------------------------


def test_a_command_is_created_for_the_device_of_the_route(client):
    token = _second_device(client)
    _sms(client, "iso-cmd-a", device_headers(), SENDER_A, "for a")

    response = _send(client, DEVICE_ID, THREAD_A, body="answer a",
                     ref="ref-iso-cmd-01")
    assert response.status_code == 200, response.text
    assert response.json()["device_id"] == DEVICE_ID

    stored = db.command_by_client_ref(client.app.state.db_conn,
                                      "ref-iso-cmd-01")
    assert stored["device_id"] == DEVICE_ID
    assert stored["thread_key"] == THREAD_A

    # Only that device polls the command.
    polled = client.get("/commands", headers=device_headers()).json()["commands"]
    assert [command["id"] for command in polled] == [stored["id"]]
    polled_b = client.get("/commands",
                          headers=device_headers(token)).json()["commands"]
    assert polled_b == []

    # And only that device may report the result.
    foreign = client.post(f"/commands/{stored['id']}/result",
                          json={"status": "sent"},
                          headers=device_headers(token))
    assert foreign.status_code == 404
    assert db.command(client.app.state.db_conn,
                      stored["id"])["status"] == "pending"


def test_a_client_ref_of_another_device_is_a_conflict(client):
    token = _second_device(client)
    _sms(client, "iso-ref-a", device_headers(), SENDER_A, "for a")
    _sms(client, "iso-ref-b", device_headers(token), SENDER_B, "for b")

    first = _send(client, DEVICE_ID, THREAD_A, body="one",
                  ref="ref-iso-shared-01")
    assert first.status_code == 200, first.text

    # The same key in another device is a conflict, not a second SMS.
    second = _send(client, SECOND_DEVICE_ID, THREAD_B, body="two",
                   ref="ref-iso-shared-01")
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "client_ref_conflict"

    rows = client.app.state.db_conn.execute(
        "SELECT id, device_id FROM sms_commands").fetchall()
    assert [(row["id"], row["device_id"]) for row in rows] == [
        (first.json()["id"], DEVICE_ID)]


def test_the_same_client_ref_in_the_same_device_is_one_row(client):
    _sms(client, "iso-ref-same", device_headers(), SENDER_A, "for a")
    first = _send(client, DEVICE_ID, THREAD_A, body="one",
                  ref="ref-iso-same-0001")
    assert first.status_code == 200, first.text
    again = _send(client, DEVICE_ID, THREAD_A, body="one",
                  ref="ref-iso-same-0001")
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["id"] == first.json()["id"]


def test_a_number_of_another_device_is_unknown(client):
    token = _second_device(client)
    client.post("/contacts", json={"contacts": [
        {"number": "09120000009", "name": "Bob"}]},
        headers=device_headers(token))

    # The contact of the second device does not make the number known for
    # the first one.
    refused = _send(client, DEVICE_ID, THREAD_B, body="hello",
                    ref="ref-iso-known-01")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "unknown_number"

    confirmed = _send(client, DEVICE_ID, THREAD_B, body="hello",
                      ref="ref-iso-known-02", confirm_unknown=True)
    assert confirmed.status_code == 200


# --- devices that cannot send ---------------------------------------------


def test_a_revoked_device_can_be_read_but_not_send(client):
    _sms(client, "iso-revoked", device_headers(), SENDER_A, "for a")
    client.post(f"/app/api/devices/{DEVICE_ID}/revoke", headers=ADMIN)

    chats = _chats(client, DEVICE_ID)["chats"]
    assert [chat["thread_key"] for chat in chats] == [THREAD_A]

    refused = _send(client, DEVICE_ID, THREAD_A, body="hello",
                    ref="ref-iso-revoked-01")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "device_not_active"


def test_the_archived_device_can_be_read_but_not_send(client):
    conn = client.app.state.db_conn
    db.record_message(conn, {"id": "iso-legacy", "sender": SENDER_A,
                             "body": "old one", "received_at": NOW,
                             "state": "delivered"})
    db.backfill_legacy_device(conn)

    chats = _chats(client, "legacy")["chats"]
    assert [chat["thread_key"] for chat in chats] == [THREAD_A]

    refused = _send(client, "legacy", THREAD_A, body="hello",
                    ref="ref-iso-legacy-01")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "device_not_active"


# --- resume and dedupe ----------------------------------------------------


def test_resume_never_forwards_imported_history(client):
    conn = client.app.state.db_conn
    client.post("/import", json={"items": [
        {"sender": SENDER_A, "body": "from the archive",
         "received_at": NOW - 3600, "provider_id": "p-1"}]},
        headers=device_headers())
    assert db.counts_by_state(conn) == {"received": 1}

    resumed = db.resume_received(conn)
    assert resumed == 0
    assert db.counts_by_state(conn) == {"received": 1}
    assert db.count_jobs(conn) == 0

    # The admin resume does not flush the imported history either.
    response = client.post("/admin/resume",
                           headers={"Authorization": "Bearer admin-token"})
    assert response.status_code == 200
    assert response.json()["resumed"] == 0
    assert db.counts_by_state(conn) == {"received": 1}
    assert db.count_jobs(conn) == 0


def test_resume_moves_the_messages_of_one_device_only(client):
    _second_device(client)
    conn = client.app.state.db_conn
    client.post("/import", json={"items": [
        {"sender": SENDER_A, "body": "old", "received_at": NOW - 3600,
         "provider_id": "p-2"}]},
        headers=device_headers())
    db.record_message(conn, {
        "id": "iso-paused-a", "sender": SENDER_A, "body": "live a",
        "received_at": NOW, "state": "received", "device_id": DEVICE_ID})
    db.record_message(conn, {
        "id": "iso-paused-b", "sender": SENDER_B, "body": "live b",
        "received_at": NOW, "state": "received",
        "device_id": SECOND_DEVICE_ID})

    assert db.resume_received(conn, DEVICE_ID) == 1
    assert db.counts_by_state(conn) == {
        "received": 2, "pending": 1}


def test_a_live_sms_is_not_swallowed_by_imported_history(client):
    # The same text exists in the imported history. The live capture is
    # a new message and has to be stored and forwarded.
    client.post("/import", json={"items": [
        {"sender": SENDER_A, "body": "same text", "received_at": NOW,
         "provider_id": "p-3"}]},
        headers=device_headers())

    response = _sms(client, "iso-live", device_headers(), SENDER_A,
                    "same text")
    assert response.status_code == 200
    assert response.json()["duplicate"] is False

    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"received": 1, "pending": 1}
    assert db.count_jobs(conn) == 1
