import hashlib
import hmac
import sqlite3
import threading
import time
import uuid

import app.phones as phones

STATES = ("received", "pending", "delivered", "failed", "uncertain")
_FILTER_KINDS = ("allow", "block", "keyword")
COMMAND_STATUSES = ("pending", "sent", "delivered", "failed", "rejected")
COMMAND_TERMINAL = ("delivered", "failed", "rejected")
_COMMAND_TRANSITIONS = {
    "pending": ("sent", "delivered", "failed", "rejected"),
    "sent": ("delivered", "failed"),
    "delivered": (),
    "failed": (),
    "rejected": (),
}
# The call-log kinds the phone may upload. They mirror the Android type
# mapping and the columns of ``call_log``.
CALL_KINDS = ("incoming", "outgoing", "missed", "rejected", "blocked",
              "voicemail", "unknown")
# Live call states and the actions an action row may carry.
CALL_STATES = ("ringing", "dialing", "active", "held", "disconnected")
CALL_DIRECTIONS = ("incoming", "outgoing")
CALL_ACTIONS = ("answer", "reject", "hangup", "mute", "unmute", "speaker_on",
                "speaker_off", "hold", "unhold", "dtmf", "dial")
CALL_ACTION_STATUSES = ("pending", "delivered", "done", "failed", "expired",
                        "rejected")
