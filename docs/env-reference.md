# Environment variable reference

The backend reads its configuration from the environment, or from a
`KEY=VALUE` file passed with `--env-file` (systemd also injects the file via
`EnvironmentFile`). The live file is `/etc/sms-relay/env`, mode 0640
`root:smsrelay`.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | – | Bot token of the Telegram bot used for delivery. Treat as a secret. |
| `TELEGRAM_CHAT_ID` | yes | – | Numeric chat that receives the messages. In a private chat it equals the user id. |
| `ADMIN_TELEGRAM_USER_ID` | yes | – | Numeric Telegram user id allowed to run remote commands. |
| `PHONE_A_TOKEN` | yes | – | Bearer token the Android app must send on `POST /relay/sms` and `POST /relay/import`. |
| `ADMIN_TOKEN` | yes | – | Bearer token for the administrative endpoints and `GET /relay/messages`. |
| `DATABASE_PATH` | yes | – | Absolute path of the SQLite file, e.g. `/var/lib/sms-relay/sms.db`. Must be on persistent storage, never `/tmp`. |
| `UVICORN_HOST` | no | `127.0.0.1` | Bind address. Keep it on loopback: Caddy is the public entry point. |
| `UVICORN_PORT` | no | `8090` | Bind port. |
| `LOG_LEVEL` | no | `info` | Python log level (`debug`, `info`, `warning`, `error`). |
| `RELAY_DISABLE_UPDATER` | no | unset | Set to `1` to run the API without the Telegram command poller (useful while debugging). |
| `MINIAPP_URL` | no | unset | Public Mini App URL, e.g. `https://ai-barrow.ir/relay/app/`. When set, the bot gets an "Open inbox" menu button and each relayed SMS gets an "Open chat" button. Must be `https`. |
| `MINIAPP_BOT_USERNAME` | no | unset | Bot username without `@`, used to build optional `t.me/<bot>?startapp=…` deep links. Not needed for menu buttons. |
| `MINIAPP_AUTH_MAX_AGE` | no | `86400` | Maximum age in seconds of Telegram `initData`. The Mini App must be reopened after it expires. |
| `MINIAPP_SEND_PER_MINUTE` | no | `6` | Server-side cap on Mini App sends per minute, per admin account. |
| `MINIAPP_SEND_DAILY_CAP` | no | `50` | Server-side cap on Mini App sends per rolling 24 hours. |

The Mini App settings are optional but must be valid when present: an empty
value means "unset"; a non-integer or a value ≤ 0 makes startup fail.

Startup fails fast with `ValueError: missing required setting: NAME` if a
required value is missing or empty, and with a non-numeric
`ADMIN_TELEGRAM_USER_ID` error if that value is not a number.

## Generating tokens

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use a different value for `PHONE_A_TOKEN` and `ADMIN_TOKEN`. If a token
leaks, generate a new one, update the file, restart the service
(`systemctl restart sms-relay-api`) and re-enter the new device token in
the Android app.

## Settings that live in the database

These are changed through the admin API or the Telegram commands, not
through environment variables:

| Setting | Default | Meaning |
|---|---|---|
| `paused` | `0` | Global forwarding switch (`/pause`, `/admin/pause`). |
| `fallback_enabled` | `0` | Master switch for the direct-SIM fallback (`/fallback on`). |
| `phone_b` | empty | Destination number for fallback SMS (`/phoneb +1555…`). |
| `fallback_min_gap_s` | `30` | Minimum seconds between two fallback SMS. |
| `fallback_max_per_hour` | `10` | Maximum fallback SMS per hour. |

Filters (allow / block / keyword) are also stored in the database, in the
`filters` table.

## Where things live on disk

| Path | Content | Permissions |
|---|---|---|
| `/etc/sms-relay/env` | secrets and settings | 0640 root:smsrelay (the service account reads it; never world readable) |
| `/var/lib/sms-relay/sms.db` | SQLite database (messages, jobs, settings, filters) | created 0644 inside a 0700 directory owned by `smsrelay`; only the service account can traverse the directory |
| `/opt/sms-relay` | application code and virtualenv | 0755 |
| `/var/lib/backups/sms-relay` | gzip backups | 0700 |
