# SMS-to-Telegram Relay — Design (2026-10-21)

Personal, private-use system: Android Phone A (Samsung Galaxy A51 / Xiaomi 11T) forwards incoming SMS to a private Telegram chat via a lightweight HTTPS backend on the owner's VPS, with a direct-SIM SMS fallback from Phone A to iPhone Phone B when internet delivery fails.

Source of truth for requirements is the owner's brief; this document records the committed design and the decisions taken where the brief left choices open.

## Deployment environment

- Single VPS `vps-61721`, Ubuntu 24.04, 2 CPU cores, ~2 GiB RAM (545 MiB free at design time), 2 GiB swapfile, 38 GB disk (16 GB free).
- This VPS also hosts unrelated production services (Next.js web, Go API, PostgreSQL, Caddy, proxies, OpenCode stack). These must not be degraded, stopped, or reconfigured.
- **No Android toolchain on the VPS**: no JDK, Gradle, Android SDK, emulators, heavy test suites, or Docker builds. The APK is built externally (local machine or CI).
- Backend target budget: ≤300 MB steady-state RSS for the full stack (app + Caddy share + SQLite).

## Decisions (vs. the brief)

| Topic | Decision |
|---|---|
| HTTPS ingress | Path-based mount: `https://ai-barrow.ir/relay/*` → `127.0.0.1:8090` via a `handle` block **appended** to the existing Caddyfile (no overwrites). |
| Listen port | `127.0.0.1:8090` (verified unused; 8080 is taken by barrow-api). |
| Repo location | Monorepo at `/root/sms-relay` on the VPS; backend also fits here for deploy. |
| Telegram bot | Reuse the running bot's credentials: `TELEGRAM_BOT_TOKEN` and admin user id `TELEGRAM_ALLOWED_USER_ID` from `/root/.config/opencode-telegram-bot/.env`, copied (never printed) into the relay's protected env file at deploy time. Messages land in the same private chat. |
| Process model | One Uvicorn process, one API worker; inside it: one delivery worker task + one Telegram long-poll updater task (`getUpdates` timeout 30 s). No Redis/Celery/postgres. |
| Database | SQLite, WAL on, `busy_timeout`, dedicated indexes; file at `/var/lib/sms-relay/sms.db` (persistent local storage), owned by non-root `smsrelay`. |

## Backend architecture

Stack: Python 3.12, FastAPI, Uvicorn, HTTPX, SQLite via stdlib `sqlite3`. Standard-library logging only.

Process layout (single process, three asyncio blocks):

1. **API** — FastAPI app bound to 127.0.0.1:8090 (Caddy-only ingress).
2. **Delivery worker** — single asyncio task; pops small batches (≤10) of persisted jobs whose `next_attempt_at <= now`; sleeps on `asyncio.Event`/`sleep()` when idle. No unbounded in-memory queues; failed jobs stay in SQLite.
3. **Telegram updater** — single asyncio task; authorized long-poll for remote commands (`/start /help /status /pause /resume /filters /fallback /phoneb /allow /block`); commands honored only from the configured admin user id in an expected private chat. Executes no shell.

