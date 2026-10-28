#!/usr/bin/env bash
# Install the SMS relay backend on this VPS.
#
# Idempotent and non-destructive by default: it never overwrites the
# Caddyfile blindly (it backs it up and only inserts the handle block
# once), never touches unrelated services, and does NOT start the
# service -- starting is a separate, explicit step.
#
# Usage:
#   sudo deploy/install.sh [--yes] [--start]
#
# Run the resource inspection first (see docs/vps-setup.md):
#   free -h; df -h; ps aux --sort=-%mem; ss -tlpn; systemctl list-units --type=service --state=running
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/sms-relay/app
VENV_DIR=/opt/sms-relay/venv
DATA_DIR=/var/lib/sms-relay
CONF_DIR=/etc/sms-relay
ENV_FILE="$CONF_DIR/env"
UNIT=/etc/systemd/system/sms-relay-api.service
CADDYFILE=/etc/caddy/Caddyfile
SITE=ai-barrow.ir
SERVICE_USER=smsrelay
DO_START=0
ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=1 ;;
    --start) DO_START=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

log "pre-flight resource check"
free -h
df -h / | tail -1
echo "--- top memory consumers ---"
ps aux --sort=-%mem | head -8
echo "--- listening ports ---"
ss -tlpn || true
echo "--- existing caddy config ---"
[ -f "$CADDYFILE" ] && cat "$CADDYFILE" || echo "(no $CADDYFILE)"
echo
if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "Continue with installation? [y/N] " reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) log "aborted by user"; exit 0 ;;
  esac
fi

# --- service account -------------------------------------------------------
if ! getent group "$SERVICE_USER" >/dev/null; then
  log "creating group $SERVICE_USER"
  groupadd --system "$SERVICE_USER"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  log "creating system user $SERVICE_USER"
  useradd --system --gid "$SERVICE_USER" --home-dir "$DATA_DIR" \
          --shell /usr/sbin/nologin --no-create-home "$SERVICE_USER"
fi

# --- directories -----------------------------------------------------------
log "creating directories"
install -d -m 0755 /opt/sms-relay
install -d -m 0755 "$APP_DIR"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF_DIR"

# --- application code ------------------------------------------------------
log "copying backend to $APP_DIR"
rm -rf "$APP_DIR/app"
cp -a "$REPO_DIR/backend/app" "$APP_DIR/"
cp -a "$REPO_DIR/backend/requirements.txt" "$APP_DIR/"

# --- virtualenv ------------------------------------------------------------
if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "creating virtualenv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -q --upgrade pip
  "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"
else
  log "virtualenv already present, refreshing dependencies"
  "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi
chmod -R a+rX "$VENV_DIR" "$APP_DIR"

# --- environment file ------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  log "creating $ENV_FILE from template (fill in the values!)"
  install -m 0640 -o root -g "$SERVICE_USER" "$REPO_DIR/deploy/env.template" "$ENV_FILE"
  cat <<'EOF'

The real file was NOT overwritten. Edit /etc/sms-relay/env and set:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ADMIN_TELEGRAM_USER_ID,
  PHONE_A_TOKEN, ADMIN_TOKEN
then re-run with --start (or start the unit manually).

The unit is NOT enabled while this file is incomplete, so the box cannot
end up in a restart loop.

Create a DEDICATED Telegram bot for the relay:
  1. talk to @BotFather -> /newbot -> copy the token
  2. open a private chat with the new bot and press Start
  3. get your numeric user id (e.g. from @userinfobot)
  4. put token, chat id and user id into the file above

Do NOT reuse the token of the OpenCode bot that already runs on this
host: Telegram allows only one getUpdates consumer per token, so two
pollers would fight and the existing bot would crash-loop.
EOF
else
  log "$ENV_FILE already exists -- leaving it untouched"
  # The app reads the file itself (--env-file), so the service account
  # needs read access: root:smsrelay 0640, never world readable.
  chown root:"$SERVICE_USER" "$ENV_FILE" 2>/dev/null || true
  chmod 0640 "$ENV_FILE" 2>/dev/null || true
fi

# --- systemd unit ----------------------------------------------------------
log "installing systemd unit"
install -m 0644 "$REPO_DIR/deploy/sms-relay-api.service" "$UNIT"
systemctl daemon-reload

# Only enable the unit when the environment file is complete, otherwise the
# service would crash-loop with "missing required setting" on every start.
if grep -qE '^[A-Z_]+=$' "$ENV_FILE" 2>/dev/null; then
  log "$ENV_FILE is incomplete -- unit installed but NOT enabled"
else
  systemctl enable sms-relay-api.service >/dev/null
  log "unit enabled (not started)"
fi

# --- Caddy -----------------------------------------------------------------
if [ -f "$CADDYFILE" ]; then
  if grep -q 'handle /relay/\*' "$CADDYFILE"; then
    log "Caddyfile already contains the /relay handler -- leaving it alone"
  else
    backup="$CADDYFILE.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "$CADDYFILE" "$backup"
    log "backed up Caddyfile to $backup"
    log "inserting the /relay handler into the $SITE site block"
    python3 - "$CADDYFILE" "$REPO_DIR/deploy/sms-relay.caddy-snippet" <<'PY'
import re
import sys

caddyfile, snippet_path = sys.argv[1], sys.argv[2]
with open(caddyfile, encoding="utf-8") as fh:
    text = fh.read()
with open(snippet_path, encoding="utf-8") as fh:
    # Only the configuration lines go into the live Caddyfile, not the
    # explanatory comment header of the snippet.
    snippet = "\n".join(
        line for line in fh.read().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ).rstrip("\n")

lines = text.splitlines()
out, inserted = [], False
for line in lines:
    out.append(line)
    if not inserted:
        # Exact site-block match only: "api.ai-barrow.ir {" must not match.
        match = re.match(r"^\s*([A-Za-z0-9._-]+)\s*\{\s*$", line)
        if match and match.group(1) == "ai-barrow.ir":
            out.extend(snippet.splitlines())
            inserted = True
if not inserted:
    sys.exit("could not find the ai-barrow.ir site block -- not modifying the Caddyfile")
with open(caddyfile, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
PY
    if caddy validate --config "$CADDYFILE"; then
      log "caddy validate passed"
    else
      log "caddy validate FAILED -- restoring backup $backup"
      cp -a "$backup" "$CADDYFILE"
      die "invalid Caddyfile; nothing was changed"
    fi
  fi
else
  log "WARNING: $CADDYFILE not found -- add the snippet manually (see deploy/README.md)"
fi

log "done"
log "next: edit $ENV_FILE, then 'systemctl start sms-relay-api' and 'caddy reload'"
if [ "$DO_START" -eq 1 ]; then
  log "starting service (--start given)"
  systemctl start sms-relay-api.service
  systemctl --no-pager --full status sms-relay-api.service | head -20 || true
  log "reloading caddy"
  caddy reload --config "$CADDYFILE"
  curl -fsS "http://127.0.0.1:8090/health" && echo
fi