# Only a pending or a delivered action may still reach a terminal state: a
# phone reports once, and a late report never reopens an action.
_CALL_ACTION_TRANSITIONS = {
    "pending": ("done", "failed", "rejected"),
    "delivered": ("done", "failed", "rejected"),
    "done": (),
    "failed": (),
    "expired": (),
    "rejected": (),
}
# The archived device that holds everything from before the device split.
LEGACY_DEVICE_ID = "legacy"
LEGACY_DEVICE_NAME = "Previous device (archived)"
# A device heartbeat is written at most every 30 s, so a chatty client
# does not turn every request into a write.
DEVICE_TOUCH_INTERVAL_S = 30

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        sender TEXT NOT NULL,
        sender_name TEXT,
        body TEXT NOT NULL,
        parts INTEGER NOT NULL,
        part_index INTEGER NOT NULL,
        group_id TEXT,
        thread_key TEXT,
        sub_id INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'sms',
        received_at INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('received','pending','delivered','failed','uncertain')),
        telegram_msg_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        seen_at INTEGER,
        telegram_sent_at INTEGER,
        telegram_delete_at INTEGER,
        telegram_deleted_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_state ON messages(state)",
    "CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_key, received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_tg_delete"
    " ON messages(telegram_delete_at)"
    " WHERE telegram_delete_at IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS jobs (
        msg_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL,
        locked_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(next_attempt_at)",
    """
    CREATE TABLE IF NOT EXISTS sms_commands (
        id TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL,
        target TEXT NOT NULL,
        body TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        result TEXT,
        reply_to_telegram_id INTEGER,
        thread_key TEXT,
        sub_id INTEGER NOT NULL DEFAULT 0,
        client_ref TEXT,
        updated_at INTEGER,
        finished_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_commands_pending ON sms_commands(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_commands_thread ON sms_commands(thread_key, created_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_commands_client_ref"
    " ON sms_commands(client_ref) WHERE client_ref IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS contacts (
        number TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        number_key TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_number_key ON contacts(number_key)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS filters (
        kind TEXT NOT NULL CHECK (kind IN ('allow','block','keyword')),
        pattern TEXT NOT NULL,
        PRIMARY KEY (kind, pattern)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS thread_reads (
        thread_key   TEXT PRIMARY KEY,
        last_read_at INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_status (
        device_id       TEXT PRIMARY KEY,
        last_seen_at    INTEGER NOT NULL,
        app_version     TEXT,
        sending_enabled INTEGER,
        sims_json       TEXT,
        updated_at      INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        platform      TEXT NOT NULL DEFAULT 'android',
        app_version   TEXT,
        token_hash    TEXT,
        token_prefix  TEXT,
        created_at    INTEGER NOT NULL,
        paired_at     INTEGER,
        last_seen_at  INTEGER,
        sending_enabled INTEGER,
        sims_json     TEXT,
        revoked_at    INTEGER,
        archived      INTEGER NOT NULL DEFAULT 0,
        updated_at    INTEGER NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_token"
    " ON devices(token_hash) WHERE token_hash IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS pairings (
        id               TEXT PRIMARY KEY,
        scan_code_hash   TEXT NOT NULL,
        claim_secret_hash TEXT NOT NULL,
        device_id        TEXT NOT NULL,
        device_name      TEXT NOT NULL,
        platform         TEXT NOT NULL,
        app_version      TEXT,
        created_at       INTEGER NOT NULL,
        expires_at       INTEGER NOT NULL,
        approved_at      INTEGER,
        approved_by      INTEGER,
        approved_by_name      TEXT,
        approved_by_username  TEXT,
        claimed_at       INTEGER,
        status           TEXT NOT NULL CHECK
          (status IN ('pending','approved','claimed','expired','cancelled'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pairings_expiry ON pairings(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS device_contacts (
        device_id  TEXT NOT NULL,
        number     TEXT NOT NULL,
        name       TEXT NOT NULL,
        number_key TEXT,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (device_id, number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_device_contacts_key"
    " ON device_contacts(device_id, number_key)",
    """
    CREATE TABLE IF NOT EXISTS thread_reads_scoped (
        device_id    TEXT NOT NULL,
        thread_key   TEXT NOT NULL,
        last_read_at INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL,
        PRIMARY KEY (device_id, thread_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_device_thread"
    " ON messages(device_id, thread_key, received_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_commands_device_thread"
    " ON sms_commands(device_id, thread_key, created_at DESC, id DESC)",
    """
    CREATE TABLE IF NOT EXISTS call_log (
        id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        provider_id TEXT,
        number TEXT NOT NULL DEFAULT '',
        number_key TEXT,
        cached_name TEXT,
        kind TEXT NOT NULL,
        raw_type INTEGER,
        started_at INTEGER NOT NULL,
        duration_s INTEGER NOT NULL DEFAULT 0,
        sub_id INTEGER NOT NULL DEFAULT 0,
        phone_account TEXT,
        is_read INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT,
        present INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_call_log_device_time"
    " ON call_log(device_id, started_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_call_log_device_key"
    " ON call_log(device_id, number_key, started_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS call_sessions (
        device_id TEXT PRIMARY KEY,
        call_ref TEXT NOT NULL,
        number TEXT NOT NULL DEFAULT '',
        number_key TEXT,
        direction TEXT NOT NULL,
        state TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        answered_at INTEGER,
        ended_at INTEGER,
        muted INTEGER NOT NULL DEFAULT 0,
        speaker INTEGER NOT NULL DEFAULT 0,
        sim_label TEXT,
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS call_actions (
        id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        call_ref TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        payload TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        client_ref TEXT,
        result TEXT,
        finished_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_call_actions_device_status"
    " ON call_actions(device_id, status, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_call_actions_client_ref"
    " ON call_actions(client_ref) WHERE client_ref IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS tg_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        kind       TEXT NOT NULL,
        delete_at  INTEGER NOT NULL,
        deleted_at INTEGER,
        created_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tg_messages_due"
    " ON tg_messages(delete_at)",
]

# Indexes may reference columns that only exist after _ensure_column() has
# run on an old database, so migrate() executes them in a second pass.
_TABLE_STATEMENTS = [stmt for stmt in _SCHEMA
                     if stmt.lstrip().upper().startswith("CREATE TABLE")]
_INDEX_STATEMENTS = [stmt for stmt in _SCHEMA
                     if stmt.lstrip().upper().startswith(
                         ("CREATE INDEX", "CREATE UNIQUE INDEX"))]
BACKFILL_BATCH = 500

_COLUMNS = (
    ("messages", "sender_name", "ALTER TABLE messages ADD COLUMN sender_name TEXT"),
    ("messages", "sub_id",
     "ALTER TABLE messages ADD COLUMN sub_id INTEGER NOT NULL DEFAULT 0"),
    ("messages", "source",
     "ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'sms'"),
    ("messages", "thread_key", "ALTER TABLE messages ADD COLUMN thread_key TEXT"),
    ("sms_commands", "thread_key",
     "ALTER TABLE sms_commands ADD COLUMN thread_key TEXT"),
    ("sms_commands", "sub_id",
     "ALTER TABLE sms_commands ADD COLUMN sub_id INTEGER NOT NULL DEFAULT 0"),
    ("sms_commands", "client_ref",
     "ALTER TABLE sms_commands ADD COLUMN client_ref TEXT"),
    ("sms_commands", "updated_at",
     "ALTER TABLE sms_commands ADD COLUMN updated_at INTEGER"),
    ("sms_commands", "finished_at",
     "ALTER TABLE sms_commands ADD COLUMN finished_at INTEGER"),
    ("contacts", "number_key",
     "ALTER TABLE contacts ADD COLUMN number_key TEXT"),
    ("messages", "device_id",
     "ALTER TABLE messages ADD COLUMN device_id TEXT"),
    ("messages", "seen_at",
     "ALTER TABLE messages ADD COLUMN seen_at INTEGER"),
    ("messages", "telegram_sent_at",
     "ALTER TABLE messages ADD COLUMN telegram_sent_at INTEGER"),
    ("messages", "telegram_delete_at",
     "ALTER TABLE messages ADD COLUMN telegram_delete_at INTEGER"),
    ("messages", "telegram_deleted_at",
     "ALTER TABLE messages ADD COLUMN telegram_deleted_at INTEGER"),
    ("sms_commands", "device_id",
     "ALTER TABLE sms_commands ADD COLUMN device_id TEXT"),
    ("pairings", "approved_by_name",
     "ALTER TABLE pairings ADD COLUMN approved_by_name TEXT"),
    ("pairings", "approved_by_username",
     "ALTER TABLE pairings ADD COLUMN approved_by_username TEXT"),
)

_WRITE_LOCK = threading.Lock()  # writes and reads; app is single-threaded asyncio, lock is cheap, and reads must never observe another thread's uncommitted tx
_TX_OWNER = threading.local()


FILTER_MAX_PATTERNS = 100
FILTER_MAX_PATTERN_LEN = 64
RESUME_BATCH = 200
# Internal page caps. They sit above the API limit (200) so a handler can
# ask for one extra row and still learn whether more history exists.
TIMELINE_MAX_LIMIT = 1000
CHAT_MAX_LIMIT = 1000


def clean_filter_patterns(patterns) -> list[str]:
    """Validate and normalise a list of filter patterns.

    Shared by the REST API and the Telegram command interface so both
    enforce the same bounds. Raises ValueError with a human readable
    message; callers translate that into their own error style.
    """
    if not isinstance(patterns, list):
        raise ValueError("patterns must be a list")
    if len(patterns) > FILTER_MAX_PATTERNS:
        raise ValueError(
            f"at most {FILTER_MAX_PATTERNS} patterns per kind")
    cleaned = []
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise ValueError("pattern entries must be strings")
        trimmed = pattern.strip()
        if not 1 <= len(trimmed) <= FILTER_MAX_PATTERN_LEN:
            raise ValueError(
                f"pattern entries must be 1..{FILTER_MAX_PATTERN_LEN} chars")
        cleaned.append(trimmed)
    return cleaned


def replace_contacts(conn: sqlite3.Connection, contacts: list[dict]) -> int:
    """Replace the contact list the phone uploaded.

    The list is small and uploaded whole, so replacing it in one
    transaction keeps the bot's view consistent with the phone.
    """
    def fn() -> int:
        now = _now()
        conn.execute("DELETE FROM contacts")
        stored = 0
        for contact in contacts:
            number = (contact.get("number") or "").strip()
            name = (contact.get("name") or "").strip()
            if not number or not name:
                continue
            number_key = phones.thread_key(number)
            conn.execute(
                "INSERT OR REPLACE INTO contacts"
                " (number, name, updated_at, number_key)"
                " VALUES (?, ?, ?, ?)",
                (number, name, now, number_key),
            )
            stored += 1
        return stored
    return _write_tx(conn, fn)


def list_contacts(conn: sqlite3.Connection, limit: int = 500) -> list[dict]:
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT number, name FROM contacts ORDER BY name LIMIT ?",
            (limit,),
        ).fetchall()
    return [_asdict(row) for row in rows]


def search_contacts(conn: sqlite3.Connection, query: str,
                    limit: int = 20) -> list[dict]:
    """Contacts whose name or number contains the query."""
    like = f"%{query.lower()}%"
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT number, name FROM contacts"
            " WHERE lower(name) LIKE ? OR number LIKE ?"
            " ORDER BY name LIMIT ?",
            (like, like, limit),
        ).fetchall()
    return [_asdict(row) for row in rows]


def contact_count(conn: sqlite3.Connection) -> int:
    with _WRITE_LOCK:
        row = conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()
    return int(row["n"])


def resolve_contact(conn: sqlite3.Connection, target: str) -> str | None:
    """Resolve a contact name (or a number) to a number.

    Used when the user replies to a forwarded message whose sender was
    stored as a name: the phone cannot send an SMS to a name.
    """
    text = (target or "").strip()
    if not text:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT number FROM contacts WHERE lower(name) = ? LIMIT 1",
            (text.lower(),),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT number FROM contacts WHERE lower(name) LIKE ? LIMIT 1",
                (f"%{text.lower()}%",),
            ).fetchone()
    return row["number"] if row is not None else None


def create_command(conn: sqlite3.Connection, command_id: str, target: str,
                   body: str, source: str,
                   reply_to_telegram_id: int | None = None,
                   thread_key: str | None = None, sub_id: int = 0,
                   client_ref: str | None = None,
                   device_id: str | None = None) -> str | None:
    """Queue an SMS the phone should send from its own SIM.

    ``client_ref`` is an optional idempotency key from the Mini App: the
    partial unique index makes a repeated insert a no-op. Callers that
    need the existing row should look it up with command_by_client_ref().

    ``device_id`` is the phone that has to run the command. It is never
    broadcast: a command without a device is never handed to a phone.

    Returns the conversation key the command was filed under, so a caller
    can scope a change notification to that conversation.
    """
    key = thread_key if thread_key is not None else phones.thread_key(target)

    def fn() -> None:
        now = _now()
        conn.execute(
            "INSERT OR IGNORE INTO sms_commands"
            " (id, created_at, target, body, source, status, result,"
            "  reply_to_telegram_id, thread_key, sub_id, client_ref,"
            "  device_id, updated_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?, ?, ?, NULL)",
            (command_id, now, target, body, source, reply_to_telegram_id,
             key, int(sub_id or 0), client_ref, device_id, now),
        )
    _write_tx(conn, fn)
    return key


def pending_commands(conn: sqlite3.Connection, device_id: str | None,
                     limit: int = 20) -> list[dict]:
    """The pending commands of one device.

    There is no broadcast: a phone only ever gets the commands that were
    addressed to it.
    """
    if not device_id:
        return []
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT id, created_at, target, body, source, reply_to_telegram_id,"
            " thread_key, sub_id, device_id"
            " FROM sms_commands WHERE status = 'pending' AND device_id = ?"
            " ORDER BY created_at, id LIMIT ?",
            (device_id, limit),
        ).fetchall()
    return [_asdict(row) for row in rows]


def command_by_client_ref(conn: sqlite3.Connection,
                          client_ref: str | None) -> dict | None:
    """The command created for an idempotency key, if any."""
    if not client_ref:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM sms_commands WHERE client_ref = ? LIMIT 1",
            (client_ref,),
        ).fetchone()
    return _asdict(row) if row is not None else None


def count_miniapp_sends(conn: sqlite3.Connection, since_ts: int) -> int:
    """Commands queued from the Mini App at or after ``since_ts``.

    Used for the send caps. Only ``source='miniapp'`` rows count, so a
    Telegram reply or a bot command can never consume the Mini App quota.
    """
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sms_commands"
            " WHERE source = 'miniapp' AND created_at >= ?",
            (int(since_ts),),
        ).fetchone()
    return int(row["n"])


def finish_command(conn: sqlite3.Connection, command_id: str, status: str,
                   result: str | None = None) -> tuple[bool, str | None]:
    """Apply a status transition and report whether it was accepted.

    Returns ``(accepted, current_status)``. A duplicate or late report
    (for example ``sent`` after ``delivered``) is rejected with the
    current status so the phone can stop retrying. An unknown id returns
    ``(False, None)``.
    """
    if status not in COMMAND_STATUSES:
        raise ValueError(f"invalid command status: {status}")

    def fn() -> tuple[bool, str | None]:
        row = conn.execute(
            "SELECT status FROM sms_commands WHERE id = ?", (command_id,)
        ).fetchone()
        if row is None:
            return False, None
        current = row["status"]
        allowed = _COMMAND_TRANSITIONS.get(current, ())
        if status not in allowed:
            return False, current
        now = _now()
        finished_at = now if status in COMMAND_TERMINAL else None
        conn.execute(
            "UPDATE sms_commands SET status = ?, result = ?, updated_at = ?,"
            " finished_at = COALESCE(?, finished_at) WHERE id = ?",
            (status, result, now, finished_at, command_id),
        )
        return True, status

    return _write_tx(conn, fn)


def command(conn: sqlite3.Connection, command_id: str) -> dict | None:
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM sms_commands WHERE id = ?",
            (command_id,),
        ).fetchone()
    return _asdict(row) if row is not None else None


def find_by_telegram_id(conn: sqlite3.Connection,
                         telegram_message_id: int) -> dict | None:
    """The relayed SMS a Telegram reply refers to."""
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT id, sender, sender_name, body, telegram_msg_id, device_id"
            " FROM messages WHERE telegram_msg_id = ?",
            (telegram_message_id,),
        ).fetchone()
    return _asdict(row) if row is not None else None


def message_by_telegram_id(conn: sqlite3.Connection,
                           telegram_message_id: int) -> dict | None:
    """A message looked up by its Telegram message id (for edits)."""
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT id, sender, sender_name, body, telegram_msg_id, "
            "received_at, state, attempts FROM messages"
            " WHERE telegram_msg_id = ?",
            (telegram_message_id,),
        ).fetchone()
    return _asdict(row) if row is not None else None


def normalize_sender(sender: str) -> str:
    """Canonical form of a sender address.

    The same number reaches the relay spelled in two ways: as the network
    displays it ("0912 000 0001") and as E.164 ("+989120000001"). Both
    must map to the same key, otherwise every message looks like a
    duplicate of the one that arrived a second earlier through the other
    capture path.
    """
    trimmed = (sender or "").strip()
    if not trimmed:
        return ""
    digits = "".join(ch for ch in trimmed if ch.isdigit())
    if not digits:
        return trimmed.lower()
    if trimmed.startswith("+"):
        return "+" + digits
    if digits.startswith("0098"):
        return "+" + digits[2:]
    if digits.startswith("009"):
        return "+" + digits[2:]
    if digits.startswith("98") and len(digits) >= 12:
        return "+" + digits
    if digits.startswith("0") and 10 <= len(digits) <= 12:
        return "+98" + digits[1:]
    return digits


def find_recent_duplicate(conn: sqlite3.Connection, sender: str, body: str,
                           received_at: int, source: str = "sms",
                           window: int = 120,
                           device_id: str | None = None) -> dict | None:
    """A message with the same sender and text from ANOTHER capture path.

    Used to stop the same SMS being stored twice when the phone captures
    it through two paths (broadcast and notification). Two genuine repeats
    from the same path are separate messages and are both kept. The sender
    is compared in its normalised form, because the two paths spell the
    same number differently.

    Imported history is never another capture path: an import is a replay
    of the phone's inbox, so a live SMS that happens to have the same text
    must still be stored and forwarded.

    ``device_id`` keeps the check inside one device: a phone never
    deduplicates against the messages of another phone.
    """
    key = normalize_sender(sender)
    sql = (
        "SELECT id, state, sender FROM messages WHERE body = ?"
        " AND source != ?"
        " AND source != 'import'"
    )
    params: list = [body, source]
    if device_id is not None:
        sql += " AND device_id = ?"
        params.append(device_id)
    sql += " AND received_at BETWEEN ? AND ? ORDER BY received_at LIMIT 25"
    params.extend([received_at - window, received_at + window])
    with _WRITE_LOCK:
        rows = conn.execute(sql, tuple(params)).fetchall()
    for row in rows:
        if normalize_sender(row["sender"]) == key:
            return {"id": row["id"], "state": row["state"]}
    return None


def find_import_duplicate(conn: sqlite3.Connection, thread_key: str,
                          body: str, received_at: int,
                          window: int = 60,
                          device_id: str | None = None) -> str | None:
    """The id of a message that already covers an imported item.

    Historical import overlaps with live capture: the same SMS may have
    been stored when it arrived. A message from the same thread with the
    same text within a minute of the inbox timestamp is treated as the
    same message. Unlike :func:`find_recent_duplicate` this also matches
    rows from the same source, because the import path is not a distinct
    capture mechanism but a replay of the phone's own inbox.
    """
    sql = ("SELECT id FROM messages WHERE thread_key = ? AND body = ?")
    params: list = [thread_key, body]
    if device_id is not None:
        sql += " AND device_id = ?"
        params.append(device_id)
    sql += " AND received_at BETWEEN ? AND ? LIMIT 1"
    params.extend([received_at - window, received_at + window])
    with _WRITE_LOCK:
        row = conn.execute(sql, tuple(params)).fetchone()
    return row["id"] if row is not None else None


def find_import_command_duplicate(conn: sqlite3.Connection,
                                  device_id: str | None, thread_key: str,
                                  body: str, created_at: int,
                                  window: int = 60) -> str | None:
    """The id of a sent command that already covers an imported item.

    The Sent folder of the phone overlaps with the commands the relay
    itself queued: a message sent through the Mini App and later imported
    from the phone's Sent folder is one message, not two. A command in the
    same conversation with the same text within a minute of the Sent
    timestamp is treated as the same one. Any source matches, because the
    import is a replay of what the phone already has.
    """
    sql = ("SELECT id FROM sms_commands WHERE thread_key = ? AND body = ?")
    params: list = [thread_key, body]
    if device_id is not None:
        sql += " AND device_id = ?"
        params.append(device_id)
    sql += " AND created_at BETWEEN ? AND ? LIMIT 1"
    params.extend([created_at - window, created_at + window])
    with _WRITE_LOCK:
        row = conn.execute(sql, tuple(params)).fetchone()
    return row["id"] if row is not None else None


def record_imported_command(conn: sqlite3.Connection, cmd: dict) -> bool:
    """Store a message the phone already sent, as history.

    The row is written with a terminal status so it can never be handed to
    a phone by :func:`pending_commands`: an imported message is not work to
    do. Nothing is enqueued and Telegram is not involved. Returns whether
    the row was inserted, so a replay counts as a duplicate.
    """
    status = cmd.get("status") or "sent"
    if status not in COMMAND_STATUSES:
        raise ValueError(f"invalid command status: {status}")
    created_at = int(cmd["created_at"])
    device_id = cmd.get("device_id")
    key = cmd.get("thread_key")
    if key is None:
        key = phones.thread_key(cmd["target"])
        if key is None:
            key = phones.raw_key(cmd["target"])
    target = cmd["target"]
    body = cmd["body"]
    source = cmd.get("source") or "import"
    sub_id = int(cmd.get("sub_id") or 0)
    command_id = cmd["id"]

    def fn() -> bool:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sms_commands"
            " (id, created_at, target, body, source, status, result,"
            "  reply_to_telegram_id, thread_key, sub_id, client_ref,"
            "  device_id, updated_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?, ?)",
            (command_id, created_at, target, body, source, status, key,
             sub_id, device_id, created_at, created_at),
        )
        return cur.rowcount == 1

    return bool(_write_tx(conn, fn))


def count_jobs(conn: sqlite3.Connection) -> int:
    with _WRITE_LOCK:
        row = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    return int(row["n"])


def states_for_ids(conn: sqlite3.Connection, ids: list[str],
                   device_id: str | None = None) -> dict:
    """Delivery state for a bounded list of message ids.

    ``device_id`` restricts the answer to the messages of one device: a
    phone never learns the state of another phone's message.
    """
    if not ids:
        return {}
    with _WRITE_LOCK:
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT id, state, attempts, last_error FROM messages"
            f" WHERE id IN ({placeholders})"
        )
        params: list = list(ids)
        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)
        rows = conn.execute(sql, tuple(params)).fetchall()
    return {row["id"]: {"state": row["state"], "attempts": row["attempts"],
                        "last_error": row["last_error"]} for row in rows}


def _now() -> int:
    return int(time.time())


def _check_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"invalid state: {state}")


def _check_kind(kind: str) -> None:
    if kind not in _FILTER_KINDS:
        raise ValueError(f"invalid filter kind: {kind}")


def _asdict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _write_tx(conn: sqlite3.Connection, fn) -> object:
    if getattr(_TX_OWNER, "owner", False):
        return fn()
    with _WRITE_LOCK:
        if conn.in_transaction:
            _TX_OWNER.owner = True
            try:
                return fn()
            finally:
                _TX_OWNER.owner = False
        conn.execute("BEGIN IMMEDIATE")
        _TX_OWNER.owner = True
        try:
            result = fn()
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            _TX_OWNER.owner = False


def _enqueue(conn: sqlite3.Connection, msg_id: str, due_at: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO jobs (msg_id, attempts, next_attempt_at, locked_at)"
        " VALUES (?, 0, ?, NULL)",
        (msg_id, due_at),
    )


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """Add a column to an existing table when it is missing."""
    columns = {row[1] for row in conn.execute(
        f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(ddl)


def migrate(conn: sqlite3.Connection) -> None:
    def fn() -> None:
        for stmt in _TABLE_STATEMENTS:
            conn.execute(stmt)
        for table, column, ddl in _COLUMNS:
            _ensure_column(conn, table, column, ddl)
        for stmt in _INDEX_STATEMENTS:
            conn.execute(stmt)

    _write_tx(conn, fn)
    backfill_thread_keys(conn)
    seed_reads_once(conn, _now())
    backfill_legacy_device(conn)


def _legacy_rows_present(conn: sqlite3.Connection) -> bool:
    """True when rows from before the device split still exist.

    Messages and commands without a device belong to the previous phone;
    the old contact and read tables belong to it as well.
    """
    for table in ("messages", "sms_commands"):
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE device_id IS NULL"
        ).fetchone()
        if int(row["n"]) > 0:
            return True
    for table in ("contacts", "thread_reads"):
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        if int(row["n"]) > 0:
            return True
    return False


def _legacy_copy_pending(conn: sqlite3.Connection) -> bool:
    """True when the legacy device still misses rows of the old tables.

    The legacy device is created once. After that the copy is a no-op, so
    this check lets a restart skip the work instead of writing the same
    rows again.
    """
    row = conn.execute(
        "SELECT 1 FROM devices WHERE id = ? LIMIT 1", (LEGACY_DEVICE_ID,)
    ).fetchone()
    if row is None:
        return True
    for table in ("messages", "sms_commands"):
        pending = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE device_id IS NULL"
        ).fetchone()
        if int(pending["n"]) > 0:
            return True
    missing_contacts = conn.execute(
        "SELECT COUNT(*) AS n FROM contacts c WHERE NOT EXISTS ("
        "  SELECT 1 FROM device_contacts d"
        "   WHERE d.device_id = ? AND d.number = c.number)",
        (LEGACY_DEVICE_ID,),
    ).fetchone()
    if int(missing_contacts["n"]) > 0:
        return True
    missing_reads = conn.execute(
        "SELECT COUNT(*) AS n FROM thread_reads t WHERE NOT EXISTS ("
        "  SELECT 1 FROM thread_reads_scoped r"
        "   WHERE r.device_id = ? AND r.thread_key = t.thread_key)",
        (LEGACY_DEVICE_ID,),
    ).fetchone()
    return int(missing_reads["n"]) > 0


def backfill_legacy_device(conn: sqlite3.Connection) -> bool:
    """Move pre-device rows under one archived 'legacy' device.

    The old phone keeps its history, but it is read-only: it cannot pair,
    upload, poll commands or send. The row is only created when there is
    something to preserve, and the whole step is idempotent, so a restart
    never duplicates or loses a row.
    """
    def fn() -> bool:
        if not _legacy_rows_present(conn):
            return False
        if not _legacy_copy_pending(conn):
            # Everything is already under the legacy device: nothing left
            # to copy, so a restart does not write the same rows again.
            return False
        now = _now()
        conn.execute(
            "INSERT OR IGNORE INTO devices"
            " (id, name, platform, app_version, token_hash, token_prefix,"
            "  created_at, paired_at, last_seen_at, sending_enabled,"
            "  sims_json, revoked_at, archived, updated_at)"
            " VALUES (?, ?, 'legacy', NULL, NULL, NULL, ?, ?, NULL, NULL,"
            "         NULL, ?, 1, ?)",
            (LEGACY_DEVICE_ID, LEGACY_DEVICE_NAME, now, now, now, now),
        )
        conn.execute(
            "UPDATE messages SET device_id = ? WHERE device_id IS NULL",
            (LEGACY_DEVICE_ID,),
        )
        conn.execute(
            "UPDATE sms_commands SET device_id = ? WHERE device_id IS NULL",
            (LEGACY_DEVICE_ID,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO device_contacts"
            " (device_id, number, name, number_key, updated_at)"
            " SELECT ?, number, name, number_key, updated_at FROM contacts",
            (LEGACY_DEVICE_ID,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO thread_reads_scoped"
            " (device_id, thread_key, last_read_at, updated_at)"
            " SELECT ?, thread_key, last_read_at, updated_at FROM thread_reads",
            (LEGACY_DEVICE_ID,),
        )
        return True

    return _write_tx(conn, fn)


def _backfill(conn: sqlite3.Connection, select_sql: str, apply) -> int:
    """Run a keyset-paginated backfill in batches of BACKFILL_BATCH.

    Rows whose key cannot be derived stay NULL and are retried by the
    next startup; the rowid cursor guarantees this loop terminates.
    """
    total = 0
    cursor = 0
    while True:
        rows = conn.execute(
            select_sql + " AND rowid > ? ORDER BY rowid LIMIT ?",
            (cursor, BACKFILL_BATCH),
        ).fetchall()
        if not rows:
            return total

        def fn(batch=tuple(rows)) -> int:
            changed = 0
            for row in batch:
                changed += apply(conn, row)
            return changed

        total += _write_tx(conn, fn)
        cursor = rows[-1]["rowid"]


def backfill_thread_keys(conn: sqlite3.Connection) -> int:
    """Derive thread keys for rows written before the column existed."""

    def apply_message(conn, row) -> int:
        key = phones.thread_key(row["sender"])
        if key is None:
            key = phones.raw_key(row["sender"])
        if key is None:
            return 0
        return conn.execute(
            "UPDATE messages SET thread_key = ?"
            " WHERE id = ? AND thread_key IS NULL",
            (key, row["id"]),
        ).rowcount

    def apply_command(conn, row) -> int:
        key = phones.thread_key(row["target"])
        if key is None:
            key = phones.raw_key(row["target"])
        finished = (row["created_at"]
                    if row["status"] in COMMAND_TERMINAL else None)
        return conn.execute(
            "UPDATE sms_commands SET"
            " thread_key = COALESCE(thread_key, ?),"
            " updated_at = COALESCE(updated_at, ?),"
            " finished_at = COALESCE(finished_at, ?)"
            " WHERE id = ?",
            (key, row["created_at"], finished, row["id"]),
        ).rowcount

    def apply_contact(conn, row) -> int:
        key = phones.thread_key(row["number"])
        if key is None:
            key = phones.raw_key(row["number"])
        if key is None:
            return 0
        return conn.execute(
            "UPDATE contacts SET number_key = ?"
            " WHERE number = ? AND number_key IS NULL",
            (key, row["number"]),
        ).rowcount

    changed = _backfill(
        conn,
        "SELECT rowid AS rowid, id, sender FROM messages"
        " WHERE thread_key IS NULL",
        apply_message,
    )
    changed += _backfill(
        conn,
        "SELECT rowid AS rowid, id, target, status, created_at, updated_at"
        " FROM sms_commands"
        " WHERE (thread_key IS NULL OR updated_at IS NULL)",
        apply_command,
    )
    changed += _backfill(
        conn,
        "SELECT rowid AS rowid, number FROM contacts WHERE number_key IS NULL",
        apply_contact,
    )
    return changed


def seed_reads_once(conn: sqlite3.Connection, now: int) -> int:
    """Mark every existing thread read on the first Mini App rollout.

    Guarded by the ``miniapp_seeded_at`` setting so later inbound SMS
    count as unread. Returns the number of threads seeded (0 when the
    seed already happened).
    """
    def fn() -> int:
        row = conn.execute(
            "SELECT 1 FROM settings WHERE key = 'miniapp_seeded_at'"
        ).fetchone()
        if row is not None:
            return 0
        rows = conn.execute(
            "SELECT thread_key FROM ("
            "  SELECT DISTINCT thread_key FROM messages"
            "   WHERE thread_key IS NOT NULL"
            "  UNION"
            "  SELECT DISTINCT thread_key FROM sms_commands"
            "   WHERE thread_key IS NOT NULL)"
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO thread_reads"
                " (thread_key, last_read_at, updated_at) VALUES (?, ?, ?)",
                (row["thread_key"], now, now),
            )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('miniapp_seeded_at', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(now),),
        )
        return len(rows)

    return _write_tx(conn, fn)


def message_thread_key(sender: str) -> str | None:
    """The conversation key of a sender.

    The same rule as :func:`record_message`: a number becomes a thread
    key, anything else becomes a raw sender key.
    """
    key = phones.thread_key(sender)
    if key is None:
        key = phones.raw_key(sender)
    return key


def record_message(conn: sqlite3.Connection, msg: dict) -> bool:
    state = msg.get("state") or "pending"
    _check_state(state)
    now = _now()
    group_id = msg.get("thread_id", msg.get("group_id"))
    source = msg.get("source") or "sms"
    key = message_thread_key(msg.get("sender") or "")
    sub_id = int(msg.get("sub_id") or 0)
    device_id = msg.get("device_id")

    def fn() -> bool:
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages"
            " (id, sender, body, parts, part_index, group_id, source, "
            "  received_at, state, telegram_msg_id, attempts, last_error, "
            "  created_at, updated_at, thread_key, sub_id, device_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?, ?, ?)",
            (msg["id"], msg["sender"], msg["body"],
             msg.get("parts", 1), msg.get("part_index", 0), group_id, source,
             msg["received_at"], state, now, now, key, sub_id, device_id),
        )
        inserted = cur.rowcount == 1
        if inserted and state == "pending":
            enqueue(conn, msg["id"])
        return inserted

    return _write_tx(conn, fn)


def enqueue(conn: sqlite3.Connection, msg_id: str) -> None:
    _write_tx(conn, lambda: _enqueue(conn, msg_id, _now()))


def next_jobs(conn: sqlite3.Connection, now: int, limit: int) -> list[dict]:
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT msg_id, attempts, next_attempt_at, locked_at FROM jobs"
            " WHERE next_attempt_at <= ? ORDER BY next_attempt_at, msg_id LIMIT ?",
            (now, limit),
        ).fetchall()
    return [_asdict(row) for row in rows]


def update_state(conn: sqlite3.Connection, msg_id: str, state: str, attempts: int,
                 next_attempt_at: int, telegram_msg_id: str | None = None,
                 last_error: str | None = None) -> None:
    _check_state(state)
    now = _now()

    def fn() -> None:
        cur = conn.execute(
            "UPDATE messages SET state = ?, attempts = ?, telegram_msg_id = ?,"
            " last_error = ?, updated_at = ? WHERE id = ?",
            (state, attempts, telegram_msg_id, last_error, now, msg_id),
        )
        if cur.rowcount == 0:
            raise LookupError(f"message not found: {msg_id}")
        if state in ("delivered", "failed"):
            conn.execute("DELETE FROM jobs WHERE msg_id = ?", (msg_id,))
        else:
            conn.execute(
                "INSERT INTO jobs (msg_id, attempts, next_attempt_at, locked_at)"
                " VALUES (?, ?, ?, NULL)"
                " ON CONFLICT(msg_id) DO UPDATE SET attempts = excluded.attempts,"
                " next_attempt_at = excluded.next_attempt_at, locked_at = NULL",
                (msg_id, attempts, next_attempt_at),
            )

    _write_tx(conn, fn)


def counts_by_state(conn: sqlite3.Connection) -> dict:
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM messages GROUP BY state"
        ).fetchall()
    return {row["state"]: row["n"] for row in rows}


def page_messages(conn: sqlite3.Connection, limit: int, offset: int,
                  state: str | None = None,
                  device_id: str | None = None) -> list[dict]:
    """A page of messages, newest first.

    ``device_id`` restricts the page to one device. The device fields are
    joined in, so the caller does not need a second query.
    """
    sql = ("SELECT m.*, d.name AS device_name, d.platform AS device_platform"
           " FROM messages m LEFT JOIN devices d ON d.id = m.device_id")
    params: list = []
    conditions: list[str] = []
    if state is not None:
        _check_state(state)
        conditions.append("m.state = ?")
        params.append(state)
    if device_id is not None:
        conditions.append("m.device_id = ?")
        params.append(device_id)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY m.received_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with _WRITE_LOCK:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_asdict(row) for row in rows]


def resume_received(conn: sqlite3.Connection,
                    device_id: str | None = None) -> int:
    """Atomically move 'received' messages to 'pending' and enqueue them.

    Imported history is never resumed: an import is a replay of the
    phone's own inbox, not a message that has to be forwarded to
    Telegram. Resuming it would flood the chat with old messages.

    ``device_id`` restricts the resume to one device; ``None`` resumes
    every device.
    """
    def fn() -> int:
        now = _now()
        moved = 0
        sql = ("SELECT id FROM messages WHERE state = 'received'"
               " AND source != 'import'")
        params: list = []
        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += " LIMIT ?"
        while True:
            rows = conn.execute(sql, (*params, RESUME_BATCH)).fetchall()
            if not rows:
                return moved
            for row in rows:
                conn.execute(
                    "UPDATE messages SET state = 'pending', updated_at = ?"
                    " WHERE id = ?",
                    (now, row["id"]),
                )
                _enqueue(conn, row["id"], now)
            moved += len(rows)

    return _write_tx(conn, fn)


def settings_get(conn: sqlite3.Connection, key: str) -> str | None:
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row is not None else None


def settings_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    _write_tx(conn, lambda: conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    ))


def filters_all(conn: sqlite3.Connection) -> dict:
    result = {kind: set() for kind in _FILTER_KINDS}
    with _WRITE_LOCK:
        rows = conn.execute("SELECT kind, pattern FROM filters").fetchall()
    for row in rows:
        result[row["kind"]].add(row["pattern"])
    return result


def filters_add(conn: sqlite3.Connection, kind: str, pattern: str) -> None:
    _check_kind(kind)

    def fn() -> None:
        conn.execute(
            "INSERT OR IGNORE INTO filters (kind, pattern) VALUES (?, ?)",
            (kind, pattern),
        )

    _write_tx(conn, fn)


def filters_remove(conn: sqlite3.Connection, kind: str, pattern: str) -> None:
    _check_kind(kind)

    def fn() -> None:
        conn.execute("DELETE FROM filters WHERE kind = ? AND pattern = ?",
                     (kind, pattern))

    _write_tx(conn, fn)


def filters_replace(conn: sqlite3.Connection, kind: str,
                    patterns: list[str]) -> dict:
    """Replace every pattern of a kind with `patterns` in a single transaction."""

    _check_kind(kind)

    def fn() -> dict:
        conn.execute("DELETE FROM filters WHERE kind = ?", (kind,))
        for pattern in patterns:
            conn.execute(
                "INSERT OR IGNORE INTO filters (kind, pattern) VALUES (?, ?)",
                (kind, pattern),
            )
        rows = conn.execute(
            "SELECT pattern FROM filters WHERE kind = ?", (kind,)
        ).fetchall()
        return {kind: sorted(row["pattern"] for row in rows)}

    return _write_tx(conn, fn)


def _like_pattern(query: str) -> str:
    """A LIKE pattern with the wildcards escaped.

    SQLite has no default escape character; callers use ESCAPE '\\'.
    """
    escaped = (query or "").strip().lower()
    escaped = (escaped.replace("\\", "\\\\")
                      .replace("%", "\\%")
                      .replace("_", "\\_"))
    return f"%{escaped}%"


def _clamp_limit(limit: int, default: int, maximum: int = 200) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _search_likes(query: str) -> tuple[str, str, str]:
    """LIKE patterns for a search term: number, local number, text.

    A thread key stores the international digits ("98912..."), while the
    admin may type the local spelling ("0912..." or "912..."). The
    second number pattern covers that form.
    """
    base = _like_pattern(query)
    digits = "".join(ch for ch in phones.to_ascii_digits(query)
                     if ch.isdigit())
    alternative = base
    if digits.startswith("0"):
        alternative = _like_pattern("98" + digits[1:])
    elif digits.startswith("9") and len(digits) == 10:
        alternative = _like_pattern("98" + digits)
    elif digits.startswith("98"):
        alternative = _like_pattern(digits)
    return base, alternative, base


def chat_summaries(conn: sqlite3.Connection, device_id: str,
                   query: str | None = None, limit: int = 100,
                   before_ts: int | None = None,
                   before_key: str | None = None) -> list[dict]:
    """One row per thread of ONE device: last event, unread and a name.

    A thread belongs to a device, so this helper never looks at another
    device: the events, the contact names and the read markers all come
    from the rows of ``device_id``. The old global tables are not read at
    all, so nothing can leak from one device into another.

    The rows are ordered by ``last_ts DESC, thread_key``. ``before_ts`` and
    ``before_key`` are the two halves of the cursor: they continue the list
    after the given row, so a client can walk the whole inbox without
    skipping the threads that share a timestamp.

    ``before_ts`` without ``before_key`` is the strict timestamp cursor:
    only the rows with an older timestamp are returned, so no row is ever
    handed out twice.
    """
    if not isinstance(device_id, str) or not device_id:
        return []
    limit = _clamp_limit(limit, 100, CHAT_MAX_LIMIT)
    params: list = []
    sql = (
        "WITH events AS ("
        "  SELECT thread_key, 'in' AS direction, received_at AS ts, body,"
        "         sender_name, 'msg:' || id AS event_id"
        "    FROM messages WHERE thread_key IS NOT NULL AND device_id = ?"
        "  UNION ALL"
        "  SELECT thread_key, 'out' AS direction, created_at AS ts, body,"
        "         NULL AS sender_name, 'cmd:' || id AS event_id"
        "    FROM sms_commands WHERE thread_key IS NOT NULL AND device_id = ?"
        "),"
        " ranked AS ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "      PARTITION BY thread_key ORDER BY ts DESC, event_id DESC) AS rn"
        "    FROM events"
        "),"
        " latest AS (SELECT * FROM ranked WHERE rn = 1),"
        " names AS ("
        "  SELECT number_key, MIN(name) AS name FROM device_contacts"
        "   WHERE device_id = ? AND number_key IS NOT NULL"
        "   GROUP BY number_key"
        ")"
    )
    params.extend([device_id, device_id, device_id])
    sql += (
        " SELECT l.thread_key AS thread_key,"
        "        l.direction AS last_direction,"
        "        l.ts AS last_ts,"
        "        l.body AS last_body,"
        "        l.sender_name AS last_sender_name,"
        "        n.name AS contact_name,"
        "        (SELECT COUNT(*) FROM messages m"
        "          WHERE m.device_id = ?"
        "            AND m.thread_key = l.thread_key"
        "            AND m.received_at > COALESCE(r.last_read_at, 0)) AS unread"
        "   FROM latest l"
        "   LEFT JOIN names n ON n.number_key = l.thread_key"
        "   LEFT JOIN thread_reads_scoped r"
        "          ON r.device_id = ? AND r.thread_key = l.thread_key"
    )
    params.extend([device_id, device_id])
    conditions: list[str] = []
    if query and query.strip():
        number_like, local_like, text_like = _search_likes(query)
        conditions.append(
            "("
            " l.thread_key LIKE ? ESCAPE '\\'"
            " OR l.thread_key LIKE ? ESCAPE '\\'"
            " OR lower(COALESCE(n.name, '')) LIKE ? ESCAPE '\\'"
            " OR lower(COALESCE(l.sender_name, '')) LIKE ? ESCAPE '\\'"
            " OR EXISTS (SELECT 1 FROM events e"
            "            WHERE e.thread_key = l.thread_key"
            "              AND lower(e.body) LIKE ? ESCAPE '\\'))"
        )
        params.extend([number_like, local_like, text_like, text_like,
                       text_like])
    if before_ts is not None:
        if before_key is None:
            # A timestamp without a key is the old strict cursor: the rows
            # that share that second are left behind instead of being
            # returned a second time.
            conditions.append("l.ts < ?")
            params.append(before_ts)
        else:
            conditions.append(
                "(l.ts < ? OR (l.ts = ? AND l.thread_key > ?))")
            params.extend([before_ts, before_ts, before_key])
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY last_ts DESC, thread_key LIMIT ?"
    params.append(limit)
    with _WRITE_LOCK:
        rows = conn.execute(sql, tuple(params)).fetchall()
    summaries = []
    for row in rows:
        key = row["thread_key"]
        display = phones.display_number(key)
        summaries.append({
            "thread_key": key,
            "name": row["contact_name"] or row["last_sender_name"] or display,
            "display": display,
            "last_ts": row["last_ts"],
            "last_body": row["last_body"],
            "last_direction": row["last_direction"],
            "unread": int(row["unread"] or 0),
        })
    return summaries


def thread_timeline(conn: sqlite3.Connection, device_id: str,
                    thread_key: str, before_ts: int | None = None,
                    before_id: str | None = None,
                    limit: int = 50) -> list[dict]:
    """Merge inbound SMS and outbound commands of ONE device, newest first.

    A thread belongs to a device, so only the rows of ``device_id`` are
    read: the timeline of one phone never shows the history of another.

    The events are ordered by ``ts DESC, id DESC``. The cursor has two
    halves: ``before_ts`` is the timestamp and ``before_id`` the event id
    of the last row of the previous page. A page continues with the rows
    that are strictly older in that order, so events that share a second
    are never skipped.

    ``before_ts`` on its own keeps the old timestamp-only behaviour for
    older clients: the events that share that second are left behind, but
    no event is ever handed out twice.

    ``limit`` may exceed the public page size: a handler asks for one row
    more than it returns, so it can tell whether more history exists.
    """
    if not isinstance(device_id, str) or not device_id:
        return []
    limit = _clamp_limit(limit, 50, TIMELINE_MAX_LIMIT)
    sql = (
        "SELECT 'msg:' || id AS id, 'in' AS direction, received_at AS ts,"
        "       body, sender_name, NULL AS status, NULL AS result,"
        "       NULL AS client_ref, NULL AS sub_id,"
        "       NULL AS kind, NULL AS call_kind, NULL AS duration_s,"
        "       NULL AS number"
        "  FROM messages"
        " WHERE device_id = ?"
        "   AND thread_key = ?"
        "   AND (? IS NULL OR received_at < ?"
        "        OR (received_at = ? AND 'msg:' || id < ?))"
        " UNION ALL"
        " SELECT 'cmd:' || id, 'out', created_at, body, NULL, status, result,"
        "        client_ref, sub_id, NULL, NULL, NULL, NULL"
        "   FROM sms_commands"
        "  WHERE device_id = ?"
        "    AND thread_key = ?"
        "    AND (? IS NULL OR created_at < ?"
        "         OR (created_at = ? AND 'cmd:' || id < ?))"
        " UNION ALL"
        " SELECT 'call:' || id,"
        "        CASE WHEN kind = 'outgoing' THEN 'out' ELSE 'in' END,"
        "        started_at, NULL, NULL, NULL, NULL, NULL, NULL,"
        "        'call', kind, duration_s, number"
        "   FROM call_log"
        "  WHERE device_id = ?"
        "    AND number_key = ?"
        "    AND present = 1"
        "    AND (? IS NULL OR started_at < ?"
        "         OR (started_at = ? AND 'call:' || id < ?))"
        " ORDER BY ts DESC, id DESC LIMIT ?"
    )
    with _WRITE_LOCK:
        rows = conn.execute(
            sql, (device_id, thread_key, before_ts, before_ts, before_ts,
                  before_id, device_id, thread_key, before_ts, before_ts,
                  before_ts, before_id, device_id, thread_key, before_ts,
                  before_ts, before_ts, before_id, limit),
        ).fetchall()
    events = []
    for row in rows:
        event = _asdict(row)
        event["thread_key"] = thread_key
        # A call event also carries the contract key ``event_id``. A message
        # and a command keep their existing ``id`` only, so no consumer sees
        # a new field on an old row.
        if event.get("kind") == "call":
            event["event_id"] = event["id"]
        events.append(event)
    return events


def thread_exists(conn: sqlite3.Connection, device_id: str,
                  thread_key: str) -> bool:
    """True when the thread of that device has an inbound or outbound event."""
    if not isinstance(thread_key, str) or not thread_key:
        return False
    if not isinstance(device_id, str) or not device_id:
        return False
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT 1 WHERE EXISTS ("
            "  SELECT 1 FROM messages"
            "   WHERE device_id = ? AND thread_key = ?)"
            " OR EXISTS ("
            "  SELECT 1 FROM sms_commands"
            "   WHERE device_id = ? AND thread_key = ?)",
            (device_id, thread_key, device_id, thread_key),
        ).fetchone()
    return row is not None


def thread_meta(conn: sqlite3.Connection, device_id: str,
                thread_key: str) -> dict:
    """Display name and readable number for a thread of one device.

    The contact list of that device wins over the sender name carried by
    an SMS, which in turn wins over the bare number. The Mini App needs
    all three pieces so it can keep the header stable while a message
    list is paginated. The contacts of another device are never used.
    """
    display = phones.display_number(thread_key)
    contact_name = None
    sender_name = None
    if isinstance(device_id, str) and device_id:
        with _WRITE_LOCK:
            row = conn.execute(
                "SELECT MIN(name) AS name FROM device_contacts"
                " WHERE device_id = ? AND number_key = ?",
                (device_id, thread_key),
            ).fetchone()
            contact_name = row["name"] if row is not None else None
            if not contact_name:
                row = conn.execute(
                    "SELECT sender_name FROM messages"
                    " WHERE device_id = ? AND thread_key = ?"
                    "   AND sender_name IS NOT NULL"
                    "   AND TRIM(sender_name) != ''"
                    " ORDER BY received_at DESC, id DESC LIMIT 1",
                    (device_id, thread_key),
                ).fetchone()
                sender_name = row["sender_name"] if row is not None else None
    return {
        "thread_key": thread_key,
        "display": display,
        "name": contact_name or sender_name or display,
    }


def miniapp_contacts(conn: sqlite3.Connection, device_id: str, query: str = "",
                     limit: int = 50) -> list[dict]:
    """The contacts of ONE device, for the Mini App autocomplete.

    Unlike ``search_contacts`` (whose Telegram-side semantics are kept),
    this always escapes LIKE wildcards, so a query of ``%`` or ``_``
    matches those literal characters rather than every row.

    Only ``device_contacts`` of that device is read: the contacts of
    another device and the old global table are never mixed in.
    """
    if not isinstance(device_id, str) or not device_id:
        return []
    limit = _clamp_limit(limit, 50, 100)
    source = (
        "SELECT device_id, number, name, number_key FROM device_contacts"
        " WHERE device_id = ?"
    )
    params: tuple = (device_id,)
    if query and query.strip():
        like = _like_pattern(query)
        sql = (
            source +
            " AND (lower(name) LIKE ? ESCAPE '\\'"
            "    OR number LIKE ? ESCAPE '\\'"
            "    OR COALESCE(number_key, '') LIKE ? ESCAPE '\\')"
            " ORDER BY name, number LIMIT ?"
        )
        params = (device_id, like, like, like, limit)
    else:
        sql = source + " ORDER BY name, number LIMIT ?"
        params = (device_id, limit)
    with _WRITE_LOCK:
        rows = conn.execute(sql, params).fetchall()
    return [_asdict(row) for row in rows]


def mark_message_notified(conn: sqlite3.Connection, msg_id: str,
                          telegram_msg_id: str | int, now: int) -> None:
    """Record that the notification of a message reached Telegram.

    The notification lives in the chat for 24 hours by default, or only
    5 minutes when the Mini App already read the message: there is no
    reason to keep a notification the admin has seen.
    """
    value = str(telegram_msg_id)
    stamp = int(now)

    def fn() -> None:
        row = conn.execute(
            "SELECT seen_at FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"message not found: {msg_id}")
        delay = 300 if row["seen_at"] is not None else 86400
        conn.execute(
            "UPDATE messages SET telegram_msg_id = ?, telegram_sent_at = ?,"
            " telegram_delete_at = ?, updated_at = ? WHERE id = ?",
            (value, stamp, stamp + delay, stamp, msg_id),
        )

    _write_tx(conn, fn)


def mark_notification_deleted(conn: sqlite3.Connection, msg_id: str,
                              now: int) -> None:
    """Clear the Telegram pointer once the notification is gone."""
    stamp = int(now)
    _write_tx(conn, lambda: conn.execute(
        "UPDATE messages SET telegram_deleted_at = ?, telegram_msg_id = NULL,"
        " telegram_delete_at = NULL, updated_at = ? WHERE id = ?",
        (stamp, stamp, msg_id),
    ))


def due_notifications(conn: sqlite3.Connection, now: int,
                      limit: int = 20) -> list[dict]:
    """Notifications whose delete deadline has passed.

    ``telegram_sent_at`` is the marker of a message that was delivered as
    a notification. Rows forwarded by the old code, and imported history,
    carry no sent time and are never queued here: Telegram cannot delete
    a message older than 48 hours anyway.
    """
    limit = max(1, min(int(limit), 200))
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT id, device_id, telegram_msg_id, telegram_delete_at"
            " FROM messages"
            " WHERE telegram_delete_at IS NOT NULL"
            "   AND telegram_delete_at <= ?"
            "   AND telegram_msg_id IS NOT NULL"
            "   AND telegram_sent_at IS NOT NULL"
            "   AND telegram_deleted_at IS NULL"
            "   AND source != 'import'"
            " ORDER BY telegram_delete_at, id LIMIT ?",
            (int(now), limit),
        ).fetchall()
    return [_asdict(row) for row in rows]


def reschedule_notification(conn: sqlite3.Connection, msg_id: str,
                            now: int, delay: int = 300) -> None:
    """Push a failed delete back, so the reaper does not spin on it."""
    stamp = int(now)
    _write_tx(conn, lambda: conn.execute(
        "UPDATE messages SET telegram_delete_at = ?, updated_at = ?"
        " WHERE id = ? AND telegram_deleted_at IS NULL",
        (stamp + int(delay), stamp, msg_id),
    ))


# --- bot notes that are deleted later ------------------------------------
#
# Command notes ("SMS sent to ...") are ordinary bot messages, not SMS
# notifications: they have no row in ``messages``. They are tracked here
# so the reaper can drop them after a while instead of leaving them in
# the chat forever. Only the notes this relay creates are recorded.

TG_KIND_MAX_LEN = 32


def record_tg_message(conn: sqlite3.Connection, chat_id, message_id: int,
                      kind: str, delete_at: int, now: int) -> int:
    """Remember a bot message that must be deleted at ``delete_at``.

    Returns the row id, or 0 when the arguments cannot name a message:
    a bookkeeping failure must never break the send that already
    happened.
    """
    try:
        parsed = int(message_id)
    except (TypeError, ValueError):
        return 0
    if chat_id is None or str(chat_id) == "":
        return 0
    label = str(kind or "note")[:TG_KIND_MAX_LEN]

    def fn() -> int:
        cur = conn.execute(
            "INSERT INTO tg_messages"
            " (chat_id, message_id, kind, delete_at, deleted_at, created_at)"
            " VALUES (?, ?, ?, ?, NULL, ?)",
            (str(chat_id), parsed, label, int(delete_at), int(now)),
        )
        return int(cur.lastrowid or 0)

    return int(_write_tx(conn, fn) or 0)


def due_tg_messages(conn: sqlite3.Connection, now: int,
                    limit: int = 20) -> list[dict]:
    """Bot notes whose deletion deadline has passed."""
    limit = max(1, min(int(limit), 200))
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT id, chat_id, message_id, kind, delete_at"
            " FROM tg_messages"
            " WHERE delete_at <= ? AND deleted_at IS NULL"
            " ORDER BY delete_at, id LIMIT ?",
            (int(now), limit),
        ).fetchall()
    return [_asdict(row) for row in rows]


def mark_tg_message_deleted(conn: sqlite3.Connection, row_id: int,
                            now: int) -> None:
    """Mark a bot note as gone; the row stays as a tombstone."""
    stamp = int(now)
    _write_tx(conn, lambda: conn.execute(
        "UPDATE tg_messages SET deleted_at = ?"
        " WHERE id = ? AND deleted_at IS NULL",
        (stamp, int(row_id)),
    ))


def reschedule_tg_message(conn: sqlite3.Connection, row_id: int, now: int,
                          delay: int = 300) -> None:
    """Push a failed delete back, so the reaper does not spin on it."""
    stamp = int(now)
    _write_tx(conn, lambda: conn.execute(
        "UPDATE tg_messages SET delete_at = ?"
        " WHERE id = ? AND deleted_at IS NULL",
        (stamp + int(delay), int(row_id)),
    ))


def mark_thread_read(conn: sqlite3.Connection, device_id: str,
                     thread_key: str, until_ts: int) -> int:
    """Advance the read cursor of a thread, never moving it backwards.

    The marker belongs to the device: reading a thread of one phone never
    marks the same conversation of another phone as read. Reading a
    message also schedules the deletion of its Telegram notification:
    what the admin has seen in the Mini App does not need to sit in the
    chat for another day.
    """
    if not isinstance(device_id, str) or not device_id:
        return 0
    now = _now()
    until = int(until_ts)

    def fn() -> int:
        conn.execute(
            "INSERT INTO thread_reads_scoped"
            " (device_id, thread_key, last_read_at, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(device_id, thread_key) DO UPDATE SET"
            "  last_read_at = MAX(thread_reads_scoped.last_read_at,"
            "                     excluded.last_read_at),"
            "  updated_at = excluded.updated_at",
            (device_id, thread_key, until, now),
        )
        conn.execute(
            "UPDATE messages SET seen_at = COALESCE(seen_at, ?)"
            " WHERE device_id = ? AND thread_key = ?"
            "   AND received_at <= ? AND seen_at IS NULL",
            (now, device_id, thread_key, until),
        )
        conn.execute(
            "UPDATE messages SET telegram_delete_at = MIN("
            "  COALESCE(telegram_delete_at, ?), ?)"
            " WHERE device_id = ? AND thread_key = ?"
            "   AND received_at <= ?"
            "   AND telegram_msg_id IS NOT NULL"
            "   AND telegram_sent_at IS NOT NULL"
            "   AND telegram_deleted_at IS NULL",
            (now + 300, now + 300, device_id, thread_key, until),
        )
        row = conn.execute(
            "SELECT last_read_at FROM thread_reads_scoped"
            " WHERE device_id = ? AND thread_key = ?",
            (device_id, thread_key),
        ).fetchone()
        return int(row["last_read_at"])

    return _write_tx(conn, fn)


def command_history(conn: sqlite3.Connection, thread_key: str,
                    limit: int = 50,
                    device_id: str | None = None) -> list[dict]:
    """The outbound commands of a thread.

    ``device_id`` keeps the answer inside one device; without it the
    history of every device is returned.
    """
    limit = _clamp_limit(limit, 50)
    sql = "SELECT * FROM sms_commands WHERE thread_key = ?"
    params: list = [thread_key]
    if device_id is not None:
        sql += " AND device_id = ?"
        params.append(device_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with _WRITE_LOCK:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_asdict(row) for row in rows]


def device_seen(conn: sqlite3.Connection, device_id: str,
                version: str | None, sending: bool | None,
                sims_json: str | None, now: int) -> None:
    """Record a phone heartbeat; missing fields keep their old values."""
    sending_enabled = None if sending is None else (1 if sending else 0)

    def fn() -> None:
        conn.execute(
            "INSERT INTO device_status"
            " (device_id, last_seen_at, app_version, sending_enabled,"
            "  sims_json, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET"
            "  last_seen_at = excluded.last_seen_at,"
            "  app_version = COALESCE(excluded.app_version,"
            "                         device_status.app_version),"
            "  sending_enabled = COALESCE(excluded.sending_enabled,"
            "                             device_status.sending_enabled),"
            "  sims_json = COALESCE(excluded.sims_json,"
            "                       device_status.sims_json),"
            "  updated_at = excluded.updated_at",
            (device_id, int(now), version, sending_enabled, sims_json,
             int(now)),
        )

    _write_tx(conn, fn)


def device_status(conn: sqlite3.Connection) -> dict | None:
    """The most recently seen phone, or None before the first poll."""
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM device_status"
            " ORDER BY last_seen_at DESC, updated_at DESC LIMIT 1"
        ).fetchone()
    return _asdict(row) if row is not None else None


# --- devices, pairing and per-device state -------------------------------
#
# Everything below is scoped by device. A device only ever reads or writes
# its own rows, so one phone can never see or change another phone's data.


def token_hash(value: str | None) -> str | None:
    """SHA-256 hex digest of a secret (a token, a scan code, a claim secret).

    Only the digest is stored, so a leaked database cannot be replayed.
    """
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def device_by_id(conn: sqlite3.Connection, device_id: str) -> dict | None:
    if not isinstance(device_id, str) or not device_id:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM devices WHERE id = ? LIMIT 1", (device_id,)
        ).fetchone()
    return _asdict(row) if row is not None else None


def device_by_token(conn: sqlite3.Connection, token: str | None) -> dict | None:
    """The active device that owns a bearer token, or None."""
    digest = token_hash(token)
    if digest is None:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM devices WHERE token_hash = ?"
            " AND revoked_at IS NULL AND archived = 0 LIMIT 1",
            (digest,),
        ).fetchone()
    return _asdict(row) if row is not None else None


def list_devices(conn: sqlite3.Connection) -> list[dict]:
    """Every device, archived ones included."""
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT * FROM devices"
            " ORDER BY archived, name COLLATE NOCASE, id"
        ).fetchall()
    return [_asdict(row) for row in rows]


def active_devices(conn: sqlite3.Connection) -> list[dict]:
    """The paired devices: neither revoked nor archived."""
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT * FROM devices WHERE archived = 0 AND revoked_at IS NULL"
            " ORDER BY created_at, id"
        ).fetchall()
    return [_asdict(row) for row in rows]


def device_is_active(conn: sqlite3.Connection, device_id: str) -> bool:
    return _device_active_row(conn, device_id)


def _device_active_row(conn: sqlite3.Connection, device_id: str) -> bool:
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT 1 FROM devices WHERE id = ? AND archived = 0"
            " AND revoked_at IS NULL LIMIT 1", (device_id,)
        ).fetchone()
    return row is not None


def latest_device(conn: sqlite3.Connection) -> dict | None:
    """The most recently seen device, or None before the first poll."""
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM devices WHERE archived = 0"
            " ORDER BY COALESCE(last_seen_at, 0) DESC, updated_at DESC LIMIT 1"
        ).fetchone()
    return _asdict(row) if row is not None else None


def touch_device(conn: sqlite3.Connection, device_id: str, now: int) -> None:
    """Record that a device was seen, at most every 30 seconds.

    A chatty client must not turn every request into a write, so the
    heartbeat is only written when the last one is old enough.
    """
    stamp = int(now)
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT last_seen_at FROM devices WHERE id = ? LIMIT 1",
            (device_id,),
        ).fetchone()
        if row is None:
            return
        last_seen = row["last_seen_at"]
        if last_seen is not None and last_seen > stamp - DEVICE_TOUCH_INTERVAL_S:
            return

    def fn() -> None:
        conn.execute(
            "UPDATE devices SET last_seen_at = ? WHERE id = ?"
            " AND (last_seen_at IS NULL OR last_seen_at <= ?)",
            (stamp, device_id, stamp - DEVICE_TOUCH_INTERVAL_S),
        )

    _write_tx(conn, fn)


def device_heartbeat(conn: sqlite3.Connection, device_id: str,
                     version: str | None, sending: bool | None,
                     sims_json: str | None, now: int) -> None:
    """Record a command poll; missing fields keep their old values."""
    sending_enabled = None if sending is None else (1 if sending else 0)

    def fn() -> None:
        conn.execute(
            "UPDATE devices SET last_seen_at = ?,"
            " app_version = COALESCE(?, app_version),"
            " sending_enabled = COALESCE(?, sending_enabled),"
            " sims_json = COALESCE(?, sims_json),"
            " updated_at = ? WHERE id = ?",
            (int(now), version, sending_enabled, sims_json, int(now), device_id),
        )

    _write_tx(conn, fn)


def activate_device(conn: sqlite3.Connection, device_id: str, name: str,
                    platform: str, app_version: str | None,
                    now: int) -> dict:
    """Create a device row, or reactivate a revoked one.

    The token is cleared on purpose: a re-paired device gets a new token
    and the old one has to stop working immediately. The archived flag is
    never touched, so the archived device of the previous phone stays
    read-only.
    """
    def fn() -> dict:
        conn.execute(
            "INSERT INTO devices"
            " (id, name, platform, app_version, token_hash, token_prefix,"
            "  created_at, paired_at, last_seen_at, sending_enabled,"
            "  sims_json, revoked_at, archived, updated_at)"
            " VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, NULL,"
            "         NULL, 0, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  name = excluded.name,"
            "  platform = excluded.platform,"
            "  app_version = excluded.app_version,"
            "  token_hash = NULL,"
            "  token_prefix = NULL,"
            "  revoked_at = NULL,"
            "  updated_at = excluded.updated_at",
            (device_id, name, platform, app_version, int(now), int(now)),
        )
        row = conn.execute(
            "SELECT * FROM devices WHERE id = ? LIMIT 1", (device_id,)
        ).fetchone()
        return _asdict(row)

    return _write_tx(conn, fn)


def set_device_token(conn: sqlite3.Connection, device_id: str, token_digest: str,
                     token_prefix: str, now: int) -> bool:
    """Store the token of a device (hashed) and mark it as paired."""
    def fn() -> bool:
        cur = conn.execute(
            "UPDATE devices SET token_hash = ?, token_prefix = ?,"
            " paired_at = ?, revoked_at = NULL, archived = 0, updated_at = ?"
            " WHERE id = ?",
            (token_digest, token_prefix, int(now), int(now), device_id),
        )
        return cur.rowcount > 0

    return _write_tx(conn, fn)


def rename_device(conn: sqlite3.Connection, device_id: str, name: str,
                  now: int) -> bool:
    def fn() -> bool:
        cur = conn.execute(
            "UPDATE devices SET name = ?, updated_at = ? WHERE id = ?",
            (name, int(now), device_id),
        )
        return cur.rowcount > 0

    return _write_tx(conn, fn)


def revoke_device(conn: sqlite3.Connection, device_id: str, now: int) -> bool:
    """Revoke a device: its token dies and its pending commands fail.

    The archived legacy device is refused: it was never paired, so there
    is nothing to revoke.

    A revoked device cannot send, so it is no longer the default device.
    The empty value is the way this codebase stores "not set".
    """
    def fn() -> bool:
        cur = conn.execute(
            "UPDATE devices SET revoked_at = ?, token_hash = NULL,"
            " token_prefix = NULL, updated_at = ?"
            " WHERE id = ? AND revoked_at IS NULL AND archived = 0",
            (int(now), int(now), device_id),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE settings SET value = '' WHERE key = 'default_device_id'"
            " AND value = ?", (device_id,),
        )
        return True

    return _write_tx(conn, fn)


def device_summaries(conn: sqlite3.Connection) -> list[dict]:
    """Every device with the state the admin needs to see.

    The token of a device is never part of this: only its hash is stored
    at all, and the digest must not leave the database.
    """
    now = _now()
    summaries = []
    for row in list_devices(conn):
        last_seen = row.get("last_seen_at")
        summaries.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "platform": row.get("platform"),
            "app_version": row.get("app_version"),
            "archived": bool(row.get("archived")),
            "revoked": row.get("revoked_at") is not None,
            "online": bool(last_seen and last_seen > now - 180),
            "last_seen": last_seen,
            "sending": (None if row.get("sending_enabled") is None
                        else bool(row.get("sending_enabled"))),
            "unread": device_unread_count(conn, row["id"]),
            "pending_commands": len(
                pending_commands(conn, row["id"], limit=100)),
        })
    return summaries


def device_unread_count(conn: sqlite3.Connection, device_id: str) -> int:
    """Inbound messages of a device that are newer than its read marker."""
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages m"
            " WHERE m.device_id = ?"
            " AND m.received_at > COALESCE((SELECT r.last_read_at"
            "   FROM thread_reads_scoped r"
            "   WHERE r.device_id = m.device_id"
            "     AND r.thread_key = m.thread_key), 0)",
            (device_id,),
        ).fetchone()
    return int(row["n"])


def device_last_preview(conn: sqlite3.Connection, device_id: str,
                        limit: int = 80) -> str | None:
    """A short preview of the newest message of a device."""
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT body FROM messages WHERE device_id = ?"
            " ORDER BY received_at DESC, id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
    if row is None:
        return None
    body = " ".join((row["body"] or "").split())
    return body[:limit]


def replace_device_contacts(conn: sqlite3.Connection, device_id: str,
                             contacts: list[dict]) -> int:
    """Replace the contact list of one device.

    Only the rows of that device are touched; the other devices keep
    theirs.
    """
    def fn() -> int:
        now = _now()
        conn.execute("DELETE FROM device_contacts WHERE device_id = ?",
                     (device_id,))
        stored = 0
        for contact in contacts:
            number = (contact.get("number") or "").strip()
            name = (contact.get("name") or "").strip()
            if not number or not name:
                continue
            number_key = phones.thread_key(number)
            conn.execute(
                "INSERT OR REPLACE INTO device_contacts"
                " (device_id, number, name, number_key, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (device_id, number, name, number_key, now),
            )
            stored += 1
        return stored

    return _write_tx(conn, fn)


def list_device_contacts(conn: sqlite3.Connection, device_id: str | None = None,
                         limit: int = 200) -> list[dict]:
    with _WRITE_LOCK:
        if device_id is None:
            rows = conn.execute(
                "SELECT device_id, number, name, number_key FROM device_contacts"
                " ORDER BY device_id, name, number LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT device_id, number, name, number_key FROM device_contacts"
                " WHERE device_id = ? ORDER BY name, number LIMIT ?",
                (device_id, limit),
            ).fetchall()
    return [_asdict(row) for row in rows]


def device_contact_count(conn: sqlite3.Connection,
                         device_id: str | None = None) -> int:
    with _WRITE_LOCK:
        if device_id is None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM device_contacts").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM device_contacts WHERE device_id = ?",
                (device_id,)).fetchone()
    return int(row["n"])


def search_device_contacts(conn: sqlite3.Connection, device_id: str,
                           query: str, limit: int = 20) -> list[dict]:
    """The contacts of one device whose name or number matches."""
    like = f"%{(query or '').lower()}%"
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT number, name FROM device_contacts"
            " WHERE device_id = ? AND (lower(name) LIKE ? OR number LIKE ?)"
            " ORDER BY name LIMIT ?",
            (device_id, like, like, int(limit)),
        ).fetchall()
    return [_asdict(row) for row in rows]


def list_contacts_for_device(conn: sqlite3.Connection, device_id: str | None,
                             limit: int = 2000) -> list[dict]:
    """The contacts a device may address.

    The per-device list comes first; the contacts of the old global table
    are still offered, so the history of the previous phone stays usable.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    if device_id:
        for row in list_device_contacts(conn, device_id, limit=limit):
            rows.append({"number": row["number"], "name": row["name"]})
            seen.add(row["number"])
    for row in list_contacts(conn, limit=limit):
        if row["number"] in seen:
            continue
        rows.append({"number": row["number"], "name": row["name"]})
        seen.add(row["number"])
    return rows[:limit]


def search_contacts_for_device(conn: sqlite3.Connection, device_id: str | None,
                               query: str, limit: int = 20) -> list[dict]:
    """The contacts of a device (plus the old global ones) that match."""
    rows: list[dict] = []
    seen: set[str] = set()
    if device_id:
        for row in search_device_contacts(conn, device_id, query, limit=limit):
            rows.append(row)
            seen.add(row["number"])
    for row in search_contacts(conn, query, limit=limit):
        if row["number"] in seen:
            continue
        rows.append(row)
        seen.add(row["number"])
    return rows[:limit]


def contact_count_for_device(conn: sqlite3.Connection,
                             device_id: str | None) -> int:
    """How many contacts a device may address."""
    with _WRITE_LOCK:
        if device_id:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ("
                "  SELECT number FROM device_contacts WHERE device_id = ?"
                "  UNION"
                "  SELECT number FROM contacts)", (device_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM contacts").fetchone()
    return int(row["n"])


def device_contact_name_for_key(conn: sqlite3.Connection, device_id: str,
                                thread_key: str) -> str | None:
    """The contact name a device knows for a thread key."""
    if not isinstance(thread_key, str) or not thread_key:
        return None
    if not device_id:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT MIN(name) AS name FROM device_contacts"
            " WHERE device_id = ? AND number_key = ?",
            (device_id, thread_key),
        ).fetchone()
    return row["name"] if row is not None else None


def resolve_device_contact(conn: sqlite3.Connection, device_id: str | None,
                           text: str) -> dict | None:
    """The contact of one device whose name matches (exactly or partly)."""
    if not device_id:
        return None
    value = (text or "").strip()
    if not value:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT number, name FROM device_contacts"
            " WHERE device_id = ? AND lower(name) = ? LIMIT 1",
            (device_id, value.lower()),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT number, name FROM device_contacts"
                " WHERE device_id = ? AND lower(name) LIKE ? LIMIT 1",
                (device_id, f"%{value.lower()}%"),
            ).fetchone()
    if row is None:
        return None
    return {"number": row["number"], "name": row["name"]}


def resolve_command_device(conn: sqlite3.Connection,
                           requested: str | None = None) -> str | None:
    """The device a command should be routed to.

    An explicit device wins when it is active. Otherwise the configured
    default is used, and when exactly one device is active it is used as
    well. ``None`` means no device can be picked.
    """
    if requested and _device_active_row(conn, requested):
        return requested
    default_id = settings_get(conn, "default_device_id")
    if default_id and _device_active_row(conn, default_id):
        return default_id
    active = active_devices(conn)
    if len(active) == 1:
        return active[0]["id"]
    return None


def fail_device_commands(conn: sqlite3.Connection, device_id: str,
                          result: str = "device revoked",
                          now: int | None = None) -> int:
    """Fail the pending commands of a device that cannot run them."""
    stamp = int(now) if now is not None else _now()

    def fn() -> int:
        cur = conn.execute(
            "UPDATE sms_commands SET status = 'failed', result = ?,"
            " updated_at = ?, finished_at = ?"
            " WHERE device_id = ? AND status = 'pending'",
            (result, stamp, stamp, device_id),
        )
        return cur.rowcount

    return _write_tx(conn, fn)


# --- pairing --------------------------------------------------------------


def pairing_create(conn: sqlite3.Connection, pairing_id: str,
                    scan_code_hash: str, claim_secret_hash: str,
                    device_id: str, device_name: str, platform: str,
                    app_version: str | None, created_at: int,
                    expires_at: int) -> None:
    def fn() -> None:
        # A phone must not have two live pairing codes. The admin may
        # still have an old QR on screen; starting a new code supersedes
        # it so that an old scan cannot approve a device after the phone
        # has moved on to a newer code.
        conn.execute(
            "UPDATE pairings SET status = 'cancelled'"
            " WHERE device_id = ? AND status IN ('pending', 'approved')",
            (device_id,),
        )
        conn.execute(
            "INSERT INTO pairings"
            " (id, scan_code_hash, claim_secret_hash, device_id,"
            "  device_name, platform, app_version, created_at, expires_at,"
            "  approved_at, approved_by, claimed_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 'pending')",
            (pairing_id, scan_code_hash, claim_secret_hash, device_id,
             device_name, platform, app_version, int(created_at),
             int(expires_at)),
        )

    _write_tx(conn, fn)


def pairing_by_id(conn: sqlite3.Connection,
                  pairing_id: str) -> dict | None:
    if not isinstance(pairing_id, str) or not pairing_id:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM pairings WHERE id = ? LIMIT 1", (pairing_id,)
        ).fetchone()
    return _asdict(row) if row is not None else None


def pairing_by_scan_code(conn: sqlite3.Connection, scan_code: str,
                         now: int) -> list[dict]:
    """Live pairings whose scan code matches ``scan_code``.

    The digest is computed once and compared with ``hmac.compare_digest``
    against every open pairing. Only pending and approved pairings that
    have not expired are candidates: a cancelled, expired or claimed code
    must not be resolved through this path. A short code can collide with
    another live pairing, so the caller gets the whole list and decides.
    """
    digest = token_hash(scan_code)
    if not digest:
        return []
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT * FROM pairings"
            " WHERE status IN ('pending', 'approved') AND expires_at > ?",
            (int(now),)
        ).fetchall()
    matches = []
    for row in rows:
        stored = row["scan_code_hash"]
        if stored and hmac.compare_digest(digest, stored):
            matches.append(_asdict(row))
    return matches