### API surface (all under `/relay`)

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /relay/health` | none (liveness only, static info) | systemd/Caddy health |
| `POST /relay/sms` | `PHONE_A_TOKEN` bearer | ingest one SMS (id, sender, body, received_at, part_info) |
| `GET /relay/messages` | `ADMIN_TOKEN` | paginated history (limit/offset, state filter) |
| `GET /relay/status` | either token | counts by state, queue depth, last activity |
| `POST /relay/admin/pause` `resume` | `ADMIN_TOKEN` | global forwarding state |
| `GET/PUT /relay/admin/settings` | `ADMIN_TOKEN` | fallback enabled, phone B number, fallback pacing |
| `GET/PUT /relay/admin/filters` | `ADMIN_TOKEN` | allowlist/blocklist senders, keywords (mode, list) |

Input validation: body cap 512 KB; sender ≤ 64 chars; body ≤ 16000 chars; timestamp sanity ±1 day; idempotency on `(id)` PK — duplicates return 200 with `duplicate: true`, never double-send. Same-text captures are only treated as duplicates when they arrive through different capture paths (`source` differs).

### Data model (SQLite)

- `messages(id TEXT PK, sender TEXT, body TEXT, parts INTEGER, part_index, group_id, received_at INTEGER, state TEXT CHECK(state IN ('received','pending','delivered','failed','uncertain')), telegram_msg_id, attempts, last_error, created_at, updated_at)` — indexes on `(state)`, `(received_at DESC)`.
- `jobs(msg_id TEXT REFERENCES messages(id), attempts INTEGER, next_attempt_at INTEGER, locked_at INTEGER)` with index on `next_attempt_at`. One transaction writes the `messages` row plus its `jobs` row (persist-before-send). Job rows are deleted when their message reaches a terminal state (`delivered` or `failed`); `pending`/`uncertain` rows stay queued; `locked_at` keeps the single-worker claim safe across restarts.
- `settings(key TEXT PK, value TEXT)` — persisted toggles only: `paused`, `fallback_enabled`, `phone_b`, `fallback_min_gap_s`, `fallback_max_per_hour`. The admin Telegram user id comes from env (`ADMIN_TELEGRAM_USER_ID`), not the DB.
- `filters(kind, pattern, PRIMARY KEY(kind, pattern))` — `kind ∈ {allow, block, keyword}`; policy: block beats allow (non-empty allowlist ⇒ only allowlisted senders accepted); keywords apply `drop_if_contains` to the body.
- Transactions: single connection per short operation or a single long-lived connection with serialized writes; WAL enabled once at startup; `busy_timeout=5000`.

### Delivery semantics (honest, not exactly-once)

- Only a 2xx success response from Bot API marks `delivered` (record `telegram_msg_id`).
- HTTP 429: respect `retry_after`; timeout, unavailable network, or ambiguous 5xx: mark `uncertain`, capped retries with exponential backoff (base 30 s, cap 1 h, max attempts 8) — no tight loops; hard terminal `failed`, retained for admin visibility; dedup risk of duplicated Telegram sends on retry after timeout is accepted and documented (message id suffixing to Telegram text makes eventual dedup visible to the human reader, not guaranteed).
- Telegram `sendMessage` caps text at 4096 chars; bodies are chunked at 4090 chars including the header, across sequential calls, all-or-failed per message (a partially delivered chunk set replays chunks that never got an ack).
- Secrets/SMS bodies never logged; logs carry ids, states, error classes only.

### Auth

- `PHONE_A_TOKEN`, `ADMIN_TOKEN`: ≥48-char secrets from `secrets.token_urlsafe`, distinct; constant-time compare; failures rate-limited (in-process limiter), 401 otherwise.
- Env file `/etc/sms-relay/env` mode 0640 `root:smsrelay`; service reads via systemd `EnvironmentFile`. Real creds never committed.

## Android app (Kotlin, built off-VPS)

- Modules: `SmsReceiver` (incoming: `onReceive` → parse via `Telephony.Sms.Intents`, persist to Room first), `OutgoingGateway` (direct SIM send via `SmsManager` chosen subscription; multipart-aware `divideMessage`/`sendMultipartTextMessage`, delivery-status awareness where permitted), `Uploader` (WorkManager network-constrained retries with backoff; queue from Room; unique ids UUIDv4 generated on arrival and reused for dedup and multipart linkage), `BootReceiver` re-schedule.
- Fallback triggers (either of): (a) the backend explicitly reports Telegram delivery `failed` for that message id (not mere timeout — timeouts are `uncertain`), or (b) the upload was rejected with a 4xx, so the backend has no row for the message at all. Being offline alone never triggers a fallback. Then Phone A sends the consolidated SMS to Phone B: `[Relay:<short-id>-<seq>] <sender> @<orig ts>` + full body; inter-send minimum gap 30 s; ≤10 fallback SMS/hour (setting `fallback_max_per_hour`); never re-send a fallback for a message whose every part was confirmed sent.
- `Uploader` Room-first, WorkManager network-constrained backoff; ids are UUIDv4 at arrival, reused for dedup and multipart linkage.
- UI (single Activity, XML views, no Compose — lighter local builds): connection/queue state, pause toggle, fallback toggle, Phone B number, SIM pick where supported, test-connection button (probe `/relay/health` at 10 s timeout), delivery history (Room-backed), permission & battery-optimization diagnostics (device-aware hints for Samsung/Xiaomi OEM restrictions).
- Honest limits surfaced in-app + docs: no guaranteed receive/send under force-stop, OEM aggressiveness, Doze, no carrier-charge responsibility, no guaranteed once-only delivery.
- Build: local machine/CI; no local-requirements to VPS. `./gradlew assembleDebug` documented; GitHub Actions workflow provided (`on: workflow_dispatch`), never executed on the VPS.

## Deployment (applied at Phase 7, only after confirmation)

- Pre-checks: `free -h`, `df -h`, `ps aux --sort=-%mem`, ports, unit list, Caddyfile (done; recorded above and re-run at deploy).
- Dedicated user `smsrelay` (no shell); venv at repo; runtime data `/var/lib/sms-relay` (0700, owned by it); SQLite there.
- systemd unit `sms-relay-api.service`: `127.0.0.1:8090`, `EnvironmentFile=/etc/sms-relay/env`, hardened like barrow-api (NoNewPrivileges, ProtectSystem=full, PrivateTmp, etc.), `Restart=on-failure`, `RestartSec=5`; memory: soft `MemoryHigh=200M`, hard `MemoryMax=300M` initially, then measured and adjusted upward only if throttled by actual usage (not arbitrary tight caps; no OOM-kill loops).
- Journald log persistence + retention prompts; no `LogMessage` of content.
- Caddy: append inside/after `ai-barrow.ir` site:
  `handle /relay/* { uri strip_prefix /relay; reverse_proxy 127.0.0.1:8090 }` then `caddy reload` (graceful). Validate config with `caddy validate` first.
- Backups: `scripts/backup.sh` exports sqlite `.backup` to `/var/lib/backups/sms-relay/NNN.db.gz` (0600) + restore script; retention 14 days; documented permissions + deletion of sensitive history (`scripts/purge-history.sh`).
- Firewall: no new public ports (Caddy already owns 80/443); backend loopback-bound.

## Testing (mocked, cheap; no real sends ever)

Pytest + TestClient/`httpx` ASGI transport + fake Telegram client. Matrix: auth matrix; duplicate ids; ingest validation; pause/resume filters; settings/filters CRUD; delivery success/failure/timeout/429/uncertain/retry cap; chunked length; persistence across simulated restarts; pagination; Telegram command auth (wrong user, group chat, valid admin); state transitions of both POST ingest and fallback webhooks. Android-side states covered in unit tests where tooling exists (local build); report un-runnable pieces otherwise honestly.

## Known limitations (documented, not hidden)

- Background SMS reliability depends heavily on OEM (Samsung/Xiaomi aggressive killers) and user's battery settings; no more guarantees than Android allows.
- At-least-mostly-once semantics only; duplicates possible in both Telegram and fallback SMS.
- Telegram bots are unsuitable for sensitive content (no E2E on server side by default here; brief already acknowledges this; mitigate: retention/backup/purge guidance + never sharing token).
- If neither internet nor cellular SMS is available on A, nothing is delivered until it is; local queue is durable.
- `ai-barrow.ir` domain must already point to the VPS for Caddy TLS; nothing new invented.

## Open items

- None blocking. DNS for `ai-barrow.ir` already resolves to this host; Caddyfile edit occurs only at Phase 7 after confirmation.
