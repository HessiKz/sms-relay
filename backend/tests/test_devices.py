"""Per-device behaviour: isolation, command ownership and the migration.

Every rule of the multi-device split is checked here: a device only ever
sees its own data, a command belongs to exactly one device, and the rows
of the previous phone survive the migration under an archived device.
"""

import sqlite3
import time

import app.db as db
from tests.device_support import (DEVICE_ID, DEVICE_TOKEN, SECOND_DEVICE_ID,
                                  device_headers, pair_device, seed_device)
from tests.miniapp_support import headers as admin_headers

ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}
NOW = int(time.time())


def _second_device(client) -> str:
    """A second phone, paired through the real flow."""
    return pair_device(client, device_id=SECOND_DEVICE_ID, name="Phone B")


def _sms(client, message_id, headers, sender="+15550100", body="hello"):
    return client.post("/sms", json={"id": message_id, "sender": sender,
                                     "body": body, "received_at": NOW},
                       headers=headers)


# --- uploads belong to the device of the token ----------------------------


def test_sms_is_stored_for_the_authenticated_device(client):
    token = _second_device(client)
    assert _sms(client, "msg-b", device_headers(token)).status_code == 200
    conn = client.app.state.db_conn
    row = conn.execute("SELECT device_id FROM messages WHERE id = 'msg-b'").fetchone()
    assert row["device_id"] == SECOND_DEVICE_ID


def test_sms_ignores_a_device_id_in_the_body(client):
    response = client.post("/sms",
                           json={"id": "msg-x", "sender": "+15550100",
                                 "body": "hello", "received_at": NOW,
                                 "device_id": "someone-else"},
                           headers=device_headers())
    assert response.status_code == 200
    conn = client.app.state.db_conn
    row = conn.execute(
        "SELECT device_id FROM messages WHERE id = 'msg-x'").fetchone()
    assert row["device_id"] == DEVICE_ID


def test_import_is_stored_for_the_authenticated_device(client):
    token = _second_device(client)
    response = client.post("/import", json={"items": [
        {"sender": "+15550100", "body": "old one", "received_at": NOW,
         "provider_id": "p-1"}]}, headers=device_headers(token))
    assert response.status_code == 200
    conn = client.app.state.db_conn
    row = conn.execute(
        "SELECT device_id FROM messages WHERE source = 'import'").fetchone()
    assert row["device_id"] == SECOND_DEVICE_ID


def test_contacts_are_stored_per_device(client):
    token = _second_device(client)
    client.post("/contacts", json={"contacts": [
        {"number": "+15551230000", "name": "Alice"}]},
        headers=device_headers())
    client.post("/contacts", json={"contacts": [
        {"number": "+15559990000", "name": "Bob"}]},
        headers=device_headers(token))
    conn = client.app.state.db_conn
    rows = db.list_device_contacts(conn)
    assert {(row["device_id"], row["name"]) for row in rows} == {
        (DEVICE_ID, "Alice"), (SECOND_DEVICE_ID, "Bob")}


def test_contacts_of_one_device_are_not_replaced_by_another(client):
    token = _second_device(client)
    client.post("/contacts", json={"contacts": [
        {"number": "+15551230000", "name": "Alice"}]},
        headers=device_headers(token))
    client.post("/contacts", json={"contacts": [
        {"number": "+15559990000", "name": "Bob"}]},
        headers=device_headers())
    conn = client.app.state.db_conn
    names = {row["name"] for row in db.list_device_contacts(conn)}
    assert names == {"Alice", "Bob"}


def test_the_old_global_contact_table_is_not_touched(client):
    client.post("/contacts", json={"contacts": [
        {"number": "+15551230000", "name": "Alice"}]},
        headers=device_headers())
    conn = client.app.state.db_conn
    assert db.contact_count(conn) == 0
    assert db.list_contacts(conn) == []


# --- a device only sees its own data --------------------------------------


def test_sms_state_is_scoped_to_the_device(client):
    token = _second_device(client)
    _sms(client, "msg-a", device_headers())
    _sms(client, "msg-b", device_headers(token))
    conn = client.app.state.db_conn
    db.update_state(conn, "msg-b", "failed", 1, NOW)

    states = client.get("/sms-state?ids=msg-a,msg-b",
                        headers=device_headers()).json()["states"]
    assert set(states) == {"msg-a"}
    assert states["msg-a"]["state"] == "pending"

    states_b = client.get("/sms-state?ids=msg-a,msg-b",
                          headers=device_headers(token)).json()["states"]
    assert set(states_b) == {"msg-b"}
    assert states_b["msg-b"]["state"] == "failed"


