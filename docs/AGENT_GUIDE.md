# Agent Guide

Practical, code-level guidance for working on the SMS relay. Read
[`../AGENTS.md`](../AGENTS.md) first; it contains the binding rule that every
task must append an entry to [`AGENT_LOG.md`](AGENT_LOG.md).

The code is the source of truth. This guide explains where things live, which
invariants must not be broken, how to change each part, and which known traps
already exist.

## 1. What this project is

A multi-device SMS bridge for a single admin:

- **Phones (devices)** capture SMS, keep a durable Room queue, upload them,
  poll for outbound commands and send through their own SIM. A phone is not
  trusted by default: it has to be paired with the backend through a QR
  hand-shake before it may talk to it.
- **Backend (VPS)** receives, stores, filters and forwards messages to one
  private Telegram chat. Every forwarded message carries the name of the
  device that captured it. It also runs the admin bot and serves the Mini App.
- **Mini App** is a Telegram WebApp with a composer. It is device-first: the
  admin picks a device and sees only that device's conversations. It talks to
  the backend with Telegram `initData` and creates the same `sms_commands`
  rows as the Telegram bot.

Three rules shape everything else:

- **Per-device isolation.** Every message, command, contact and read marker
  belongs to exactly one device. A thread is the pair
  `(device_id, thread_key)`: the same phone number on two phones is two
  different threads, and a device never sees the history, the contacts or
  the unread counters of another device.
- **One shared Telegram chat.** There is no per-device chat. The device label
  on the forwarded message and the device-first Mini App keep the streams
  apart; the bot routes a reply back to the device that captured the message.
- **The Phone B fallback is frozen.** The fallback to a second phone over its
  own SIM is deprecated in this model and off by default. The live fallback
  configuration is the phone's local `SettingsStore`, not the backend
  settings; `/fallback` and `/phoneb` in the bot only write backend rows the
  phone never reads.

There is no multi-tenant model, no user table, and no public API consumer. The
admin is identified by `ADMIN_TELEGRAM_USER_ID`; a phone is identified by its
own bearer token.

## 2. Golden rules

1. **Single process.** The backend uses one SQLite connection, one updater
   offset, one revision counter, and in-memory rate limiters. Do not run
   multiple Uvicorn workers.
2. **A thread is `(device_id, thread_key)`. Never mix devices.** Every query
   that reads or writes conversation data filters by `device_id` first, and
   every new query must do the same. A thread key without a device is a bug,
   not a shortcut. The device id comes from the bearer token, never from the
   request body.
3. **Pairing is a credential boundary.** The QR code carries the pairing id
   and the short scan code only: never a token, never the claim secret. The
   scan code and the claim secret are stored as SHA-256 digests, the claim
   secret never leaves the phone, and the device token is issued exactly
   once, in the answer of `POST /pair/claim`. A revoked or archived device
   is refused by the token lookup, so a device that gets a 401 has to pair
   again.
4. **Commands are never broadcast; ownership is strict.** A command belongs
   to exactly one device. The phone only polls the commands of its own
   device, and a result report for a command of another device is answered
   with 404 so a phone cannot even learn that the command exists.
5. **Durability before network.** Android writes to Room before uploading.
   The backend writes `messages` + `jobs` before calling Telegram.
6. **No message body in logs.** Log ids, kinds, counts, and state, never SMS
   text, phone numbers, contact names, tokens, or initData.
7. **Fallback is conservative.** Only a confirmed backend `failed` state (or a
   local permanent 4xx rejection with no backend row) authorizes the cellular
   fallback. `timeout`, `network`, `uncertain`, `429`, and `5xx` never do.
8. **A command is executed at most once.** The local `handled_commands` guard
   row is written before the radio call. A lost report may duplicate a status
   message, never an SMS.
9. **Additive, idempotent migrations.** Do not destroy the message queue.
10. **Update the documentation and the agent log** as part of the change.

## 3. Backend map

