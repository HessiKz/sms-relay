# SMS Relay

A private, single-admin bridge between several Android phones and Telegram.

Every paired phone captures incoming SMS and uploads them to a small FastAPI
backend under its own per-device token. The backend stores them in SQLite,
tags each message with the device that captured it, and forwards it to one
private Telegram chat, where every forwarded SMS carries a device label. The
same backend exposes a device-first Telegram Mini App — devices, then the
per-device chats, then a per-device thread; there is no merged inbox — and a
command queue that lets the admin send an SMS from a specific phone through
Telegram or the Mini App. If Telegram delivery is confirmed failed, a phone
can still send the original message to a second phone over its own SIM, but
that cellular fallback is frozen: the code is kept, the bot controls that
wrote backend settings are disconnected from the phone, and the feature is
no longer being developed.

This document describes the code, not an aspiration. It was written from the
current source tree and intentionally includes the sharp edges that are
visible in the implementation.

## Topology

```
 Paired phones (Android)              VPS
 ┌────────────────────┐   HTTPS   ┌──────────────────────┐
 │ SmsReceiver /      │ ────────► │ Caddy /relay/*       │
 │ NotificationListener│          │   → 127.0.0.1:8090   │
 │   → Room queue     │           │ FastAPI, 1 worker    │
 │ WorkManager upload │ ◄──────── │ SQLite (WAL)         │
 │ CommandWorker      │  /commands│ DeliveryWorker       │
 │ SmsSender          │    /pair/*│ Updater (Telegram)   │
 │ PairingController  │           │ QR pairing           │
 └────────────────────┘           └──────────┬───────────┘
                                              │
                                       Telegram Bot API
                                              │
                                   one private admin chat
                            (each forwarded SMS carries a device label)
                                              │
                                       Telegram Mini App
                                 (device-first; served by the backend)
```

Each paired phone is one row in the `devices` table with its own bearer
token. There is no shared device token any more: the only configured token
is `ADMIN_TOKEN` for the administrative routes. The fallback target is an
ordinary second phone that receives plain SMS; it runs none of this code.

The public entry point is `https://ai-barrow.ir/relay/*`, which Caddy strips
to `/` and proxies to `127.0.0.1:8090`. The backend is never exposed directly.

## Repository map

```
sms-relay/
├── README.md                    this document
├── AGENTS.md                    binding rules for coding agents
├── android-app/                 Kotlin app (build off the VPS)
│   └── app/src/main/java/dev/smsrelay/
│       ├── SettingsStore.kt     local app settings and pairing state
│       ├── TokenStore.kt        AES-GCM/Keystore store for token and claim secret
│       ├── data/                Room database, daos and records
│       ├── net/                 RelayClient.kt (backend API), PairingClient.kt
│       ├── pairing/             PairingController.kt (flow), QrCode.kt (rendering)
│       ├── receiver/            boot and SMS receivers
│       ├── sms/                 sending, fallback policy, part trackers
│       ├── ui/                  activities (XML views)
│       ├── work/                UploadWorker, ImportWorker, CommandWorker, Schedule
│       └── util/                dedupe, phone numbers, role state
├── backend/
│   ├── app/                     FastAPI service
│   │   ├── main.py              app assembly, middleware, lifespan, CLI
│   │   ├── config.py            env-file parsing and Settings
│   │   ├── security.py          bearer auth, device-token lookup, rate limiting
│   │   ├── db.py                schema, migrations, queries, queue helpers
│   │   ├── pairing.py           QR pairing, approval and per-device tokens
│   │   ├── ingest.py            POST /sms
│   │   ├── importer.py          POST /import (historical inbox)
│   │   ├── api.py               device/admin REST API
│   │   ├── delivery.py          Telegram delivery worker and retries
│   │   ├── tg.py                Telegram Bot API client and error taxonomy
│   │   ├── updater.py           Telegram long-poll bot
│   │   ├── flow.py              in-memory button conversation
│   │   ├── tg_ui.py             keyboards and phone-number helpers
│   │   ├── miniapp.py           Mini App JSON API (device-scoped)
│   │   ├── miniapp_auth.py      Telegram initData validation
│   │   ├── rev.py               device-scoped Mini App revision counter
│   │   └── static/app/          Mini App shell, CSS and ES modules
│   └── tests/                   569 tests across 23 modules
├── deploy/                      systemd, Caddy, installer, backup/restore
├── docs/
│   ├── AGENT_GUIDE.md           implementation map and change recipes
│   └── AGENT_LOG.md             append-only work log
└── scripts/                     memory probe and Mini App menu helper
```