def pairing_pending_count(conn: sqlite3.Connection, now: int) -> int:
    """Pairings that are still waiting for an approval."""
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pairings"
            " WHERE status = 'pending' AND expires_at > ?", (int(now),)
        ).fetchone()
    return int(row["n"])


def pairing_approve(conn: sqlite3.Connection, pairing_id: str, approved_at: int,
                    approved_by: int | None, device_name: str | None,
                    approved_by_name: str | None = None,
                    approved_by_username: str | None = None) -> bool:
    """Mark a pairing as approved; the device token comes later.

    The admin's display name and username are kept so the phone can show
    who approved the pairing before it accepts the token. They are
    optional, and only ever the values Telegram handed the Mini App.
    """
    def fn() -> bool:
        cur = conn.execute(
            "UPDATE pairings SET status = 'approved', approved_at = ?,"
            " approved_by = ?,"
            " approved_by_name = COALESCE(?, approved_by_name),"
            " approved_by_username = COALESCE(?, approved_by_username),"
            " device_name = COALESCE(?, device_name)"
            " WHERE id = ? AND status = 'pending'",
            (int(approved_at), approved_by, approved_by_name,
             approved_by_username, device_name, pairing_id),
        )
        return cur.rowcount > 0

    return _write_tx(conn, fn)


