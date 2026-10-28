# Backup, restore, retention and deletion

The database holds the received SMS content. Treat it as sensitive.

## Backup

```bash
sudo /root/sms-relay/deploy/backup.sh
```

The script uses SQLite's online backup API, so it is safe to run while the
service is live. Output:

```
/var/lib/backups/sms-relay/sms-relay-20260121-041017.db.gz   (mode 0600)
```

Backups older than 14 days are removed automatically. For a daily backup,
add to root's crontab:

```bash
sudo crontab -e
17 4 * * * /root/sms-relay/deploy/backup.sh >/dev/null
```

Keep an encrypted copy off the VPS if the content is sensitive; the backup
directory is on the same machine as the database.

## Restore

```bash
sudo systemctl stop sms-relay-api
sudo /root/sms-relay/deploy/restore.sh /var/lib/backups/sms-relay/sms-relay-20260121-041017.db.gz
sudo systemctl start sms-relay-api
```

The script verifies the gzip archive, runs `PRAGMA integrity_check`, keeps
the previous database as `sms.db.pre-restore` and only then replaces the
live file. The service is started again automatically if it was enabled.

## Retention

Nothing expires automatically today: the database keeps every message
until you delete it. Decide a retention window that fits your use (a week
is usually enough for OTP codes and delivery notifications) and run the
purge script from cron:

```bash
# keep 7 days
sudo /root/sms-relay/deploy/purge-history.sh --older-than 7
```

```cron
41 5 * * * /root/sms-relay/deploy/purge-history.sh --older-than 7 >/dev/null
```

## Deleting sensitive history

```bash
sudo /root/sms-relay/deploy/purge-history.sh --older-than 0   # everything
sudo /root/sms-relay/deploy/purge-history.sh --all             # all messages
sudo /root/sms-relay/deploy/purge-history.sh --state pending   # one state
```

The script deletes the rows, vacuums the file and prints the remaining
message count. Note that:

- the **backups** are not touched — delete or re-encrypt them separately,
- the **Telegram chat** still contains whatever was already delivered, and
  Telegram keeps its own copies according to its retention policy,
- the **Android phone** keeps its local queue/history until you clear it
  in the app.

## Full removal

```bash
sudo systemctl disable --now sms-relay-api.service
sudo rm -f /etc/systemd/system/sms-relay-api.service
sudo systemctl daemon-reload
sudo rm -rf /var/lib/sms-relay /var/lib/backups/sms-relay /opt/sms-relay
sudo rm -f /etc/sms-relay/env
# remove the Caddy handle block, then:
sudo caddy validate --config /etc/caddy/Caddyfile && sudo caddy reload --config /etc/caddy/Caddyfile
```
