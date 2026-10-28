# SMS Relay — Telegram Mini App Inbox + Android Transport Plan

- Date: 2026-10-24
- Status: proposed (not implemented)
- Scope: a messenger-style Telegram Mini App over the existing SMS relay, plus the Android changes needed to make it honest and useful.
- Working tree: `/root/sms-relay` (live repo, root-owned), mirror `/home/hessi/sms-relay-work`.
- Deployed copy: `/opt/sms-relay/app`, service `sms-relay-api`, Caddy site `ai-barrow.ir`.

## 0. Decisions already made

| Decision | Value |
|---|---|
| Product | SMS threads, not a Telegram client |
| Access | Admin-only: validated `initData.user.id == ADMIN_TELEGRAM_USER_ID` |
| Media | Text-only v1; no MMS, images or attachments |
| URL | `https://ai-barrow.ir/relay/app/` (Caddy strips `/relay`) |
| Notifications | Bot chat remains the alert channel; Mini App is the inbox |
| Deep links | Opening a relayed SMS can open its thread |
| Transport for outgoing | SMS via phone A; existing `sms_commands` queue |
| Multi-SIM | Optional `sub_id` per command; no silent SIM fallback |
| Delivery state | `pending → sent → delivered`, with `failed`/`rejected` |
| Backfill | Optional phase; not part of v1 acceptance |

### Explicit non-goals

- No userbot / personal Telegram account automation.
- No messaging arbitrary Telegram users.
- No read receipts or typing indicators from the other party.
- No background execution or push from the Mini App.
- No password login, cookies or CSRF token store.
- No change to the hardcoded device token or Phone B number in `SettingsStore.kt` (finding 3).

---

## Phase 0 — Discovery (verified, not assumed)

This phase is already complete. Its output is the contract every later phase must obey.

### 0.1 Telegram Mini App APIs that are allowed

Sources: <https://core.telegram.org/bots/webapps>, <https://core.telegram.org/bots/api#validating-data-received-via-the-mini-app>, <https://core.telegram.org/bots/api#setchatmenubutton>, <https://core.telegram.org/bots/api#inlinekeyboardbutton>.

**initData validation** (official wording):

- `secret_key = HMAC_SHA256(key="WebAppData", message=bot_token)`
- `data_check_string` = all received fields except `hash`, sorted alphabetically, `key=value`, joined by `\n`
- `hex(HMAC_SHA256(key=secret_key, message=data_check_string)) == hash`
- Parse with URL-decoding (`urllib.parse.parse_qsl`), never by naive `split("&")`.
- `auth_date` freshness is **not** specified by Telegram; a TTL is our policy (24 h here).
- The `signature` field is a received field and is therefore included in the bot-side HMAC check string.
- `initData` is empty for reply-keyboard launches and inline mode; reject those (we only support menu button / inline-keyboard WebApp launches).
- `initDataUnsafe` must never be trusted server-side.

**JS SDK** (only these names exist):

- Script: `<script src="https://telegram.org/js/telegram-web-app.js?64"></script>` in `<head>`.
- `Telegram.WebApp`: `ready()`, `expand()`, `themeParams`, `colorScheme`, `viewportHeight`, `viewportStableHeight`, `viewportChanged`, `isVersionAtLeast(version)`, `platform`, `version`.
- `BackButton`: `show()`, `hide()`, `onClick()`, `offClick()` (6.1+).
- `MainButton` is a `BottomButton`: `setText`, `show`, `hide`, `enable`, `disable`, `showProgress`, `hideProgress`, `onClick`, `offClick`.
- `HapticFeedback`: `impactOccurred(style)`, `notificationOccurred(type)`, `selectionChanged()` (6.1+).
- `showPopup(params, cb)` / `showAlert` / `showConfirm` (6.2+), `openLink(url)`.
- `enableClosingConfirmation()` / `disableClosingConfirmation()` (6.2+).
- `sendData` exists only for reply-keyboard launches and closes the app; **not used**.
- `EventSource` cannot send custom headers; use `fetch` for API and long-polling.
- No official CSP guidance exists; we still set CSP ourselves (see Phase 2).

**Launching**:

- BotFather `/setmenubutton` or `setChatMenuButton` (`MenuButtonWebApp` with `WebAppInfo.url`, HTTPS).
- Inline keyboard `web_app` buttons are available only in private chats between user and bot — ours is the admin private chat.
- `startapp` goes to `initData.start_param` and the GET parameter `tgWebAppStartParam`; the SDK does not parse `tgWebAppStartParam`, so read it from `location.search` yourself.

### 0.2 Backend facts

| Fact | Location |
|---|---|
| `create_app`, lifespan, two workers (delivery, updater) | `backend/app/main.py:140-189` |
| Only middleware is a 512 KiB body limit for POST/PUT | `backend/app/main.py:20-79`, `:198` |
| Auth deps: `require_device`, `require_admin`, `require_any` | `backend/app/security.py:88-106` |
| Bearer parsing + constant-time compare + 10 failures/60 s | `backend/app/security.py:15-94` |
| No cookies, sessions, CSRF, CORS, static files or Jinja2 | verified by grep; `jinja2` absent from venv |
| `StaticFiles`/`FileResponse` available | Starlette 0.46.2, no extra install |
| SQLite schema and idempotent migration | `backend/app/db.py:8-68`, `:374-401` |
| `migrate()` only `ALTER`s the `messages` table today | `backend/app/db.py:385-401` |
| Command insert only in the bot updater | `backend/app/updater.py:401-409`, `:498-523` |
| No admin endpoint to queue an SMS | route audit `backend/app/api.py:122-278` |
| `finish_command` overwrites unconditionally | `backend/app/db.py:205-212` |
| Result endpoint accepts only `sent/failed/rejected` | `backend/app/api.py:164-195` |
| `GET /commands` is device-only, pending only, limit 20 | `backend/app/api.py:155-162` |
| `sms_commands` has no `sub_id`, `thread_key`, `client_ref`, `updated_at` | `backend/app/db.py:39-49` |
| `messages.thread_key` absent; `group_id` is never uploaded by Android | `backend/app/db.py:9-27`; `SmsReceiver.kt:68`, `SmsNotificationListener.kt:85` |
| `messages.sub_id` exists but is never written | `backend/app/db.py:394-395`, `:404-427` |
| No read/unread state, no phone heartbeat | schema audit |
| Telegram body is appended **unescaped** under `parse_mode="HTML"` | `backend/app/delivery.py:194-202`, `:137-144` |
| Tests: 204 test functions, TestClient fixtures | `backend/tests/conftest.py:27-36` |
| Public `/relay/docs`, `/relay/openapi.json` are enabled | `backend/app/main.py:191` + live probe |

