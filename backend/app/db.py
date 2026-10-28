import sqlite3
import threading
import time

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
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_state ON messages(state)",
    "CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_key, received_at DESC)",
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
)

_WRITE_LOCK = threading.Lock()  # writes and reads; app is single-threaded asyncio, lock is cheap, and reads must never observe another thread's uncommitted tx
_TX_OWNER = threading.local()


FILTER_MAX_PATTERNS = 100
FILTER_MAX_PATTERN_LEN = 64
RESUME_BATCH = 200


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
                   client_ref: str | None = None) -> None:
    """Queue an SMS the phone should send from its own SIM.

    ``client_ref`` is an optional idempotency key from the Mini App: the
    partial unique index makes a repeated insert a no-op. Callers that
    need the existing row should look it up with command_by_client_ref().
    """
    key = thread_key if thread_key is not None else phones.thread_key(target)

    def fn() -> None:
        now = _now()
        conn.execute(
            "INSERT OR IGNORE INTO sms_commands"
            " (id, created_at, target, body, source, status, result,"
            "  reply_to_telegram_id, thread_key, sub_id, client_ref,"
            "  updated_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?, ?, NULL)",
            (command_id, now, target, body, source, reply_to_telegram_id,
             key, int(sub_id or 0), client_ref, now),
        )
    _write_tx(conn, fn)


def pending_commands(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT id, created_at, target, body, source, reply_to_telegram_id,"
            " thread_key, sub_id"
            " FROM sms_commands WHERE status = 'pending'"
            " ORDER BY created_at LIMIT ?",
            (limit,),
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


def contact_name_for_key(conn: sqlite3.Connection,
                         thread_key: str) -> str | None:
    """The contact name stored for a thread key, if any."""
    if not isinstance(thread_key, str) or not thread_key:
        return None
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT MIN(name) AS name FROM contacts WHERE number_key = ?",
            (thread_key,),
        ).fetchone()
    return row["name"] if row is not None else None


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
            "SELECT id, sender, sender_name, body, telegram_msg_id"
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
                           window: int = 120) -> dict | None:
    """A message with the same sender and text from ANOTHER capture path.

    Used to stop the same SMS being stored twice when the phone captures
    it through two paths (broadcast and notification). Two genuine repeats
    from the same path are separate messages and are both kept. The sender
    is compared in its normalised form, because the two paths spell the
    same number differently.
    """
    key = normalize_sender(sender)
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT id, state, sender FROM messages WHERE body = ?"
            " AND source != ?"
            " AND received_at BETWEEN ? AND ? ORDER BY received_at LIMIT 25",
            (body, source, received_at - window, received_at + window),
        ).fetchall()
    for row in rows:
        if normalize_sender(row["sender"]) == key:
            return {"id": row["id"], "state": row["state"]}
    return None


def find_import_duplicate(conn: sqlite3.Connection, thread_key: str,
                          body: str, received_at: int,
                          window: int = 60) -> str | None:
    """The id of a message that already covers an imported item.

    Historical import overlaps with live capture: the same SMS may have
    been stored when it arrived. A message from the same thread with the
    same text within a minute of the inbox timestamp is treated as the
    same message. Unlike :func:`find_recent_duplicate` this also matches
    rows from the same source, because the import path is not a distinct
    capture mechanism but a replay of the phone's own inbox.
    """
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT id FROM messages WHERE thread_key = ? AND body = ?"
            " AND received_at BETWEEN ? AND ? LIMIT 1",
            (thread_key, body, received_at - window, received_at + window),
        ).fetchone()
    return row["id"] if row is not None else None


def count_jobs(conn: sqlite3.Connection) -> int:
    with _WRITE_LOCK:
        row = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    return int(row["n"])


def states_for_ids(conn: sqlite3.Connection, ids: list[str]) -> dict:
    """Delivery state for a bounded list of message ids."""
    if not ids:
        return {}
    with _WRITE_LOCK:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            "SELECT id, state, attempts, last_error FROM messages"
            f" WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
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