| File | Responsibility | Change it when |
|---|---|---|
| `backend/app/main.py` | FastAPI assembly, middleware, lifespan, CLI, static Mini App mount | adding middleware, startup tasks, routes at app level |
| `backend/app/config.py` | env-file parsing and immutable `Settings` | adding/renaming an environment variable |
| `backend/app/security.py` | bearer-token dependencies (per-device, admin) and failure rate limiting | auth rules, token handling |
| `backend/app/db.py` | schema, migrations, transactions, every SQL query, queue helpers | data model, states, queue semantics, new queries |
| `backend/app/phones.py` | sender/thread-key normalization | number formats, shortcode rules, thread identity |
| `backend/app/ingest.py` | `POST /sms` live ingest, filters, dedupe, pause | live-ingest behavior |
| `backend/app/importer.py` | `POST /import` historical inbox | import format, limits, import dedupe |
| `backend/app/api.py` | device/admin REST API, status, settings, filters | REST contracts or admin controls |
| `backend/app/pairing.py` | public pairing endpoints (`/pair/start`, `/pair/claim`) and the Mini App pairing/device endpoints | pairing flow, device lifecycle, device list |
| `backend/app/delivery.py` | delivery worker, chunking, retry policy, Telegram buttons, device label | forwarding behavior |
| `backend/app/tg.py` | Telegram HTTP client and error taxonomy | Bot API calls, error classification |
| `backend/app/updater.py` | long-poll loop, bot commands, callbacks, command creation, default-device routing | bot commands and Telegram UX |
| `backend/app/flow.py` | in-memory guided conversation state | button conversation steps |
| `backend/app/tg_ui.py` | keyboard builders, masking, number normalization | keyboard layout, typed-number parsing |
| `backend/app/miniapp.py` | `/app/api/*` handlers, send caps, idempotency | Mini App API behavior |
| `backend/app/miniapp_auth.py` | Telegram initData HMAC and admin check | Mini App auth |
| `backend/app/rev.py` | in-process revision counter, event and scope history | Mini App refresh semantics |
| `backend/app/static/app/` | Mini App shell, CSS, JS modules | Mini App UI |

`PHONE_A_TOKEN` is **removed**. There is no shared phone token any more:
each device gets its own token (`sr_…`, `pairing._new_token()`) at claim
time, and only its SHA-256 digest is stored in `devices.token_hash` (the
first 11 characters are kept in `devices.token_prefix` for the UI).

### Data model essentials

- `devices`: one row per paired phone.
  - `id` is the id the phone generates itself
    (`SettingsStore.deviceId`, a random UUID that survives a re-pair).
  - `token_hash` is the SHA-256 digest of the device token, `token_prefix`
    its short prefix. The token itself is never stored.
  - `revoked_at` marks a withdrawn token, `archived` marks the read-only
    legacy device.
  - `last_seen_at`, `app_version`, `sending_enabled` and `sims_json` come
    from the command-poll heartbeat and the throttled touch
    (`DEVICE_TOUCH_INTERVAL_S = 30`).
- `pairings`: one row per pairing attempt.
  - `scan_code_hash` and `claim_secret_hash` are SHA-256 digests; the plain
    values are never stored.
  - `status` is `pending`, `approved`, `claimed`, `expired` or `cancelled`.
  - The token is issued exactly once: `claimed_at` is set in the same
    transaction that writes `devices.token_hash`.
- `device_contacts`: the contact list of one device, primary key
  `(device_id, number)`.
- `thread_reads_scoped`: the read markers of one device, primary key
  `(device_id, thread_key)`.
- `messages`: one row per inbound SMS, including imported history.
  - `id` primary key; live ids come from Android (UUID); import ids are
    deterministic `imp-<hash>`.
  - `device_id` is the device that captured the message.
  - `thread_key` is the canonical conversation key (`989...`, `short:110`,
    `raw:bank`).
  - `source` is `sms`, `notification`, or `import`.
  - `state` is one of `received`, `pending`, `delivered`, `failed`,
    `uncertain`.
  - `telegram_msg_id` stores the last successfully sent Telegram message id;
    it powers reply-to-SMS.
- `jobs`: one row per message that still needs forwarding. It has its own
  `attempts`, `next_attempt_at`, and `locked_at`. A job lock older than 300 s
  is stale and can be reclaimed after a crash.
- `sms_commands`: outbound requests. `device_id` is mandatory: it names the
  phone that has to run the command. Status is `pending`, `sent`,
  `delivered`, `failed`, or `rejected`. `client_ref` is a partial unique
  index used for Mini App idempotency.