### 0.3 Android facts

| Fact | Location |
|---|---|
| Command poll: 1-minute chained one-shot + 15-minute periodic | `work/Schedule.kt:23`, `:78-114` |
| Unique work name includes a timestamp, so chains multiply | `work/Schedule.kt:100-114` |
| No per-command `subId`; global `sim_id` preference | `net/RelayClient.kt:35-41`; `sms/SmsSender.kt:103-128` |
| Invalid SIM silently falls back to the first active card | `sms/SmsSender.kt:106-111`, `:126` |
| Delivery intents are always `null` | `sms/SmsSender.kt:77` |
| Sent report means "radio submitted", not delivered | `sms/SmsSender.kt:21-27`; `work/CommandWorker.kt:128-164` |
| All parts share one PendingIntent for commands | `work/CommandWorker.kt:133-140`; `sms/CommandSendStatusReceiver.kt:56-65` |
| Multipart all-parts tracker exists for fallback only | `sms/FallbackPartTracker.kt:77-102` |
| Command statuses local: `handling/sent/failed/rejected` | `data/HandledCommandDao.kt:30-42` |
| `markReported` has no status predicate (race) | `data/HandledCommandDao.kt:78-79` |
| No Room schema change is required if we reuse string statuses | `data/CommandDatabase.kt:15-34` |
| Upload payload has no `sub_id` | `net/RelayClient.kt:56-66` |
| Contact sync uploads raw numbers every 6 h | `contacts/ContactsSync.kt:20-23`, `:68-113` |
| Number normalization logic to mirror | `util/PhoneNumbers.kt:63-90`; `util/SenderNormalizer.kt:22-38` |
| Harness compiles Kotlin against stubs, no Room/KSP/runtime | `/tmp/opencode/kt2/buildall.sh` |

### 0.4 Host facts

| Fact | Value |
|---|---|
| Caddy site | 12-line Caddyfile; `/relay/dl/*` → `/srv/downloads`; `/relay/*` → `127.0.0.1:8090` after stripping `/relay`; fallback Next.js on `:3000` |
| Security headers | **none** today (no XFO, no CSP, no HSTS) |
| `/relay/app` today | already reaches FastAPI and returns `404` — no Caddy routing change needed |
| Service | `smsrelay`, `ReadWritePaths=/var/lib/sms-relay`, `MemoryMax=320M`, RSS ~44 MB |
| Static assets location | `backend/app/static/` → copied to `/opt/sms-relay/app/app/static` by `install.sh:79-83` |
| Writable by service | only `/var/lib/sms-relay` |
| Backups | `deploy/backup.sh`, gzip 0600, 14 days, destination `/var/lib/backups/sms-relay` |
| Deploy | `install.sh` copies only the `app` package, does **not** restart a running service, does **not** edit Caddy once `/relay/*` exists |
| Repo state | working tree has 30 uncommitted files; deployed code matches the working tree, not `HEAD` |

### 0.5 Preconditions (must happen before Phase 1)

1. **Commit the current working tree.** The deployed backend matches uncommitted code; a rollback via `git checkout` would otherwise discard the deployed fixes. Record the commit hash in the implementation log.
2. **Back up the database** with `sudo deploy/backup.sh` and record the filename.
3. **Do not touch** the `SettingsStore.kt` hardcoded token/Phone B values. Because the APK/source zip is publicly downloadable, recommend rotating `PHONE_A_TOKEN` after the Android release and removing old zips; that is an operational decision, not part of this plan.
4. Confirm only one Telegram admin is intended. `ADMIN_TELEGRAM_USER_ID` is parsed as a single `int` (`config.py:9`, `:55`) and cannot be a list.

### 0.6 Anti-patterns discovered (must not be repeated)

- Do not use `EventSource` (no headers) or cookies for auth (Telegram Web iframe).
- Do not set `X-Frame-Options: DENY/SAMEORIGIN` on `/relay/app`; Telegram Web embeds it.
- Do not trust `initDataUnsafe`; validate the signed `initData` on every request.
- Do not put `initData`, admin token or device token in a URL or log line.
- Do not render SMS bodies with `innerHTML`; use `textContent`.
- Do not silently fall back to another SIM when an explicit `sub_id` was requested.
- Do not reuse one PendingIntent for every multipart part.
- Do not let a late `sent` report overwrite `delivered`.
- Do not add a Room schema version bump for this release; it would break downgrade.
- Do not change the Caddy `/relay/dl` or Next.js fallback handlers.
- Do not give the browser the admin bearer token.

---

## 1. Architecture

```
Telegram client (iOS/Android/Desktop/Web)
        │  menu button / web_app inline button
        ▼
https://ai-barrow.ir/relay/app/        (static shell, no data)
        │  fetch  X-Telegram-Init-Data: <initData>
        ▼
Caddy  /relay/*  →  strip /relay  →  FastAPI :8090
        │
        ├── /app/api/*   initData-validated JSON
        ├── /app/events  long-poll (rev counter)
        └── /app/assets/* static
        │
        ▼
SQLite (/var/lib/sms-relay/sms.db)
        ▲                │
        │                ├── delivery worker → Telegram (unchanged)
        │                └── updater → bot commands (unchanged)
        │
Android phone A  ── GET /commands (X-Device-Info) ──►  queue
                 ── POST /commands/{id}/result ──────►  sent/delivered/failed
                 ── POST /sms ───────────────────────►  inbound
```