def pairing_expire(conn: sqlite3.Connection, pairing_id: str) -> bool:
    def fn() -> bool:
        cur = conn.execute(
            "UPDATE pairings SET status = 'expired'"
            " WHERE id = ? AND status IN ('pending', 'approved')",
            (pairing_id,),
        )
        return cur.rowcount > 0

    return _write_tx(conn, fn)


def pairing_cancel(conn: sqlite3.Connection, pairing_id: str) -> bool:
    def fn() -> bool:
        cur = conn.execute(
            "UPDATE pairings SET status = 'cancelled'"
            " WHERE id = ? AND status IN ('pending', 'approved')",
            (pairing_id,),
        )
        return cur.rowcount > 0

    return _write_tx(conn, fn)


def claim_pairing(conn: sqlite3.Connection, pairing_id: str,
                  token_digest: str, token_prefix: str,
                  now: int) -> dict | None:
    """Claim an approved pairing and hand the device its token.

    The token is stored hashed, so it is readable exactly once, when the
    device claims it. Returns the device row, or None when the pairing
    was not claimable.
    """
    def fn() -> dict | None:
        row = conn.execute(
            "SELECT * FROM pairings WHERE id = ? LIMIT 1", (pairing_id,)
        ).fetchone()
        if row is None or row["status"] != "approved":
            return None
        conn.execute(
            "UPDATE pairings SET status = 'claimed', claimed_at = ?"
            " WHERE id = ?", (int(now), pairing_id))
        conn.execute(
            "UPDATE devices SET token_hash = ?, token_prefix = ?,"
            " paired_at = ?, revoked_at = NULL, archived = 0, updated_at = ?"
            " WHERE id = ?",
            (token_digest, token_prefix, int(now), int(now),
             row["device_id"]),
        )
        device = conn.execute(
            "SELECT * FROM devices WHERE id = ? LIMIT 1", (row["device_id"],)
        ).fetchone()
        return _asdict(device) if device is not None else None

    return _write_tx(conn, fn)