- `contacts`, `filters`, `settings`, `thread_reads`, `device_status` are
  supporting tables. `contacts`, `thread_reads` and `device_status` are the
  pre-split tables: they are kept for a rollback, but the new readers never
  use them — the Mini App names come from `device_contacts`, the unread
  counters from `thread_reads_scoped`, the heartbeats from `devices`.
- Writes go through `db._write_tx` (`BEGIN IMMEDIATE`) and the process-wide
  `_WRITE_LOCK`. Do not execute ad-hoc writes outside that mechanism.

### The archived legacy device

`db.backfill_legacy_device()` moves every row from before the device split
under one archived device `legacy` ("Previous device (archived)"). It runs
inside `migrate()`, is idempotent and has a fast path, so a restart never
duplicates or loses a row. The legacy device is read-only: it has no token,
cannot pair, cannot poll commands and cannot send. The old global tables
(`contacts`, `thread_reads`) are copied into `device_contacts` and
`thread_reads_scoped` for the same device and then left alone.

### Message state machine

```
new live SMS, not paused ──► pending ──► delivered
        │                       │
        │                       ├── retryable/ambiguous ──► pending/uncertain (job stays)
        │                       └── attempts exhausted or invalid input ──► failed
        │
new live SMS, paused ──► received ──(resume)──► pending
imported history ──────► received  (no job, not forwarded)
```

- `failed` deletes the job and is terminal.
- `uncertain` still has a job and is retried.
- The delivery worker claims up to 5 due jobs per cycle, marks `locked_at`,
  and processes them sequentially.
- Retry backoff: 30 s base, doubling to 3600 s, ±10% jitter. Maximum 8
  attempts. `rate_limited` uses Telegram's `retry_after` when present.
- A Python `ValueError` during formatting/sending is treated as permanent
  `invalid_input`.
- `db.resume_received()` never resumes `source='import'` rows: an import is
  a replay of the phone's own inbox, not something to forward. It can be
  limited to one device (`device_id=...`) or to all of them.
- `db.find_recent_duplicate()` treats only another capture path of the same
  device as a twin; an import never swallows a live SMS.

### Command state machine

`pending` → `sent` → `delivered`, with `failed`/`rejected` as terminal
alternatives. `db.finish_command` rejects backwards or duplicate transitions
and reports whether the report was accepted. The phone may report `sent`
first and later `delivered`.

`device_id` is mandatory on a command: `POST /commands/{id}/result` looks the
command up and compares its `device_id` with the authenticated device. A
mismatch is a 404, never a silent accept. Revoking a device fails its pending
commands (`db.fail_device_commands`).

## 4. API and auth contracts

- Device token: a per-device bearer token, header
  `Authorization: Bearer sr_...`. The lookup is a SHA-256 digest lookup in
  `devices.token_hash`; a revoked (`revoked_at`) or archived (`archived=1`)
  device is rejected even when the digest still matches. Failed attempts are
  rate-limited per presented credential, and the heartbeat is refreshed at
  most every 30 s.
- Admin token: `ADMIN_TOKEN`, same header. The comparison is constant-time
  after SHA-256; failed attempts are rate-limited per presented credential.
  `require_any` accepts either the admin token or a device token; a device
  token is scoped to that device.
- Mini App: `X-Telegram-Init-Data` header only. The backend validates the Bot
  API HMAC, age (`MINIAPP_AUTH_MAX_AGE`), future skew, and that `user.id`
  equals `ADMIN_TELEGRAM_USER_ID`.
- Request bodies are capped at 512 KiB before parsing.
- `/health` is public. FastAPI's default `/docs` and `/openapi.json` are
  currently also reachable unless disabled; do not add sensitive examples to
  schemas.

Pairing (public, no token, rate limited per address):

| Route | Purpose |
|---|---|
| `POST /pair/start` | the phone asks for a pairing; answer: `pairing_id`, `scan_code`, `qr_payload`, `expires_at`. No token. |
| `POST /pair/claim` | the phone polls with its claim secret; `202` = waiting for the admin, `200` = the token, exactly once. |

Mini App pairing and devices (admin only):