No Caddy routing change is required. Security headers and cache policy are set by a FastAPI middleware scoped to `/app` and `/app/*`, so they are testable with `TestClient` and cannot affect `/relay/dl` or the Next.js site.

### 1.1 Identity and authorization

- Every `/app/api/*` request carries `X-Telegram-Init-Data`.
- Validate HMAC with `TELEGRAM_BOT_TOKEN` (already in settings), require a fresh `auth_date`, parse `user`, require `user.id == ADMIN_TELEGRAM_USER_ID`.
- 401 for missing/invalid/expired; 403 for a valid non-admin.
- No cookies, no sessions, no CSRF tokens. The custom header cannot be set cross-origin without CORS, and we do not enable CORS.
- The existing `Authorization: Bearer` admin/device API is untouched.

### 1.2 Data model changes

All additive. Old code ignores new columns/tables, so a code rollback is safe.

```sql
-- messages: canonical conversation key
ALTER TABLE messages ADD COLUMN thread_key TEXT;
CREATE INDEX IF NOT EXISTS idx_messages_thread
  ON messages(thread_key, received_at DESC);

-- sms_commands: thread, SIM, idempotency, timestamps
ALTER TABLE sms_commands ADD COLUMN thread_key TEXT;
ALTER TABLE sms_commands ADD COLUMN sub_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sms_commands ADD COLUMN client_ref TEXT;
ALTER TABLE sms_commands ADD COLUMN updated_at INTEGER;
ALTER TABLE sms_commands ADD COLUMN finished_at INTEGER;
CREATE INDEX IF NOT EXISTS idx_commands_thread
  ON sms_commands(thread_key, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_commands_client_ref
  ON sms_commands(client_ref) WHERE client_ref IS NOT NULL;

-- contacts: normalized lookup alongside the display number
ALTER TABLE contacts ADD COLUMN number_key TEXT;
CREATE INDEX IF NOT EXISTS idx_contacts_number_key ON contacts(number_key);

-- per-thread read cursor (single admin; no user id in v1)
CREATE TABLE IF NOT EXISTS thread_reads (
  thread_key   TEXT PRIMARY KEY,
  last_read_at INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);

-- phone liveness and SIM inventory, reported by the phone
CREATE TABLE IF NOT EXISTS device_status (
  device_id       TEXT PRIMARY KEY,
  last_seen_at    INTEGER NOT NULL,
  app_version     TEXT,
  sending_enabled INTEGER,
  sims_json       TEXT,
  updated_at      INTEGER NOT NULL
);
```

Backfills, run once at startup and idempotently:

| Backfill | Rule |
|---|---|
| `messages.thread_key` | `thread_key(sender)` where NULL; batch 500 |
| `sms_commands.thread_key` | `thread_key(target)` where NULL; `updated_at = created_at`; `finished_at = updated_at` for terminal states |
| `contacts.number_key` | `thread_key(number)` where NULL |
| First-run read seed | if setting `miniapp_seeded_at` is absent, insert `thread_reads(thread_key, now)` for every existing thread, then set the key |

Migration mechanics to follow: extend `_SCHEMA` for new tables/indexes and add a `_ensure_column(conn, table, column, ddl)` helper used by `migrate()`, modeled on the existing `messages` checks at `backend/app/db.py:385-401`. Backfills use the existing `_write_tx`/`BEGIN IMMEDIATE` pattern at `db.py:342-363`.

### 1.3 Number normalization

New `backend/app/phones.py`, mirroring `util/PhoneNumbers.kt:63-90` and `util/SenderNormalizer.kt:22-38`:

| Input | `thread_key` |
|---|---|
| `+98 912 123 4567` | `989121234567` |
| `00989121234567` | `989121234567` |
| `09121234567` | `989121234567` |
| `9121234567` | `989121234567` |
| `989121234567` | `989121234567` |
| `110`, `1000` (3–6 digits) | `short:110` |
| alphanumeric sender (`AD-BANK`) | `raw:ad-bank` |
| empty / unusable | `None` (ingest keeps the raw sender but the thread key falls back to `raw:<sender>`) |

Rules: convert Persian/Arabic/fullwidth digits, strip separators/spaces/parentheses, handle `00` and `+`, map local `0…` and bare 10-digit `9…` to `+98`, keep shortcodes unmodified, preserve `+` numbers outside Iran as digits-only keys. `display_number()` re-adds `+` for international keys and leaves `short:`/`raw:` readable.

### 1.4 Command status semantics

Server-side transition table (enforced under `_write_tx`):

| From | Allowed to |
|---|---|
| `pending` | `sent`, `delivered`, `failed`, `rejected` |
| `sent` | `delivered`, `failed` |
| `delivered` | terminal |
| `failed` | terminal |
| `rejected` | terminal |

- `delivered` may arrive before `sent` (delivery intent races); accept it.
- Invalid/duplicate transitions return HTTP 200 `{"accepted": false, "status": "<current>"}` so the phone stops retrying. Do **not** return 4xx, or `CommandWorker` will retry forever.
- Telegram result notes are sent only for the first accepted `sent`/`failed`/`rejected`, never for `delivered`.
- UI mapping: `pending` = queued clock, `sent` = single tick (submitted to network), `delivered` = double tick (carrier report), `failed`/`rejected` = red.