def pairing_cleanup(conn: sqlite3.Connection, now: int,
                    keep_seconds: int = 86400) -> int:
    """Drop finished pairings and the devices that were never claimed.

    An approved but unclaimed device is removed once its pairing expired:
    it would otherwise sit in the device list without a token.
    """
    cutoff = int(now) - int(keep_seconds)

    def fn() -> int:
        expired = conn.execute(
            "UPDATE pairings SET status = 'expired'"
            " WHERE status IN ('pending', 'approved') AND expires_at <= ?",
            (int(now),),
        ).rowcount
        # A device that was approved but never claimed is useless: it
        # would sit in the device list without a token. It is only removed
        # when no other pairing of that device is still open.
        removed = conn.execute(
            "DELETE FROM devices WHERE id IN ("
            "  SELECT device_id FROM pairings"
            "   WHERE status IN ('expired', 'cancelled')"
            "     AND expires_at <= ?"
            "     AND device_id NOT IN (SELECT id FROM pairings"
            "                             WHERE status IN ('pending','approved')))"
            " AND token_hash IS NULL AND archived = 0 AND paired_at IS NULL",
            (int(now),),
        ).rowcount
        conn.execute(
            "DELETE FROM pairings WHERE status IN ('claimed', 'expired',"
            " 'cancelled') AND expires_at <= ?",
            (cutoff,),
        )
        return expired + removed

    return _write_tx(conn, fn)


