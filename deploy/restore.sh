#!/usr/bin/env bash
# Restore the relay database from a gzip backup.
#
# Usage: sudo deploy/restore.sh /var/lib/backups/sms-relay/sms-relay-YYYYmmdd-HHMMSS.db.gz
#
# The restore is atomic: the archive is unpacked and integrity-checked
# first, the service is stopped only after the check passes, and the
# previous database is kept as <name>.pre-restore.
set -euo pipefail

ARCHIVE=${1:-}
DATA_DIR=/var/lib/sms-relay
DB_PATH=${DB_PATH:-$DATA_DIR/sms.db}
SERVICE=sms-relay-api.service

[ -n "$ARCHIVE" ] || { echo "usage: $0 <backup.db.gz>" >&2; exit 2; }
[ -f "$ARCHIVE" ] || { echo "no such backup: $ARCHIVE" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
gunzip -c "$ARCHIVE" > "$TMP/restore.db"

python3 - "$TMP/restore.db" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
result = conn.execute("PRAGMA integrity_check").fetchone()[0]
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
conn.close()
if result != "ok":
    sys.exit(f"integrity check failed: {result}")
print("integrity ok; tables:", ", ".join(tables))
PY

if systemctl is-active --quiet "$SERVICE"; then
  echo "stopping $SERVICE"
  systemctl stop "$SERVICE"
fi

if [ -f "$DB_PATH" ]; then
  cp -a "$DB_PATH" "$DB_PATH.pre-restore"
  echo "previous database kept as $DB_PATH.pre-restore"
fi

install -m 0640 -o smsrelay -g smsrelay "$TMP/restore.db" "$DB_PATH"
rm -f "$DB_PATH-wal" "$DB_PATH-shm"

echo "restored $ARCHIVE -> $DB_PATH"
if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then
  echo "starting $SERVICE"
  systemctl start "$SERVICE"
  systemctl --no-pager --full status "$SERVICE" | head -12 || true
fi
