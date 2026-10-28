import time

import app.db as db

DEVICE_HEADERS = {"Authorization": "Bearer device-token"}
NOW = int(time.time())
OLD = NOW - 400 * 86400


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