### 1.5 API surface (after Caddy strips `/relay`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/app`, `/app/` | public shell | `index.html`, no data |
| GET | `/app/assets/*` | public | static CSS/JS |
| GET | `/app/api/me` | initData | `{user, admin:true}` |
| GET | `/app/api/chats?q=&limit=` | initData | thread summaries + unread |
| GET | `/app/api/chats/{thread}/messages?before_ts=&limit=` | initData | merged inbound/outbound timeline |
| POST | `/app/api/chats/{thread}/messages` | initData | queue an SMS |
| POST | `/app/api/chats/{thread}/read` | initData | mark read up to `until_ts` |
| GET | `/app/api/events?rev=&timeout=` | initData | long-poll revision |
| GET | `/app/api/status` | initData | phone last seen, SIMs, paused, queue |
| GET | `/app/api/contacts?q=` | initData | autocomplete |
| POST | `/app/api/relay/pause` / `resume` | initData | optional controls |

`POST /app/api/chats/{thread}/messages` request:

```json
{
  "body": "text",
  "client_ref": "8-64 chars [A-Za-z0-9_-]",
  "sub_id": 0,
  "confirm_unknown": false
}
```

Validation and guards:

- `body` 1–16000 chars after trim (parity with `/sms`); UI shows the estimated SMS part count.
- `thread` must match `^[0-9]{5,20}$|^short:[0-9]{3,6}$`; `raw:` threads are read-only.
- `client_ref` is unique; a repeat returns the existing command with `"duplicate": true` and never sends twice.
- Unknown number (not in `contacts.number_key`, no thread activity in 90 days) requires `confirm_unknown: true`; otherwise `409 {"detail":"unknown_number"}`.
- `sub_id`: `0` means "phone default"; a nonzero id must be present in the last reported SIM list, or `422`.
- Rate caps: `MINIAPP_SEND_PER_MINUTE` (default 6) and `MINIAPP_SEND_DAILY_CAP` (default 50) counted over `sms_commands WHERE source='miniapp'`; exceed → `429`.
- Relay pause does not block outbound; it only affects inbound delivery. The UI shows the paused state.

---

## Phase 1 — Backend foundation (no UI yet)

**Goal:** schema, normalization, command lifecycle and phone status are correct and tested before any web code exists.

### Implement

1. `backend/app/phones.py` + `backend/tests/test_phones.py`.
2. `backend/app/db.py`:
   - extend `_SCHEMA` with `thread_reads`, `device_status`, new indexes;
   - add `_ensure_column` and extend `migrate()` for all new columns;
   - add and call `backfill_thread_keys(conn)`;
   - add `create_command(..., thread_key, sub_id, client_ref)`, `finish_command` returning `(accepted, status)`, `command_history`, `chat_summaries`, `thread_timeline`, `mark_thread_read`, `seed_reads_once`, `device_seen`, `device_status`;
   - write `thread_key` in `record_message`; write `sub_id` from the ingest payload.
3. `backend/app/api.py`:
   - accept `sub_id` in `SmsIn` (`ingest.py`) and store it;
   - accept `delivered` in `POST /commands/{id}/result`; use the transition function; return 200 with `accepted:false` on ignored transitions;
   - parse `X-Device-Info` on `GET /commands` (advisory; never reject the poll) and update `device_status`;
   - expose phone status in `GET /status`.
4. `backend/app/delivery.py`: escape the SMS body with `_html_escape` before building chunks (existing `_html_escape` at `delivery.py:6-9`); add a regression test with `<`, `>`, `&`.
5. `backend/app/config.py`: optional `MINIAPP_URL`, `MINIAPP_AUTH_MAX_AGE` (86400), `MINIAPP_SEND_PER_MINUTE` (6), `MINIAPP_SEND_DAILY_CAP` (50). Keep them optional so existing env files still start.

### Verification

```bash
cd /root/sms-relay/backend
.venv/bin/python -m pytest -q
```

Checklist:

- Normalization table above passes, including Persian digits and shortcodes.
- Migration on a copy of the live DB preserves all messages and backfills every non-null `thread_key`.
- `pending → sent → delivered` accepted; `sent` after `delivered` ignored; repeated `delivered` ignored.
- Old `sent/failed/rejected` tests still pass.
- A body containing `<b>bank</b>` reaches Telegram escaped (unit test on `_chunks`/`_header`).
- `GET /commands` still returns identical JSON plus `sub_id`/`thread_key`; a malformed `X-Device-Info` does not break polling.
- `GET /status` gains `phone: {last_seen, online, sims}` without changing existing keys.

### Anti-pattern guards

- Do not use `PRAGMA user_version` for this release; follow the existing idempotent migration style.
- Do not make backfills block startup on a large DB; batch and commit.
- Do not reject a device poll because its heartbeat header is malformed.
- Do not escape the header and body twice.

---

## Phase 2 — Mini App auth and static shell

**Goal:** prove the Telegram auth path and the iframe/header/CSP setup before building UI.

### Implement

1. `backend/app/miniapp_auth.py`:
   - `validate_init_data(raw, bot_token, max_age, now)` exactly as in §0.1;
   - require `hash`, `auth_date`, `user`; reject `auth_date` older than `max_age` or more than 300 s in the future;
   - `require_miniapp(settings)` dependency: 8 KiB header cap, 401 invalid/missing, 403 non-admin; attach `request.state.tg_user`;
   - in-memory failure limiter (reuse the pattern in `security.py:29-66`) keyed by IP, 20 failures/min;
   - never log raw `initData`.
2. `backend/app/main.py`:
   - mount `StaticFiles(directory=<app>/static/app, html=False)` at `/app/assets`;
   - `GET /app` and `/app/` return `FileResponse(.../static/app/index.html, headers={"Cache-Control": "no-store"})`;
   - add a middleware for paths `/app` and `/app/*` setting:
     - `Content-Security-Policy: default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org; base-uri 'none'; form-action 'none'; object-src 'none'`
     - `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`
     - `Cache-Control: no-store` for `/app` and `/app/api/*`; `no-cache` for assets.
   - **Do not** set `X-Frame-Options`.
