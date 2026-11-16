import time

import app.db as db
from tests.device_support import (DEVICE_ID, SECOND_DEVICE_ID, device_headers,
                                  pair_device)
from tests.miniapp_support import headers as admin_headers

DEVICE_HEADERS = device_headers()
ADMIN_HEADERS = admin_headers()
NOW = int(time.time())
OLD = NOW - 400 * 86400
THREAD_KEY = "989121234567"


def post_import(client, items, headers=DEVICE_HEADERS):
    return client.post("/import", json={"items": items}, headers=headers)


def item(i=1, **overrides):
    payload = {
        "provider_id": i,
        "sender": "+989121234567",
        "body": "hello from the archive",
        "received_at": OLD + i,
        "sub_id": 1,
    }
    payload.update(overrides)
    return payload


def test_import_requires_device_token(client):
    r = post_import(client, [item()], headers={})
    assert r.status_code == 401
    r = post_import(client, [item()], headers={"Authorization": "Bearer admin-token"})
    assert r.status_code == 401


def test_import_stores_received_rows_and_never_queues_telegram(client):
    r = post_import(client, [item()])
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "imported": 1,
                        "duplicates": 0, "skipped": 0}
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"received": 1}
    assert db.count_jobs(conn) == 0
    row = db.page_messages(conn, limit=10, offset=0)[0]
    assert row["source"] == "import"
    assert row["state"] == "received"
    assert row["id"].startswith("imp-")
    assert len(row["id"]) == len("imp-") + 32
    assert row["thread_key"]
    assert row["sub_id"] == 1
    assert row["received_at"] == OLD + 1
    assert client.app.state.rev == 1


def test_replaying_the_same_batch_is_idempotent(client):
    first = post_import(client, [item(1), item(2, body="second")])
    assert first.json()["imported"] == 2
    second = post_import(client, [item(1), item(2, body="second")])
    assert second.json() == {"accepted": True, "imported": 0,
                             "duplicates": 2, "skipped": 0}
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"received": 2}
    assert client.app.state.rev == 1


def test_content_without_provider_id_is_still_deterministic(client):
    payload = item(provider_id=None, id=None)
    assert post_import(client, [payload]).json()["imported"] == 1
    assert post_import(client, [payload]).json()["duplicates"] == 1
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"received": 1}


def test_import_dedupes_against_a_live_capture(client):
    live = {"id": "evt-live", "sender": "+989121234567",
            "body": "already captured", "received_at": NOW - 120}
    assert client.post("/sms", json=live, headers=DEVICE_HEADERS).status_code == 200
    r = post_import(client, [item(7, body="already captured", received_at=NOW - 90)])
    assert r.json() == {"accepted": True, "imported": 0,
                        "duplicates": 1, "skipped": 0}
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"pending": 1}
    assert db.count_jobs(conn) == 1


def test_the_imported_history_is_never_forwarded(client):
    """An import is history, not something to deliver to Telegram."""
    assert post_import(client, [item()]).json()["imported"] == 1
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"received": 1}

    resumed = client.post("/admin/resume",
                           headers={"Authorization": "Bearer admin-token"})
    assert resumed.status_code == 200
    assert resumed.json()["resumed"] == 0
    assert db.counts_by_state(conn) == {"received": 1}
    assert db.count_jobs(conn) == 0


def test_import_accepts_old_timestamps_outside_the_live_window(client):
    r = post_import(client, [item(received_at=OLD, provider_id=None, id=None)])
    assert r.status_code == 200
    assert r.json()["imported"] == 1


def test_import_rejects_impossible_timestamps(client):
    ancient = post_import(client, [item(received_at=1000)])
    assert ancient.status_code == 422
    future = post_import(client, [item(received_at=NOW + 2 * 86400)])
    assert future.status_code == 422


def test_import_batch_limit(client):
    too_many = [item(i) for i in range(201)]
    assert post_import(client, too_many).status_code == 422
    empty = post_import(client, [])
    assert empty.status_code == 422


def test_unkeyable_sender_is_skipped(client):
    r = post_import(client, [item(provider_id=11, sender="!!!")])
    assert r.json() == {"accepted": True, "imported": 0,
                        "duplicates": 0, "skipped": 1}
    assert db.counts_by_state(client.app.state.db_conn) == {}
    assert client.app.state.rev == 0


def test_numeric_string_timestamp_and_defaults(client):
    r = post_import(client, [{
        "sender": "09121234567",
        "body": "numeric string date",
        "received_at": str(OLD),
    }])
    assert r.json()["imported"] == 1
    row = db.page_messages(client.app.state.db_conn, limit=10, offset=0)[0]
    assert row["received_at"] == OLD
    assert row["sub_id"] == 0
    assert row["thread_key"] == "989121234567"


# --- the Sent folder: imported outbound history ---------------------------


def out_item(i=1, **overrides):
    payload = item(i, **overrides)
    payload["direction"] = "out"
    return payload


def command_rows(client):
    return client.app.state.db_conn.execute(
        "SELECT * FROM sms_commands ORDER BY created_at, id").fetchall()