| Route | Purpose |
|---|---|
| `POST /app/api/pair/preview` | look up a pairing by id + scan code, before approving |
| `POST /app/api/pair/approve` | approve it; creates or reactivates the device, still without a token |
| `POST /app/api/pair/cancel` | cancel a pending pairing |
| `GET /app/api/devices` | the device list with online state, SIMs, unread and preview |
| `PATCH /app/api/devices/{id}` | rename a device |
| `POST /app/api/devices/{id}/revoke` | revoke the token and fail the pending commands |
| `POST /app/api/devices/{id}/default` | choose the device the `/sms` bot command sends with |

Mini App conversations (admin only, always device-scoped):

| Route | Purpose |
|---|---|
| `GET /app/api/devices/{id}/chats` | the conversations of one device |
| `GET /app/api/devices/{id}/chats/{thread}/messages` | the timeline of one thread |
| `POST /app/api/devices/{id}/chats/{thread}/messages` | queue an SMS with that device's SIM |
| `POST /app/api/devices/{id}/chats/{thread}/read` | move the read marker of that thread |
| `GET /app/api/devices/{id}/contacts` | the contacts of one device |

The old device-less routes (`/app/api/chats`,
`/app/api/chats/{thread}/messages`, `/app/api/chats/{thread}/read`,
`/app/api/contacts`) answer **410 Gone** with
`{"error": "moved"}`, so an old Mini App build fails loudly instead of
reading a mixed inbox.

Global Mini App routes: `GET /app/api/me`, `GET /app/api/status` (the relay
state plus the device list; it contains no conversation of any device) and
`GET /app/api/events?rev=N&timeout=25` (the revision long poll).

Device surface: `POST /sms`, `POST /import`, `GET /commands`,
`POST /commands/{id}/result`, `POST /contacts`.
Admin surface: `GET /messages`, `GET /contacts`, `POST /admin/pause`,
`POST /admin/resume`, `GET/PUT /admin/settings`, `GET/PUT /admin/filters`.
Shared: `GET /sms-state`, `GET /status`.

`/admin/settings` knows `default_device_id`; it must name an active device.
Revoking the default device clears the setting.

### The default-device resolver

`db.resolve_command_device()` picks the device a bot command goes to:

1. an explicitly requested device, when it is active;
2. `settings.default_device_id`, when it is active;
3. the only active device, when exactly one is paired;
4. `None` otherwise — the bot then answers with a plain error instead of
   broadcasting ("Several devices are paired, so I do not know which one
   should send this").

### The events scope payload

`GET /app/api/events` waits on `app.state.rev_event`. The revision counter is
in memory and resets to 0 on restart; a client that is ahead of the server is
told to reload instead of waiting for a change that already happened.

The answer names the scope of the change:

```json
{"rev": 42, "changed": true,
 "threads": [{"device_id": "…", "thread_key": "989…"},
             {"device_id": "…", "thread_key": null}]}
```

- `thread_key: null` means a device-wide change (every conversation of that
  device may have changed).
- `threads: null` means the scope is unknown: the client must reload
  everything. That happens when the scope window is too old, the counter was
  reset, the change was global, or a change named a thread without its
  device.

## 5. Mini App map

- `static/app/index.html` is the shell; `<base href="/relay/app/">` is
  required by the Caddy prefix.
- `assets/js/telegram.js` wraps the Telegram WebApp SDK and exposes
  `initData`, the start parameter and the native QR scanner.
- `assets/js/api.js` sends initData only in the header and classifies
  auth/rate-limit/network errors.
- `assets/js/store.js` holds the device-first inbox state, loads chats and
  threads per device, sends messages, and runs the revision long poll with
  backoff.
- `assets/js/ui.js` renders every user-controlled string with `textContent`;
  never switch to `innerHTML` for message content.
- `assets/js/app.js` handles routing, search, composer, the device menu, the
  pairing sheet and the Telegram navigation buttons.

Routes are device-first:

| Route | Screen |
|---|---|
| `#/` | the device list |
| `#/d/<device_id>` | the conversations of that device |
| `#/d/<device_id>/t/<thread_key>` | one thread of that device |

A deep link from the bot carries `?device=<id>&thread=<key>`
(`delivery.thread_url`); the start parameter may carry the same values as
`device_…`/`thread_…`. `app.js` reads both and opens the matching thread.
There is **no merged inbox**: switching the device resets the chats, the
thread and the pending bubbles of the old one (`store.selectDevice`), and
every list is loaded per device.

Pairing in the Mini App uses the **native** `showScanQrPopup` of the Telegram
client (`telegram.js`). The Mini App must never ask for the camera itself:
the shell sends `Permissions-Policy: camera=()` and a
`Content-Security-Policy` with `default-src 'self'` and no `camera-src`, and
only Telegram can scan a code inside its own UI. A client without
`showScanQrPopup` falls back to the scan code typed by hand. The pairing
sheet previews the pairing, approves it or cancels it; the device menu
renames a device, sets it as the default and revokes it.

### Mini App history and refresh rules
- The thread timeline is paged with a cursor of **two halves**: the timestamp
  and the event id (`msg:<id>` / `cmd:<id>`). `db.thread_timeline()` compares
  them together, so events that share a second are never skipped. A
  timestamp-only cursor keeps the old behaviour for older clients: the events
  of that second are left behind, and no event is ever returned twice.
- `db.chat_summaries()` is paged with the timestamp and the thread key.
  `before_ts` **without** `before_key` is the strict cursor (`l.ts < ?`); the
  composite condition is only used when both halves are present, so a
  timestamp-only cursor cannot hand out a row twice.
- Both helpers take a limit above the public page size on purpose: a
  handler asks for one row more than it returns so `has_more` is correct.
- A revision-driven refresh must **merge**, never reset: fetch the newest
  page, diff by event id, and update the rows that changed. The DOM nodes are
  cached in `ui.js`; `renderThread()` and `renderChatList()` reconcile the
  children instead of calling `replaceChildren()`.
- The long poll answers with the scope of the change. When the open
  conversation is not in it, the thread is not reloaded.

## 6. Android map

| Area | Files | Responsibility |
|---|---|---|
| Capture | `receiver/SmsReceiver.kt`, `sms/SmsNotificationListener.kt` | default-SMS broadcast path and notification-listener path |
| Dedupe | `util/Dedupe.kt`, `util/SenderNormalizer.kt` | prevent double capture across paths |
| Provider | `sms/SmsProviderWriter.kt`, `sms/SmsSubscriptionLookup.kt` | write inbox rows when default app; infer SIM |
| Local queue | `data/MessageRecord.kt`, `data/MessageDao.kt`, `data/AppDatabase.kt` | Room message state and migrations |
| Upload | `work/UploadWorker.kt`, `net/RelayClient.kt` | sequential upload and result classification |
| Commands | `work/CommandWorker.kt`, `data/HandledCommandDao.kt`, `data/CommandDatabase.kt` | poll, guard, execute, report |
| SMS sending | `sms/SmsSender.kt`, `sms/CommandPartTracker.kt`, `sms/FallbackPartTracker.kt` | SIM choice, multipart tracking |
| Fallback | `sms/FallbackPolicy.kt`, `sms/FallbackSender.kt`, `sms/SendStatusReceiver.kt` | confirmed-failure fallback and caps |
| Scheduling | `work/Schedule.kt`, `receiver/BootReceiver.kt`, `RelayApp.kt` | 5 s command chain, 15 min safety net, uploads |
| Contacts | `contacts/ContactsSync.kt`, `contacts/ContactResolver.kt`, `data/ContactsRepository.kt` | upload and local name resolution |
| Pairing | `pairing/PairingController.kt`, `pairing/QrCode.kt`, `net/PairingClient.kt`, `TokenStore.kt` | QR hand-shake, claim polling, token storage |
| Settings | `SettingsStore.kt` | base URL, device id and name, pairing state |
| UI | `ui/MainActivity.kt`, `ui/HistoryActivity.kt`, `ui/ComposeSmsActivity.kt` | setup, pairing, permissions, history, role stubs |

Pairing and secrets:

- `TokenStore` seals the device token and the claim secret with AES-GCM under
  a key that lives in the Android Keystore and never leaves it. The
  ciphertext sits in a private preferences file; a value that cannot be
  opened (the key was invalidated) is dropped and the app asks to pair again.
- `SettingsStore` is the only place the rest of the app reads the token from.
  The old plain-preferences token is deleted on startup: the server issues a
  new token during the pairing and the old one is worthless.
- `PairingClient` talks to the two public endpoints (`pair/start`,
  `pair/claim`). The claim secret only ever travels in the body of these two
  requests; it is never part of the QR code and never logged.
- `PairingController` owns the state of a running pairing (it survives a
  restart of the app through the settings), polls every 2 s, and writes the
  token into `TokenStore` the moment the claim succeeds. A generation
  counter makes a stale answer harmless.
- `QrCode` renders the payload the server returned; the phone needs no
  camera permission of its own.
- A 401 (or 403) from the backend calls `SettingsStore.handleUnauthorized()`:
  the token is dropped, the pairing state becomes `removed`, **the Room queue
  is kept**, and the device id is **not** changed, so the same `device_id`
  can be paired again.
- Changing the base URL invalidates the token (`invalidateForNewServer()`,
  state `server_changed`): a token belongs to one server only.
- The default-SMS-role request uses the `RoleManager` on Android 10+ and
  the platform intent on Android 8/9, and it re-reads the role instead of
  trusting the result code, because a vendor build can refuse the change
  without a word.

Android invariants:

- Room insert happens before scheduling upload.
- Upload is sequential, batch 20, 500 ms gap.
- A 401 keeps the message queued; other 4xx marks it permanently failed;
  429/5xx/network keep it queued.
- The command guard row exists before `SmsSender.send`.
- `sendingEnabled` is false by default; commands are rejected explicitly, not
  dropped.
- A fallback send is confirmed only when every part reports through the
  status receiver; caps count confirmed sends only.

The app is **not built on the VPS**: there is no Android toolchain there. Say
so in the log whenever the Android code changes.

## 7. Change recipes

### Add a device-scoped REST or Mini App route

1. Put the device id in the path (`/app/api/devices/{device_id}/...`) and
   resolve it with `db.device_by_id`; a read of a revoked or archived device
   is allowed (`miniapp._known_device`), a write needs an active device
   (`miniapp._active_device`).
2. Never take the device id from the request body. For the REST device
   routes it comes from `request.state.device`, which `security.require_device`
   sets from the bearer token.
3. Every query must filter by `device_id` **and** `thread_key`. If a helper
   in `db.py` cannot take a `device_id`, it does not belong in a device-scoped
   route.
4. Validate input with Pydantic or the existing `_reject` helpers, and keep
   bodies under the 512 KiB middleware cap.
5. Bump the revision with the right scope: `rev.bump_rev(app, device_id,
   thread_key)`. A device-wide change passes `thread_key=None`.
6. Add tests in the matching `backend/tests/test_*.py` module
   (`test_device_isolation.py` for the isolation rules).
7. Update the API tables in this guide and in `README.md`.

### Add a pairing or device field

1. Add the column to `_SCHEMA` **and** an `_ensure_column` entry in
   `db._COLUMNS`, and give it a default that works on an old database.
2. If the field is a secret, store only its SHA-256 digest
   (`db.token_hash`) and compare with `hmac.compare_digest`.
3. Keep the QR payload free of secrets: only the pairing id and the scan
   code may be in it.
4. Decide what happens on a re-pair: `db.activate_device` clears the old
   token, so the old token stops working immediately.
5. Update `pairing.py`, `db.py`, the Mini App `store.js`/`ui.js` and the
   Android `PairingController`/`SettingsStore` in the same change.

### Change the database schema

1. Add a `CREATE TABLE`/`CREATE INDEX` statement to `_SCHEMA` **and** an
   idempotent `_ensure_column` entry for existing databases. Indexes that
   reference a new column go into `_INDEX_STATEMENTS`, which run after the
   columns exist.
2. Run `db.migrate()` on an old database and verify the backfill behavior.
   The legacy backfill (`db.backfill_legacy_device`) must stay idempotent
   and must not lose a row.
3. Update `db.py` queries and the state helpers together.
4. Add or update tests in `test_db.py`, `test_import.py`, `test_ingest.py`,
   `test_devices.py`.
5. Never drop and recreate `messages`; queued rows are user data. A new
   table gets its own scoped copy; do not reuse the global `contacts`,
   `thread_reads` or `device_status` tables for new readers.

### Change the Android upload or command payload

1. Update the Kotlin model/`RelayClient` and the backend Pydantic model in
   the same change.
2. Keep backward compatibility if older APKs may still run — but remember
   that a pre-pairing APK only has the removed shared phone token: it gets
   a 401 and has to be re-paired.
3. A 401 must clear the token, keep the Room queue and keep the device id.
4. Update `README.md` and this guide if the contract is visible.
5. Bump the Android `versionCode`/`versionName` only when shipping an APK.
   The app is not built on the VPS.

### Change the forwarding labels or the Telegram routing

1. `delivery.py` builds the header: the sender, the timestamp and the
   `📱 <b>device name</b>` line. A missing device row must not block the
   delivery, it only drops the label.
2. The Mini App link in the "Open chat" button is built by
   `delivery.thread_url(base, thread_key, device_id)`; the device id has to
   be in the URL, otherwise the Mini App cannot open the right conversation.
3. A reply to a forwarded message goes back to the device that captured it
   (`updater._handle_reply`). A device that is no longer active cannot be
   replied to: say so instead of queueing a command nobody would run.
4. The bot never broadcasts: `db.resolve_command_device()` picks exactly one
   device or the command is refused with a readable error.
5. Add backend tests for the state transitions and the label.

### Change an auth or security check

1. Device tokens are looked up by digest (`db.device_by_token`) and must
   reject revoked and archived devices. Admin tokens are compared
   constant-time.
2. The rate limiters are keyed by the digest of the *presented* credential,
   so an attacker cannot lock a real device out. The pairing claim limiter
   counts only the wrong secrets, per pairing, so the phone that owns a
   pairing is never blocked.
3. Every new secret is hashed before it is stored and never written to a
   log line.
4. Add tests in `test_security.py` and `test_pairing.py`.

## 8. Testing

Backend:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

The suite collects **569 tests**. Four of them do not pass for reasons that
predate the multi-device work and are still open (three failures and one
error):

- `tests/test_commands.py::test_search_by_name_suggests_contacts`
- `tests/test_commands.py::test_contacts_are_uploaded_and_searchable`
- `tests/test_db.py::test_chat_search_matches_name_number_and_body_and_escapes_wildcards`
  (two contact-search failures and one chat-search failure)
- `tests/test_app_assembly.py::test_shutdown_is_prompt_when_updater_blocked_in_long_poll`
  (the shutdown-timing error)

Everything else passes. Do not report the suite as green, and do not treat
these four as new regressions.

Useful focused runs:

```bash
.venv/bin/python -m pytest tests/test_delivery.py -q
.venv/bin/python -m pytest tests/test_ingest.py tests/test_import.py -q
.venv/bin/python -m pytest tests/test_miniapp_send.py -q
.venv/bin/python -m pytest tests/test_pairing.py tests/test_devices.py -q
.venv/bin/python -m pytest tests/test_device_isolation.py -q
```

Test modules: `test_db`, `test_delivery`, `test_ingest`, `test_import`,
`test_commands`, `test_command_routing`, `test_api_state`,
`test_admin_config`, `test_devices`, `test_device_isolation`, `test_pairing`,
`test_updater`, `test_tg`, `test_security`, `test_config`, `test_phones`,
`test_miniapp_auth`, `test_miniapp_inbox`, `test_miniapp_send`,
`test_miniapp_shell`, `test_miniapp_frontend_static`, `test_app_assembly`,
`test_set_menu_script`.

There are no Android instrumentation tests and no JavaScript test runner. The
Mini App tests are Python static assertions plus API tests. If you change the
Android app, state clearly in the log that it was not built or tested on the
VPS.

## 9. Debugging and operations

- Health: `curl -fsS https://ai-barrow.ir/relay/health`.
- Service logs: `journalctl -u sms-relay-api -n 100 --no-pager`.
- Service state: `systemctl status sms-relay-api --no-pager`.
- Memory: `systemctl show sms-relay-api -p MemoryCurrent`.
- Database: `/var/lib/sms-relay/sms.db`; never edit it directly while the
  service is running.
- Caddy: `/etc/caddy/Caddyfile`; the relay block is `handle /relay/*` with
  `uri strip_prefix /relay`.
- Backups: `deploy/backup.sh`; restore with `deploy/restore.sh` after taking a
  fresh backup.
- Telegram bot token conflict: only one process may call `getUpdates` per
  token. If the bot stops answering, check for a second poller.
- Egress proxy (this VPS): `api.telegram.org` is not reachable directly —
  DNS resolves it to a blocked address and TCP fails. All Bot API traffic
  goes through the local sing-box mixed proxy (`HTTPS_PROXY=
  http://127.0.0.1:12380`, loopback-only, set in `/etc/sms-relay/env`).
  The systemd drop-in
  `/etc/systemd/system/sms-relay-api.service.d/10-egress-proxy.conf` adds
  `Wants=sing-box.service`/`After=sing-box.service`, and `sing-box` is
  enabled at boot. If Telegram delivery or polling fails while `/health`
  is fine, check `systemctl status sing-box` first. Scripts that call the
  Bot API (e.g. `scripts/set_miniapp_menu.py`, urllib/httpx) need
  `HTTPS_PROXY` in the invoking shell as well.
- Device list: `GET /app/api/devices` (Mini App) or `GET /status` (REST). A
  device that is `revoked` still shows its history but cannot send; the
  archived `legacy` device is read-only.

## 10. Known traps (do not rediscover the hard way)
1. `db.resume_received()` never resumes `source='import'` rows — an import
   is history, not something to forward. Resuming after an import would
   flood the chat.
2. `find_recent_duplicate()` never treats an import as another capture
   path, so a live SMS that overlaps an imported row is not swallowed.
3. `delivery.MAX_CHUNK_LEN=4090` is larger than `tg.MAX_TEXT_LEN=4000`; chunks
   in between raise `ValueError` and become permanently `failed`.
4. `/fallback` and `/phoneb` write backend settings that Android never reads.
   The phone's local `SettingsStore` is the real fallback configuration, and
   the Phone B fallback itself is frozen and off by default.
5. The backend accepts 3–4 digit `short:` threads; Android requires 5 digits
   to dial, so those threads are read-only in practice.
6. `ImportWorker` sends 100 items and treats 413 as permanent; a large batch
   can stall the import cursor.
7. Filters match raw sender strings and case-sensitive keyword substrings;
   they are not applied to imported history.
8. The Telegram update offset is in memory; a restart can redeliver a command.
9. Fallback candidates are the 50 newest rows in 48 hours; older failures can
   starve.
10. Multi-chunk sends are retried from chunk 1, so a later failure can duplicate
    earlier chunks.
11. After 8 attempts any error, including `timeout`, becomes `failed`; this can
    authorize a fallback for a message Telegram may have accepted.
12. FastAPI `/docs` and `/openapi.json` are reachable through the proxy unless
    explicitly disabled.
13. **After the upgrade every phone has to pair again.** The shared phone
    token is gone; an old APK that still uses it gets a 401 on every request.
    The app keeps its Room queue and its device id, so a fresh pairing
    restores the same device row with its old history.
14. The archived `legacy` device holds the history of the previous phone. It
    is read-only: it cannot pair, cannot poll commands and cannot send, and
    the Mini App shows it as archived.
15. The old device-less Mini App routes (`/app/api/chats…`,
    `/app/api/contacts`) always answer **410**. A client that still has the
    old bundle therefore shows an error until the new device-first bundle
    reaches it. That is intentional: a stale client fails loudly instead of
    reading a mixed inbox.
16. The pairing claim limiter counts only the *wrong* secrets, per pairing
    (and per address for an unknown pairing id). A valid poll is never
    rate-limited, because every phone shares one address behind Caddy.
17. The Telegram bot's contact search (`flow.py`) still falls back to the
    global legacy `contacts` table when the resolved device has no matching
    contact. The Mini App readers are strictly per-device; the bot picker
    can mix legacy names until it is made device-aware.

## 11. Definition of done
- [ ] The requested behavior is implemented or the investigation is complete.
- [ ] Relevant tests pass, or the inability to run them is recorded.
- [ ] Per-device isolation is verified: no query, route or UI path reads or
      writes conversation, contact or read data of another device.
- [ ] README/guide updated if behavior, contracts, commands, env vars, or
      limitations changed.
- [ ] `docs/AGENT_LOG.md` has a new appended entry with date, agent, task,
      files, verification, risks, and follow-ups.
- [ ] No secrets, message bodies, numbers, or contact names were added to
      logs or documentation.
- [ ] No deployment, restart, push, or credential change was performed unless
      explicitly requested.
