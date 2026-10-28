import time

import app.db as db

DEVICE_HEADERS = {"Authorization": "Bearer device-token"}
NOW = int(time.time())


def post_sms(client, payload, headers=DEVICE_HEADERS):
    return client.post("/sms", json=payload, headers=headers)


def valid_payload(i=1, **overrides):
    payload = {"id": f"evt-0000-000{i}", "sender": "+15550100", "body": "hi there",
               "received_at": NOW, "thread_id": "t1"}
    payload.update(overrides)
    return payload


def test_unauthorized_returns_401(client):
    payload = valid_payload()
    r = post_sms(client, payload, headers={})
    assert r.status_code == 401
    r = post_sms(client, payload, headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_valid_ingest_pending(client):
    payload = valid_payload(received_at=str(NOW))
    r = post_sms(client, payload)
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "duplicate": False, "state": "pending"}
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"pending": 1}
    msg = db.page_messages(conn, limit=10, offset=0)[0]
    assert msg["id"] == payload["id"]
    assert msg["parts"] == 1
    assert msg["part_index"] == 0
    assert msg["group_id"] == "t1"
    assert len(db.next_jobs(conn, int(time.time()) + 60, limit=10)) == 1


def test_duplicate_id_no_resent_job(client):
    r = post_sms(client, valid_payload())
    assert r.json() == {"accepted": True, "duplicate": False, "state": "pending"}
    r = post_sms(client, valid_payload())
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "duplicate": True, "state": "pending"}
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"pending": 1}
    assert len(db.next_jobs(conn, int(time.time()) + 60, limit=10)) == 1


def test_same_text_from_other_capture_path_is_duplicate(client):
    first = valid_payload(id="evt-broadcast", source="sms")
    r = post_sms(client, first)
    assert r.json()["duplicate"] is False
    second = valid_payload(id="evt-notification", source="notification")
    r = post_sms(client, second)
    assert r.json() == {"accepted": True, "duplicate": True, "state": "pending"}
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"pending": 1}


def test_same_text_repeated_on_the_same_path_is_kept(client):
    r = post_sms(client, valid_payload(id="evt-repeat-1", source="sms"))
    assert r.json()["duplicate"] is False
    r = post_sms(client, valid_payload(id="evt-repeat-2", source="sms"))
    assert r.json() == {"accepted": True, "duplicate": False, "state": "pending"}
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {"pending": 2}


def test_future_timestamp_beyond_1_day_422(client):
    future = NOW + 2 * 86400
    r = post_sms(client, valid_payload(received_at=future))
    assert r.status_code == 422
    r = post_sms(client, valid_payload(received_at="not-a-number"))
    assert r.status_code == 422
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {}


def test_sender_too_long_422(client):
    r = post_sms(client, valid_payload(sender="x" * 65))
    assert r.status_code == 422
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {}


def test_bad_id_422(client):
    r = post_sms(client, valid_payload(id="bad id with spaces!"))
    assert r.status_code == 422


def test_paused_stored_received_no_job(client):
    conn = client.app.state.db_conn
    db.settings_set(conn, "paused", "1")
    r = post_sms(client, valid_payload())
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "duplicate": False, "state": "received",
                        "paused": True}
    assert db.counts_by_state(conn) == {"received": 1}
    assert db.next_jobs(conn, NOW + 10, limit=10) == []


def test_blocklisted_sender_not_stored(client):
    conn = client.app.state.db_conn
    db.filters_add(conn, "block", "9000")
    r = post_sms(client, valid_payload(sender="9000"))
    assert r.status_code == 200
    assert r.json() == {"accepted": False, "reason": "blocked"}
    assert db.counts_by_state(conn) == {}


def test_allowlist_only_listed_senders(client):
    conn = client.app.state.db_conn
    db.filters_add(conn, "allow", "+15550999")
    r = post_sms(client, valid_payload(sender="+15558888"))
    assert r.status_code == 200
    assert r.json() == {"accepted": False, "reason": "blocked"}
    assert db.counts_by_state(conn) == {}
    r = post_sms(client, valid_payload(id="evt-allow-1", sender="+15550999"))
    assert r.json() == {"accepted": True, "duplicate": False, "state": "pending"}
    assert db.counts_by_state(conn) == {"pending": 1}


def test_keyword_body_dropped(client):
    conn = client.app.state.db_conn
    db.filters_add(conn, "keyword", "drop")
    r = post_sms(client, valid_payload(body="pipeline dropFailed lunch"))
    assert r.status_code == 200
    assert r.json() == {"accepted": False, "reason": "blocked"}
    assert db.counts_by_state(conn) == {}


def test_oversized_body_413(client):
    payload = valid_payload(id="evt-too-big", body="x" * 524_289)
    r = post_sms(client, payload)
    assert r.status_code == 413


def test_rate_limit_after_11_bad_tokens(client):
    for _ in range(10):
        r = post_sms(client, valid_payload(), headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
    r = post_sms(client, valid_payload(), headers={"Authorization": "Bearer nope"})
    assert r.status_code == 429


def test_sub_id_is_stored(client):
    r = post_sms(client, valid_payload(sub_id=2))
    assert r.status_code == 200
    conn = client.app.state.db_conn
    msg = db.page_messages(conn, limit=10, offset=0)[0]
    assert msg["sub_id"] == 2
    assert msg["thread_key"] == "15550100"


def test_sub_id_defaults_to_zero(client):
    assert post_sms(client, valid_payload()).status_code == 200
    conn = client.app.state.db_conn
    assert db.page_messages(conn, limit=10, offset=0)[0]["sub_id"] == 0


def test_invalid_sub_id_is_rejected(client):
    for value in (-1, 100, "abc"):
        r = post_sms(client, valid_payload(id=f"evt-sub-{value}", sub_id=value))
        assert r.status_code == 422, value
    conn = client.app.state.db_conn
    assert db.counts_by_state(conn) == {}
