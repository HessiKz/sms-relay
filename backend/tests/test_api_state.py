import json
import time

import app.db as db

ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}
DEVICE_HEADERS = {"Authorization": "Bearer device-token"}
NOW = int(time.time())
BASE_TS = 1_700_000_000
MSG_KEYS = {"id", "sender", "body", "parts", "part_index", "group_id",
            "received_at", "state", "telegram_msg_id", "attempts",
            "last_error", "updated_at"}


def _seed(conn, i, **overrides):
    msg = {"id": f"seed-{i}", "sender": "+15550100", "body": "seed body",
           "received_at": BASE_TS + i, "thread_id": f"t{i}", "parts": 1,
           "part_index": 0}
    msg.update(overrides)
    return db.record_message(conn, msg)


def test_messages_requires_admin(client):
    r = client.get("/messages", headers=DEVICE_HEADERS)
    assert r.status_code == 401
    r = client.get("/messages", headers={})
    assert r.status_code == 401
    r = client.get("/messages", headers=ADMIN_HEADERS)
    assert r.status_code == 200


def test_admin_pause_resume_require_admin(client):
    r = client.post("/admin/pause", headers=DEVICE_HEADERS)
    assert r.status_code == 401
    r = client.post("/admin/pause", headers={})
    assert r.status_code == 401
    r = client.post("/admin/resume", headers=DEVICE_HEADERS)
    assert r.status_code == 401
    r = client.post("/admin/resume", headers={})
    assert r.status_code == 401