3. `backend/app/static/app/index.html` placeholder:
   - `<base href="/relay/app/">` so relative `assets/…` and `api/…` URLs work at both `/relay/app` and `/relay/app/`;
   - load `telegram-web-app.js` in `<head>`, then `assets/app.js` as a module;
   - minimal shell with an auth-state screen.
4. `backend/tests/test_miniapp_auth.py`, `test_miniapp_shell.py`.

### Verification

- Unit tests: valid initData; bad hash; tampered field; stale `auth_date`; future `auth_date`; missing user; non-admin user (403); oversized header; malformed URL encoding; `signature` present and included; duplicate field handling.
- `GET /app` and `/app/` return 200 with the CSP and no XFO; `/app/assets/app.js` returns 200.
- `/app/api/me` returns 401 with no header and 200 with a test-signed header.
- Existing backend tests unchanged.

### Anti-pattern guards

- Do not use `initDataUnsafe`.
- Do not accept a user id from a query/body instead of the signed payload.
- Do not add CORS.
- Do not set `frame-ancestors 'none'`.
- Do not issue a cookie or a long-lived browser token.

---

## Phase 3 — Read-only inbox (chat list, timeline, search, live updates)

**Goal:** an admin can open the Mini App and read every SMS thread, with unread state and near-real-time updates.

### Implement

1. `backend/app/miniapp.py` (router):
   - `GET /app/api/me`
   - `GET /app/api/chats?q=&limit=100`: one row per `thread_key` with `display`, `name` (contact → `sender_name` → display), `last_ts`, `last_body`, `last_direction`, `unread`;
   - `GET /app/api/chats/{thread}/messages?before_ts=&limit=50`: merge `messages` (in) and `sms_commands` (out), newest first, ID prefix `msg:`/`cmd:`;
   - `POST /app/api/chats/{thread}/read` `{until_ts}`;
   - `GET /app/api/contacts?q=`;
   - `GET /app/api/status`;
   - `GET /app/api/events?rev=&timeout=25`: long-poll a global in-process `app.state.rev`; return immediately when it differs; `timeout=0` for tests.
   - bump `rev` in `record_message`, `create_command`, accepted status transitions.
   - seed `thread_reads` once on the first chats call (or at startup if messages exist).
2. Chat aggregation SQL (SQLite window functions are available):
   - CTE of inbound and outbound events keyed by `thread_key`;
   - `ROW_NUMBER() OVER (PARTITION BY thread_key ORDER BY ts DESC)` to pick the last event per thread;
   - left join `thread_reads` for unread counts;
   - search joins `contacts.number_key` and matches body/name/number with `LIKE` (no FTS yet).
3. Frontend files:
   - `js/telegram.js`: `initData`, `ready()`, `expand()`, theme, BackButton, haptics, popups, closing confirmation, `isVersionAtLeast`;
   - `js/api.js`: fetch wrapper adding `X-Telegram-Init-Data`; 401/403 screens; error mapping;
   - `js/store.js`: state, `rev`, long-poll loop with 1/2/5/15 s backoff, pause on `document.hidden`, resume on visible;
   - `js/ui.js`: chat list, thread, search, date separators, avatars, unread badges, pagination, empty/loading states;
   - `js/app.js`: route between list/thread, `tgWebAppStartParam`/`initData.start_param` deep link;
   - `app.css`: Telegram theme variables with fallbacks, two-pane ≥720 px, single-pane mobile, `100dvh`, no external fonts.
4. Security details:
   - render every body with `textContent`/`createTextNode`; never `innerHTML` for user data;
   - no inline event handlers;
   - encode thread keys in URLs (`encodeURIComponent`);
   - do not auto-link text.
5. Third-party script policy:
   - `telegram-web-app.js` is the **only** third-party script, loaded from `https://telegram.org` as the Mini App contract requires;
   - do not add analytics, fonts, CDNs or other script tags;
   - SRI is not practical here because Telegram updates the file; pin `script-src` to exactly `https://telegram.org` and re-check the served script after client updates;
   - start with `style-src 'self'`; relax to `'unsafe-inline'` only if a tested Telegram client requires injected styles, and document why.

### Verification

- Chats list contains all existing threads; names come from contacts and unread counts start at zero after the one-time seed.
- Opening a thread marks it read; a subsequent inbound message increments unread.
- Timeline interleaves inbound and outbound by timestamp; pagination loads older without duplicates at the boundary (document the same-second caveat).
- Search matches name, number and body; `%`/`_` are escaped or handled.
- A new SMS and a status change both increment `rev` and update an open thread without reload.
- XSS probes (`<img src=x onerror=alert(1)>`, `"><script>`) render literally.
- Thread key not owned by the admin? There is only one admin; still test an unknown thread returns 404, not another thread's data.

### Anti-pattern guards

- Do not build URLs with root-absolute `/app/...`; Caddy strips `/relay`.
- Do not use `viewportHeight` to pin the composer; use flex + `viewportStableHeight`/`100dvh`.
- Do not use a framework/build step; ES modules only.
- Do not cache API responses or the shell.

---

## Phase 4 — Sending from the Mini App

**Goal:** compose to an existing or unknown number, queue the SMS, and show honest status.

### Implement

1. `POST /app/api/chats/{thread}/messages` in `miniapp.py`:
   - normalize/validate target, body, `client_ref`, `sub_id`, `confirm_unknown`;
   - counts for caps over the last minute/24 h;
   - `db.create_command(..., source="miniapp", thread_key=thread, sub_id=..., client_ref=...)`;
   - return `{id, status:"pending", client_ref, duplicate:false}` or the existing command on a repeated ref.