def test_sms_state_of_the_admin_sees_every_device(client):
    token = _second_device(client)
    _sms(client, "msg-a", device_headers())
    _sms(client, "msg-b", device_headers(token))
    states = client.get("/sms-state?ids=msg-a,msg-b",
                        headers=ADMIN_HEADERS).json()["states"]
    assert set(states) == {"msg-a", "msg-b"}


def test_messages_are_admin_only_and_filterable_by_device(client):
    token = _second_device(client)
    _sms(client, "msg-a", device_headers())
    _sms(client, "msg-b", device_headers(token))

    assert client.get("/messages", headers=device_headers()).status_code == 401

    items = client.get("/messages", headers=ADMIN_HEADERS).json()["items"]
    assert {item["id"] for item in items} == {"msg-a", "msg-b"}
    assert {item["device_id"] for item in items} == {DEVICE_ID, SECOND_DEVICE_ID}
    assert all(item["device_name"] for item in items)

    only_a = client.get(f"/messages?device_id={DEVICE_ID}",
                         headers=ADMIN_HEADERS).json()["items"]
    assert [item["id"] for item in only_a] == ["msg-a"]


def test_status_shows_only_its_own_device_to_a_device(client):
    token = _second_device(client)
    data = client.get("/status", headers=device_headers()).json()
    assert [row["id"] for row in data["devices"]] == [DEVICE_ID]
    assert data["phone"]["online"] is True

    data_b = client.get("/status", headers=device_headers(token)).json()
    assert [row["id"] for row in data_b["devices"]] == [SECOND_DEVICE_ID]

    admin = client.get("/status", headers=ADMIN_HEADERS).json()
    assert {row["id"] for row in admin["devices"]} == {DEVICE_ID, SECOND_DEVICE_ID}


def test_status_keeps_the_global_counters(client):
    token = _second_device(client)
    _sms(client, "msg-a", device_headers())
    _sms(client, "msg-b", device_headers(token))
    data = client.get("/status", headers=device_headers(token)).json()
    assert data["counts_by_state"] == {"pending": 2}
    assert data["queue"] == 2
    assert data["paused"] is False


# --- commands belong to one device ----------------------------------------


def test_a_device_only_polls_its_own_commands(client):
    token = _second_device(client)
    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-a", "+15551230000", "for a", "miniapp",
                      device_id=DEVICE_ID)
    db.create_command(conn, "cmd-b", "+15551230000", "for b", "miniapp",
                      device_id=SECOND_DEVICE_ID)

    polled = client.get("/commands", headers=device_headers()).json()["commands"]
    assert [command["id"] for command in polled] == ["cmd-a"]

    polled_b = client.get("/commands",
                          headers=device_headers(token)).json()["commands"]
    assert [command["id"] for command in polled_b] == ["cmd-b"]


def test_a_command_of_another_device_cannot_be_reported(client):
    token = _second_device(client)
    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-b", "+15551230000", "for b", "miniapp",
                      device_id=SECOND_DEVICE_ID)
    response = client.post("/commands/cmd-b/result", json={"status": "sent"},
                            headers=device_headers())
    assert response.status_code == 404
    assert db.command(conn, "cmd-b")["status"] == "pending"

    response_b = client.post("/commands/cmd-b/result", json={"status": "sent"},
                              headers=device_headers(token))
    assert response_b.status_code == 200
    assert response_b.json() == {"accepted": True, "status": "sent"}


def test_pending_commands_are_not_broadcast(client):
    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-a", "+15551230000", "for a", "miniapp",
                      device_id=DEVICE_ID)
    assert db.pending_commands(conn, None) == []
    assert db.pending_commands(conn, "another-device") == []


def test_revoking_a_device_fails_its_pending_commands(client):
    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-a", "+15551230000", "for a", "miniapp",
                      device_id=DEVICE_ID)
    response = client.post(f"/app/api/devices/{DEVICE_ID}/revoke",
                            headers=admin_headers())
    assert response.status_code == 200
    command = db.command(conn, "cmd-a")
    assert command["status"] == "failed"
    assert command["result"] == "device revoked"
    # the messages of the device stay
    assert client.get("/messages", headers=ADMIN_HEADERS).json()["items"] == []


