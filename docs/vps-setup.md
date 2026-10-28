# VPS setup

The backend runs as a single Uvicorn process on a small (1-2 GB RAM) VPS.
Everything below assumes Ubuntu 24.04, systemd, and Caddy already running
(for the HTTPS entry point).

## 1. Inspect before changing anything

Never deploy blind onto a shared box. Run this first and read the output:

```bash
free -h                       # total / used / available RAM and swap
df -h                         # disk usage
ps aux --sort=-%mem | head   # who is using memory right now
swapon --show                 # swap size and usage
ss -tlpn                      # listening ports
systemctl list-units --type=service --state=running
cat /etc/caddy/Caddyfile      # existing reverse proxy configuration
```

On this particular host the box is shared with a web app, a Go API,
PostgreSQL and the OpenCode stack, and it is already close to its memory
limit. The relay is sized for ~60-120 MB RSS; if `free -h` shows less than
~300 MB available, do not deploy yet.

## 2. Install

```bash
cd /root/sms-relay
sudo deploy/install.sh
```

The installer:

1. creates the non-root system user `smsrelay` (no shell),
2. creates `/opt/sms-relay` (code + venv), `/var/lib/sms-relay` (database,
   mode 0700) and `/etc/sms-relay` (secrets),
3. installs the systemd unit (enabled, **not** started),
4. backs up `/etc/caddy/Caddyfile` and merges the `handle /relay/*` block
   into the existing site, then runs `caddy validate` and restores the
   backup if the result is invalid.

It never stops or restarts another service and never overwrites the
Caddyfile wholesale.

## 3. Secrets

```bash
sudo nano /etc/sms-relay/env       # see deploy/env.template
sudo chown root:smsrelay /etc/sms-relay/env
sudo chmod 640 /etc/sms-relay/env
```

Required keys: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`ADMIN_TELEGRAM_USER_ID`, `PHONE_A_TOKEN`, `ADMIN_TOKEN`, `DATABASE_PATH`.
Generate the two API tokens with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 4. Start and verify

```bash
sudo systemctl start sms-relay-api.service
sudo systemctl --no-pager --full status sms-relay-api.service
curl -fsS http://127.0.0.1:8090/health && echo
sudo caddy reload --config /etc/caddy/Caddyfile
curl -fsS https://ai-barrow.ir/relay/health && echo
```

`/health` returns `{"ok": true}` and needs no token. Everything else
requires a bearer token.

## 5. Measure memory

```bash
sudo scripts/measure_memory.sh
```

Expected: the relay process in the 60-120 MB range, Caddy unchanged, no
OOM entries in the kernel log. The unit sets `MemoryHigh=200M` and
`MemoryMax=320M` as a guardrail, not a target. If the process actually
needs more, raise the limit — a service that kills itself is worse than a
service that uses 250 MB:

```bash
sudo systemctl edit sms-relay-api.service   # add MemoryMax=400M
sudo systemctl daemon-reload && sudo systemctl restart sms-relay-api.service
```

Check for OOM kills:

```bash
journalctl -k --since "24 hours ago" | grep -i -e oom -e "killed process"
```

## 6. Log rotation

The service logs to the journal (stdout/stderr), which systemd rotates and
by default keeps a bounded amount. To keep a week of history:

```bash
sudo journalctl --disk-usage
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nMaxRetentionSec=7day\n' | sudo tee /etc/systemd/journald.conf.d/retention.conf
sudo systemctl restart systemd-journald
```

No separate logrotate file is needed: the service writes no log files of
its own.

## 7. Backups

See [backup-restore.md](backup-restore.md). The recommended cron entry
(after the service exists):

```cron
17 4 * * * /root/sms-relay/deploy/backup.sh >/dev/null
```

## 8. Rollback

```bash
sudo systemctl disable --now sms-relay-api.service
sudo rm /etc/systemd/system/sms-relay-api.service && sudo systemctl daemon-reload
sudo cp "$(ls -1t /etc/caddy/Caddyfile.bak.* | head -1)" /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile && sudo caddy reload --config /etc/caddy/Caddyfile
```

Leave `/var/lib/sms-relay` in place if you may want the message history
back; delete it explicitly to remove the data as well.
