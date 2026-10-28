#!/usr/bin/env bash
# Back up the relay SQLite database.
#
# Uses SQLite's online backup API (via Python) so the copy is consistent
# even while the service is running. Output is gzipped, mode 0600, and
# kept for 14 days by default.
#
# Usage: sudo deploy/backup.sh [destination-dir]
set -euo pipefail

DATA_DIR=/var/lib/sms-relay
DB_PATH=${DB_PATH:-$DATA_DIR/sms.db}
DEST=${1:-/var/lib/backups/sms-relay}
KEEP_DAYS=${KEEP_DAYS:-14}

[ -f "$DB_PATH" ] || { echo "no database at $DB_PATH" >&2; exit 1; }

install -d -m 0700 "$DEST"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/sms-relay-$STAMP.db.gz"

python3 - "$DB_PATH" "$OUT" <<'PY'
import gzip
import os
import sqlite3
import sys

src, dest = sys.argv[1], sys.argv[2]
tmp = dest + ".tmp"
conn = sqlite3.connect(src)
backup = sqlite3.connect(tmp)
with backup:
    conn.backup(backup)
backup.close()
conn.close()
with open(tmp, "rb") as raw, gzip.open(dest, "wb") as gz:
    os.fsync(raw.fileno())
    gz.write(raw.read())
os.unlink(tmp)
os.chmod(dest, 0o600)
print("wrote", dest)
PY

find "$DEST" -maxdepth 1 -name 'sms-relay-*.db.gz' -mtime "+$KEEP_DAYS" -delete
echo "backups in $DEST:"
ls -l "$DEST" | tail -5