2. UI:
   - composer with send button, character/part estimate, SIM picker when `/app/api/status` reports more than one SIM;
   - optimistic bubble with status `pending`; replace with the server id on response;
   - unknown-number `showPopup` confirmation, then retry with `confirm_unknown:true`;
   - failure → inline retry using the **same** `client_ref`;
   - status ticks driven by long-poll updates;
   - `enableClosingConfirmation()` while a draft exists; haptic light on send, success/error on outcome;
   - `MainButton` is optional and not used for the composer (keyboard conflicts).
3. Telegram mirror: the existing result endpoint already posts notes; ensure only the first terminal transition posts one.

### Verification

- Send to a contact → command appears as `pending`, phone polls, status becomes `sent`, then `delivered` if the carrier reports.
- Send to an unknown number without confirmation → 409; with confirmation → queued.
- Double-tap/retry with the same `client_ref` → one command row, one SMS.
- Caps: the 7th send in a minute returns 429; the 51st in 24 h returns 429.
- `sub_id` invalid → 422; `sub_id=0` lets the phone use its default.
- Paused relay still allows sending; UI shows the pause.
- Long body (e.g. 500 chars) shows multiple parts and is delivered as multipart.

### Anti-pattern guards

- Do not trust the client's confirmation flag as a security control; caps are the control.
- Do not create a new `client_ref` on retry.
- Do not show `delivered` without a delivery report.
- Do not send a second Telegram note for `delivered`.

---

## Phase 5 — Android transport changes

**Goal:** the phone can send on a chosen SIM, reports carrier delivery honestly, and tells the server it is alive.

**Release target:** `versionCode 3`, `versionName 1.2.0`. **No Room schema version bump** (rollback safety).

### 5.1 Per-command SIM

- `net/RelayClient.kt`: add `subId: Int` to `RelayCommand`; parse `sub_id` with a default of 0.
- `sms/SmsSender.kt`: add `subscriptionId: Int? = null`:
  - explicit id → resolve exactly; if not active, return `Outcome.Failed("sim_unavailable")` (no silent fallback);
  - `null`/0 → existing global `sim_id` behavior;
  - manager creation: `SmsManager.createForSubscriptionId(id)` on API 31+, `SmsManager.getSmsManagerForSubscriptionId(id)` (deprecated, `@Suppress`) below; add missing harness stubs.
- `work/CommandWorker.kt`: pass `command.subId`; persist nothing new (no Room change).

### 5.2 Carrier delivery reports

- `sms/SmsSender.kt`: add `deliveryIntents: ((partCount: Int) -> List<PendingIntent>)? = null` and pass it as the 5th argument of `sendMultipartTextMessage` at `SmsSender.kt:77`.
- `work/CommandWorker.kt`: build **per-part** sent and delivery PendingIntents with unique request codes and extras `{commandId, partIndex, kind}`; copy the pattern from `FallbackPartTracker.kt:77-96` (`FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE`, request code `commandHash*31 + partIndex`).
- Add `sms/CommandPartTracker.kt` modeled on `FallbackPartTracker` (separate file; do not refactor the fallback path in this release):
  - two phases, `sent` and `delivered`, each with all-parts aggregation;
  - 24 h prune; wrong id/part → `Unknown`, no state change.
- Extend `sms/CommandSendStatusReceiver.kt`:
  - read `EXTRA_KIND`, `EXTRA_COMMAND_ID`, `EXTRA_PART_INDEX`;
  - sent phase non-OK → `failed` (`radio_<code>`);
  - delivered phase all-OK → `delivered`; any non-OK → `failed`;
  - no change on pending/unknown parts.
- `data/HandledCommandDao.kt`:
  - add `STATUS_DELIVERED = "delivered"` (string only);
  - include `delivered` in `unreported`;
  - replace `markReported(id)` with `markReported(id, expectedStatus)` → `WHERE id=:id AND status=:status` (fix the race);
  - `prune` only rows with `reported = 1` (so a late delivered report is not pruned before upload).
- `work/CommandWorker.kt`: after a delivery transition, enqueue an immediate command poll so the report is sent without waiting a minute.

**Caveats:** delivery reports are carrier-dependent; no report must stay `sent`, never `failed`. Delivery intents are tied to the same `SmsManager`/subscription. A force-stop can lose a report; `sent` remains the honest fallback.

### 5.3 Phone heartbeat and SIM inventory

- `SettingsStore.kt`: new key `device_id` (UUID, generated lazily). No secret changes.
- `relay/RelayClient.kt`: on `GET /commands`, add header `X-Device-Info` with compact JSON:
  ```json
  {"id":"<uuid>","version":"1.2.0","sending":true,
   "sims":[{"sub_id":1,"slot":0,"label":"Personal"}]}
  ```
  Cap at ~2 KiB; never include the token or phone number.
- `sms/SimCards.kt`: expose sub id, slot index and display name (labels are not secret).
- Backend parses the header, upserts `device_status`, and derives `online = last_seen_at > now - 180`.
- Fix the poll-chain multiplication in `work/Schedule.kt`: use one stable unique name (e.g. `sms-relay-commands-next`) with `ExistingWorkPolicy.REPLACE`; keep the 15-minute periodic safety net. This makes the heartbeat meaningful and stops unbounded parallel polls.

### 5.4 Inbound SIM on upload

- `backend/app/ingest.py`: accept `sub_id` (int ≥0) in `SmsIn`; store it.
- `android .../net/RelayClient.kt`: include `"sub_id": record.subId` in the upload JSON.
- No Room change; `MessageRecord.subId` already exists (migration 2→3).

### 5.5 Version and packaging

- Bump `android-app/app/build.gradle.kts` to `versionCode 3`, `versionName "1.2.0"`.
- Compile with the existing harness:
  ```bash
  /tmp/opencode/kt2/buildall.sh
  # expect EXIT_CODE=0, ERRORS=0
  ```
  The harness is a type-check only; state that explicitly.