def test_status_ok_with_any_token(client):
    r = client.get("/status", headers=DEVICE_HEADERS)
    assert r.status_code == 200
    r = client.get("/status", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    r = client.get("/status", headers={})
    assert r.status_code == 401


def test_status_counts_match_seeds(client):
    conn = client.app.state.db_conn
    for i in range(3):
        _seed(conn, i)
    _seed(conn, 10, state="received")
    _seed(conn, 11, state="received")
    _seed(conn, 20, state="delivered")
    _seed(conn, 21, state="failed")
    r = client.get("/status", headers=DEVICE_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["paused"] is False
    assert data["counts_by_state"] == {"pending": 3, "received": 2,
                                       "delivered": 1, "failed": 1}
    assert data["queue"] == 3
    assert data["version"] == "0.1.0"


def test_pause_response_sets_setting(client):
    r = client.post("/admin/pause", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"paused": True}
    conn = client.app.state.db_conn
    assert db.settings_get(conn, "paused") == "1"
    assert client.get("/status", headers=ADMIN_HEADERS).json()["paused"] is True


def test_resume_without_received_returns_zero(client):
    r = client.post("/admin/resume", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"paused": False, "resumed": 0}


def test_messages_pagination_next_offset(client):
    conn = client.app.state.db_conn
    for i in range(25):
        _seed(conn, i)
    r = client.get("/messages?limit=10", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 10
    assert data["next_offset"] == 10
    assert [m["id"] for m in data["items"]] == [f"seed-{n}" for n in range(24, 14, -1)]
    item = data["items"][0]
    assert item["sender"] == "+15550100"
    assert item["body"] == "seed body"
    assert item["parts"] == 1
    assert item["part_index"] == 0
    assert item["group_id"] == "t24"
    assert item["state"] == "pending"
    assert MSG_KEYS <= set(item)

    r = client.get("/messages?limit=10&offset=10", headers=ADMIN_HEADERS)
    assert r.json()["next_offset"] == 20
    assert len(r.json()["items"]) == 10

    r = client.get("/messages?limit=10&offset=20", headers=ADMIN_HEADERS)
    data = r.json()
    assert len(data["items"]) == 5
    assert data["next_offset"] is None

    r = client.get("/messages?limit=10&offset=30", headers=ADMIN_HEADERS)
    data = r.json()
    assert data["items"] == []
    assert data["next_offset"] is None


def test_messages_default_limit_50(client):
    conn = client.app.state.db_conn
    for i in range(25):
        _seed(conn, i)
    data = client.get("/messages", headers=ADMIN_HEADERS).json()
    assert len(data["items"]) == 25
    assert data["next_offset"] is None


def test_messages_state_filter(client):
    conn = client.app.state.db_conn
    _seed(conn, 1)
    _seed(conn, 2, state="received")
    data = client.get("/messages?state=received", headers=ADMIN_HEADERS).json()
    assert [m["id"] for m in data["items"]] == ["seed-2"]
    assert data["next_offset"] is None
    data = client.get("/messages?state=delivered", headers=ADMIN_HEADERS).json()
    assert data["items"] == []
    assert data["next_offset"] is None


def test_messages_invalid_params_422(client):
    for qs in ("limit=201", "limit=0", "limit=-5", "limit=abc", "offset=-1",
               "offset=abc", "state=nope"):
        r = client.get(f"/messages?{qs}", headers=ADMIN_HEADERS)
        assert r.status_code == 422, qs


def test_pause_blocks_enqueue(client):
    conn = client.app.state.db_conn
    r = client.post("/admin/pause", headers=ADMIN_HEADERS)
    assert r.json() == {"paused": True}
    payload = {"id": "evt-1", "sender": "+15550100", "body": "queued later",
               "received_at": NOW, "thread_id": "t1"}
    r = client.post("/sms", json=payload, headers=DEVICE_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "duplicate": False, "state": "received",
                        "paused": True}
    assert db.counts_by_state(conn) == {"received": 1}
    assert db.next_jobs(conn, NOW + 60, limit=10) == []


def test_resume_flips_received_to_pending_with_jobs(client):
    conn = client.app.state.db_conn
    client.post("/admin/pause", headers=ADMIN_HEADERS)
    for i in (1, 2):
        payload = {"id": f"evt-{i}", "sender": "+15550100",
                   "body": f"queued later {i}", "received_at": NOW,
                   "thread_id": "t1"}
        assert client.post("/sms", json=payload, headers=DEVICE_HEADERS).json()[
            "state"] == "received"
    r = client.post("/admin/resume", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"paused": False, "resumed": 2}
    assert db.counts_by_state(conn) == {"pending": 2}
    jobs = db.next_jobs(conn, NOW + 60, limit=10)
    assert {j["msg_id"] for j in jobs} == {"evt-1", "evt-2"}
    assert client.get("/status", headers=ADMIN_HEADERS).json()["paused"] is False


def test_resume_only_touches_received_rows(client):
    conn = client.app.state.db_conn
    client.post("/admin/pause", headers=ADMIN_HEADERS)
    assert client.post("/sms", json={"id": "evt-1", "sender": "+15550100",
                                     "body": "paused", "received_at": NOW,
                                     "thread_id": "t1"},
                       headers=DEVICE_HEADERS).json()["state"] == "received"
    client.post("/admin/resume", headers=ADMIN_HEADERS)
    payload = {"id": "evt-2", "sender": "+15550100", "body": "live",
               "received_at": NOW, "thread_id": "t1"}
    assert client.post("/sms", json=payload, headers=DEVICE_HEADERS).json()[
        "state"] == "pending"
    r = client.post("/admin/resume", headers=ADMIN_HEADERS)
    assert r.json() == {"paused": False, "resumed": 0}
    assert db.counts_by_state(conn) == {"pending": 2}
    assert len(db.next_jobs(conn, NOW + 60, limit=10)) == 2


def test_admin_auth_failures_rate_limited(client):
    for _ in range(10):
        r = client.get("/messages", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
    r = client.get("/messages", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 429


# --- Phase 1: phone heartbeat -------------------------------------------

PHONE_KEYS = {"last_seen", "online", "app_version", "sending", "sims"}


def _poll(client, info=None, raw=None):
    headers = dict(DEVICE_HEADERS)
    if raw is not None:
        headers["X-Device-Info"] = raw
    elif info is not None:
        headers["X-Device-Info"] = json.dumps(info)
    return client.get("/commands", headers=headers)


def test_status_phone_shape_without_heartbeat(client):
    data = client.get("/status", headers=DEVICE_HEADERS).json()
    assert {"paused", "counts_by_state", "queue", "version"} <= set(data)
    assert data["phone"] == {"last_seen": None, "online": False,
                             "app_version": None, "sending": None,
                             "sims": []}
    assert set(data["phone"]) == PHONE_KEYS


def test_commands_heartbeat_updates_phone_status(client):
    info = {"id": "phone-a", "version": "1.2.0", "sending": True,
            "sims": [{"sub_id": 1, "slot": 0, "label": "Personal"},
                     {"sub_id": 2, "slot": 1, "label": "Work"}]}
    before = int(time.time())
    assert _poll(client, info).status_code == 200
    phone = client.get("/status", headers=DEVICE_HEADERS).json()["phone"]
    assert phone["online"] is True
    assert phone["app_version"] == "1.2.0"
    assert phone["sending"] is True
    assert phone["sims"] == info["sims"]
    assert before - 1 <= phone["last_seen"] <= int(time.time()) + 1


def test_malformed_device_info_never_rejects_poll(client):
    for raw in ("{not json", json.dumps([1, 2]), "x" * 3000,
                json.dumps({"id": "dev", "version": 123, "sending": "yes"})):
        r = _poll(client, raw=raw)
        assert r.status_code == 200, raw
        assert r.json()["commands"] == []
    phone = client.get("/status", headers=DEVICE_HEADERS).json()["phone"]
    assert phone["last_seen"] is not None
    assert phone["online"] is True
    # the numeric version and the non-bool sending were ignored
    assert phone["app_version"] is None
    assert phone["sending"] is None
    # a missing id falls back to phone-a; a valid id is kept
    rows = client.app.state.db_conn.execute(
        "SELECT device_id FROM device_status").fetchall()
    assert sorted(row["device_id"] for row in rows) == ["dev", "phone-a"]


def test_device_info_keeps_only_valid_fields_and_sims(client):
    raw = json.dumps({
        "id": "dev-1",
        "version": 42,
        "sending": True,
        "sims": [
            {"sub_id": 1, "slot": 0, "label": "A"},
            {"sub_id": "nope", "slot": 1},
            {"sub_id": 2, "slot": 1, "label": "B", "extra": "ignored"},
            {"sub_id": 3, "slot": 99},
        ],
    })
    assert _poll(client, raw=raw).status_code == 200
    phone = client.get("/status", headers=DEVICE_HEADERS).json()["phone"]
    assert phone["app_version"] is None
    assert phone["sending"] is True
    assert phone["sims"] == [{"sub_id": 1, "slot": 0, "label": "A"},
                             {"sub_id": 2, "slot": 1, "label": "B"}]


def test_oversized_device_info_is_ignored_but_last_seen_updates(client):
    info = {"id": "phone-a", "version": "1.2.0", "sending": True, "sims": []}
    assert _poll(client, info).status_code == 200
    conn = client.app.state.db_conn
    conn.execute("UPDATE device_status SET last_seen_at = 1")

    oversized = json.dumps({"id": "phone-a", "version": "9.9.9",
                            "pad": "y" * 3000})
    assert _poll(client, raw=oversized).status_code == 200
    phone = client.get("/status", headers=DEVICE_HEADERS).json()["phone"]
    assert phone["last_seen"] > 1
    assert phone["app_version"] == "1.2.0"
    assert phone["sims"] == []