def test_revoking_a_device_kills_its_token(client):
    response = client.post(f"/app/api/devices/{DEVICE_ID}/revoke",
                            headers=admin_headers())
    assert response.status_code == 200
    assert client.get("/commands", headers=device_headers()).status_code == 401
    assert client.post("/sms", json={"id": "x", "sender": "+1", "body": "b",
                                     "received_at": NOW},
                        headers=device_headers()).status_code == 401


def test_the_archived_legacy_device_cannot_be_revoked(client):
    conn = client.app.state.db_conn
    db.record_message(conn, {"id": "old", "sender": "+15550100",
                             "body": "old", "received_at": NOW})
    db.backfill_legacy_device(conn)
    response = client.post("/app/api/devices/legacy/revoke",
                            headers=admin_headers())
    assert response.status_code == 409


def test_the_device_list_shows_active_and_archived_devices(client):
    conn = client.app.state.db_conn
    db.record_message(conn, {"id": "old", "sender": "+15550100",
                             "body": "old", "received_at": NOW})
    db.backfill_legacy_device(conn)
    devices = client.get("/app/api/devices", headers=admin_headers()).json()["devices"]
    ids = {row["id"] for row in devices}
    assert ids == {DEVICE_ID, "legacy"}
    legacy = [row for row in devices if row["id"] == "legacy"][0]
    assert legacy["archived"] is True
    assert legacy["revoked"] is True
    assert legacy["name"] == "Previous device (archived)"
    assert legacy["platform"] == "legacy"


def test_the_device_heartbeat_is_throttled(client):
    conn = client.app.state.db_conn
    assert client.get("/commands", headers=device_headers()).status_code == 200
    first = db.device_by_id(conn, DEVICE_ID)["last_seen_at"]
    assert first is not None
    # a second poll right away does not write again
    client.get("/commands", headers=device_headers())
    assert db.device_by_id(conn, DEVICE_ID)["last_seen_at"] == first
    # the poll itself always refreshes the heartbeat
    db.touch_device(conn, DEVICE_ID, first - 100)
    client.get("/commands", headers=device_headers())
    assert db.device_by_id(conn, DEVICE_ID)["last_seen_at"] > first - 100


def test_a_device_can_be_renamed(client):
    response = client.patch(f"/app/api/devices/{DEVICE_ID}",
                             json={"name": "Kitchen phone"},
                             headers=admin_headers())
    assert response.status_code == 200
    assert response.json()["device"]["name"] == "Kitchen phone"
    assert db.device_by_id(client.app.state.db_conn,
                           DEVICE_ID)["name"] == "Kitchen phone"


def test_renaming_an_unknown_device_is_404(client):
    response = client.patch("/app/api/devices/nope", json={"name": "x"},
                             headers=admin_headers())
    assert response.status_code == 404


# --- the default device ---------------------------------------------------


