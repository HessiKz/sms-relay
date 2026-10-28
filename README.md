# SMS-to-Telegram Relay

Personal, private relay that forwards incoming SMS from an Android phone
(Samsung Galaxy A51 / Xiaomi 11T) to a private Telegram chat, with a
direct-SIM SMS fallback to an iPhone when internet delivery fails.

```
 Phone A (Android)                 VPS (1-2 GB RAM)                 Phone B (iPhone)
 ┌──────────────────┐   HTTPS     ┌──────────────────────┐         ┌──────────────┐
 │ SmsReceiver      │ ──────────► │ FastAPI (1 worker)   │         │              │
 │  → Room queue    │  /relay/sms │  → SQLite (WAL)      │ ──────► │ normal SMS   │
 │ WorkManager      │             │  → delivery worker   │  SMS    │ from Phone A │
 │  → retry/backoff │             │  → Telegram Bot API  │ (fallback│              │
 └──────────────────┘             └──────────────────────┘  only)  └──────────────┘
```

- Backend: Python 3.12, FastAPI, Uvicorn (**one** worker), HTTPX, SQLite.
- Public entry point: `https://ai-barrow.ir/relay/*` → Caddy → `127.0.0.1:8090`.
- Android app: Kotlin, Room + WorkManager, built **off** the VPS.

## Repository layout

```
sms-relay/
├── android-app/        Kotlin app (build on your machine or CI)
├── backend/            FastAPI service + pytest suite
│   ├── app/            config, db, security, ingest, api, tg, delivery, updater, main
│   └── tests/          206 unit tests, all mocked
├── deploy/             systemd unit, Caddy snippet, installer, backup/restore/purge
├── docs/               guides (see below)
├── scripts/            memory measurement helper
└── .env.example        template for the environment file
```

## Documentation

| Guide | Contents |
|---|---|
| [docs/vps-setup.md](docs/vps-setup.md) | Installing and running the backend on a small VPS |
| [docs/https-dns.md](docs/https-dns.md) | DNS, Caddy, TLS, health checks |
| [docs/android-local-build.md](docs/android-local-build.md) | Building the APK on your own machine |
| [docs/apk-install.md](docs/apk-install.md) | Installing the APK and granting permissions |
| [docs/telegram-bot-setup.md](docs/telegram-bot-setup.md) | Bot token, chat id, admin id, commands |
| [docs/env-reference.md](docs/env-reference.md) | Every environment variable |
| [docs/api.md](docs/api.md) | REST API reference |
| [docs/backup-restore.md](docs/backup-restore.md) | Backup, restore, retention, deletion |
| [docs/troubleshooting-samsung-xiaomi.md](docs/troubleshooting-samsung-xiaomi.md) | OEM background limits, diagnostics |
| [docs/security-privacy.md](docs/security-privacy.md) | Threat model, token hygiene, Telegram caveats |
| [docs/manual-e2e-checklist.md](docs/manual-e2e-checklist.md) | Manual end-to-end test checklist |

## Quick start

Backend (on the VPS, after the [VPS setup](docs/vps-setup.md)):

```bash
sudo deploy/install.sh          # user, dirs, venv, unit, Caddy merge
sudo nano /etc/sms-relay/env    # tokens + Telegram credentials
sudo systemctl start sms-relay-api.service
sudo caddy reload --config /etc/caddy/Caddyfile
curl -fsS https://ai-barrow.ir/relay/health && echo
```

Android (on your computer — **never** on the VPS):

```bash
cd android-app
gradle wrapper                  # only needed once, if the wrapper is missing
./gradlew assembleDebug
# output: android-app/app/build/outputs/apk/debug/app-debug.apk
```

## Honest limitations

- On Android 10 and newer the app must be the **default SMS app** to
  receive SMS at all (platform-enforced hard restriction on the SMS
  permission group). It does not replace your messenger in practice: it
  only observes and forwards, and hands "send to" intents back.
- Android decides when a broadcast receiver runs. Force-stop, aggressive OEM
  battery managers (Samsung, Xiaomi) and Doze can delay or block both
  receiving SMS and sending the fallback SMS. This project cannot and does
  not bypass those controls.
- Delivery is **at-least-mostly-once**, never exactly-once. A network timeout
  may still have reached Telegram; the backend records that as `uncertain`
  and may resend. You can end up with a duplicate Telegram message or a
  duplicate fallback SMS.
- If Phone A has neither internet nor cellular service, nothing is delivered
  until one of them returns. The queue on the phone and the database on the
  VPS are durable in the meantime.
- A Telegram bot reads and relays plain text through Telegram's servers.
  Telegram bots are **not** end-to-end encrypted and Telegram servers can
  read the content. Treat the chat as a channel you trust, not a secret
  channel. See [docs/security-privacy.md](docs/security-privacy.md).

## Development

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

The suite is fully mocked: no real SMS is sent and no real Telegram call is
made. The Android app has no build step here — see
[docs/android-local-build.md](docs/android-local-build.md).
