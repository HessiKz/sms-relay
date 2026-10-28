# Deployment pack

Files in this directory are templates only. Nothing here touches the live
system until you run `install.sh` (and confirm with the operator).

| File | Purpose |
|---|---|
| `sms-relay-api.service` | systemd unit for the API (single process, loopback bind, hardened) |
| `sms-relay.caddy-snippet` | the `handle /relay/*` block to merge into the existing Caddy site |
| `env.template` | template for `/etc/sms-relay/env` (0640 root:smsrelay) |
| `install.sh` | idempotent installer (user, dirs, venv, unit, Caddy merge) |
| `backup.sh` | consistent SQLite backup via the online backup API |
| `restore.sh` | integrity-checked, atomic restore with rollback copy |
| `purge-history.sh` | delete SMS history (by age, state, or all) |

## Install

```bash
cd /root/sms-relay
# 1. look at the machine first (mandatory on a small VPS)
free -h; df -h; ps aux --sort=-%mem; ss -tlpn; systemctl list-units --type=service --state=running
# 2. install (interactive confirmation)
sudo deploy/install.sh
# 3. fill in the secrets
sudo nano /etc/sms-relay/env
# 4. start the service and reload Caddy
sudo systemctl start sms-relay-api.service
sudo caddy reload --config /etc/caddy/Caddyfile
# 5. verify
curl -fsS https://ai-barrow.ir/relay/health && echo
```

`install.sh` never starts the service unless you pass `--start`, never
rewrites the Caddyfile blindly (it backs it up, inserts the handler once,
runs `caddy validate`, and restores the backup if validation fails), and
never touches other services.

## Release 1.2.x / Mini App (this release)

All backend changes are additive; Caddy needs **no change** (`/relay/app`
already reaches FastAPI). The Android app is source-compatible on the
server side, and the APK is `versionCode 4`, `versionName 1.2.1`. The
1.2.1 build only shortens the phone's command poll to five seconds; the
server deployment is unchanged from 1.2.0.

```bash
cd /root/sms-relay
# 1) backup: record the filename
sudo deploy/backup.sh
# 2) full test suite (uses the project venv)
backend/.venv/bin/python -m pytest -q backend/tests
# 3) copy code (static/app is inside backend/app)
sudo deploy/install.sh --yes
# 4) restart explicitly (install.sh does not restart an existing unit)
sudo systemctl restart sms-relay-api
# 5) verify
curl -fsS https://ai-barrow.ir/relay/health
curl -sI https://ai-barrow.ir/relay/app | \
  grep -Ei 'HTTP/|content-security-policy|x-frame-options|cache-control'
sudo journalctl -u sms-relay-api -n 50 --no-pager
sudo systemctl show sms-relay-api -p MemoryCurrent
```

Expected: health `{"ok":true}`, CSP with `frame-ancestors` and **no**
`X-Frame-Options`, memory well under 200 MB.

Schema check on the live database (no secrets printed):

```bash
sudo -u smsrelay /opt/sms-relay/venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('/var/lib/sms-relay/sms.db')
print([r[1] for r in c.execute("PRAGMA table_info(sms_commands)")])
print(c.execute("SELECT COUNT(*) FROM messages WHERE thread_key IS NULL").fetchone())
PY
```

Expect the new columns (`thread_key`, `sub_id`, `client_ref`, `updated_at`,
`finished_at`) and `0` rows with a NULL `thread_key`.

Enable the Mini App in the bot:

```bash
# add MINIAPP_URL=https://ai-barrow.ir/relay/app/ to /etc/sms-relay/env,
# then restart, so the bot menu/deep links include the Mini App
sudo systemctl restart sms-relay-api
sudo python3 scripts/set_miniapp_menu.py \
  --env-file /etc/sms-relay/env --url https://ai-barrow.ir/relay/app/
```

Open it from the menu on iOS, Android, Desktop and Web before publishing the
APK. Publish `sms-relay-android-v0.1.3.zip` only after the phone-side E2E
passes.

### Rollback

- **Backend:** redeploy the previous tree and restart. New columns/tables are
  ignored by old code and need no destructive migration. Restore the DB
  backup only if a migration/backfill is wrong.
- **Caddy:** untouched by this release; no rollback step.
- **Android:** reinstall 1.1.0. There is no Room version bump, so downgrade
  is safe. Commands already delivered are terminal and are not re-sent.
- **Bot:** clear the menu button with
  `sudo python3 scripts/set_miniapp_menu.py --env-file /etc/sms-relay/env --clear`
  or in BotFather; the text commands keep working.

## Secrets

`/etc/sms-relay/env` is mode 0640, owned by `root:smsrelay`. systemd reads
it before dropping privileges, and the service account can read it because
`app.main --env-file` reads the file itself.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # token generator
```

Reuse the credentials of the already-running OpenCode Telegram bot:

```bash
set -a; . /root/.config/opencode-telegram-bot/.env; set +a
# TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID are what you need
```

In a private chat with a bot the chat id equals the user id, so
`TELEGRAM_CHAT_ID` = `TELEGRAM_ALLOWED_USER_ID` from that file.

## Rollback

```bash
sudo systemctl disable --now sms-relay-api.service
sudo rm /etc/systemd/system/sms-relay-api.service && sudo systemctl daemon-reload
# restore the Caddyfile from the backup the installer created
ls -1t /etc/caddy/Caddyfile.bak.* | head -1
sudo cp "$(ls -1t /etc/caddy/Caddyfile.bak.* | head -1)" /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile && sudo caddy reload --config /etc/caddy/Caddyfile
sudo rm -rf /opt/sms-relay /var/lib/sms-relay   # optional: data removal
```

## Memory

The API is a single Uvicorn process. Expected steady state: 60-120 MB RSS,
plus Caddy's existing footprint. Measure after starting:

```bash
scripts/measure_memory.sh
```

If the process approaches `MemoryMax=320M`, look at what is actually
consuming memory before raising the limit:

```bash
ps -o pid,rss,etime,cmd -p "$(systemctl show -p MainPID --value sms-relay-api.service)"
journalctl -k --since "1 hour ago" | grep -i -e oom -e killed
```