## Runtime model

The backend is deliberately a **single Uvicorn process**:

- one SQLite connection with WAL mode, `BEGIN IMMEDIATE` writes, and a
  process-wide write lock;
- one `DeliveryWorker` task that drains the `jobs` table;
- one `Updater` task that long-polls Telegram and drives the admin bot;
- an in-memory, device-scoped revision counter that wakes Mini App long
  polls and names the device and thread that changed;
- an in-memory Telegram update offset;
- in-memory authentication-failure and pairing rate limiters.

Running multiple workers would split or duplicate all of that state. Do not do
it without redesigning those components.

Each phone is the durable capture edge for its own messages: an SMS is written
to Room before any upload is attempted. The backend is the durable forwarding
edge: a message is written to `messages` and `jobs` before any Telegram call
is attempted. The system tolerates network loss at every hop; it does not
promise exactly-once delivery.

### Devices

A device is a paired phone. Its row carries the display name, platform and
app version, the SHA-256 hash and short prefix of its token (the token itself
is shown once at claim time), heartbeat data (`last_seen_at`, `sims`,
`sending_enabled`), and two lifecycle flags:

- `revoked_at` — the token stops working; the queued commands of the device
  are failed instead of waiting for a phone that will never pick them up.
- `archived` — the device was never paired in the current model. Exactly one
  such device exists: the `legacy` device that holds all pre-upgrade history
  read-only. It cannot be paired, renamed into an active role, or revoked.

Messages, commands, contacts, read markers and thread reads all carry a
`device_id`. Nothing is ever mixed: a device never sees the history, the
contacts or the unread counters of another device.

### Message states

| State | Meaning | Has a job? |
|---|---|---|
| `received` | held while forwarding is paused, or imported history | no |
| `pending` | queued for Telegram delivery | yes |
| `delivered` | Telegram accepted every chunk | no |
| `uncertain` | ambiguous failure (timeout/network/Telegram unavailable); still retrying | yes |
| `failed` | terminal failure; this is what can authorize the frozen cellular fallback | no |

Retries use exponential backoff from 30 seconds to one hour, with jitter, and
a maximum of 8 attempts. A Telegram `429` honors `retry_after`.

### Command states

| State | Meaning |
|---|---|
| `pending` | queued for the device |
| `sent` | radio accepted every part |
| `delivered` | carrier confirmed every part |
| `failed` | radio/carrier failure, unknown contact, interrupted, or sending disabled |
| `rejected` | phone refused because local sending is disabled |

Commands are device-scoped and reported by the owning phone through
`POST /commands/{id}/result`. The backend accepts only forward transitions,
so late or duplicate reports are harmless.

## End-to-end flows

### 1. Pairing a device

There is no token that can be pasted into the app. A phone earns its token:

1. The phone calls `POST /pair/start` with its stable device id, a display
   name, and a random claim secret that only this phone knows. The backend
   creates a pairing with a 5-minute TTL and answers with a QR payload and a
   short scan code (six characters from an unambiguous alphabet).
2. The phone renders the payload as a QR code on its own screen — drawn with
   `com.google.zxing:core`, no camera permission, no network round trip —
   plus the scan code in large letters and the time the code is still valid.
3. The admin scans the payload with the Mini App's native scanner (the Mini
   App never asks for the camera itself), or opens the t.me link; the Mini
   App shows a preview of the pending pairing before the admin approves it. Approval creates or reactivates the device but still issues no token.