# --- calls ----------------------------------------------------------------

# An answer, a reject and a hang up only matter while the phone rings, so
# their action dies quickly. Everything else (mute, hold, DTMF, dial) may
# wait for a phone that is in a tunnel for a few minutes.
CALL_ACTION_TTL_S = {"answer": 20, "reject": 20, "hangup": 20}
CALL_ACTION_DEFAULT_TTL_S = 300
MAX_CALL_NUMBER_LEN = 64
MAX_CALL_NAME_LEN = 96
MAX_CALL_ACCOUNT_LEN = 64
MAX_CALL_SIM_LABEL_LEN = 32
# 2100-01-01 in epoch seconds: far past any real call, and small enough that
# SQLite can always store the value (a wild stamp is skipped, never a 500).
MAX_CALL_STARTED_AT = 4102444800


def call_log_id(device_id: str, item: dict) -> str | None:
    """The deterministic id of one call-log row.

    The phone's call-log row id is the strongest identity: a replayed
    batch yields the same ids, so the upsert is a no-op. A row without a
    provider id falls back to its number and timestamp, which the call
    log never reuses for two different calls.

    The device is part of the basis, because two phones may reuse the
    same provider row id.
    """
    provider = item.get("provider_id")
    basis = None
    if provider is not None and str(provider).strip() != "":
        basis = "pid:" + str(provider).strip()
    else:
        started_at = item.get("started_at")
        if started_at is None or isinstance(started_at, bool):
            return None
        try:
            stamp = int(started_at)
        except (TypeError, ValueError):
            return None
        number = str(item.get("number") or "").strip()
        basis = "n:" + number + "\x00" + str(stamp)
    digest = hashlib.sha256(
        "\x00".join((str(device_id or ""), basis)).encode("utf-8")
    ).hexdigest()[:32]
    return "call-" + digest


