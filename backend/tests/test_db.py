import sqlite3
import threading
import time

import pytest

import app.db as db


def _msg(i, sender="555", body="hi"):
    return {"id": f"id-{i}", "sender": sender, "body": body, "received_at": 1_700_000_000 + i,
            "thread_id": f"t{i}", "parts": 1, "part_index": 0}


def test_schema_idempotent(tmp_path):
    path = str(tmp_path / "x.db")
    conn = db.open_db(path)
    db.record_message(conn, _msg(1))
    db.migrate(conn)
    db.migrate(conn)
    conn2 = db.open_db(path)
    assert db.counts_by_state(conn2) == {"pending": 1}


def test_dedup(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    assert db.record_message(conn, _msg(1)) is True
    assert db.record_message(conn, _msg(1)) is False
    assert db.counts_by_state(conn) == {"pending": 1}
    assert len(db.next_jobs(conn, int(time.time()) + 10, limit=5)) == 1


def test_message_row_fields(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.record_message(conn, _msg(1, sender="+7", body="hello"))
    msg = db.page_messages(conn, limit=5, offset=0)[0]
    assert msg["id"] == "id-1"
    assert msg["sender"] == "+7"
    assert msg["body"] == "hello"
    assert msg["received_at"] == _msg(1)["received_at"]
    assert msg["parts"] == 1
    assert msg["part_index"] == 0
    assert msg["group_id"] == "t1"
    assert msg["state"] == "pending"
    assert msg["attempts"] == 0
    assert msg["telegram_msg_id"] is None
    assert msg["last_error"] is None


def test_next_jobs_order_by_next_attempt_at(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    base = 1_700_000_000
    for i in (0, 1, 2, 3):
        db.record_message(conn, _msg(i))
        db.update_state(conn, f"id-{i}", "pending", attempts=1, next_attempt_at=base + i)
    jobs = db.next_jobs(conn, now=base + 100, limit=10)
    assert [j["msg_id"] for j in jobs] == ["id-0", "id-1", "id-2", "id-3"]
    assert all(j["attempts"] == 1 for j in jobs)
    assert [j["next_attempt_at"] for j in jobs] == [base + i for i in range(4)]


def test_next_jobs_due_time_filtering(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.record_message(conn, _msg(1))
    db.record_message(conn, _msg(2))
    now = int(time.time())
    db.update_state(conn, "id-2", "pending", attempts=1, next_attempt_at=now + 10_000)
    assert [j["msg_id"] for j in db.next_jobs(conn, now=now, limit=10)] == ["id-1"]
    future = db.next_jobs(conn, now=now + 10_001, limit=10)
    assert {j["msg_id"] for j in future} == {"id-1", "id-2"}
    assert len(db.next_jobs(conn, now=now + 10_001, limit=1)) == 1


def test_update_state_delivered_removes_job(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.record_message(conn, _msg(1))
    assert len(db.next_jobs(conn, int(time.time()) + 60, limit=5)) == 1
    db.update_state(conn, "id-1", "delivered", attempts=0, next_attempt_at=0,
                    telegram_msg_id="tg-9")
    assert db.next_jobs(conn, int(time.time()) + 60, limit=5) == []
    msg = db.page_messages(conn, limit=5, offset=0)[0]
    assert msg["state"] == "delivered"
    assert msg["telegram_msg_id"] == "tg-9"
    assert db.counts_by_state(conn) == {"delivered": 1}


def test_update_state_failed_removes_job(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.record_message(conn, _msg(1))
    db.record_message(conn, _msg(2))
    db.update_state(conn, "id-1", "failed", attempts=8, next_attempt_at=0,
                    last_error="rate_limited")
    assert [j["msg_id"] for j in db.next_jobs(conn, int(time.time()) + 60, limit=5)] == \
        ["id-2"]
    msg = db.page_messages(conn, limit=5, offset=1)[0]
    assert msg["id"] == "id-1"
    assert msg["state"] == "failed"
    assert msg["last_error"] == "rate_limited"
    assert db.counts_by_state(conn) == {"pending": 1, "failed": 1}


def test_update_state_uncertain_keeps_job_and_bumps_attempts(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.record_message(conn, _msg(1))
    db.update_state(conn, "id-1", "uncertain", attempts=1,
                    next_attempt_at=int(time.time()) + 60, last_error="timeout")
    jobs = db.next_jobs(conn, int(time.time()) + 61, limit=5)
    assert len(jobs) == 1 and jobs[0]["attempts"] == 1
    msg = db.page_messages(conn, limit=5, offset=0)[0]
    assert msg["state"] == "uncertain"
    assert msg["attempts"] == 1
    assert msg["last_error"] == "timeout"
    assert db.counts_by_state(conn) == {"uncertain": 1}


def test_pagination(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    for i in range(25):
        db.record_message(conn, _msg(i))
    page = db.page_messages(conn, limit=10, offset=5)
    assert len(page) == 10
    assert [m["id"] for m in page] == [f"id-{n}" for n in range(19, 9, -1)]
    assert [m["id"] for m in db.page_messages(conn, limit=10, offset=0)] == \
        [f"id-{n}" for n in range(24, 14, -1)]
    assert db.page_messages(conn, limit=30, offset=25) == []
    assert db.page_messages(conn, limit=10, offset=20)[4]["id"] == "id-0"
    db.update_state(conn, "id-7", "delivered", attempts=0, next_attempt_at=0)
    pending = db.page_messages(conn, limit=50, offset=0, state="pending")
    assert len(pending) == 24
    assert "id-7" not in [m["id"] for m in pending]
    assert [m["id"] for m in db.page_messages(conn, limit=50, offset=0, state="delivered")] == \
        ["id-7"]


def test_settings_roundtrip(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    assert db.settings_get(conn, "paused") is None
    db.settings_set(conn, "paused", "1")
    assert db.settings_get(conn, "paused") == "1"
    db.settings_set(conn, "paused", "0")
    assert db.settings_get(conn, "paused") == "0"
    assert db.settings_get(conn, "unknown") is None


def test_filters_roundtrip(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    assert db.filters_all(conn) == {"allow": set(), "block": set(), "keyword": set()}
    db.filters_add(conn, "allow", "+15551234")
    db.filters_add(conn, "keyword", "stop")
    db.filters_add(conn, "allow", "+15551234")
    assert db.filters_all(conn) == {"allow": {"+15551234"}, "block": set(),
                                    "keyword": {"stop"}}
    db.filters_remove(conn, "allow", "+15551234")
    assert db.filters_all(conn) == {"allow": set(), "block": set(), "keyword": {"stop"}}


def test_wal_file_exists_after_write(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    version = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert version == "wal"
    db.record_message(conn, _msg(1))
    assert list(tmp_path.glob("*-wal"))


def test_owner_thread_nested_writes_share_transaction(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))

    def fn():
        conn.execute("INSERT INTO settings (key, value) VALUES ('a', '1')")
        db.settings_set(conn, "b", "2")
        assert conn.in_transaction

    db._write_tx(conn, fn)
    assert getattr(db._TX_OWNER, "owner", False) is False
    assert db.settings_get(conn, "a") == "1"
    assert db.settings_get(conn, "b") == "2"


def test_write_from_other_thread_blocks_until_owner_tx_commit(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    owner_paused = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()

    def owner():
        def fn():
            conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
            owner_paused.set()
            release.wait(5)

        db._write_tx(conn, fn)

    def foreign_writer():
        db.settings_set(conn, "w", "1")
        writer_done.set()

    t_owner = threading.Thread(target=owner)
    t_owner.start()
    assert owner_paused.wait(5)
    t_writer = threading.Thread(target=foreign_writer)
    t_writer.start()
    assert not writer_done.wait(0.2), "foreign writer must block on the open tx"
    release.set()
    t_owner.join(5)
    assert not t_owner.is_alive()
    t_writer.join(5)
    assert not t_writer.is_alive()
    assert writer_done.is_set()
    assert db.settings_get(conn, "k") == "v"
    assert db.settings_get(conn, "w") == "1"


def test_update_state_unknown_id_raises_lookup_error(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.record_message(conn, _msg(1))
    with pytest.raises(LookupError):
        db.update_state(conn, "id-404", "delivered", attempts=0, next_attempt_at=0)
    assert db.counts_by_state(conn) == {"pending": 1}
    assert [j["msg_id"] for j in db.next_jobs(conn, int(time.time()) + 60, limit=5)] == \
        ["id-1"]
    assert db.record_message(conn, _msg(2)) is True


def test_received_state_message_not_enqueued(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    msg = _msg(1)
    msg["state"] = "received"
    assert db.record_message(conn, msg) is True
    assert db.next_jobs(conn, int(time.time()) + 60, limit=5) == []
    assert db.counts_by_state(conn) == {"received": 1}


# --- Phase 1: thread keys, read cursors, command lifecycle, heartbeat -----

_OLD_SCHEMA = """
CREATE TABLE messages (
    id TEXT PRIMARY KEY, sender TEXT NOT NULL, sender_name TEXT,
    body TEXT NOT NULL, parts INTEGER NOT NULL, part_index INTEGER NOT NULL,
    group_id TEXT, source TEXT NOT NULL DEFAULT 'sms',
    received_at INTEGER NOT NULL, state TEXT NOT NULL,
    telegram_msg_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE sms_commands (
    id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, target TEXT NOT NULL,
    body TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
    result TEXT, reply_to_telegram_id INTEGER
);
CREATE TABLE contacts (
    number TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _old_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO messages (id, sender, body, parts, part_index,"
        " received_at, state, created_at, updated_at)"
        " VALUES (?, ?, ?, 1, 0, ?, 'delivered', ?, ?)",
        [("m1", "+98 912 123 4567", "old one", 100, 100, 100),
         ("m2", "AD-BANK", "old two", 101, 101, 101)],
    )
    conn.executemany(
        "INSERT INTO sms_commands (id, created_at, target, body, source,"
        " status) VALUES (?, ?, ?, 'hi', 'telegram_sms', ?)",
        [("c1", 200, "09121234567", "delivered"),
         ("c2", 201, "09121234567", "sent")],
    )
    conn.execute("INSERT INTO contacts VALUES (?, ?, ?)",
                 ("09121234567", "Ali", 300))
    conn.commit()
    conn.close()


def test_migration_adds_columns_and_backfills_old_rows(tmp_path):
    path = str(tmp_path / "old.db")
    _old_db(path)

    conn = db.open_db(path)
    try:
        msg_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(messages)")}
        cmd_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(sms_commands)")}
        contacts_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(contacts)")}
        assert {"thread_key", "sub_id"} <= msg_columns
        assert {"thread_key", "sub_id", "client_ref", "updated_at",
                "finished_at"} <= cmd_columns
        assert "number_key" in contacts_columns

        assert db.counts_by_state(conn) == {"delivered": 2}
        messages = {row["id"]: row for row in conn.execute(
            "SELECT id, thread_key, sub_id FROM messages")}
        assert messages["m1"]["thread_key"] == "989121234567"
        assert messages["m2"]["thread_key"] == "raw:ad-bank"
        assert messages["m1"]["sub_id"] == 0

        commands = {row["id"]: row for row in conn.execute(
            "SELECT id, thread_key, updated_at, finished_at"
            " FROM sms_commands")}
        assert commands["c1"]["thread_key"] == "989121234567"
        assert commands["c1"]["updated_at"] == 200
        assert commands["c1"]["finished_at"] == 200
        assert commands["c2"]["updated_at"] == 201
        assert commands["c2"]["finished_at"] is None

        assert conn.execute(
            "SELECT number_key FROM contacts").fetchone()[0] == "989121234567"
        assert {row[1] for row in conn.execute(
            "PRAGMA index_list(sms_commands)")} >= {
                "idx_commands_thread", "idx_commands_client_ref"}
        assert {row[1] for row in conn.execute(
            "PRAGMA index_list(messages)")} >= {"idx_messages_thread"}
        # idempotent: a second migration and backfill change nothing
        assert db.backfill_thread_keys(conn) == 0
        db.migrate(conn)
        assert db.counts_by_state(conn) == {"delivered": 2}
    finally:
        conn.close()


def test_record_message_writes_thread_key_and_sub_id(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    msg = _msg(1, sender="+98 912 123 4567")
    msg["sub_id"] = 3
    db.record_message(conn, msg)
    stored = db.page_messages(conn, limit=5, offset=0)[0]
    assert stored["thread_key"] == "989121234567"
    assert stored["sub_id"] == 3

    raw = _msg(2, sender="AD-BANK")
    db.record_message(conn, raw)
    rows = {row["id"]: row for row in db.page_messages(conn, limit=5, offset=0)}
    assert rows["id-2"]["thread_key"] == "raw:ad-bank"


def test_contacts_store_number_key_but_keep_raw_number(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.replace_contacts(conn, [{"number": "0912 123 4567", "name": "Ali"}])
    row = conn.execute(
        "SELECT number, name, number_key FROM contacts").fetchone()
    assert row["number"] == "0912 123 4567"
    assert row["number_key"] == "989121234567"


def test_client_ref_is_unique(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.create_command(conn, "cmd-1", "09121234567", "one", "miniapp",
                      client_ref="ref-1")
    db.create_command(conn, "cmd-2", "09121234567", "two", "miniapp",
                      client_ref="ref-1")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM sms_commands").fetchone()["n"] == 1
    found = db.command_by_client_ref(conn, "ref-1")
    assert found is not None and found["id"] == "cmd-1"
    assert found["thread_key"] == "989121234567"
    assert db.command_by_client_ref(conn, None) is None
    assert db.command_by_client_ref(conn, "missing") is None

    # NULL client refs are not deduplicated (partial unique index)
    db.create_command(conn, "cmd-3", "09121234567", "three", "telegram_sms")
    db.create_command(conn, "cmd-4", "09121234567", "four", "telegram_sms")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM sms_commands").fetchone()["n"] == 3


def test_command_transitions(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.create_command(conn, "cmd-1", "09121234567", "hi", "miniapp",
                      sub_id=2, client_ref="ref")
    stored = db.command(conn, "cmd-1")
    assert stored["sub_id"] == 2
    assert stored["thread_key"] == "989121234567"
    assert stored["updated_at"] is not None
    assert stored["finished_at"] is None
    conn.execute("UPDATE sms_commands SET updated_at = 1 WHERE id = 'cmd-1'")

    assert db.finish_command(conn, "cmd-1", "sent", "ok") == (True, "sent")
    stored = db.command(conn, "cmd-1")
    assert stored["status"] == "sent" and stored["result"] == "ok"
    assert stored["updated_at"] > 1
    assert stored["finished_at"] is None

    # duplicate sent is ignored
    assert db.finish_command(conn, "cmd-1", "sent") == (False, "sent")
    # sent -> failed is allowed and terminal
    assert db.finish_command(conn, "cmd-1", "failed", "boom") == (True, "failed")
    assert db.command(conn, "cmd-1")["finished_at"] is not None
    # failed is terminal
    assert db.finish_command(conn, "cmd-1", "delivered") == (False, "failed")
    assert db.finish_command(conn, "cmd-1", "rejected") == (False, "failed")

    # delivered may arrive before sent
    db.create_command(conn, "cmd-2", "09121234567", "hi", "miniapp")
    assert db.finish_command(conn, "cmd-2", "delivered") == (True, "delivered")
    assert db.command(conn, "cmd-2")["finished_at"] is not None
    # a late sent report must not overwrite delivered
    assert db.finish_command(conn, "cmd-2", "sent") == (False, "delivered")
    # repeated delivered is ignored
    assert db.finish_command(conn, "cmd-2", "delivered") == (False, "delivered")

    # rejected from pending is terminal
    db.create_command(conn, "cmd-3", "09121234567", "hi", "miniapp")
    assert db.finish_command(conn, "cmd-3", "rejected") == (True, "rejected")
    assert db.finish_command(conn, "cmd-3", "sent") == (False, "rejected")

    # pending -> sent -> delivered
    db.create_command(conn, "cmd-4", "09121234567", "hi", "miniapp")
    assert db.finish_command(conn, "cmd-4", "sent") == (True, "sent")
    assert db.finish_command(conn, "cmd-4", "delivered") == (True, "delivered")

    # unknown ids and invalid target statuses
    assert db.finish_command(conn, "missing", "sent") == (False, None)
    with pytest.raises(ValueError):
        db.finish_command(conn, "cmd-4", "bogus")


def test_seed_reads_once_marks_existing_threads(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.record_message(conn, _msg(1, sender="09121234567"))
    db.record_message(conn, _msg(2, sender="09120000003"))
    db.settings_set(conn, "miniapp_seeded_at", "1")
    conn.execute("DELETE FROM thread_reads")
    conn.execute("DELETE FROM settings WHERE key = 'miniapp_seeded_at'")

    now = 1_700_000_000
    assert db.seed_reads_once(conn, now) == 2
    rows = conn.execute(
        "SELECT thread_key, last_read_at FROM thread_reads").fetchall()
    assert {row["thread_key"] for row in rows} == {
        "989121234567", "989120000003"}
    assert all(row["last_read_at"] == now for row in rows)
    assert db.settings_get(conn, "miniapp_seeded_at") == str(now)
    # second call is a no-op
    assert db.seed_reads_once(conn, now + 5) == 0


def test_chat_summaries_timeline_and_read_cursor(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    now = 1_700_000_000
    db.record_message(conn, {"id": "m1", "sender": "09121234567",
                             "body": "first", "received_at": now,
                             "state": "delivered"})
    db.record_message(conn, {"id": "m2", "sender": "09121234567",
                             "body": "second", "received_at": now + 10,
                             "state": "delivered"})
    db.replace_contacts(conn, [{"number": "+989121234567", "name": "Ali"}])
    db.create_command(conn, "c1", "09121234567", "outbound", "miniapp",
                      thread_key="989121234567")
    conn.execute("UPDATE sms_commands SET created_at = ? WHERE id = 'c1'",
                 (now + 20,))

    summaries = db.chat_summaries(conn)
    assert len(summaries) == 1
    row = summaries[0]
    assert row["thread_key"] == "989121234567"
    assert row["name"] == "Ali"
    assert row["display"] == "+989121234567"
    assert row["last_ts"] == now + 20
    assert row["last_body"] == "outbound"
    assert row["last_direction"] == "out"
    assert row["unread"] == 2  # seeded read cursor is 0 for new messages

    assert db.mark_thread_read(conn, "989121234567", now) == now
    assert db.chat_summaries(conn)[0]["unread"] == 1
    # the cursor never moves backwards
    assert db.mark_thread_read(conn, "989121234567", now - 10) == now
    assert db.chat_summaries(conn)[0]["unread"] == 1

    timeline = db.thread_timeline(conn, "989121234567")
    assert [event["id"] for event in timeline] == ["cmd:c1", "msg:m2", "msg:m1"]
    assert [event["direction"] for event in timeline] == ["out", "in", "in"]
    assert db.thread_timeline(conn, "989121234567", before_ts=now + 20) == [
        event for event in timeline[1:]]

    history = db.command_history(conn, "989121234567")
    assert [item["id"] for item in history] == ["c1"]


def test_chat_search_matches_name_number_and_body_and_escapes_wildcards(
        tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    base = 1_700_000_000
    db.record_message(conn, {"id": "m1", "sender": "09121234567",
                             "body": "100% sure", "received_at": base,
                             "state": "delivered"})
    db.record_message(conn, {"id": "m2", "sender": "09120000003",
                             "body": "hello", "received_at": base + 1,
                             "state": "delivered"})
    db.replace_contacts(conn, [{"number": "09120000003", "name": "Alice"}])

    assert [row["thread_key"] for row in db.chat_summaries(conn, "sure")] == \
        ["989121234567"]
    assert [row["thread_key"] for row in db.chat_summaries(conn, "delaram")] == \
        ["989120000003"]
    assert [row["thread_key"] for row in db.chat_summaries(conn, "0939")] == \
        ["989120000003"]
    assert [row["thread_key"] for row in db.chat_summaries(conn, "100%")] == \
        ["989121234567"]
    # a wildcard is a literal, so "%" only matches the body containing it
    assert [row["thread_key"] for row in db.chat_summaries(conn, "%")] == \
        ["989121234567"]
    assert db.chat_summaries(conn, "_") == []


def test_device_seen_upserts_and_preserves_missing_fields(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.device_seen(conn, "phone-a", "1.2.0", True,
                   '[{"sub_id":1,"slot":0,"label":"Personal"}]', 1000)
    first = db.device_status(conn)
    assert first["device_id"] == "phone-a"
    assert first["last_seen_at"] == 1000
    assert first["app_version"] == "1.2.0"
    assert first["sending_enabled"] == 1

    db.device_seen(conn, "phone-a", None, None, None, 2000)
    second = db.device_status(conn)
    assert second["last_seen_at"] == 2000
    assert second["app_version"] == "1.2.0"
    assert second["sending_enabled"] == 1
    assert second["sims_json"] == first["sims_json"]

    db.device_seen(conn, "phone-a", "1.3.0", False, "[]", 3000)
    third = db.device_status(conn)
    assert third["app_version"] == "1.3.0"
    assert third["sending_enabled"] == 0
    assert third["sims_json"] == "[]"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM device_status").fetchone()["n"] == 1
