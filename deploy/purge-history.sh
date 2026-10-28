#!/usr/bin/env bash
# Delete SMS history from the relay database.
#
# Usage:
#   sudo deploy/purge-history.sh --older-than 30   # keep 30 days
#   sudo deploy/purge-history.sh --all              # remove every message
#   sudo deploy/purge-history.sh --state pending    # remove one state
#
# The database file is VACUUMed afterwards so the space is reclaimed.
# This permanently destroys message content: take a backup first if the
# data might still be needed.
set -euo pipefail

DATA_DIR=/var/lib/sms-relay
DB_PATH=${DB_PATH:-$DATA_DIR/sms.db}
MODE=""
VALUE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --older-than) MODE=older; VALUE=${2:-}; shift 2 ;;
    --all) MODE=all; shift ;;
    --state) MODE=state; VALUE=${2:-}; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$MODE" ] || { echo "usage: $0 --older-than N | --all | --state STATE" >&2; exit 2; }
[ -f "$DB_PATH" ] || { echo "no database at $DB_PATH" >&2; exit 1; }

python3 - "$DB_PATH" "$MODE" "$VALUE" <<'PY'
import sqlite3
import sys
import time

db_path, mode, value = sys.argv[1], sys.argv[2], sys.argv[3]
conn = sqlite3.connect(db_path, isolation_level=None)
conn.execute("PRAGMA busy_timeout=5000")
try:
    conn.execute("BEGIN IMMEDIATE")
    if mode == "all":
        cur = conn.execute("DELETE FROM jobs")
        cur = conn.execute("DELETE FROM messages")
    elif mode == "older":
        cutoff = int(time.time()) - int(value) * 86400
        cur = conn.execute("DELETE FROM jobs WHERE msg_id IN "
                           "(SELECT id FROM messages WHERE received_at < ?)", (cutoff,))
        cur = conn.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
    elif mode == "state":
        if value not in ("received", "pending", "delivered", "failed", "uncertain"):
            sys.exit("invalid state")
        cur = conn.execute("DELETE FROM jobs WHERE msg_id IN "
                           "(SELECT id FROM messages WHERE state = ?)", (value,))
        cur = conn.execute("DELETE FROM messages WHERE state = ?", (value,))
    conn.execute("COMMIT")
    remaining = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.execute("VACUUM")
finally:
    conn.close()
print(f"purged ({mode} {value}); {remaining} messages remain")
PY