4. The phone polls `POST /pair/claim` every two seconds (longer after a
   network error) with its claim secret. While approval is pending the answer
   is `202`; when it is approved the token is returned **exactly once** and
   the phone writes it into the encrypted store right away.

The QR payload carries the pairing id and the scan code only. The claim
secret never leaves the phone, so a photographed QR code cannot collect the
token; both the scan code and the claim secret are stored as hashes and the
secret is compared in constant time. Pairing starts are rate limited per
client address, the number of open pairings is capped, and wrong claim-secret
attempts are counted against the pairing, not against the phone that owns it;
a phone with the correct secret may poll as often as it needs.

A `401` on any device route means the device was removed or the server was
reset: the token is dropped, the queued messages stay queued and the phone
asks to be paired again with the same device id. Changing the server URL
drops the token as well, because a token belongs to one server only.
Approving a pairing for a device that is still active is refused (`409`,
already paired); re-pairing a revoked device reactivates it.

### 2. Incoming SMS

1. Android captures the SMS through the default-SMS-app broadcast or the
   notification listener.
2. It writes the message to Room and schedules `UploadWorker`.
3. `UploadWorker` sends `POST /sms` with the device's per-device bearer
   token; the backend resolves the device from the token, never from the
   body.
4. The backend applies filters, checks cross-path duplicates, stores the
   row as `pending` (or `received` while paused), inserts a `jobs` row, and
   tags it with the authenticating `device_id`.
5. `DeliveryWorker` claims the job, formats the message — including the
   device label that tells the admin which phone it came from — and sends it
   to the configured Telegram chat. A failed label lookup must never block
   delivery.
6. On success the message becomes `delivered`. On a definitive error it is
   retried. On exhaustion it becomes `failed`.

### 3. Paused forwarding

`/pause`, the pause button, or `POST /admin/pause` sets `settings.paused=1`.
New live messages are stored as `received` with no job. They remain visible
in the Mini App under their device. `/resume` or the pause button sets the
flag back to `0` and calls `db.resume_received()`, which moves `received`
rows to `pending` — but **never imported history**. An import is a replay of
the phone's own inbox, not a message to forward, so `resume_received()`
excludes `source='import'`.

### 4. Historical import

The Android app uploads the phone's existing inbox in batches of 100
items (the backend accepts up to 200). The backend stores these rows with
`source='import'`,
`state='received'` and a deterministic id, scoped to the authenticating
device only: an import never touches another device's history. They never
enter the Telegram delivery queue and they are never resumed; their purpose
is to make old conversations visible in that device's Mini App threads.

### 5. Send from Telegram

A `/sms` command or a reply to a forwarded message creates an `sms_commands`
row bound to one device:

- A reply goes to the device that received the original message. If that
  device is revoked or archived, the bot refuses instead of guessing.
- A plain `/sms` goes to the configured `default_device_id`; when no default
  is set but exactly one device is active, that device is used. With several
  active devices and no default, the bot asks the admin to pick a default in
  the Mini App (there is no broadcast).

The phone polls `GET /commands` every few seconds and receives only its own
commands, writes a local guard row, sends through its SIM, and reports the
result. The backend notifies the Telegram chat for `sent`, `failed`, and
`rejected`; `delivered` is silent.

### 6. Send from the Mini App

The Mini App is device-first: `#/` shows the devices, `#/d/<device_id>` the
conversations of that device, and `#/d/<device_id>/t/<thread_key>` one thread.
A send always goes through the device in the route. The backend resolves the
thread key, checks the unknown-number confirmation against **that device's**
contacts and history (another device's contact list must not make a number
known here), enforces 6 sends/minute and 50/day across the whole backend, and
creates an `sms_commands` row for that device with a unique `client_ref`.
Retries with the same `client_ref` return the existing command instead of
sending twice; the same ref under another device or another thread is a
conflict, never a second SMS.

### 7. Cellular fallback (frozen)

After each upload cycle, Android asks the backend for the state of recent
messages. A fallback SMS to the second phone is sent only when:

- the backend reports `state='failed'`; or
- the upload was locally rejected with a 4xx and the backend has no row.

Timeouts, `uncertain`, `429`, and `5xx` never authorize a fallback. The caps
count only radio-confirmed sends. The feature is frozen, not removed: the
policy code and the caps still work, but the backend `/fallback` and
`/phoneb` bot controls are disconnected from the phone (see
[Known code-level issues](#known-code-level-issues)) and no new work is
planned on this path.

## HTTP surface

Device routes require a per-device token issued by QR pairing:
`Authorization: Bearer sr_…`. Admin routes require
`Authorization: Bearer $ADMIN_TOKEN`; "either" accepts both. Any other
credential on a device route — including a token from a server reset or an
old shared-token APK — is answered with `401`.

| Method and path | Auth | Purpose |
|---|---|---|
| `POST /pair/start` | public, rate limited | begin pairing; returns pairing id, scan code, QR payload, expiry |
| `POST /pair/claim` | public (claim secret) | phone polls; the token is handed out exactly once |
| `POST /sms` | device | ingest one live SMS |
| `POST /import` | device | ingest historical SMS in batches for that device |
| `GET /sms-state?ids=...` | either | delivery states for Android's fallback sweep |
| `GET /messages` | admin | paged message history, optionally filtered by device |
| `GET /commands` | device | that device's pending outbound commands + heartbeat |
| `POST /commands/{id}/result` | device | report outbound command status |
| `POST /contacts` | device | replace the contact list of that device |
| `GET /contacts` | admin | read contacts |
| `GET /status` | either | pause flag, queue depth, counts, device status |
| `POST /admin/pause` | admin | pause forwarding |
| `POST /admin/resume` | admin | resume and enqueue held live messages (never imports) |
| `GET/PUT /admin/settings` | admin | backend settings |
| `GET/PUT /admin/filters` | admin | allow/block/keyword filters |
| `GET /health` | public | liveness |

### Mini App API

The whole `/app/api/*` surface is guarded by `require_miniapp` (Telegram
`initData`). Every conversation, contact and read route is scoped to the
device in the path; the old device-less routes answer `410` so a stale build
fails loudly instead of reading a mixed inbox.

| Method and path | Purpose |
|---|---|
| `GET /app/api/me` | the Telegram user behind the request |
| `GET /app/api/devices` | device list with online state, unread, preview, pending commands, default flag |
| `PATCH /app/api/devices/{id}` | rename a device |
| `POST /app/api/devices/{id}/revoke` | revoke a paired device |
| `POST /app/api/devices/{id}/default` | choose the device for Telegram `/sms` |
| `POST /app/api/pair/preview` | show a pending pairing before approval |
| `POST /app/api/pair/approve` | approve the pairing shown on the phone |
| `POST /app/api/pair/cancel` | cancel a pending pairing |
| `GET /app/api/devices/{id}/chats` | the conversations of one device |
| `GET/POST /app/api/devices/{id}/chats/{thread}/messages` | thread history / queue a send on that device |
| `POST /app/api/devices/{id}/chats/{thread}/read` | move the read marker of one thread |
| `GET /app/api/devices/{id}/contacts` | the contacts of one device |
| `GET /app/api/status` | relay header state + device summaries (no conversations) |
| `GET /app/api/events` | the long poll; names the device and thread that changed |
| `GET/POST /app/api/chats…`, `GET /app/api/contacts` | removed routes; always `410` |

Both lists are paged with a cursor of two halves, so nothing is skipped when
two events share a second:

- `GET /app/api/devices/{id}/chats/{thread}/messages?before_ts=<ts>&before_id=<event id>`
  returns the next older page of the conversation. `before_ts` alone keeps
  working for older clients. `has_more` stays true until the real beginning
  of the thread.
- `GET /app/api/devices/{id}/chats?before_ts=<ts>&before_key=<thread key>`
  returns the next older page of the conversation list. `before_ts` alone is
  the strict cursor: the conversations of that second are left behind, so
  none is handed out twice.
- `GET /app/api/events` answers `{"rev", "changed", "threads"}`. `threads`
  names the conversations that changed — every entry carries a `device_id`
  and a `thread_key` (`null` for a device-wide change) — or is `null` when
  the scope is unknown and the client has to reload everything.

## Configuration

The backend reads a plain `KEY=value` env file (no `export`, no shell
expansion). The systemd unit uses `/etc/sms-relay/env`; local runs can pass
`--env-file`.

### Required

| Key | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | bot that delivers messages and runs the admin bot |
| `TELEGRAM_CHAT_ID` | private chat that receives everything |
| `ADMIN_TELEGRAM_USER_ID` | numeric Telegram user id allowed to command the bot |
| `ADMIN_TOKEN` | bearer token for administrative routes |
| `DATABASE_PATH` | SQLite path, e.g. `/var/lib/sms-relay/sms.db` |

There is no device-token setting. Every phone pairs through the Mini App and
receives its own `sr_…` token.

### Optional

| Key | Default | Meaning |
|---|---|---|
| `UVICORN_HOST` | `127.0.0.1` | bind address; keep on loopback |
| `UVICORN_PORT` | `8090` | bind port |
| `LOG_LEVEL` | `INFO` | Uvicorn/app log level |
| `MINIAPP_URL` | unset | public Mini App URL, e.g. `https://ai-barrow.ir/relay/app/`; fallback for QR links when no bot username is set |
| `MINIAPP_BOT_USERNAME` | unset | bot username used for `t.me/<bot>?startapp=` QR links; without it the backend falls back to `MINIAPP_URL` or a raw pairing code |
| `MINIAPP_AUTH_MAX_AGE` | `86400` | accepted initData age in seconds |
| `MINIAPP_SEND_PER_MINUTE` | `6` | Mini App send cap |
| `MINIAPP_SEND_DAILY_CAP` | `50` | Mini App daily send cap |
| `RELAY_DISABLE_UPDATER` | unset | set to `1` to disable the Telegram poller (tests/dev) |

Generate the admin token with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Do not reuse the Telegram bot token of another running process. Telegram
allows only one `getUpdates` consumer per token.

## Development

### Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

The suite is 569 tests across 23 modules; 565 pass and four are known
pre-existing failures (two contact-search tests, one chat-search test, and
one shutdown-timing error).

Run the service locally:

```bash
cd backend
.venv/bin/python -m app.main --env-file /path/to/env --host 127.0.0.1 --port 8090
```

`GET /health` returns `{"ok":true}`. The delivery worker and Telegram updater
start with the app unless `RELAY_DISABLE_UPDATER=1`.

### Android

Build on a normal development machine or through the GitHub Actions workflow;
never build on the production VPS.

Requirements: JDK 17, Android SDK platform 35, build-tools 35.0.0, and Gradle
8.11.1 (AGP 8.7.2 does not support Gradle 9). The repository does not commit
the Gradle wrapper JAR, so generate it once:

```bash
cd android-app
gradle wrapper --gradle-version 8.11.1
./gradlew assembleDebug
```

Output: `android-app/app/build/outputs/apk/debug/app-debug.apk`.

Every Android change is built by the GitHub Actions workflow. The rolling
debug build is published as a public release asset:

<https://github.com/HessiKz/sms-relay/releases/download/android-latest/sms-relay-debug.apk>

Do not commit an APK to the repository or hand out an Actions artifact URL
(artifacts require a GitHub login). If the workflow fails, fix the build before
deploying. The workflow signs the APK with a stable debug key stored in GitHub
Actions secrets, so a newly downloaded build can update the previous one.

The app targets SDK 35 and supports API 26+. On Android 10+ it must be made
the default SMS app to receive the SMS permission group, or the user must
grant notification access and rely on the notification-listener capture
path. `RoleState` reports at startup which capture paths are actually
available, so the UI never lies about the default-SMS role.

Pairing and token storage live in the app: `PairingController` drives the QR
flow, `QrCode` renders the payload on the phone, `PairingClient` talks to
`/pair/*`, and `TokenStore` seals the device token and the running claim
secret with AES-GCM under an Android Keystore key. Nothing in there is
logged.

### Mini App

Set `MINIAPP_URL` to the public `/relay/app/` URL and restart the backend. The
bot then gains Mini App buttons. The menu button can be set independently:

```bash
python3 scripts/set_miniapp_menu.py \
  --env-file /etc/sms-relay/env \
  --url https://ai-barrow.ir/relay/app/
```

The Mini App authenticates with Telegram `initData`; it cannot be used from a
normal browser without a valid Telegram session. QR scanning uses the native
Telegram scanner (`showScanQrPopup`); the shell's Permissions-Policy forbids
camera access, so there is no `getUserMedia` anywhere.

The inbox keeps the whole history it has loaded: a revision-driven refresh
fetches the newest page, merges it by event id and updates the bubbles that
changed instead of rebuilding the thread, so the scroll position survives and
a burst of messages costs one reload. Older pages are fetched with the
"Load older messages" button, older conversations with "Load more
conversations". The revision counter is device-scoped, so a burst touches
only the threads that actually changed.

## Deployment

The deployment pack is under `deploy/`:

| File | Purpose |
|---|---|
| `sms-relay-api.service` | single-process, hardened systemd unit |
| `sms-relay.caddy-snippet` | `handle /relay/*` block for the existing site |
| `env.template` | template for `/etc/sms-relay/env` |
| `install.sh` | idempotent installer; does not start the service unless `--start` |
| `backup.sh` | consistent SQLite backup via the online backup API |
| `restore.sh` | integrity-checked restore with a `.pre-restore` copy |
| `purge-history.sh` | delete history by age, state, or all |

Typical first install:

```bash
sudo deploy/install.sh
sudo editor /etc/sms-relay/env       # fill required values
sudo systemctl start sms-relay-api
sudo caddy reload --config /etc/caddy/Caddyfile
curl -fsS https://ai-barrow.ir/relay/health
```

The installer backs up the Caddyfile before editing it, inserts the handler
only once, runs `caddy validate`, and restores the backup if validation fails.
It does not restart an existing service.

### Upgrading to the multi-device model

Upgrading is not a backend-only change:

1. Install the new backend and fill the env file (no device token any more).
2. On the next start the migration moves every pre-upgrade message, command
   and contact under one archived `legacy` device. That history stays
   visible in the Mini App, read-only, under its own device entry.
3. Install the new APK on every phone and pair each phone through the QR
   flow. An old APK that still presents a shared token gets `401` on every
   device route; it keeps its messages queued locally until it is upgraded.
4. Pick a `default_device_id` in the Mini App when more than one phone is
   paired, so Telegram `/sms` knows where to go.

The service runs as `smsrelay`, binds to loopback, and is restricted by
systemd (`NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`,
`CapabilityBoundingSet=`, memory caps). Only `/var/lib/sms-relay` is writable.

### Backup and restore

```bash
sudo deploy/backup.sh
sudo deploy/restore.sh /var/lib/backups/sms-relay/sms-relay-YYYYmmdd-HHMMSS.db.gz
sudo deploy/purge-history.sh --older-than 30
```

Backups are gzipped and mode 0600. A restore checks database integrity before
stopping the service and keeps the previous database as `.pre-restore`.

## Security model

- **Per-device tokens** authenticate each Android device. A token is issued
  once at claim time; the database stores only its SHA-256 hash and a short
  prefix, never the token. Token lookup and comparison are constant time,
  and failed attempts are rate-limited with a limiter keyed by the digest of
  the presented credential, so an attacker cannot lock out a real device by
  guessing other tokens.
- **Admin token** authenticates administrative REST calls; it is compared in
  constant time.
- **Pairing** uses a 5-minute code with a hashed, one-time scan code and a
  hashed claim secret. The claim secret never leaves the phone, so a
  photographed QR code is useless. The endpoints are rate limited per
  address and per pairing, and the number of open pairings is capped.
- **Android storage**: the device token and the running claim secret are
  sealed with AES-GCM under a key that lives in the Android Keystore; only
  the cipher text reaches the disk, in a private preferences file. App
  backup is disabled, so it is not included in the Android backup transport.
  Nothing is written to plain preferences or logs.
- **Mini App** requests carry Telegram `initData` in `X-Telegram-Init-Data`.
  The backend validates the Bot API HMAC, enforces a maximum age, and
  requires the Telegram user id to equal the configured admin.
- The Mini App is served with a restrictive CSP, no CORS, no inline scripts,
  and `frame-ancestors` limited to Telegram. Message text is rendered with
  `textContent`, never as markup.
- Request bodies are capped at 512 KiB before parsing.
- The SQLite database contains full SMS bodies in plaintext. Protect the file
  and its backups accordingly. Telegram bots are not end-to-end encrypted;
  Telegram servers can read messages.
- No SMS body, token, claim secret, or pairing payload is ever logged.

## Known code-level issues

These are confirmed from the implementation and should be considered before
relying on the affected behavior.

1. **Chunk limit mismatch.** `delivery.MAX_CHUNK_LEN=4090` exceeds
   `tg.MAX_TEXT_LEN=4000`. A 4,001–4,090 character chunk raises `ValueError`
   and the message is permanently marked `failed`.
2. **Backend fallback settings are disconnected.** `/fallback` and `/phoneb`
   update backend `settings`, but Android's fallback policy reads only its
   local `SettingsStore`. The bot controls have no effect on the phone.
3. **3–4 digit shortcodes are unsendable.** The backend accepts `short:110`,
   but Android requires at least 5 digits and Telegram's number normalizer
   requires 5–20.
4. **Large import batches can stall permanently.** A full batch of long
   bodies can exceed the 512 KiB request cap; `ImportWorker` treats the 413
   as permanent and does not advance its cursor or split the batch.
5. **Filters use raw strings and case-sensitive keywords.** Allow/block
   matching does not use canonical thread keys, and keyword matching is a
   case-sensitive substring check.
6. **The Telegram update offset is memory-only.** A restart after handling an
   update but before the next poll can redeliver it; a redelivered `/sms`
   becomes a new command and can send a duplicate SMS.
7. **Fallback sweeps can starve old failures.** Candidates are the 50 newest
   messages in a 48-hour window, so older failed messages may never be
   checked.
8. **Partial multi-chunk sends duplicate text.** A failed later chunk causes
   the earlier chunks to be resent on retry.
9. **Eight ambiguous failures become `failed`.** After the attempt limit, a
   timeout/network error becomes terminal, which can authorize a fallback
   even though Telegram may have accepted the message.
10. **FastAPI's default `/docs` and `/openapi.json` are enabled.** They are
    reachable through `/relay/docs` and `/relay/openapi.json` without auth
    unless disabled at the app or proxy level.
11. **The Telegram bot's contact search can still use the legacy list.**
    `flow.py` falls back to the global `contacts` table when the resolved
    device has no matching contact. The Mini App is strictly per-device;
    the bot's name picker can mix legacy names until it is made
    device-aware.

## Testing

The backend suite is the primary automated safety net: 569 tests across 23
modules; 565 pass and four are known pre-existing failures. It uses fakes for
Telegram, so no real message is sent. The Android app has no instrumentation
tests, and the Mini App has only Python-side static assertions.

Run:

```bash
cd backend
.venv/bin/python -m pytest tests -q
```

For any change, run at least the directly affected test module; run the full
suite before deployment.

## Documentation and agent rules

- [AGENTS.md](AGENTS.md) — binding rules for every coding agent. The most
  important rule: **append an entry to `docs/AGENT_LOG.md` before finishing any
  task.**
- [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) — module-by-module implementation
  map, invariants, change recipes, testing, and known traps.
- [docs/AGENT_LOG.md](docs/AGENT_LOG.md) — append-only history of agent work.

If code and documentation disagree, inspect the code and update this document
as part of the change. Documentation drift is a bug.