def _call_content_equal(existing: sqlite3.Row, item: dict) -> bool:
    """True when a re-synced call-log row would not change anything.

    ``content_hash`` from the phone wins when it is present: the phone
    knows its own row better than we can reconstruct it. Without one, the
    stored fields are compared, so a replayed batch is still a no-op.

    The read marker only ever moves forward, so an already-read row is
    equal to an unread upload of the same call.
    """
    content_hash = item.get("content_hash")
    if content_hash is not None:
        return existing["content_hash"] == content_hash
    stored_read = int(existing["is_read"] or 0)
    incoming_read = int(item.get("is_read") or 0)
    if stored_read != max(stored_read, incoming_read):
        return False
    for field in ("number", "number_key", "cached_name", "kind", "raw_type",
                  "started_at", "duration_s", "sub_id", "phone_account"):
        stored = existing[field] if existing[field] is not None else None
        if stored != item.get(field):
            return False
    return True


def _call_row_values(device_id: str, item) -> dict | None:
    """Normalise one call-log item, or None when it cannot be stored.

    This never raises: the sync endpoint feeds it rows from the phone, and
    one unusable row must be skipped, not abort the transaction that holds
    the rest of the batch. Everything that could raise on a hostile value
    is inside the ``try``, and the caller only touches SQLite with the
    values returned here.
    """
    if not isinstance(item, dict):
        return None
    try:
        kind = item.get("kind")
        if kind not in CALL_KINDS:
            return None
        started_raw = item.get("started_at")
        if started_raw is None or isinstance(started_raw, bool):
            return None
        started_at = int(started_raw)
        # SQLite cannot store an arbitrary Python int, and no call of this
        # relay is placed after 2100: a wild stamp is a bad row, not a 500.
        if started_at < 0 or started_at > MAX_CALL_STARTED_AT:
            return None
        call_id = call_log_id(device_id, item)
        if call_id is None:  # pragma: no cover - guarded above
            return None
        number = str(item.get("number") or "").strip()[:MAX_CALL_NUMBER_LEN]
        number_key = item.get("number_key")
        if number_key is None:
            number_key = phones.thread_key(number) or None
        if not isinstance(number_key, str) or not number_key:
            number_key = None
        cached_name = item.get("cached_name")
        if cached_name is not None:
            cached_name = str(cached_name).strip()[:MAX_CALL_NAME_LEN] or None
        raw_type = item.get("raw_type")
        if raw_type is not None and (isinstance(raw_type, bool)
                                     or not isinstance(raw_type, int)
                                     or not 0 <= raw_type <= 2 ** 31 - 1):
            raw_type = None
        sub_id = item.get("sub_id")
        if (isinstance(sub_id, bool) or not isinstance(sub_id, int)
                or not 0 <= sub_id <= 99):
            sub_id = 0
        account = item.get("phone_account")
        if account is not None:
            account = str(account).strip()[:MAX_CALL_ACCOUNT_LEN] or None
        duration = item.get("duration_s")
        if (isinstance(duration, bool) or not isinstance(duration, int)
                or not 0 <= duration <= 604800):
            duration = 0
        provider_id = item.get("provider_id")
        if provider_id is not None:
            provider_id = str(provider_id)[:64]
        content_hash = item.get("content_hash")
        if content_hash is not None:
            content_hash = str(content_hash)[:128]
        return {
            "id": call_id, "provider_id": provider_id, "number": number,
            "number_key": number_key, "cached_name": cached_name,
            "kind": kind, "raw_type": raw_type, "started_at": started_at,
            "duration_s": duration, "sub_id": sub_id,
            "phone_account": account,
            "is_read": 1 if item.get("is_read") else 0,
            "content_hash": content_hash,
        }
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def record_call_items(conn: sqlite3.Connection, device_id: str,
                      items: list[dict]) -> dict:
    """Upsert a batch of call-log rows of ONE device.

    A row is inserted when its id is unknown and updated only when its
    ``content_hash`` differs, so a replayed or a rolling re-read batch
    stays a no-op. The counters tell the caller whether anything changed;
    this helper never bumps the revision itself.

    The read marker never moves backwards: a Mini App read that the phone
    has not seen yet must not be undone by the next sync.

    One unusable row (a non-dict, an unknown kind, a value SQLite could
    not bind) is counted as skipped and the rest of the batch is still
    committed: ``_call_row_values`` does all normalisation outside SQL.
    """
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    def fn() -> dict:
        now = _now()
        for item in items:
            row = _call_row_values(device_id, item)
            if row is None:
                counts["skipped"] += 1
                continue
            call_id = row["id"]
            existing = conn.execute(
                "SELECT * FROM call_log WHERE id = ? AND device_id = ?",
                (call_id, device_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO call_log"
                    " (id, device_id, provider_id, number, number_key,"
                    "  cached_name, kind, raw_type, started_at, duration_s,"
                    "  sub_id, phone_account, is_read, content_hash, present,"
                    "  created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,"
                    "         ?, ?)",
                    (call_id, device_id, row["provider_id"], row["number"],
                     row["number_key"], row["cached_name"], row["kind"],
                     row["raw_type"], row["started_at"], row["duration_s"],
                     row["sub_id"], row["phone_account"], row["is_read"],
                     row["content_hash"], now, now),
                )
                counts["inserted"] += 1
                continue
            if _call_content_equal(existing, row):
                counts["unchanged"] += 1
                continue
            conn.execute(
                "UPDATE call_log SET provider_id = ?, number = ?,"
                " number_key = ?, cached_name = ?, kind = ?, raw_type = ?,"
                " started_at = ?, duration_s = ?, sub_id = ?,"
                " phone_account = ?, is_read = MAX(is_read, ?),"
                " content_hash = ?, present = 1, updated_at = ?"
                " WHERE id = ?",
                (row["provider_id"], row["number"], row["number_key"],
                 row["cached_name"], row["kind"], row["raw_type"],
                 row["started_at"], row["duration_s"], row["sub_id"],
                 row["phone_account"], row["is_read"], row["content_hash"],
                 now, call_id),
            )
            counts["updated"] += 1
        return dict(counts)

    return _write_tx(conn, fn)


def page_calls(conn: sqlite3.Connection, device_id: str, limit: int = 100,
               before_ts: int | None = None, before_id: str | None = None,
               query: str | None = None) -> list[dict]:
    """One page of the call log of ONE device, newest first.

    The order is ``started_at DESC, id DESC`` and the cursor has the same
    two halves as the thread timeline: ``before_ts`` alone is the strict
    timestamp cursor, while ``before_ts`` plus ``before_id`` continues
    inside a second, so two calls of the same second are never skipped.

    Names are not resolved here: the caller uses ``call_name_for_key``
    with the same device, so nothing of another device can leak in.
    """
    if not isinstance(device_id, str) or not device_id:
        return []
    limit = _clamp_limit(limit, 100, CHAT_MAX_LIMIT)
    conditions = ["device_id = ?", "present = 1"]
    params: list = [device_id]
    if before_ts is not None:
        if before_id is None:
            conditions.append("started_at < ?")
            params.append(int(before_ts))
        else:
            conditions.append(
                "(started_at < ? OR (started_at = ? AND id < ?))")
            params.extend([int(before_ts), int(before_ts), str(before_id)])
    if query and query.strip():
        number_like, local_like, text_like = _search_likes(query)
        conditions.append(
            "("
            " COALESCE(number_key, '') LIKE ? ESCAPE '\\'"
            " OR COALESCE(number_key, '') LIKE ? ESCAPE '\\'"
            " OR number LIKE ? ESCAPE '\\'"
            " OR lower(COALESCE(cached_name, '')) LIKE ? ESCAPE '\\'"
            " OR EXISTS (SELECT 1 FROM device_contacts d"
            "            WHERE d.device_id = call_log.device_id"
            "              AND d.number_key = call_log.number_key"
            "              AND lower(d.name) LIKE ? ESCAPE '\\')"
            ")"
        )
        params.extend([number_like, local_like, local_like, text_like,
                       text_like])
    sql = ("SELECT * FROM call_log WHERE " + " AND ".join(conditions)
           + " ORDER BY started_at DESC, id DESC LIMIT ?")
    params.append(limit)
    with _WRITE_LOCK:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_asdict(row) for row in rows]