def record_message(conn: sqlite3.Connection, msg: dict) -> bool:
    state = msg.get("state") or "pending"
    _check_state(state)
    now = _now()
    group_id = msg.get("thread_id", msg.get("group_id"))
    source = msg.get("source") or "sms"
    key = phones.thread_key(msg.get("sender") or "")
    if key is None:
        key = phones.raw_key(msg.get("sender") or "")
    sub_id = int(msg.get("sub_id") or 0)

    def fn() -> bool:
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages"
            " (id, sender, body, parts, part_index, group_id, source, "
            "  received_at, state, telegram_msg_id, attempts, last_error, "
            "  created_at, updated_at, thread_key, sub_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?, ?)",
            (msg["id"], msg["sender"], msg["body"],
             msg.get("parts", 1), msg.get("part_index", 0), group_id, source,
             msg["received_at"], state, now, now, key, sub_id),
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
                  state: str | None = None) -> list[dict]:
    if state is None:
        with _WRITE_LOCK:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY received_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    else:
        _check_state(state)
        with _WRITE_LOCK:
            rows = conn.execute(
                "SELECT * FROM messages WHERE state = ? ORDER BY received_at DESC"
                " LIMIT ? OFFSET ?",
                (state, limit, offset),
            ).fetchall()
    return [_asdict(row) for row in rows]


def resume_received(conn: sqlite3.Connection) -> int:
    """Atomically move every 'received' message to 'pending' and enqueue it.

    Batched so a large backlog never has to be materialised in memory.
    """
    def fn() -> int:
        now = _now()
        moved = 0
        while True:
            rows = conn.execute(
                "SELECT id FROM messages WHERE state = 'received' LIMIT ?",
                (RESUME_BATCH,),
            ).fetchall()
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


def chat_summaries(conn: sqlite3.Connection, query: str | None = None,
                   limit: int = 100) -> list[dict]:
    """One row per thread: last event, unread count and a display name."""
    limit = _clamp_limit(limit, 100)
    params: list = []
    search = ""
    if query and query.strip():
        number_like, local_like, text_like = _search_likes(query)
        search = (
            " WHERE ("
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
    sql = (
        "WITH events AS ("
        "  SELECT thread_key, 'in' AS direction, received_at AS ts, body,"
        "         sender_name, 'msg:' || id AS event_id"
        "    FROM messages WHERE thread_key IS NOT NULL"
        "  UNION ALL"
        "  SELECT thread_key, 'out' AS direction, created_at AS ts, body,"
        "         NULL AS sender_name, 'cmd:' || id AS event_id"
        "    FROM sms_commands WHERE thread_key IS NOT NULL"
        "),"
        " ranked AS ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "      PARTITION BY thread_key ORDER BY ts DESC, event_id DESC) AS rn"
        "    FROM events"
        "),"
        " latest AS (SELECT * FROM ranked WHERE rn = 1),"
        " names AS ("
        "  SELECT number_key, MIN(name) AS name FROM contacts"
        "   WHERE number_key IS NOT NULL GROUP BY number_key"
        ")"
        " SELECT l.thread_key AS thread_key,"
        "        l.direction AS last_direction,"
        "        l.ts AS last_ts,"
        "        l.body AS last_body,"
        "        l.sender_name AS last_sender_name,"
        "        n.name AS contact_name,"
        "        (SELECT COUNT(*) FROM messages m"
        "          WHERE m.thread_key = l.thread_key"
        "            AND m.received_at > COALESCE(r.last_read_at, 0)) AS unread"
        "   FROM latest l"
        "   LEFT JOIN names n ON n.number_key = l.thread_key"
        "   LEFT JOIN thread_reads r ON r.thread_key = l.thread_key"
        + search
        + " ORDER BY last_ts DESC, thread_key LIMIT ?"
    )
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


def thread_timeline(conn: sqlite3.Connection, thread_key: str,
                    before_ts: int | None = None,
                    limit: int = 50) -> list[dict]:
    """Merge inbound SMS and outbound commands, newest first.

    ``before_ts`` is exclusive: a message received in the same second as
    the pagination cursor can be skipped, which is the documented
    limitation of timestamp pagination.
    """
    limit = _clamp_limit(limit, 50)
    sql = (
        "SELECT 'msg:' || id AS id, 'in' AS direction, received_at AS ts,"
        "       body, sender_name, NULL AS status, NULL AS result,"
        "       NULL AS client_ref, NULL AS sub_id"
        "  FROM messages"
        " WHERE thread_key = ? AND (? IS NULL OR received_at < ?)"
        " UNION ALL"
        " SELECT 'cmd:' || id, 'out', created_at, body, NULL, status, result,"
        "        client_ref, sub_id"
        "   FROM sms_commands"
        "  WHERE thread_key = ? AND (? IS NULL OR created_at < ?)"
        " ORDER BY ts DESC, id DESC LIMIT ?"
    )
    with _WRITE_LOCK:
        rows = conn.execute(
            sql, (thread_key, before_ts, before_ts,
                  thread_key, before_ts, before_ts, limit),
        ).fetchall()
    events = []
    for row in rows:
        event = _asdict(row)
        event["thread_key"] = thread_key
        events.append(event)
    return events