- Manual E2E on the real phone: single SIM send; dual SIM send with explicit `sub_id`; invalid SIM → `sim_unavailable`; delivery report → double tick; no report → single tick; result retry; heartbeat visible in `/app/api/status`.
- Rebuild the public source zip as `sms-relay-android-v0.1.2.zip` with the existing layout and publish to `/srv/downloads/321fa29f111363a1/`; verify SHA-256 over HTTPS.

### Verification checklist

- Kotlin harness exit code 0.
- `GET /commands` response includes `sub_id`; old clients ignoring it still work.
- A command with `sub_id` for an inactive SIM fails without sending on another SIM.
- Multipart command produces N distinct sent intents and N distinct delivery intents.
- `sent` then `delivered` transitions are both reported; a late `sent` does not overwrite `delivered`.
- No duplicate report loops (conditional `markReported`).
- Poll chain count stays at one 1-minute chain + one 15-minute periodic job.
- Rollback to 1.1.0 does not hit a Room downgrade (no schema bump).

### Anti-pattern guards

- Do not add columns to `handled_commands`; use the existing `status` string.
- Do not change `FallbackPartTracker` semantics in this release.
- Do not use `FLAG_UPDATE_CURRENT` with identical request codes for different parts.
- Do not report `delivered` when the carrier never sent a report.
- Do not put the device token, phone number or contact names in `X-Device-Info`.

---

## Phase 6 — Bot integration and deep links

**Goal:** the Mini App is reachable from Telegram, and each relayed SMS can open its thread.

### Implement

1. `config.py`: optional `MINIAPP_URL` (e.g. `https://ai-barrow.ir/relay/app/`), `MINIAPP_BOT_USERNAME` (optional, for `t.me` links).
2. `flow.py`: add a first row `📨 Open inbox` with:
   ```json
   {"text":"📨 Open inbox","web_app":{"url":"<MINIAPP_URL>"}}
   ```
   Only when `MINIAPP_URL` is set (keeps the current menu working otherwise).
3. `delivery.py`: add a second inline button to the last chunk:
   ```json
   {"text":"Open chat","web_app":{"url":"<MINIAPP_URL>?thread=<thread_key>"}}
   ```
   Keep `🔍 More details`. `web_app` buttons are valid in the private admin chat.
4. Deep link handling in `js/app.js`: read `thread` from `location.search`; if absent, read `tgWebAppStartParam`/`initData.start_param`; open that thread if it exists, otherwise the list.
5. One-time menu button: document BotFather `/setmenubutton` (preferred), and provide an optional script `scripts/set_miniapp_menu.py` that reads `TELEGRAM_BOT_TOKEN` from the env file and calls `setChatMenuButton` without printing it. Do not hardcode the token.
6. Keep the Telegram delivery/notification behavior unchanged. Do not add a mute toggle in v1.

### Verification

- `/start` shows the inbox button only when `MINIAPP_URL` is configured; tapping it opens the app with valid `initData`.
- A relayed SMS shows both buttons; "Open chat" lands on the right thread.
- Long SMS (multiple chunks) exposes the buttons on the last chunk only (existing behavior).
- A malformed/unknown `thread` query falls back to the chat list.
- Query-preservation on iOS/Android/Desktop/Web is verified manually; if a client drops the query, fall back to `startapp` + `t.me/<bot>?startapp=<thread>` (documented alternative, requires `MINIAPP_BOT_USERNAME`).

### Anti-pattern guards

- Do not use `sendData` (menu-button launches cannot use it).
- Do not add `web_app` buttons to channel/business messages.
- Do not put the thread key in a hash fragment; Telegram uses the hash for `tgWebAppData`.

---

## Phase 7 (optional) — Import the phone's existing SMS history

**Goal:** seed old conversations so the inbox is not empty on first open. Off by default.

### Backend

- New device-authenticated `POST /import` accepting batches (≤200) of `{id, sender, body, received_at, sub_id, provider_id?}`.
- Store with `state='received'` and `source='import'`; **never enqueue to Telegram**.
- `received_at` is not bounded by the ±24 h rule used by `/sms`.
- Idempotent: deterministic `id` (`imp-<hash>`) + `INSERT OR IGNORE`; dedupe against existing rows by `(thread_key, body, received_at ±60 s)`.
- Update `docs/api.md` and tests (no Telegram jobs created).

### Android

- `work/ImportWorker.kt` reading `Telephony.Sms.Inbox.CONTENT_URI` with keyset pagination (`_ID > cursor ORDER BY _ID`), batches of 100, direct POST (not via Room).
- Cursor stored in SharedPreferences; resumable.
- Opt-in button in `MainActivity` with a confirmation and progress; requires the default-SMS role (`READ_SMS` is hard-restricted on Android 10+).
- No Room schema change. Imported messages appear in the web inbox only.

### Risks to document

- Overlap with live-captured messages; the ±60 s server dedupe is a heuristic.
- Storage growth; `purge-history.sh` covers imported rows.
- Privacy: the entire historical inbox becomes readable in the Mini App; opt-in only.

---

## Phase 8 — Documentation, deployment, release

### Documentation

| File | Change |
|---|---|
| `docs/miniapp.md` (new) | Architecture, auth, endpoints, UI, threat model, BotFather setup, troubleshooting, CSP/iframe notes |
| `docs/api.md` | New `/app/*` endpoints; `delivered` status; `sub_id`/`thread_key`/`client_ref`; `X-Device-Info` |
| `docs/security-privacy.md` | initData trust boundary, no cookies, send caps, initData never logged, public link risk |
| `docs/env-reference.md` + `deploy/env.template` | `MINIAPP_URL`, `MINIAPP_BOT_USERNAME`, `MINIAPP_AUTH_MAX_AGE`, `MINIAPP_SEND_PER_MINUTE`, `MINIAPP_SEND_DAILY_CAP` |
| `docs/manual-e2e-checklist.md` | Mini App open/auth, send/unknown/caps, delivery ticks, deep link, Android dual-SIM, heartbeat |
| `deploy/README.md` | Deploy/restart/rollback sequence for this release; no Caddy change required |