def call_name_for_key(conn: sqlite3.Connection, device_id: str,
                      number_key: str | None) -> str | None:
    """The contact name ONE device knows for a call number key."""
    if not isinstance(number_key, str) or not number_key:
        return None
    if not isinstance(device_id, str) or not device_id:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT MIN(name) AS name FROM device_contacts"
            " WHERE device_id = ? AND number_key = ?",
            (device_id, number_key),
        ).fetchone()
    return row["name"] if row is not None else None


def count_missed_calls(conn: sqlite3.Connection, device_id: str) -> int:
    """The unread missed calls of one device."""
    if not isinstance(device_id, str) or not device_id:
        return 0
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM call_log"
            " WHERE device_id = ? AND kind = 'missed' AND is_read = 0"
            "   AND present = 1",
            (device_id,),
        ).fetchone()
    return int(row["n"])


def mark_calls_read(conn: sqlite3.Connection, device_id: str,
                    until_ts: int) -> int:
    """Mark every call of one device up to ``until_ts`` as read."""
    if not isinstance(device_id, str) or not device_id:
        return 0

    def fn() -> int:
        return conn.execute(
            "UPDATE call_log SET is_read = 1, updated_at = ?"
            " WHERE device_id = ? AND is_read = 0 AND present = 1"
            "   AND started_at <= ?",
            (_now(), device_id, int(until_ts)),
        ).rowcount

    return _write_tx(conn, fn)


def call_session_get(conn: sqlite3.Connection,
                     device_id: str) -> dict | None:
    """The live call of one device, or None when it is idle."""
    if not isinstance(device_id, str) or not device_id:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM call_sessions WHERE device_id = ? LIMIT 1",
            (device_id,),
        ).fetchone()
    return _asdict(row) if row is not None else None


def call_session_set(conn: sqlite3.Connection, device_id: str, call_ref: str,
                     state: str, direction: str | None = None,
                     number: str | None = None,
                     number_key: str | None = None,
                     started_at: int | None = None,
                     ended_at: int | None = None,
                     muted: bool | None = None,
                     speaker: bool | None = None,
                     sim_label: str | None = None,
                     now: int | None = None) -> dict:
    """Upsert the live call of one device.

    A device has one primary call. An event for the same ``call_ref``
    merges: an omitted field keeps the stored value, so a mute update
    does not erase the caller. An event for a new ``call_ref`` replaces
    the row, because the previous call is over by definition.

    ``state='disconnected'`` stamps ``ended_at`` when the phone did not
    send one.
    """
    if state not in CALL_STATES:
        raise ValueError(f"invalid call state: {state}")
    if direction is not None and direction not in CALL_DIRECTIONS:
        raise ValueError(f"invalid call direction: {direction}")
    stamp = int(now) if now is not None else _now()

    def fn() -> dict:
        existing = conn.execute(
            "SELECT * FROM call_sessions WHERE device_id = ? LIMIT 1",
            (device_id,),
        ).fetchone()
        previous = _asdict(existing) if existing is not None else None
        same_call = (previous is not None
                     and previous.get("call_ref") == call_ref)
        if same_call:
            direction_value = direction or previous["direction"]
            number_value = (previous["number"] if number is None
                            else number)
            key_value = (previous["number_key"] if number is None
                         else (number_key if number_key is not None
                               else (phones.thread_key(number) or None)))
            started_value = (previous["started_at"] if started_at is None
                             else int(started_at))
            muted_value = (previous["muted"] if muted is None
                           else (1 if muted else 0))
            speaker_value = (previous["speaker"] if speaker is None
                             else (1 if speaker else 0))
            label_value = (previous["sim_label"] if sim_label is None
                           else sim_label)
            answered_value = previous.get("answered_at")
            if answered_value is None and state in ("active", "held"):
                answered_value = stamp
            ended_value = previous.get("ended_at")
        else:
            direction_value = direction or "incoming"
            number_value = number or ""
            key_value = (number_key if number_key is not None
                         else (phones.thread_key(number_value) or None))
            started_value = int(started_at) if started_at is not None else stamp
            muted_value = 1 if muted else 0
            speaker_value = 1 if speaker else 0
            label_value = sim_label
            answered_value = stamp if state in ("active", "held") else None
            ended_value = None
        if state == "disconnected":
            ended_value = int(ended_at) if ended_at is not None else stamp
        elif ended_at is not None:
            ended_value = int(ended_at)
        conn.execute(
            "INSERT INTO call_sessions"
            " (device_id, call_ref, number, number_key, direction, state,"
            "  started_at, answered_at, ended_at, muted, speaker, sim_label,"
            "  updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET"
            "  call_ref = excluded.call_ref, number = excluded.number,"
            "  number_key = excluded.number_key,"
            "  direction = excluded.direction, state = excluded.state,"
            "  started_at = excluded.started_at,"
            "  answered_at = excluded.answered_at,"
            "  ended_at = excluded.ended_at, muted = excluded.muted,"
            "  speaker = excluded.speaker,"
            "  sim_label = excluded.sim_label,"
            "  updated_at = excluded.updated_at",
            (device_id, call_ref, number_value, key_value, direction_value,
             state, started_value, answered_value, ended_value, muted_value,
             speaker_value, label_value, stamp),
        )
        row = conn.execute(
            "SELECT * FROM call_sessions WHERE device_id = ? LIMIT 1",
            (device_id,),
        ).fetchone()
        return _asdict(row)

    return _write_tx(conn, fn)


def call_action_by_client_ref(conn: sqlite3.Connection,
                              client_ref: str | None) -> dict | None:
    """The action created for an idempotency key, if any."""
    if not client_ref:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM call_actions WHERE client_ref = ? LIMIT 1",
            (client_ref,),
        ).fetchone()
    return _asdict(row) if row is not None else None


def call_action_create(conn: sqlite3.Connection, device_id: str, action: str,
                       call_ref: str = "", payload: str | None = None,
                       client_ref: str | None = None,
                       expires_at: int | None = None,
                       now: int | None = None) -> dict | None:
    """Write the guard row of one call action.

    Returns the stored row, or None when the ``client_ref`` already
    belonged to an action (the partial unique index makes the insert a
    no-op, and the caller re-reads with ``call_action_by_client_ref``).

    The TTL is decided here so the bot and the Mini App cannot disagree:
    answer, reject and hang up die in 20 s, everything else in 300 s.
    """
    if action not in CALL_ACTIONS:
        raise ValueError(f"invalid call action: {action}")
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("device_id is required")
    stamp = int(now) if now is not None else _now()
    ttl = CALL_ACTION_TTL_S.get(action, CALL_ACTION_DEFAULT_TTL_S)
    expiry = int(expires_at) if expires_at is not None else stamp + ttl
    action_id = "ca-" + uuid.uuid4().hex

    def fn() -> dict | None:
        cur = conn.execute(
            "INSERT OR IGNORE INTO call_actions"
            " (id, device_id, call_ref, action, payload, status, created_at,"
            "  expires_at, client_ref, result, finished_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL)",
            (action_id, device_id, call_ref or "", action, payload, stamp,
             expiry, client_ref),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM call_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return _asdict(row) if row is not None else None

    return _write_tx(conn, fn)


def claim_call_actions(conn: sqlite3.Connection, device_id: str,
                       limit: int = 10, now: int | None = None) -> list[dict]:
    """Hand a device its pending actions and mark them delivered.

    The select and the update happen in ONE transaction, so two
    consumers (the command poll and the live call poll) can never run the
    same action twice. Actions whose TTL has passed are marked expired
    instead of handed out.
    """
    if not isinstance(device_id, str) or not device_id:
        return []
    limit = max(1, min(int(limit), 50))
    stamp = int(now) if now is not None else _now()

    def fn() -> list[dict]:
        conn.execute(
            "UPDATE call_actions SET status = 'expired', finished_at = ?"
            " WHERE device_id = ? AND status = 'pending' AND expires_at <= ?",
            (stamp, device_id, stamp),
        )
        rows = conn.execute(
            "SELECT * FROM call_actions"
            " WHERE device_id = ? AND status = 'pending' AND expires_at > ?"
            " ORDER BY created_at, id LIMIT ?",
            (device_id, stamp, limit),
        ).fetchall()
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            "UPDATE call_actions SET status = 'delivered'"
            f" WHERE id IN ({placeholders}) AND status = 'pending'",
            tuple(ids),
        )
        return [_asdict(row) for row in rows]

    return _write_tx(conn, fn)


def call_action(conn: sqlite3.Connection, action_id: str) -> dict | None:
    if not isinstance(action_id, str) or not action_id:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT * FROM call_actions WHERE id = ?", (action_id,)
        ).fetchone()
    return _asdict(row) if row is not None else None


def finish_call_action(conn: sqlite3.Connection, action_id: str, status: str,
                       detail: str | None = None) -> tuple[bool, str | None]:
    """Apply a phone report to an action and say whether it was accepted.

    Only ``pending`` and ``delivered`` move forward; a duplicate or late
    report keeps the current status, so the phone stops retrying. An
    unknown id returns ``(False, None)``.
    """
    if status not in ("done", "failed", "rejected"):
        raise ValueError(f"invalid call action result: {status}")

    def fn() -> tuple[bool, str | None]:
        row = conn.execute(
            "SELECT status FROM call_actions WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None:
            return False, None
        current = row["status"]
        allowed = _CALL_ACTION_TRANSITIONS.get(current, ())
        if status not in allowed:
            return False, current
        conn.execute(
            "UPDATE call_actions SET status = ?, result = ?, finished_at = ?"
            " WHERE id = ?",
            (status, detail, _now(), action_id),
        )
        return True, status

    return _write_tx(conn, fn)


def expire_call_actions(conn: sqlite3.Connection, now: int | None = None) -> int:
    """Mark every pending action whose TTL has passed as expired.

    Only ``pending`` rows expire: a ``delivered`` action is already in the
    hand of the phone, which reports its outcome. Expired actions are
    never delivered.
    """
    stamp = int(now) if now is not None else _now()

    def fn() -> int:
        return conn.execute(
            "UPDATE call_actions SET status = 'expired', finished_at = ?"
            " WHERE status = 'pending' AND expires_at <= ?",
            (stamp, stamp),
        ).rowcount

    return _write_tx(conn, fn)