def test_direction_defaults_to_inbound():
    from app.importer import ImportItem

    assert ImportItem(sender="09121234567", body="x",
                      received_at=OLD).direction == "in"
    assert ImportItem(sender="09121234567", body="x", received_at=OLD,
                      direction="out").direction == "out"


def test_outbound_import_is_stored_as_a_sent_command(client):
    r = post_import(client, [out_item(body="my reply")])
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "imported": 1,
                        "duplicates": 0, "skipped": 0}

    conn = client.app.state.db_conn
    # It is history, not an inbox message and not something to deliver.
    assert db.counts_by_state(conn) == {}
    assert db.count_jobs(conn) == 0

    rows = command_rows(client)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"].startswith("imp-")
    assert row["target"] == "+989121234567"
    assert row["body"] == "my reply"
    assert row["source"] == "import"
    assert row["status"] == "sent"
    assert row["result"] is None
    assert row["reply_to_telegram_id"] is None
    assert row["thread_key"] == THREAD_KEY
    assert row["sub_id"] == 1
    assert row["client_ref"] is None
    assert row["device_id"] == DEVICE_ID
    assert row["created_at"] == OLD + 1
    assert row["updated_at"] == OLD + 1
    assert row["finished_at"] == OLD + 1
    assert client.app.state.rev == 1


def test_replaying_an_outbound_import_is_a_duplicate(client):
    assert post_import(client, [out_item(body="reply")]).json()["imported"] == 1
    again = post_import(client, [out_item(body="reply")])
    assert again.json() == {"accepted": True, "imported": 0,
                            "duplicates": 1, "skipped": 0}
    assert len(command_rows(client)) == 1
    assert client.app.state.rev == 1


def test_an_imported_command_overlaps_a_relayed_one(client):
    """The same reply may also be in the relay's own command history."""
    first = client.post("/app/api/devices/%s/chats/%s/messages" % (
        DEVICE_ID, THREAD_KEY),
        json={"body": "sent already", "client_ref": "ref-imp-live-01",
              "confirm_unknown": True},
        headers=ADMIN_HEADERS)
    assert first.status_code == 200, first.text
    conn = client.app.state.db_conn
    live = command_rows(client)[0]
    conn.execute("UPDATE sms_commands SET created_at = ? WHERE id = ?",
                 (OLD + 5, live["id"]))

    r = post_import(client, [out_item(body="sent already", received_at=OLD + 30)])
    assert r.json()["imported"] == 0
    assert r.json()["duplicates"] == 1
    assert len(command_rows(client)) == 1


def test_an_imported_outbound_row_is_visible_in_the_miniapp(client):
    post_import(client, [out_item(body="older reply")])

    chats = client.get(f"/app/api/devices/{DEVICE_ID}/chats",
                       headers=ADMIN_HEADERS)
    assert chats.status_code == 200, chats.text
    summary = chats.json()["chats"]
    assert [chat["thread_key"] for chat in summary] == [THREAD_KEY]
    assert summary[0]["last_direction"] == "out"
    assert summary[0]["last_body"] == "older reply"
    assert summary[0]["last_ts"] == OLD + 1

    page = client.get(
        f"/app/api/devices/{DEVICE_ID}/chats/{THREAD_KEY}/messages",
        headers=ADMIN_HEADERS)
    assert page.status_code == 200, page.text
    events = page.json()["events"]
    assert len(events) == 1
    assert events[0]["direction"] == "out"
    assert events[0]["body"] == "older reply"
    assert events[0]["status"] == "sent"
    assert events[0]["ts"] == OLD + 1
    assert events[0]["id"].startswith("cmd:imp-")


def test_two_devices_may_share_a_provider_id(client):
    token_b = pair_device(client, device_id=SECOND_DEVICE_ID, name="Phone B")

    assert post_import(client, [out_item(7, body="reply a")]).json()["imported"] == 1
    assert post_import(client, [out_item(7, body="reply b")],
                       headers=device_headers(token_b)).json()["imported"] == 1
    # The same provider id in the other direction is a third row.
    assert post_import(client, [item(7, body="inbox a")]).json()["imported"] == 1

    rows = command_rows(client)
    assert len(rows) == 2
    assert {row["device_id"] for row in rows} == {DEVICE_ID, SECOND_DEVICE_ID}
    assert len({row["id"] for row in rows}) == 2

    conn = client.app.state.db_conn
    inbound = db.page_messages(conn, limit=10, offset=0)
    assert len(inbound) == 1
    assert inbound[0]["id"] not in {row["id"] for row in rows}

    # Each device sees only its own outbound history.
    a = client.get(f"/app/api/devices/{DEVICE_ID}/chats",
                   headers=ADMIN_HEADERS).json()["chats"]
    b = client.get(f"/app/api/devices/{SECOND_DEVICE_ID}/chats",
                   headers=ADMIN_HEADERS).json()["chats"]
    assert a[0]["last_body"] == "inbox a"
    assert b[0]["last_body"] == "reply b"


def test_an_import_never_creates_a_pending_command(client):
    post_import(client, [out_item(body="old reply"), item(2, body="old inbox")])
    polled = client.get("/commands", headers=DEVICE_HEADERS)
    assert polled.status_code == 200
    assert polled.json()["commands"] == []