### Deploy sequence

1. Commit the current working tree and record the hash.
2. `sudo deploy/backup.sh`; record the backup filename.
3. Run the full backend test suite.
4. Deploy code: `sudo deploy/install.sh --yes` (copies `app/`, including `static/app/`), or copy the three changed backend files plus the new package files if using the lighter path.
5. Restart explicitly: `sudo systemctl restart sms-relay-api`.
6. Verify:
   ```bash
   curl -fsS https://ai-barrow.ir/relay/health
   curl -sI https://ai-barrow.ir/relay/app | grep -Ei 'HTTP/|content-security-policy|x-frame-options|cache-control'
   sudo journalctl -u sms-relay-api -n 50 --no-pager
   sudo systemctl show sms-relay-api -p MemoryCurrent
   ```
   Expected: 200, CSP present with `frame-ancestors`, **no** XFO, memory well under 200 MB.
7. Verify the schema on the live DB (Python, no secrets):
   ```python
   import sqlite3
   c = sqlite3.connect('/var/lib/sms-relay/sms.db')
   print(c.execute("PRAGMA table_info(sms_commands)").fetchall())
   print(c.execute("SELECT COUNT(*) FROM messages WHERE thread_key IS NULL").fetchone())
   ```
   Expect 0 rows with NULL `thread_key`.
8. Set the bot menu button (BotFather or the optional script), then open the Mini App on iOS, Android, Desktop and Web.
9. Publish the Android zip `v0.1.2` only after the phone-side E2E passes; verify SHA-256 over HTTPS.

### Rollback

- **Backend:** all changes are additive; redeploy the previous commit and restart. New columns/tables remain and are ignored. Restore the DB backup only if a migration/backfill is wrong (`deploy/restore.sh`).
- **Caddy:** unchanged by this plan. If headers were added manually, restore `/etc/caddy/Caddyfile.bak.<timestamp>`, `caddy validate`, `caddy reload`.
- **Android:** reinstall 1.1.0. No Room version bump means no downgrade failure. Commands already delivered are not re-sent (server terminal states).
- **Bot:** clear the menu button with BotFather if the Mini App must be withdrawn; the existing text commands keep working.

---

## 9. Verification matrix

| Area | Check | Blocking |
|---|---|---|
| Backend unit | Full pytest suite (existing 204 + new tests) | yes |
| Auth | Missing/tampered/stale/future/non-admin/oversized initData | yes |
| Auth | `initData` never appears in logs; admin token never sent to browser | yes |
| Data | Migration on a copy of the live DB, 0 NULL `thread_key` | yes |
| Data | Existing 186 messages preserved and grouped | yes |
| UI | Chat list, thread, search, unread, pagination | yes |
| UI | XSS probes render as text | yes |
| UI | Composer: contact, unknown number, long message, retry idempotency | yes |
| Status | `pending → sent → delivered`, late/duplicate transitions ignored | yes |
| Android | Kotlin harness compiles; dual-SIM send; delivery report; no silent SIM fallback | yes |
| Android | Heartbeat visible in `/app/api/status`; one poll chain | yes |
| Telegram | Open from menu on iOS/Android/Desktop/Web; BackButton; theme; keyboard | yes |
| Telegram | Deep link from a relayed SMS lands on the right thread | yes |
| Security | CSP has `frame-ancestors` and `script-src https://telegram.org`; no XFO | yes |
| Security | Send caps enforced server-side; unknown-number confirmation | yes |
| Security | No secrets/tokens in URLs, HTML, JS bundles or logs | yes |
| Ops | Health 200, memory < 200 MB, no error logs, Caddy untouched | yes |
| Release | Android zip builds, SHA-256 matches, URL reachable | yes |
| Optional | Import is opt-in, skips Telegram, is idempotent | no |

---

## 10. Assumptions and open decisions

1. **Single admin.** Multiple Telegram admins require changing `ADMIN_TELEGRAM_USER_ID` parsing and `thread_reads` keying; not in v1.
2. **`auth_date` TTL is our policy** (24 h). Telegram does not specify one. If the Mini App stays open longer, the user reopens it.
3. **`X-Telegram-Init-Data` is our transport.** Telegram does not prescribe one; a header avoids URL logging and works with `fetch`.
4. **Delivery reports may never arrive.** `sent` is the honest terminal state for those carriers; `delivered` only appears on a real report.
5. **One-minute outbound latency** (existing chained poll). The UI says "queued", not "sent", until the phone reports.
6. **No Caddy change.** `/relay/app` already reaches FastAPI. A future site-wide `X-Frame-Options` or strict CSP would break Telegram Web; document this in `docs/miniapp.md`.
7. **Deep-link query preservation** is de-facto, not documented by Telegram; verify on all four clients and fall back to `startapp` if needed.
8. **The public source zip contains the hardcoded device token** (finding 3, untouched). Treat it as compromised; rotate `PHONE_A_TOKEN` after the release and remove old zips if the link should not be public.
9. **FastAPI `/docs` and `/openapi.json` are currently public** under `/relay/docs`. Optional hardening: disable them or require admin auth in a separate change.
10. **No inbox backfill in v1**; Phase 7 is opt-in and can ship later.

---

## 11. Definition of done

- The Mini App opens from the bot menu on all four Telegram clients, authenticates with `initData`, and never exposes data without a valid admin identity.
- The chat list shows every thread with correct contact names, last message and unread counts.
- A thread interleaves inbound SMS and outbound commands, and sending to a known or unknown number works with a queued → sent → delivered/failed lifecycle.
- The phone can send on a chosen SIM, reports delivery reports when available, and its liveness/SIM inventory is visible in the app.
- All new backend tests pass, the Kotlin harness compiles, the live deploy is healthy, memory stays under the unit limits, and rollback needs no destructive steps.