def test_the_default_device_must_be_active(client):
    response = client.put("/admin/settings", json={"default_device_id": "nope"},
                          headers=ADMIN_HEADERS)
    assert response.status_code == 422

    response = client.put("/admin/settings",
                          json={"default_device_id": DEVICE_ID},
                          headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert response.json()["default_device_id"] == DEVICE_ID


def test_a_revoked_device_is_not_an_active_default(client):
    client.post(f"/app/api/devices/{DEVICE_ID}/revoke", headers=admin_headers())
    response = client.put("/admin/settings",
                          json={"default_device_id": DEVICE_ID},
                          headers=ADMIN_HEADERS)
    assert response.status_code == 422


def test_the_resolver_prefers_the_default_device(client):
    conn = client.app.state.db_conn
    _second_device(client)
    db.settings_set(conn, "default_device_id", SECOND_DEVICE_ID)
    assert db.resolve_command_device(conn) == SECOND_DEVICE_ID
    assert db.resolve_command_device(conn, DEVICE_ID) == DEVICE_ID
    # a revoked device is never picked
    db.revoke_device(conn, DEVICE_ID, int(time.time()))
    assert db.resolve_command_device(conn, DEVICE_ID) == SECOND_DEVICE_ID


def test_the_resolver_uses_the_only_active_device(client):
    conn = client.app.state.db_conn
    assert db.resolve_command_device(conn) == DEVICE_ID
    _second_device(client)
    # two devices and no default: nobody can be picked
    assert db.resolve_command_device(conn) is None
    db.settings_set(conn, "default_device_id", SECOND_DEVICE_ID)
    assert db.resolve_command_device(conn) == SECOND_DEVICE_ID


def test_the_resolver_returns_none_without_a_device(client):
    conn = client.app.state.db_conn
    db.revoke_device(conn, DEVICE_ID, int(time.time()))
    assert db.resolve_command_device(conn) is None


# --- the default device in the Mini App ------------------------------------


def _default_setting(client):
    value = db.settings_get(client.app.state.db_conn, "default_device_id")
    return value or None


def test_the_mini_app_can_choose_the_default_device(client):
    _second_device(client)
    conn = client.app.state.db_conn

    response = client.post(f"/app/api/devices/{SECOND_DEVICE_ID}/default",
                            headers=admin_headers())
    assert response.status_code == 200, response.text
    device = response.json()["device"]
    assert device["id"] == SECOND_DEVICE_ID
    assert device["default"] is True
    assert device["name"] == "Phone B"
    assert "token_hash" not in device
    assert _default_setting(client) == SECOND_DEVICE_ID
    # The Telegram /sms command now goes to that device.
    assert db.resolve_command_device(conn) == SECOND_DEVICE_ID


def test_the_device_list_shows_the_default_device(client):
    _second_device(client)
    devices = client.get("/app/api/devices",
                         headers=admin_headers()).json()["devices"]
    assert {row["id"]: row["default"] for row in devices} == {
        DEVICE_ID: False, SECOND_DEVICE_ID: False}
    assert all("token_hash" not in row for row in devices)

    client.post(f"/app/api/devices/{SECOND_DEVICE_ID}/default",
                headers=admin_headers())
    devices = client.get("/app/api/devices",
                         headers=admin_headers()).json()["devices"]
    assert {row["id"]: row["default"] for row in devices} == {
        DEVICE_ID: False, SECOND_DEVICE_ID: True}


def test_choosing_the_default_device_bumps_the_revision(client):
    _second_device(client)
    before = client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                        headers=admin_headers()).json()["rev"]
    client.post(f"/app/api/devices/{SECOND_DEVICE_ID}/default",
                headers=admin_headers())
    payload = client.get("/app/api/events",
                         params={"rev": before, "timeout": 0},
                         headers=admin_headers()).json()
    assert payload["changed"] is True
    assert payload["threads"] == [{"device_id": SECOND_DEVICE_ID,
                                    "thread_key": None}]


def test_an_unknown_device_cannot_be_the_default(client):
    response = client.post("/app/api/devices/nope/default",
                            headers=admin_headers())
    assert response.status_code == 404


def test_a_revoked_device_cannot_be_the_default(client):
    _second_device(client)
    client.post(f"/app/api/devices/{SECOND_DEVICE_ID}/revoke",
                headers=admin_headers())
    response = client.post(f"/app/api/devices/{SECOND_DEVICE_ID}/default",
                            headers=admin_headers())
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "device_not_active"


def test_the_archived_device_cannot_be_the_default(client):
    conn = client.app.state.db_conn
    db.record_message(conn, {"id": "old", "sender": "+15550100",
                             "body": "old", "received_at": NOW})
    db.backfill_legacy_device(conn)
    response = client.post("/app/api/devices/legacy/default",
                            headers=admin_headers())
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "device_not_active"


def test_revoking_the_default_device_clears_it(client):
    _second_device(client)
    conn = client.app.state.db_conn
    client.post(f"/app/api/devices/{SECOND_DEVICE_ID}/default",
                headers=admin_headers())
    assert _default_setting(client) == SECOND_DEVICE_ID

    client.post(f"/app/api/devices/{SECOND_DEVICE_ID}/revoke",
                headers=admin_headers())
    assert _default_setting(client) is None
    # The bot falls back to the device that is left.
    assert db.resolve_command_device(conn) == DEVICE_ID
    devices = client.get("/app/api/devices",
                         headers=admin_headers()).json()["devices"]
    assert all(row["default"] is False for row in devices)