def thread_exists(conn: sqlite3.Connection, thread_key: str) -> bool:
    """True when the thread has at least one inbound or outbound event."""
    if not isinstance(thread_key, str) or not thread_key:
        return False
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT 1 WHERE EXISTS ("
            "  SELECT 1 FROM messages WHERE thread_key = ?)"
            " OR EXISTS ("
            "  SELECT 1 FROM sms_commands WHERE thread_key = ?)",
            (thread_key, thread_key),
        ).fetchone()
    return row is not None


def thread_meta(conn: sqlite3.Connection, thread_key: str) -> dict:
    """Display name and readable number for a thread.

    The contact list wins over the sender name carried by an SMS, which in
    turn wins over the bare number. The Mini App needs all three pieces so
    it can keep the header stable while a message list is paginated.
    """
    display = phones.display_number(thread_key)
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT MIN(name) AS name FROM contacts WHERE number_key = ?",
            (thread_key,),
        ).fetchone()
        contact_name = row["name"] if row is not None else None
        if not contact_name:
            row = conn.execute(
                "SELECT sender_name FROM messages"
                " WHERE thread_key = ? AND sender_name IS NOT NULL"
                "   AND TRIM(sender_name) != ''"
                " ORDER BY received_at DESC, id DESC LIMIT 1",
                (thread_key,),
            ).fetchone()
            contact_name = row["sender_name"] if row is not None else None
    return {
        "thread_key": thread_key,
        "display": display,
        "name": contact_name or display,
    }


def miniapp_contacts(conn: sqlite3.Connection, query: str = "",
                     limit: int = 50) -> list[dict]:
    """Contacts for the Mini App autocomplete.

    Unlike ``search_contacts`` (whose Telegram-side semantics are kept),
    this always escapes LIKE wildcards, so a query of ``%`` or ``_``
    matches those literal characters rather than every row.
    """
    limit = _clamp_limit(limit, 50, 100)
    if query and query.strip():
        like = _like_pattern(query)
        sql = (
            "SELECT number, name, number_key FROM contacts"
            " WHERE lower(name) LIKE ? ESCAPE '\\'"
            "    OR number LIKE ? ESCAPE '\\'"
            "    OR COALESCE(number_key, '') LIKE ? ESCAPE '\\'"
            " ORDER BY name, number LIMIT ?"
        )
        params = (like, like, like, limit)
    else:
        sql = ("SELECT number, name, number_key FROM contacts"
               " ORDER BY name, number LIMIT ?")
        params = (limit,)
    with _WRITE_LOCK:
        rows = conn.execute(sql, params).fetchall()
    return [_asdict(row) for row in rows]


def mark_thread_read(conn: sqlite3.Connection, thread_key: str,
                     until_ts: int) -> int:
    """Advance the read cursor, never moving it backwards."""
    now = _now()

    def fn() -> int:
        conn.execute(
            "INSERT INTO thread_reads (thread_key, last_read_at, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(thread_key) DO UPDATE SET"
            "  last_read_at = MAX(thread_reads.last_read_at,"
            "                     excluded.last_read_at),"
            "  updated_at = excluded.updated_at",
            (thread_key, int(until_ts), now),
        )
        row = conn.execute(
            "SELECT last_read_at FROM thread_reads WHERE thread_key = ?",
            (thread_key,),
        ).fetchone()
        return int(row["last_read_at"])

    return _write_tx(conn, fn)


def command_history(conn: sqlite3.Connection, thread_key: str,
                    limit: int = 50) -> list[dict]:
    limit = _clamp_limit(limit, 50)
    with _WRITE_LOCK:
        rows = conn.execute(
            "SELECT * FROM sms_commands WHERE thread_key = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (thread_key, limit),
        ).fetchall()
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