# --- the migration --------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE messages (
    id TEXT PRIMARY KEY, sender TEXT NOT NULL, body TEXT NOT NULL,
    parts INTEGER NOT NULL, part_index INTEGER NOT NULL, group_id TEXT,
    source TEXT NOT NULL DEFAULT 'sms', received_at INTEGER NOT NULL,
    state TEXT NOT NULL, telegram_msg_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE jobs (
    msg_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL, locked_at INTEGER
);
CREATE TABLE sms_commands (
    id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, target TEXT NOT NULL,
    body TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
    reply_to_telegram_id INTEGER, updated_at INTEGER, finished_at INTEGER
);
CREATE TABLE contacts (number TEXT PRIMARY KEY, name TEXT NOT NULL,
                       updated_at INTEGER NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE filters (kind TEXT NOT NULL, pattern TEXT NOT NULL,
                      PRIMARY KEY (kind, pattern));
CREATE TABLE thread_reads (thread_key TEXT PRIMARY KEY, last_read_at INTEGER NOT NULL,
                           updated_at INTEGER NOT NULL);
CREATE TABLE device_status (device_id TEXT PRIMARY KEY, last_seen_at INTEGER NOT NULL,
                             app_version TEXT, sending_enabled INTEGER, sims_json TEXT,
                             updated_at INTEGER NOT NULL);
"""


def _old_database(tmp_path):
    """A database from before the device split."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO messages (id, sender, body, parts, part_index, source,"
        " received_at, state, attempts, created_at, updated_at)"
        " VALUES ('m1', '09121234567', 'old news', 1, 0, 'sms', 1700000000,"
        "         'delivered', 1, 1700000000, 1700000000)")
    conn.execute(
        "INSERT INTO sms_commands (id, created_at, target, body, source, status)"
        " VALUES ('c1', 1700000001, '+15551230000', 'hi', 'telegram_sms',"
        "         'delivered')")
    conn.execute(
        "INSERT INTO contacts (number, name, updated_at)"
        " VALUES ('09121234567', 'Delaram', 1700000000)")
    conn.execute(
        "INSERT INTO thread_reads (thread_key, last_read_at, updated_at)"
        " VALUES ('989121234567', 1700000000, 1700000000)")
    conn.commit()
    conn.close()
    return path


def test_the_migration_keeps_the_old_rows_under_the_legacy_device(tmp_path):
    path = _old_database(tmp_path)
    conn = db.open_db(path)

    legacy = db.device_by_id(conn, "legacy")
    assert legacy is not None
    assert legacy["name"] == "Previous device (archived)"
    assert legacy["platform"] == "legacy"
    assert legacy["archived"] == 1
    assert legacy["revoked_at"] is not None
    assert legacy["token_hash"] is None

    message = conn.execute(
        "SELECT device_id FROM messages WHERE id = 'm1'").fetchone()
    assert message["device_id"] == "legacy"
    command = conn.execute(
        "SELECT device_id FROM sms_commands WHERE id = 'c1'").fetchone()
    assert command["device_id"] == "legacy"

    contacts = db.list_device_contacts(conn, "legacy")
    assert [row["name"] for row in contacts] == ["Delaram"]
    assert contacts[0]["number_key"] == "989121234567"

    reads = conn.execute(
        "SELECT last_read_at FROM thread_reads_scoped"
        " WHERE device_id = 'legacy' AND thread_key = '989121234567'").fetchone()
    assert reads["last_read_at"] == 1700000000

    # nothing was thrown away
    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM sms_commands").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"] == 1
    kept = conn.execute(
        "SELECT last_read_at FROM thread_reads WHERE thread_key = '989121234567'"
    ).fetchone()
    assert kept["last_read_at"] == 1700000000
    conn.close()


def test_the_migration_is_idempotent(tmp_path):
    path = _old_database(tmp_path)
    conn = db.open_db(path)
    db.migrate(conn)
    db.migrate(conn)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM devices WHERE id = 'legacy'").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM device_contacts WHERE device_id = 'legacy'"
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 1
    conn.close()


def test_a_new_database_has_no_legacy_device(tmp_path):
    conn = db.open_db(str(tmp_path / "new.db"))
    assert db.device_by_id(conn, "legacy") is None
    conn.close()


def test_a_new_device_starts_empty(tmp_path):
    conn = db.open_db(str(tmp_path / "new.db"))
    seed_device(conn, DEVICE_ID, DEVICE_TOKEN)
    assert db.list_device_contacts(conn, DEVICE_ID) == []
    assert db.pending_commands(conn, DEVICE_ID) == []
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE device_id = ?",
        (DEVICE_ID,)).fetchone()
    assert row["n"] == 0
    conn.close()
