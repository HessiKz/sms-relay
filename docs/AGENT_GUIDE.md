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
  poll for outbound commands and send through their own SIM. They are also a
  real local **Messages client**: the launcher is the phone's own conversation
  list read from the SMS provider, and a one-to-one composer sends, stores the
  message in `content://sms` and mirrors it to the relay as outbound history.
  They also upload
  their call log, report the one call they are on, and execute remote call
  actions; the dialer places calls with no pairing and no network. A phone is
  not trusted by default: it has to be paired with the backend through a QR
  hand-shake before it may talk to it.
- **Backend (VPS)** receives, stores and filters messages, and posts one short
  notification per inbound SMS to one private Telegram chat: device name,
  sender, a `Code:` line when a plausible code is found, a 160-character
  one-line snippet of the body, and an "Open chat" button. Only that snippet
  and the extracted code go to Telegram; the full body stays in SQLite and in
  the Mini App, which is where the message is read in full. Every notification
  carries the name of the device that captured it. It mirrors the call log,
  holds the live call of each device and the queue of remote call actions, and
  posts one bounded message for a ringing or a missed call. It also runs the
  admin bot and serves the Mini App.
- **Mini App** is a Telegram WebApp with a composer. It is device-first: the
  admin picks a device and sees only that device's conversations. It has a
  `Chats | Calls` tab per device and a live call card with Answer, Decline,
  Hang up, Mute, Speaker and a keypad. It talks to the backend with Telegram
  `initData` and creates the same `sms_commands` rows as the Telegram bot; a
  call action is a `call_actions` row, never a second kind of command.

Three rules shape everything else:

- **Per-device isolation.** Every message, command, contact and read marker
  belongs to exactly one device. A thread is the pair
  `(device_id, thread_key)`: the same phone number on two phones is two
  different threads, and a device never sees the history, the contacts or
  the unread counters of another device.
- **One shared Telegram chat.** There is no per-device chat. The device line
  of the notification and the device-first Mini App keep the streams apart;
  the bot routes a reply back to the device that captured the message, as
  long as the notification still exists (`telegram_msg_id`).
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
   once, in the answer of `POST /pair/claim` — and only after the user on the
   phone confirmed the approver it was shown (`confirmed: true`). A revoked or
   archived device is refused by the token lookup, so a device that gets a 401
   has to pair again.
4. **Commands are never broadcast; ownership is strict.** A command belongs
   to exactly one device. The phone only polls the commands of its own
   device, and a result report for a command of another device is answered
   with 404 so a phone cannot even learn that the command exists.
5. **Durability before network.** Android writes to Room before uploading.
   The backend writes `messages` + `jobs` before calling Telegram.
   **The notification carries a snippet of the body, never the whole body.**
   `DeliveryWorker` sends the device, the sender, one `Code:` line when
   `extract_code()` finds a plausible one, and `body_snippet()` — the body
   collapsed to one line and cut to 160 characters — plus a Mini App button.
   Only that snippet and the extracted code leave the relay over Telegram; the
   full `messages.body` never does, and it must not be added back. Neither the
   body nor the snippet nor the code is ever logged.
6. **No message body in logs.** Log ids, kinds, counts, and state, never SMS
   text, the notification snippet, an extracted code, phone numbers, contact
   names, tokens, or initData.
7. **Fallback is conservative.** Only a confirmed backend `failed` state (or a
   local permanent 4xx rejection with no backend row) authorizes the cellular
   fallback. `timeout`, `network`, `uncertain`, `429`, and `5xx` never do.
8. **A command is executed at most once.** The local `handled_commands` guard
   row is written before the radio call. A lost report may duplicate a status
   message, never an SMS.
9. **A call action is at most once, and never lands on the next call.** The
   server marks an action `delivered` in the same transaction that hands it
   out; the phone writes the action id to `CallActionGuard` before it touches
   Telecom. An action names the phone-generated `call_ref` of the live call
   and a stale or mismatched ref is refused (`409 stale_call`), never applied
   to the call that follows. One primary call per device: call waiting is out
   of scope so the ref stays unambiguous.
10. **Emergency numbers are never remote-controlled, and there is no remote
    audio.** `110`, `112`, `115`, `125`, `911`, `999`, `000` are refused by
    the backend, by the bot and by the Mini App; only the phone's own dialer
    places them, offline. Never request `CAPTURE_AUDIO_OUTPUT` and never
    promise listening or talking: the relay controls a call, it does not carry
    it.
11. **Additive, idempotent migrations.** Do not destroy the message queue.
12. **Update the documentation and the agent log** as part of the change.

## 3. Backend map

| File | Responsibility | Change it when |
|---|---|---|
| `backend/app/main.py` | FastAPI assembly, middleware, lifespan, CLI, static Mini App mount | adding middleware, startup tasks, routes at app level |
| `backend/app/config.py` | env-file parsing and immutable `Settings` | adding/renaming an environment variable |
| `backend/app/security.py` | bearer-token dependencies (per-device, admin) and failure rate limiting | auth rules, token handling |
| `backend/app/db.py` | schema, migrations, transactions, every SQL query, queue helpers | data model, states, queue semantics, new queries |
| `backend/app/phones.py` | sender/thread-key normalization | number formats, shortcode rules, thread identity |
| `backend/app/ingest.py` | `POST /sms` live ingest, filters, dedupe, pause | live-ingest behavior |
| `backend/app/importer.py` | `POST /import` historical inbox and sent folder | import format, direction, limits, import dedupe |
| `backend/app/api.py` | device/admin REST API, status, settings, filters | REST contracts or admin controls |
| `backend/app/calls.py` | device call routes (`/calls/sync`, `/calls/events`, `/calls/actions*`), Mini App call routes, live session shaping and freshness, emergency gate, Telegram call mirrors | call log, live call, remote actions, call messages |
| `backend/app/pairing.py` | public pairing endpoints (`/pair/start`, `/pair/claim`) and the Mini App pairing/device endpoints | pairing flow, device lifecycle, device list |
| `backend/app/delivery.py` | delivery worker, notification text, retry policy, Telegram button, `NotificationReaper` | notification behavior, notification deletion |
| `backend/app/tg.py` | Telegram HTTP client and error taxonomy, including `deleteMessage` (`_interpret_ok`) | Bot API calls, error classification |
| `backend/app/updater.py` | long-poll loop, bot commands, callbacks, command creation, default-device routing, call callbacks (`call:<verb>:<device>`) | bot commands and Telegram UX |
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
  - `approved_by` is the Telegram user id of the admin, `approved_by_name`
    (`first_name`, cleaned) and `approved_by_username` (cleaned) are the
    approver shown on the phone. Both are additive, nullable columns, and a
    blank value is valid: the phone then says "Approved in the Mini App".
- `device_contacts`: the contact list of one device, primary key
  `(device_id, number)`.
- `thread_reads_scoped`: the read markers of one device, primary key
  `(device_id, thread_key)`.
- `messages`: one row per inbound SMS, including imported history.
  - `id` primary key; live ids come from Android (UUID); import ids are
    deterministic `imp-<hash>` (the SHA-256 of device, direction and the
    provider row id, or of sender/timestamp/text when there is no id).
  - `device_id` is the device that captured the message.
  - `thread_key` is the canonical conversation key (`989...`, `short:110`,
    `raw:bank`).
  - `source` is `sms`, `notification`, or `import`.
  - `state` is one of `received`, `pending`, `delivered`, `failed`,
    `uncertain`.
  - `telegram_msg_id` stores the Telegram message id of the last notification
    sent for this row. It powers reply-to-SMS (`db.find_by_telegram_id`) and
    the `details:`/`showmsg:` edit callbacks. `NotificationReaper` clears it
    when the notification is deleted.
  - `telegram_sent_at` is when Telegram accepted the notification. It is the
    marker of "this row was delivered as a notification": imported history and
    rows forwarded by the old body-forwarding code have it NULL, so the reaper
    never queues them.
  - `seen_at` is when the Mini App read this row. `mark_thread_read()` fills
    it with `COALESCE(seen_at, now)` for the rows the read cursor covers; it
    never moves back.
  - `telegram_delete_at` is the deletion deadline. `mark_message_notified()`
    writes `sent_at + 86400`, or `sent_at + 300` when `seen_at` is already
    set. A read pulls it forward with `MIN(COALESCE(...), now + 300)` — never
    back. `db.reschedule_notification()` pushes it forward by 300 s after a
    transient delete failure.
  - `telegram_deleted_at` is set once the notification is confirmed gone (or
    answered "message to delete not found"). `mark_notification_deleted()`
    writes it and clears `telegram_msg_id`/`telegram_delete_at`.
  - `idx_messages_tg_delete` is a partial index on `telegram_delete_at`, so
    `db.due_notifications()` scans only the live deadlines.
- `tg_messages`: the bot's own chatter notes ("✅ SMS sent to …", failures,
  rejections, the bot's replies) that must be deleted again.
  - `chat_id`, `message_id`, `kind` (`command_result`, `updater`, …),
    `delete_at`, `deleted_at`, `created_at`. A note is **not** an SMS
    notification and has no `messages` row, so it cannot live on `messages`.
  - `record_tg_message()` is called from `api.py` (a command result) and
    `updater.py` (a reply) with `now + TG_NOTE_TTL_S`, and `TG_NOTE_TTL_S` is
    **3600 s** in `delivery.py`. Change the constant, not the two call sites.
  - `due_tg_messages()` feeds `NotificationReaper._reap_notes()`;
    `mark_tg_message_deleted()` keeps the row as a tombstone and
    `reschedule_tg_message()` pushes it back 300 s.
  - `idx_tg_messages_due` on `delete_at` keeps the scan cheap.
- `jobs`: one row per message that still needs forwarding. It has its own
  `attempts`, `next_attempt_at`, and `locked_at`. A job lock older than 300 s
  is stale and can be reclaimed after a crash.
- `sms_commands`: outbound requests and outbound history. `device_id` is
  mandatory: it names the phone that has to run the command. Status is
  `pending`, `sent`, `delivered`, `failed`, or `rejected`. `client_ref` is a
  partial unique index used for Mini App idempotency. Imported sent messages
  also live here, with `source='import'` and terminal `status='sent'`: see
  "Historical import" below.
- `call_log`: the history of every call the phone's provider knows.
  - `id` is deterministic: `call-<sha256(device_id, "pid:"+provider_id)>`, or
    `call-<sha256(device_id, "n:"+number+"\0"+started_at)>` when the phone
    sent no provider id. A replayed batch therefore upserts to the same rows.
  - `kind` is one of `db.CALL_KINDS`: `incoming`, `outgoing`, `missed`,
    `rejected`, `blocked`, `voicemail`, `unknown`. An unknown kind is skipped,
    never stored.
  - `number_key` is `phones.thread_key(number)`, the same key the SMS side
    uses, so a call and an SMS of one conversation share `(device_id,
    thread_key)`. A private number has `number_key` NULL and is never merged
    into a thread.
  - `sub_id` and `phone_account` identify the SIM; `duration_s` is capped at
    one week per row; `is_read` only ever moves forward (a Mini App read is
    not undone by a re-sync); `content_hash` from the phone shortcuts the
    "did anything change?" check.
  - `present` is reserved for a provider-side deletion and is `1` today; every
    reader filters on it.
  - Indexes: `(device_id, started_at DESC, id DESC)` for the history page and
    `(device_id, number_key, started_at DESC)` for the thread timeline.
- `call_sessions`: the one primary call of a device, primary key `device_id`.
  - `call_ref` is the phone-generated UUID and the only handle an action may
    carry. `state` is one of `db.CALL_STATES`: `ringing`, `dialing`, `active`,
    `held`, `disconnected`; `direction` is `incoming` or `outgoing`.
  - An event of the same `call_ref` **merges**: an omitted field keeps the
    stored value, so a mute update never erases the caller. An event with a
    new `call_ref` replaces the row, because the previous call is over by
    definition.
  - `answered_at` is stamped on the first `active`/`held`; `disconnected`
    stamps `ended_at` when the phone did not send one.
  - The row is **ephemeral state, not history**. The finished call reaches
    `call_log` through the phone's own provider upload.
  - Freshness is one function, `calls.session_is_live()`: `ringing`/`dialing`
    die after 2 min without an update, `active`/`held` after 12 h,
    `disconnected` is *shown* for 60 s but is not actionable. A phone that
    dies mid-call therefore stops offering Answer/Hang up by itself.
- `call_actions`: one row per remote control, guard first.
  - `action` is one of `db.CALL_ACTIONS`: `answer`, `reject`, `hangup`,
    `mute`, `unmute`, `speaker_on`, `speaker_off`, `hold`, `unhold`, `dtmf`,
    `dial`.
  - `status` is one of `pending`, `delivered`, `done`, `failed`, `expired`,
    `rejected`. Only `pending` and `delivered` move to a terminal state
    (`db._CALL_ACTION_TRANSITIONS`), so a late report never reopens an action.
  - `call_ref` is empty **only** for `dial`, which has no call yet; its
    `payload` is the normalised destination.
  - TTL lives in `db.CALL_ACTION_TTL_S`: `answer`/`reject`/`hangup` 20 s,
    everything else `CALL_ACTION_DEFAULT_TTL_S = 300` s. `expires_at` is
    decided in `db.call_action_create()` so the bot and the Mini App cannot
    disagree.
  - `client_ref` has a partial unique index (`WHERE client_ref IS NOT NULL`):
    the Mini App passes its own idempotency key, the bot passes
    `tg:<callback id>`, and a second insert returns `None` instead of a second
    action.
  - `claim_call_actions()` selects `pending`, marks expired rows, flips the
    returned rows to `delivered` — all in one transaction, so the idle poll
    (`CommandWorker`) and the live poll (`CallService`) never run one action
    twice.
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
imported sent history ─► sms_commands, status='sent'  (terminal, no job)
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
  device as a twin; an import never swallows a live SMS. Same-source repeats
  are kept on purpose (two genuine "ok" messages must both survive), which is
  why the duplicate capture has to be stopped in Android, not here: 1.3.3
  ignores `SMS_RECEIVED` while the app holds the default-SMS role.
- Delivered means "the notification was accepted", not "every chunk was
  sent". The notification carries only a 160-character snippet and the
  extracted code, so there is no chunking on the delivery path and no
  partial-send retry.

### Delivery and the notification reaper

Two tasks run when workers are enabled (`main.create_app(run_workers=True)`):

| Task | Class | Cycle |
|---|---|---|
| `delivery-worker` | `delivery.DeliveryWorker` | claims up to 5 due jobs, pokes on insert, idles up to 120 s |
| `notification-reaper` | `delivery.NotificationReaper` | every `REAPER_INTERVAL_S = 30` s, `REAPER_BATCH = 20` due rows |

`DeliveryWorker._deliver()`:

1. `_notification_text(row)` builds the message: `📥 <b>New SMS</b>`,
   `📱 <b><device></b>`, the sender, a `Code: <b>…</b>` line when a code was
   found (and the snippet does not already show it in full), and the snippet
   in `<i>…</i>`. There is no date or time line: the chat stamps every
   message. The device name comes from
   `db.device_by_id` (fallback `Unknown device`); the sender from
   `_sender_label()`: `db.device_contact_name_for_key()` first, then
   `sender_name` from the phone, then `phones.display_number(thread_key)`,
   then the raw sender. Both lookups are wrapped: a failure logs a warning and
   degrades to the fallback, it never fails the delivery.
2. `body_snippet(body, SNIPPET_MAX=160)` collapses whitespace runs into single
   spaces and cuts the result to 160 characters (no ellipsis); the caller
   HTML-escapes it.
3. `extract_code(body)` returns the first plausible one-time code or `None`:
   a standalone 4–8 character token with at least one digit. It is
   deliberately conservative and skips a candidate that is a date
   (`_DATE_LIKE`), part of a phone number (`_names_a_phone`, runs of 8+
   digits), an amount (`_is_amount`, currency chars or words around it), the
   visible tail of a masked card/account (`_is_masked_digits`: `*`, `x`, `X`,
   `•` immediately before it), or an identifier introduced by a label word
   (`_names_an_identifier`: `card`, `a/c`, `acct`, `account`, `order`, `ref`,
   `reference`, `txn`, `transaction`, `invoice`, `bill`, `policy`, `ticket`,
   with at most three small link words such as `ending`, `no`, `last`, `is`
   between). `_snippet_shows_code()` decides whether the snippet already
   carries the whole token; a truncated snippet does not count as showing it.
4. `delivery.thread_url(miniapp_url, thread_key, device_id)` builds the
   `Open chat` `web_app` button. No URL means no button; the notification is
   still sent.
5. One `send_to_chat(..., parse_mode="HTML")`. The result must carry an int
   `message_id`; anything else is `api_error`.
6. `db.update_state(..., "delivered", telegram_msg_id=...)` then
   `db.mark_message_notified()`. The bookkeeping is wrapped separately: the
   notification is already in the chat, so a failure there must not turn a
   delivered message into a retry (which would post it twice).

`NotificationReaper.run_once()`:

1. `db.due_notifications(now, limit=batch)` — `telegram_delete_at <= now`,
   `telegram_msg_id`/`telegram_sent_at` NOT NULL, `telegram_deleted_at` NULL,
   `source != 'import'`, ordered by deadline.
2. `int(telegram_msg_id)`; a value that is not an int clears the row
   (`bad_id`) instead of retrying forever.
3. `tg.delete_message(chat_id, message_id)`. `_interpret_ok` treats a 2xx with
   `ok: true` **and** a 400 whose description contains "not found" as success
   (`already_gone`); 429 raises `rate_limited` with `retry_after`.
4. Success/`ValueError`/not-found → `db.mark_notification_deleted()`.
   `TGError` or any other exception → `db.reschedule_notification(now,
   REAPER_RETRY_S=300)` and a warning; one row never stops the loop.
5. The cycle itself is wrapped: an exception logs `reaper cycle aborted` and
   the loop sleeps again.

The same `run_once()` then calls `_reap_notes(now)`, which deletes the bot's
chatter notes from `tg_messages`:

1. `db.due_tg_messages(now, limit=batch)` — `delete_at <= now`,
   `deleted_at` NULL, ordered by deadline.
2. `int(message_id)`; a value that is not an int clears the row (`bad_id`).
3. `tg.delete_message(row["chat_id"], message_id)` — the chat id comes from the
   note's own row, because a note may have gone to another chat.
4. `ValueError`, a `TGError` of kind `bad_request`, and "not found" →
   `db.mark_tg_message_deleted()`. Any other `TGError` or exception →
   `db.reschedule_tg_message(now, REAPER_RETRY_S=300)` and a warning.
5. Notes are tracked with `TG_NOTE_TTL_S = 3600`: `api.py` records a
   `command_result` note after an accepted `sent`/`failed`/`rejected` report,
   and `updater.py` records its own replies. A `delivered` report posts no
   note by design.

`deleteMessage` limits: the bot may delete only messages it posted itself, in
a chat where it has the right, and never a message older than **48 hours**.
That is why the reaper only touches rows with `telegram_sent_at` and why the
default deadline is 24 h — a deadline beyond 48 h would be undeletable.

### Command state machine

`pending` → `sent` → `delivered`, with `failed`/`rejected` as terminal
alternatives. `db.finish_command` rejects backwards or duplicate transitions
and reports whether the report was accepted. The phone may report `sent`
first and later `delivered`.

`device_id` is mandatory on a command: `POST /commands/{id}/result` looks the
command up and compares its `device_id` with the authenticated device. A
mismatch is a 404, never a silent accept. Revoking a device fails its pending
commands (`db.fail_device_commands`).

### Historical import (`POST /import`)

`importer.ImportItem` accepts `id`/`provider_id`, `sender`, `body`,
`received_at` (epoch seconds), `sub_id`, and `direction` (`"in"` or
`"out"`, default `"in"` so an older APK keeps working). Between 1 and 200
items per request; the phone sends 100. The device always comes from the
bearer token, never from the body.

- `direction='in'`: `db.find_import_duplicate()` then `db.record_message()`
  with `source='import'`, `state='received'`. No job, never forwarded,
  never resumed.
- `direction='out'`: `db.find_import_command_duplicate()` then
  `db.record_imported_command()`, which writes `sms_commands` with
  `source='import'`, `status='sent'`, `result`/`reply_to_telegram_id` NULL,
  and `device_id` from the token. `pending_commands()` only reads
  `status='pending'`, so the row can never reach a phone, and no Telegram
  delivery path reads `sms_commands`. For a sent row `sender` is the
  recipient, which is still the thread key.

Dedupe is server-side, per device and per direction:

- inbound: same `thread_key`, same body, within ±60 s;
- outbound: same `thread_key`, same body, within ±60 s, matched against
  **any** `sms_commands` row of that device. That covers a message the relay
  itself queued and the phone later re-imported from its Sent folder.

The id is deterministic (`imp-<32 hex>`): `sha256(device_id, direction,
"pid:"+provider_id)`, or `sender\0received_at\0body` when there is no
provider id. It includes the device, because two phones may reuse provider
row ids, and the direction, so an inbound and an outbound message of the
same conversation cannot collapse into one row. A replayed batch is
therefore all duplicates, not new rows.

`received_at` must be ≥ 2000-01-01 and not more than 24 h in the future;
the ±24 h drift rule of live ingestion does not apply, because an import
exists to carry years of history. An import bumps `rev` once per request
(scope: device, no thread), only when something was inserted.

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
| `POST /pair/claim` | the phone polls with its claim secret. `202 {"status":"pending"}` = waiting for the admin. `202 {"status":"awaiting_phone_confirmation","device":…,"approved_by":{"name":…,"username":…}}` = approved, the phone's user has to confirm. `200 {"status":"claimed","token":…,"device":…}` = the token, exactly once, after `confirmed: true`. `confirmed: false` cancels the pairing (no token). `confirmed` absent = an older APK, the token is issued on the first approved poll. |

`PairClaimBody.confirmed` is `bool | None`. The decision is three-way and must
stay that way:

- `False` → `db.pairing_cancel()`, `{"status": "cancelled"}`, no token, and the
  admin's approval is withdrawn too.
- Present but not `True` (`null` while polling) → `202
  awaiting_phone_confirmation` with `device` (from `db.device_by_id`, or the
  pairing row when the device is gone) and `approved_by`
  (`approved_by_name`/`approved_by_username`). Nothing secret is in this
  answer.
- `True` → `db.claim_pairing()` issues the token exactly once.
- Absent (`"confirmed" not in payload.model_fields_set`) → the legacy path:
  the token is issued on the first approved poll, exactly as before. Do not
  turn an absent field into an error: old APKs still poll that way.

Mini App pairing and devices (admin only):

| Route | Purpose |
|---|---|
| `POST /app/api/pair/preview` | look up a pairing by id + scan code, before approving |
| `POST /app/api/pair/lookup` | resolve a bare six-character code to its live pairing (`404` unknown, `409` ambiguous) |
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

### Call API

Device surface (per-device token, `require_device`):

| Route | Body | Answer |
|---|---|---|
| `POST /calls/sync` | `{items:[…]}` 1–200 | `{inserted, updated, unchanged, skipped}` |
| `POST /calls/events` | `{call_ref, state, direction, number?, sim_label?, muted?, speaker?, started_at?, ended_at?}` | `{ok:true}` |
| `GET /calls/actions?wait=0..25` | — | `{actions:[…]}`, at most 10 |
| `POST /calls/actions/{id}/result` | `{status, detail?}` (`done`/`failed`/`rejected`) | `{accepted, status}` |

`call_sync` item: `{provider_id, number, cached_name, kind, raw_type,
started_at, duration_s, sub_id, phone_account, is_read, content_hash}`.
`started_at` must be at or after 2000-01-01 and no more than 24 h in the
future; a row that fails any check is `skipped`, the rest of the batch is
stored.

Mini App surface (`require_miniapp`, device in the path):

| Route | Purpose |
|---|---|
| `GET /app/api/devices/{id}/calls` | history of one device; `limit`, `before_ts` + `before_id`, `q` |
| `GET /app/api/devices/{id}/calls/live` | the current `call_sessions` row, shaped once by `calls.live_call_shape` |
| `POST /app/api/devices/{id}/calls/actions` | `{call_ref, action, payload?, client_ref}` |
| `POST /app/api/devices/{id}/calls/read` | `{until_ts}`; clamps the marker to now, answers `{read, missed}` |

- A read route uses `_known_device` (a revoked device's history stays
  readable); a write route uses `_active_device`.
- `POST .../calls/actions` answers `422 emergency_number` for an emergency
  destination, `422 invalid_destination` for an unusable one, `409
  stale_call` for a ref that is not the live call, and `409
  client_ref_conflict` when a `client_ref` is reused for another device,
  action or ref.
- `_device_summary` gains `call` (the live session, or null) and `missed`
  (the unread missed count), so the device list renders the badge and the
  card from one request.
- Any call change bumps `rev.bump_rev(app, device_id, None)` — device-wide,
  because a private number has no thread to name.

Telegram callback data (bot side, `updater._handle_call_callback`):

| Data | Meaning |
|---|---|
| `call:answer:<device>` | answer the live call of that device |
| `call:reject:<device>` | decline it |
| `call:callback:<device>` | dial the number of the ended session the message was about |

The data names the **device**, never the `call_ref`: Telegram caps
`callback_data` at 64 bytes and a ref plus a device id can exceed that. The
bot resolves the live session at tap time, so an old message cannot touch a
new call. Each tap becomes an action with `client_ref = "tg:<callback id>"`,
so a callback delivered twice creates one action.

Device surface: `POST /sms`, `POST /import`, `GET /commands`,
`POST /commands/{id}/result`, `POST /contacts`, `POST /calls/sync`,
`POST /calls/events`, `GET /calls/actions`, `POST /calls/actions/{id}/result`.
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
| `#/d/<device_id>/calls` | the call log of that device |

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
`showScanQrPopup` falls back to the scan code typed by hand: the admin may
type the six-character code alone (`tg.bareScanCode()`), and
`store.pairLookup()` resolves it through `POST /app/api/pair/lookup` because
the Mini App has no pairing id to send. The full payload and its t.me link
keep the direct path through `tg.pairToken()` and `POST
/app/api/pair/preview`, and a code that two live pairings share is refused
with `409` instead of being guessed. The pairing
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

### Mini App calls

- `#/d/<device_id>/calls` is a tab on the device screen (`Chats | Calls`);
  `app.js` routes `parts[2] === "calls"` and the tab keeps its own search
  scope and status line.
- `store.loadCalls()` pages with the same two-half cursor as a thread
  (`before_ts` + `before_id`), merges by id and renders through
  `ui.renderCalls()`; `callsAvailable` turns false on a `404`/feature gap and
  the tab shows "not available" instead of an error.
- The live card is `ui.renderLiveCall(call)`; it is fed first by the `call`
  field of the device summary and then by `GET .../calls/live`. The store
  keeps one timer that refreshes it while a call is up.
- Action taps go through `store.sendCallAction(deviceId, action, {callRef,
  payload, clientRef})`. The `client_ref` is generated once per attempt and
  **reused** by a retry, so a lost answer does not create a second action.
  `409 stale_call` refreshes the card instead of showing an error.
- A `dial` passes `callRef: ""`. `ui.isEmergencyNumber()` hides `Call back`
  and `Call via…` for an emergency number and the backend refuses one anyway
  (`422 emergency_number`).
- The `Call via…` sheet (`ui.callViaEntries`) offers the phone (`tel`),
  `https` links (WhatsApp, Telegram, Signal, Bale, Rubika) and best-effort
  custom schemes (Viber, Skype, FaceTime). It never places a call itself: it
  hands a URL to another app.
- The thread timeline gains `call:<id>` events next to `msg:` and `cmd:`;
  `db.thread_timeline()` returns them with `kind='call'`, the call `kind`,
  `duration_s` and `number`, and only for `present = 1` rows.

## 6. Android map

| Area | Files | Responsibility |
|---|---|---|
| Capture | `receiver/SmsReceiver.kt`, `receiver/MmsReceiver.kt`, `sms/SmsNotificationListener.kt` | default-SMS broadcast path (`SMS_DELIVER` canonical; `SMS_RECEIVED` ignored while default), MMS role stub, notification-listener path |
| Dedupe | `util/Dedupe.kt`, `util/SenderNormalizer.kt` | prevent double capture across paths |
| Provider | `sms/SmsProviderWriter.kt`, `sms/SmsSubscriptionLookup.kt` | write inbox rows when default app; infer SIM |
| Local queue | `data/MessageRecord.kt`, `data/MessageDao.kt`, `data/AppDatabase.kt` | Room message state and migrations |
| Upload | `work/UploadWorker.kt`, `net/RelayClient.kt`, `work/ImportWorker.kt` | sequential upload, historical import paging, result classification |
| Commands | `work/CommandWorker.kt`, `data/HandledCommandDao.kt`, `data/CommandDatabase.kt` | poll, guard, execute, report |
| SMS sending | `sms/SmsSender.kt`, `sms/CommandPartTracker.kt`, `sms/FallbackPartTracker.kt` | SIM choice, multipart tracking |
| Fallback | `sms/FallbackPolicy.kt`, `sms/FallbackSender.kt`, `sms/SendStatusReceiver.kt` | confirmed-failure fallback and caps |
| Scheduling | `work/Schedule.kt`, `receiver/BootReceiver.kt`, `RelayApp.kt` | 5 s command chain, 15 min safety net, uploads |
| Contacts | `contacts/ContactsSync.kt`, `contacts/ContactResolver.kt`, `data/ContactsRepository.kt` | upload and local name resolution |
| Pairing | `pairing/PairingController.kt`, `pairing/QrCode.kt`, `net/PairingClient.kt`, `TokenStore.kt` | QR hand-shake, claim polling, token storage |
| Call log | `calls/CallLogReader.kt`, `calls/CallLogWorker.kt`, `calls/CallLogEntity.kt`, `calls/CallLogDao.kt`, `calls/CallLogDatabase.kt` | provider cursor, type mapping, forward + 7-day re-read upload, durable Room upload queue |
| Dialer | `calls/DialerActivity.kt`, `calls/CallDeepLinks.kt` | `ACTION_DIAL`/`tel:`, dialpad, SIM choice, `Call via…`, emergency passthrough, default-dialer candidate |
| In-call | `calls/CallService.kt`, `calls/CallActivity.kt`, `calls/CallSession.kt` | `InCallService`, events, action long poll, controls, `LiveCallRegistry` |
| Call actions | `calls/CallExecutor.kt`, `calls/CallActionGuard.kt` | apply one action to Telecom, at-most-once guard |
| Settings | `SettingsStore.kt` | base URL, device id and name, pairing state, call-log cursor |
| Messages read model | `messages/SmsRepository.kt`, `messages/SmsThread.kt`, `messages/SmsMessage.kt`, `messages/SmsObserver.kt` | provider reads (`content://mms-sms/conversations?simple=true`, `content://mms-sms/canonical-addresses`, `content://sms`), unread scan, sent insert, read marking, content observer |
| Messages UI | `ui/ShellActivity.kt`, `ui/ThreadActivity.kt`, `ui/messages/ThreadLabels.kt`, `ui/messages/ThreadAdapter.kt`, `ui/messages/ConversationAdapter.kt`, `ui/messages/MessageDates.kt`, `ui/messages/AvatarDrawable.kt`, `ui/messages/MessagesBanner.kt` | the launcher conversation list, one thread with day headers and the composer, group/MMS notices, honest read states |
| Dialer home | `ui/DialerHomeActivity.kt`, `ui/dialer/DialerRepository.kt`, `ui/dialer/DialerTabs.kt`, `ui/dialer/RecentsAdapter.kt`, `ui/dialer/ContactsAdapter.kt`, `ui/dialer/FavoritesAdapter.kt` | Favorites / Recents / Contacts / Voicemail tabs, search, `ACTION_DIAL` rows, the dialpad FAB |
| UI | `ui/MainActivity.kt`, `ui/HistoryActivity.kt`, `ui/ComposeSmsActivity.kt` | setup, pairing, permissions, roles, history, compose stub |

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
  token into `TokenStore` only after the phone's user tapped Pair — the claim
  the `202 awaiting_phone_confirmation` answer ends with is the one that
  returns it. A generation counter makes a stale answer harmless.
- The claim is **confirmed on the phone**. `PairingClient.claim(...)` sends
  `confirmed: null` while polling, `true` after the user tapped Pair and
  `false` when the dialog was cancelled, dismissed, or could not be shown
  (`MainActivity.onPairingConfirmationRequired`). `MainActivity` draws the
  dialog with `setCancelable(false)` and
  `setCanceledOnTouchOutside(false)` and names the server URL, the device and
  the approver from `approved_by.name` / `approved_by.username`; a dismissal
  without an answer is a `false`, never a silent pairing. `respond(false)` is
  also the answer when the activity is finishing, so no token is stored
  without consent.
- `QrCode` renders the payload the server returned; the phone needs no
  camera permission of its own.
- A 401 (or 403) from the backend calls `SettingsStore.handleUnauthorized()`:
  the token is dropped, the pairing state becomes `removed`, **the Room queue
  is kept**, and the device id is **not** changed, so the same `device_id`
  can be paired again.
- Changing the base URL invalidates the token (`invalidateForNewServer()`,
  state `server_changed`): a token belongs to one server only.
- The default-SMS-role request uses the `RoleManager` on Android 10+, the
  `ACTION_CHANGE_DEFAULT` intent on Android 8/9 and the default-apps settings
  as the last fallback, and it re-reads the role instead of trusting the
  result code, because a vendor build can refuse the change without a word.
- The dialer role is requested **before** its permissions: a denial used to
  hide the role dialog for good. `askRolePermissions()` runs after the role
  result (or immediately when the role dialog could not be shown).

Android capture invariant (1.3.3, versionCode 8, commit `149504e`):

- `SMS_DELIVER` is the canonical copy of a message when the app is the
  default SMS app. Some vendor builds (MIUI among them) also deliver
  `SMS_RECEIVED` to the default app; `SmsReceiver` returns early in that case
  (`if (!isDefaultSmsApp && RoleState.isDefaultSmsApp(context)) return`).
  Without the guard both copies were stored with `source='sms'` and both were
  uploaded, because local dedupe keeps same-source repeats on purpose.
- The non-default case is unchanged: `SMS_RECEIVED` is the only broadcast
  there, and the notification-listener path still dedupes across paths.
- Do not "fix" the duplicate by making local dedupe drop same-source repeats:
  two genuine messages with the same text a second apart are both real. Stop
  the second capture at the receiver.

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

Historical import (`work/ImportWorker.kt`):

- Query: `Telephony.Sms.CONTENT_URI` with projection
  `_ID, ADDRESS, BODY, DATE, SUBSCRIPTION_ID, TYPE`, selection
  `_ID > cursor AND TYPE IN (INBOX, SENT)`, sort `_ID ASC LIMIT 100`.
  Received and sent rows share that provider table, so one `_ID` cursor
  orders the whole history; `BATCH = 100`.
- Type: `MESSAGE_TYPE_SENT` becomes `ImportMessage.DIRECTION_OUT`
  (`"out"`), everything else `DIRECTION_IN` (`"in"`). For a sent row
  `ADDRESS` is the recipient, which is still the other side of the thread.
- Cursor: `SettingsStore.importCursor` holds the highest imported `_ID`.
  `SettingsStore.importCursorFormat` describes what that cursor means:
  `0` is a pre-1.3.2 install that only ever imported the inbox, `2`
  (`ImportWorker.CURSOR_FORMAT`) means the shared store with sent rows. When
  the stored value is not `2`, the worker resets the cursor to `0` and
  writes `2` **before** the loop, so the one-time restart happens once per
  install and not on every run.
- Rows are sent directly and never written to Room; MMS is not imported.
  Bodies are never logged.
- Known one-time effect: the first run after 1.3.2 re-reads the store from
  row 0. The backend deduplicates the inbound rows it already has, so the
  cost is a longer run and `duplicates` counters, not a second copy in the
  Mini App.

The app is **not built on the VPS**: there is no Android toolchain there. Say
so in the log whenever the Android code changes.

### Messages and dialer UI (wave 2, implemented)

The local client is a second read model over the phone's own store. It is
deliberately separate from the relay's Room queue:

| Component | Does | Does not |
|---|---|---|
| `SmsRepository.conversations()` | reads `content://mms-sms/conversations?simple=true` (`_id, date, recipient_ids, snippet`, `date DESC`), resolves **every** `recipient_ids` entry through `content://mms-sms/canonical-addresses` (per-call cache), falls back to the first SMS row's `ADDRESS` for a one-to-one thread (`ADDRESS_FALLBACK_LIMIT = 300` reads), keeps an unresolvable thread as "Unknown" with its real `thread_id`, merges contact names, unread counts | drop a thread it cannot name, touch Room, write anything |
| `SmsRepository.messages()` | `content://sms` where `thread_id = ?`, newest first, `MESSAGE_LIMIT = 500` per page plus one extra row for `hasOlder` | decode MMS |
| `SmsRepository.insertSent()` | one `content://sms` row: `ADDRESS`, `BODY`, `DATE`, `DATE_SENT`, `TYPE=SENT`, `READ=1`, `SEEN=1`, `SUBSCRIPTION_ID` when known; returns `_id` + `THREAD_ID` | create a thread by hand, write `MmsSms` internals |
| `SmsRepository.markThreadRead()` | sets `READ=1` for one thread | mark a thread it could not read |
| `SmsObserver` | content observer on `content://sms` and the conversations URI | upload, refresh the relay |
| `ShellActivity` | the only `ACTION_MAIN`+`LAUNCHER`: conversation list, search, prominent SMS-role card, pairing banner, contact-picker FAB, bottom bar (Messages / Phone / Contacts / Settings), `Role & SMS diagnostics` dialog | send, dial |
| `ThreadActivity` | one thread: day headers, in/out bubbles, mark read, load older, composer for one-to-one SMS | send to a group, send MMS |
| `ThreadLabels` | the single source of a conversation label; groups are first names (`+n`) or `Group (n)`, never one member's number | guess a group's number |
| `DialerHomeActivity` | Favorites / Recents / Contacts / Voicemail, search, `ACTION_DIAL` rows, dialpad FAB, call-log delete with `WRITE_CALL_LOG`, dialer-role card, bottom bar | place a call, read visual voicemail |
| `DialerRepository` | `CallLog.Calls` (200 newest), `ContactsContract` contacts and starred favourites, `TelecomManager` voicemail number; `Load` distinguishes denied / failed / empty | cache rows, log a number or a name |

The local send and mirror flow:

1. `ThreadActivity.sendComposer()` calls
   `SmsSender.send(context, settings, destination, body, subscriptionId =
   null)`. Nothing is written before the radio answers `Outcome.Sent`.
2. `SmsRepository.insertSent(...)` writes the provider row and returns
   `StoredSms(rowId, threadId)`. A `SecurityException` or a null insert URI
   becomes `StoredSms(null, -1)`, never an exception.
3. The screen clears the composer, reloads the thread (`loadOlder` paging
   stays intact) and scrolls to the end. When `rowId == null` it shows "Sent,
   but this phone did not store it" and skips the mirror.
4. `ThreadActivity.mirrorSent()` posts one `ImportMessage(providerId, sender,
   body, receivedAt = millis / 1000, subId, direction = "out")` through
   `RelayClient.importBatch`. The backend stores an outbound import as an
   `sms_commands` row with `source='import'`, `status='sent'`: history only,
   never pending, never handed to a phone, never forwarded to Telegram.
5. Failure taxonomy is coarse on purpose: `null` result logs
   `relay mirror skipped: network`, `Rejected`/`Unauthorized` logs
   `… : refused`. The local send is already a success and is never undone.

Group, MMS and role handling:

- A thread with two or more resolved recipients is a group: `SmsThread.address`
  is empty, `avatarKey` is `group:<threadId>`, the label comes from
  `ThreadLabels`, the header hides the call button and the composer becomes
  "Group sending is not supported yet."
- An MMS-only thread has no `content://sms` rows, so `isMmsOnly()` (empty page
  **and** a non-blank provider snippet) shows the snippet as a placeholder.
  A mixed thread shows its SMS rows plus `messages_mms_partial`, which
  `showMmsNote()` raises when the provider's snippet is not the body of the
  newest visible SMS (prefix-tolerant, so a truncated long SMS does not raise
  a false note). An unresolved canonical address is reported separately
  (`messages_no_address`) and is never blamed on MMS; the list repairs the
  thread from its first SMS row first (see below).
- RCS does not exist for this app: no public API, no receiver, no promise.
- `SettingsStore.sendingEnabled` is **not** consulted by the composer. That
  switch exists so a remote command cannot make the phone send without its
  owner present; a person typing on the screen is that consent. Only
  `CommandWorker`/`FallbackPolicy` read it.
- `DialerHomeActivity` never calls `ACTION_CALL`. Every row opens
  `DialerActivity` with `ACTION_DIAL` and `tel:<number>`, so an emergency
  number in a call log is never one touch away from a placed call. Voicemail
  is a `TelecomManager` carrier-voicemail call, not visual voicemail.
- `DialerActivity` and `CallActivity` survive this phase untouched:
  `DialerActivity` keeps `ACTION_DIAL`/`tel:` and stays the default-dialer
  candidate, just not the launcher; `CallService`/`ROLE_DIALER` are unchanged.

Bottom navigation (wave 3):

- `res/menu/menu_bottom_nav.xml` has exactly four items — `nav_messages`,
  `nav_phone`, `nav_contacts`, `nav_settings` — and both
  `ShellActivity.installBottomNav()` and `DialerHomeActivity.installBottomNav()`
  use it through the shared `res/layout/view_bottom_nav.xml`, which each home
  layout includes as a real row (not an overlay). Four is
  the cap on purpose: `BottomNavigationView` keeps every label at any width
  with four items and starts hiding them with five.
- A tap goes through `startTopLevel(...)` (Phone home with `TAB_RECENTS` or
  `TAB_CONTACTS`, `MainActivity` for Settings) so the same page is never
  stacked twice, and the bar then restores its own selection with
  `selectNavSilently()`: assigning `selectedItemId` fires the listener, so a
  guard flag (`suppressNav`, reset in a `finally`) is required.
- `ThreadActivity`, `DialerActivity` and the in-call screen deliberately have
  **no** bar: they are detail screens, reached from a list, and a bar there
  would offer a "top level" that does not exist for them.

Role cards, diagnostics and the address fallback (wave 3):

- The SMS role card is **prominent** (`MessagesBanner.prominentCard`) and is
  the first banner in `ShellActivity.renderBanners()`, because the list is
  empty by design until the role is held. `requestSmsRole()` runs the full
  flow: `RoleManager.ROLE_SMS` intent on API 29+, `ACTION_CHANGE_DEFAULT` +
  `EXTRA_PACKAGE_NAME` on API 26–28, `ACTION_MANAGE_DEFAULT_APPS_SETTINGS` as
  the fallback; `handleSmsRoleResult()` re-reads `RoleState.isDefaultSmsApp()`
  instead of trusting the result code.
- The dialer role card lives on the Phone home (`renderRoleCard()`, with a
  separate sentence from `dialer_role_unavailable_card` when the phone offers
  no dialer role). `requestDialerRole()` asks for the role **first**;
  `askRolePermissions()` (missing `CALL_PHONE`, `ANSWER_PHONE_CALLS`,
  `READ_CALL_LOG`) runs from the role result, or right away when the role
  dialog could not be shown. Permissions are never a gate on the role dialog:
  a single denial used to hide the dialog for good.
- `RoleState.diagnosticsReport()` builds the "Role & SMS diagnostics" text from
  the same helpers the cards use: SDK, SMS/dialer role offered and held, the
  current default SMS and dialer packages, this package, and whether
  `READ_SMS`, `READ_CALL_LOG`, `READ_CONTACTS` are granted. It is reachable by
  long-pressing the Messages role card or from the `menuDiagnostics` menu item,
  and it never contains a number, a name or a message.
- `SmsRepository.conversations()` repairs a one-to-one thread whose
  `canonical-addresses` lookup gave nothing with the `ADDRESS` of the thread's
  first `content://sms` row (`firstAddressOfThread()`, capped by
  `ADDRESS_FALLBACK_LIMIT = 300` per list). If that is empty too, the thread is
  **kept** with its real `thread_id` and `ThreadLabels` renders "Unknown";
  messages stay readable. The debug log line carries counts only
  (`read`, `withAddress`, `withoutAddress`, `fallbacks`).

### Calls on the phone (wave 1 + wave 2 dialer)

The `calls/` package is a fourth stream next to capture, commands and
contacts. It is deliberately not one big service:

| Component | Does | Does not |
|---|---|---|
| `CallLogReader` | reads `content://call_log/calls`, maps `CallLog.Calls.TYPE` to `db.CALL_KINDS`, hashes the row | upload, decide |
| `CallLogWorker` | forward `_ID` cursor + rolling 7-day `DATE` re-read, `POST /calls/sync`, cursor in `SettingsStore.callLogCursor`/`callLogCursorFormat` | parse numbers into a new key format |
| `DialerActivity` | `ACTION_DIAL`/`tel:`, dialpad, SIM spinner, `Call via…`, emergency passthrough, default-dialer candidate | report anything to the backend, act as the launcher |
| `CallService` | `InCallService`: Telecom callbacks, `POST /calls/events`, `GET /calls/actions?wait=10`, the in-call screen and a notification path | keep per-call history, decide policy |
| `CallActivity` | the full-screen UI: answer, decline, mute, speaker, hold, DTMF, hang up | touch Telecom directly (it goes through `CallService`) |
| `CallExecutor` | applies one action to the `Call`/`CallAudioState`/`TelecomManager`, `EmergencyNumbers` | fetch actions |
| `CallActionGuard` | durable id list, written before the radio | store numbers, names or results |
| `CallSession` / `LiveCallRegistry` | the state contract, and "a call owns the action queue" | persist anything |
| `CallDeepLinks` | the call-via catalog and URL building | place a call |

The **wave 2 dialer role** is what makes the app the phone's dialer:

- `DialerActivity` is **not** the launcher any more: `ShellActivity` is. It
  keeps `ACTION_DIAL` and `tel:` and remains the default-dialer candidate, and
  it needs no `ROLE_DIALER` and no network, so it places a call even when the
  relay is down or the phone is unpaired. `DialerHomeActivity` opens it from
  its dialpad FAB with the number already in the field.
- `MainActivity` is the setup screen, reached from the **Settings** item of the
  bottom bar (or the setup menu entry); it
  requests the roles through `RoleState`, starts the pairing flow and shows the
  non-cancelable pairing confirmation dialog
  (`MainActivity.onPairingConfirmationRequired`).
- `CallService` is an `InCallService` with
  `android.telecom.IN_CALL_SERVICE_UI` and `IN_CALL_SERVICE_RINGING` metadata;
  Android binds it only while the app holds `ROLE_DIALER`. The dialer-role
  card on the Phone home offers the role through `RoleState.dialerRoleIntent()`
  (the setup screen can too) and re-reads
  `RoleState.isDialer()` afterwards, because a vendor build can refuse without
  saying so.
- `CallActivity` is `showWhenLocked`, `turnScreenOn`, `singleTask`: a ringing
  call must reach the user over the lock screen, and a second call must not
  stack a second window.
- The manifest carries `READ_CALL_LOG`, `WRITE_CALL_LOG`, `CALL_PHONE` and
  `ANSWER_PHONE_CALLS`; `MainActivity`'s `Calls` card asks for the call-log
  group and starts `Schedule.enqueueCallLog()`. `RelayApp` schedules the 15 min
  periodic net, because a call row appears when the call ends.
- Call waiting is ignored on purpose (one primary call), and nothing in this
  package logs a number, a name or a duration.

Wave 2 (the local SMS client / dialer screens in
[`UI_DESIGN.md`](UI_DESIGN.md)) is **implemented** as `ShellActivity` (the
launcher conversation list), `ThreadActivity` (one thread plus the one-to-one
composer) and `DialerHomeActivity` (the Phone home), with
`messages/SmsRepository` as the read model. It keeps the relay invariants
(Room-before-upload, one `getUpdates` consumer, `device_id` isolation) and it
must never make an emergency call depend on the relay. `DialerActivity` and
`CallActivity` survive that phase as the dialpad and the in-call screen. There
is no visual verification: the host has no JDK, Gradle or emulator, so only
the `android-build` workflow proves the code compiles.

Wave 3 adds the shared four-item bottom bar, the prominent role cards, the
"Role & SMS diagnostics" dialog and the `conversations()` address fallback —
see "Messages and dialer UI" above for the exact wiring. It is the same
`android-build`-only story: no device was available.

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
6. A field the phone must show before it stores the token (like the approver)
   travels in the `202 awaiting_phone_confirmation` answer, not in the
   approval: the approval still issues nothing. Keep the three-way
   `confirmed` contract intact (absent = older APK, `null` = still polling,
   `true`/`false` = the user answered), or an old build loses its token path.
7. `pairings.approved_by_name` / `approved_by_username` are the pattern for
   such a field: additive and nullable, filled at approval from the Telegram
   admin profile, never a secret, and allowed to be empty.

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

### Change the notification format or the Telegram routing

1. `delivery.NotificationReaper` and `delivery.DeliveryWorker` are separate
   tasks; a change to one must not make the other depend on it. Both start
   only when `run_workers=True`.
2. `delivery.py` builds the notification: `📥 <b>New SMS</b>`,
   `📱 <b>device</b>`, the sender, an optional `Code: <b>…</b>` line from
   `extract_code()`, and the HTML-escaped `body_snippet()` in `<i>…</i>`.
   **Do not add the full body and do not add a date or time line** — the
   snippet and the extracted code are the whole of what leaves the relay, and
   the chat already stamps every message. A missing
   device row must not block the delivery, it degrades to `Unknown device`.
   `extract_code()` must stay conservative: a masked tail, an identifier
   label, a date, an amount or a phone number is never a code, and an
   unrecognised shape means no `Code:` line (the snippet still shows it).
   `_snippet_shows_code()` keeps the line off when the snippet already holds
   the token in full.
3. The bot's chatter notes are a different stream: `api.py` and `updater.py`
   call `db.record_tg_message(..., kind, now + TG_NOTE_TTL_S)` and
   `NotificationReaper._reap_notes()` deletes them from `tg_messages`. Do not
   store a note on `messages`: it has no message row. Change the TTL in
   `TG_NOTE_TTL_S` (3600 s), not at the call sites.
4. The Mini App link in the "Open chat" button is built by
   `delivery.thread_url(base, thread_key, device_id)`; the device id has to
   be in the URL, otherwise the Mini App cannot open the right conversation.
5. The deadline written by `db.mark_message_notified()` and the one written by
   `db.mark_thread_read()` are the same contract: 86400 s by default, 300 s
   after a read, 300 s retry after a failed delete. Change all three together,
   and keep the default under 48 h (Telegram's delete limit).
6. Tests: `test_delivery.py` (format, snippet, code extraction, button,
   deadline, reaper outcomes), `test_tg.py` (`deleteMessage`, not-found, 429),
   `test_db.py` (schema, `seen_at`, `due_notifications`, `tg_messages`,
   rescheduling), `test_miniapp_inbox.py` (read sets `seen_at` and shortens the
   deadline).
7. A reply to a notification goes back to the device that captured it
   (`updater._handle_reply`). A device that is no longer active cannot be
   replied to: say so instead of queueing a command nobody would run.
8. The bot never broadcasts: `db.resolve_command_device()` picks exactly one
   device or the command is refused with a readable error.
9. Add backend tests for the state transitions and the label.

### Add a Messages screen or a conversation/row field

1. Read the field in `messages/SmsRepository` (projection + cursor index +
   `SmsThread`/`SmsMessage`) or add a new `suspend` reader there. Every read
   hops to `Dispatchers.IO` and reports a failure as an empty result plus the
   `failed`/`denied` flag, never a thrown `SecurityException`.
2. Decide whether it is a *conversation* field or a *message* field.
   `SmsThread` comes from `content://mms-sms/conversations?simple=true`; a
   message field comes from `content://sms`. They are different providers and
   a new column must be added to the matching `PROJECTION` array and read by
   index.
3. Render it in `ui/messages/ConversationAdapter` (list) or
   `ui/messages/ThreadAdapter` (bubbles), never both with different rules.
   Labels go through `ThreadLabels.of()`, timestamps through
   `MessageDates`: do not format a date inline, or the row, the day header and
   the bubble clock will disagree.
4. If the field changes the label of a group, change `ThreadLabels` only. A
   group must never fall back to one member's number.
5. Keep the search filter in mind: `ShellActivity` and `DialerHomeActivity`
   filter an already-loaded list, so a new field only becomes searchable when
   the filter function learns about it.
6. There is no device-free test for a provider read. State in the log that the
   screen was not built or visually verified, and keep the layout changes
   small enough to reason about without a device.

### Change the composer, the local send or the mirror

1. The order is fixed: radio → provider insert → reload → best-effort mirror.
   `ThreadActivity.sendComposer()` must not write anything before
   `SmsSender.send(...)` answers `Outcome.Sent`.
2. The provider row is written by `SmsRepository.insertSent()`; keep the
   column set (`ADDRESS`, `BODY`, `DATE`, `DATE_SENT`, `TYPE=SENT`, `READ=1`,
   `SEEN=1`, `SUBSCRIPTION_ID` when known) and `DATE = DATE_SENT` = the moment
   the radio accepted the message. Do not create a thread by hand and do not
   write `MmsSms` internals: let the provider assign `THREAD_ID`.
3. A `null` `StoredSms.rowId` is not a send failure. Keep the "Sent, but this
   phone did not store it" notice and skip the mirror; never draw a bubble
   for a row the provider refused.
4. The mirror is `RelayClient.importBatch(listOf(ImportMessage(...,
   direction = "out")))`. `receivedAt` is **epoch seconds** (`DATE` is
   millis, so divide by 1000) and `subId` is coerced to `0..99`. The backend
   stores it as `sms_commands` with `source='import'`, `status='sent'`.
5. A failed mirror must stay silent for the user. Log the category only
   (`network` / `refused`), never the number, the body or a contact name.
6. Do **not** add a `sendingEnabled` check to this path. The switch is for
   remote commands; a person typing on the screen is the consent. If a durable
   outbound queue is ever added, it belongs behind the local row, not instead
   of it.
7. Update `README.md` (the send/mirror sequence and the known limitations) in
   the same change.

### Add a tab to the Phone home

1. Add the enum case to `ui/dialer/DialerTab` with its title resource. The
   enum is the only source of the tab strip, the adapter choice and the state
   text; do not add a tab in XML.
2. Add the read to `ui/dialer/DialerRepository` and return `Load<T>`:
   `denied` for a missing permission, `failed` for a provider that broke,
   empty items only when the phone really has no rows.
3. Handle it in `DialerHomeActivity.loadCurrentTab()` and in the empty-state
   and banner rendering. A new tab must not inherit the previous tab's
   "no rows" sentence.
4. A row must open `ACTION_DIAL` through `openDialpad(number)` or
   `dial(number)`. Never `ACTION_CALL`, never a direct Telecom call, and
   never a special case for a number that looks like an emergency number.
5. Add the tab to the search filter and to `onSaveInstanceState` if it has
   state; a restored tab and a tapped tab must take the same code path.
6. Device-free tests do not exist for a provider read; require the
   `android-build` workflow and record that no visual verification was done.

### Add a call-log or contact column

1. `ui/dialer/DialerRepository.recents()` owns the `CallLog.Calls` projection;
   add the column there, read it by `getColumnIndex`, and carry it in
   `RecentCall`. Never query the provider from an adapter.
2. `contacts()`/`favorites()` read `ContactsContract`: keep the size caps
   (`MAX_CONTACTS`, `MAX_FAVORITES`) and return `Load` with the permission
   outcome. The repository stays read only: never write a star or a name back.
3. Render the new value through `DialerFormat.meta(...)`, which drops empty
   parts instead of leaving a stray separator, or extend `DialerFormat` for a
   new shape (duration, time). Do not format in the adapter.
4. A blank number is a "private number" row, not an error: keep the toast
   path in `dial()`.
5. Keep `CallLogReader`/`CallLogWorker` untouched: that is the upload stream
   and it hashes its own projection. The Phone home is a read-only local view.
6. Update `README.md` if the visible field changes what the screen means.

### Add or change a call action

1. Add the verb to `db.CALL_ACTIONS`. A verb that may not be repeated freely
   gets a `db.CALL_ACTION_TTL_S` entry; everything else falls back to
   `CALL_ACTION_DEFAULT_TTL_S = 300`.
2. `calls.CallActionBody` validates the verb against that tuple, so a new verb
   is accepted by the Mini App route as soon as the tuple knows it. Decide
   whether it needs a live `call_ref` (`dial` is the only empty-ref action) and
   whether it may carry a `payload`.
3. The TTL is written in `db.call_action_create()` — change it there and
   nowhere else, or the bot and the Mini App will disagree.
4. Android: add the verb to `CallActionNames`, execute it in
   `CallExecutor`, and make sure `CallActionGuard.claim()` runs **before** the
   Telecom call. The idle path (`CommandWorker.runIdleDialActions`) executes
   only `dial`; every other verb it claims is reported `rejected`
   (`no live call`). A new verb that must also work while idle needs an
   explicit branch there, otherwise it only ever gets rejected (trap 31).
5. Mini App: add the button in `ui.renderLiveCall()` and send it through
   `store.sendCallAction()` with a fresh `client_ref`.
6. Tests: `backend/tests/test_call_actions.py` (creation, TTL, claim, result,
   expiry, transitions) and `backend/tests/test_calls.py` (the route).

### Change the ringing or missed text

1. Both texts are built by `calls._call_text()` from a header, the device name
   and the caller; the headers are `Incoming call` and `Missed call` in
   `_notify_ringing()` and `_notify_missed()`.
2. Do **not** add a number to the text when a contact name exists, and never
   add the call body of anything: there is none. The caller label is
   `calls._caller_label()` — contact name, else `display_number`, else
   `Unknown number`.
3. The buttons come from `calls._call_keyboard()` and the verbs
   `answer`/`reject` (ringing) and `callback` (missed). `CALL_VERBS` in
   `updater.py` must know a new verb, or the tap is answered as stale.
4. Keep the send bounded: `NOTIFY_TIMEOUT_S = 2.0` and `_send_call_message()`
   swallow every failure. A slow Telegram must never delay the phone's state
   report.
5. Tests: `tests/test_calls.py` (message text, buttons, no-body rule) and
   `tests/test_updater.py` (the callback path).

### Change the freshness windows

1. The constants are `LIVE_RINGING_TTL_S` (120), `LIVE_ACTIVE_TTL_S`
   (12 * 3600) and `LIVE_DISCONNECTED_GRACE_S` (60) in `calls.py`.
2. `calls.session_is_live()` is the **only** place that applies them;
   `live_call_shape()` (what the Mini App and `_device_summary` show) and
   `session_actionable()` (what may still take an answer) both call it. Do not
   duplicate a window in a route or in the UI.
3. Widen a window only with a reason: a ringing window is a guard against a
   phone that died mid-ring, and the disconnected grace is what lets the card
   render "call ended". A shorter window hides a real call; a longer one offers
   Answer on a call that is over.
4. Tests: `tests/test_calls.py` (fresh, stale and boundary cases for each
   state).

### Add a call-via target

1. Android: add an `Entry` to `CallDeepLinks.ALL` with `id`, `labelRes`,
   `capability` (`CAPABILITY_CALL` or `CAPABILITY_CHAT`), the `scheme` the
   package manager is asked about and a `buildUrl` that cannot throw on a
   partial number. Set `alwaysVisible = true` only for an `https` URL — a
   custom scheme that nothing handles is a dead button.
2. An `https` entry is always offered. A custom scheme needs a matching
   `<queries>`/`<intent>` entry in `AndroidManifest.xml`, otherwise package
   visibility hides it on API 30+.
3. Mini App: add the counterpart in `ui.callViaEntries()` and keep it out of
   the sheet for an emergency number (`ui.isEmergencyNumber()` is checked
   before the list is built).
4. Never add a target that can place an emergency call: the emergency gate is
   in `CallExecutor.EmergencyNumbers`, `calls.is_emergency_number()`,
   `ui.isEmergencyNumber()` and the `Call back` handler, and all four must
   agree on the digit list.
5. Tests: a pure-JVM test for `buildUrl` and the number normalisation; there
   is no device-free test for package visibility.

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

The suite collects **785 tests**: **781 passed, 3 failed, 1 error**. The
four non-passing tests do not pass for reasons that predate the multi-device
work and are still open:

- `tests/test_commands.py::test_search_by_name_suggests_contacts`
- `tests/test_commands.py::test_contacts_are_uploaded_and_searchable`
- `tests/test_db.py::test_chat_search_matches_name_number_and_body_and_escapes_wildcards`
- `tests/test_app_assembly.py::test_shutdown_is_prompt_when_updater_blocked_in_long_poll`

The first three are contact/chat-search failures, and the last is the
shutdown-timing error. Everything else passes, including the notification
tests in `test_delivery.py` (format, 160-character snippet, code extraction and
its skip rules, button, deadline, reaper outcome and retry), the sent-note
tests in `test_db.py`/`test_delivery.py` (`tg_messages`, the one-hour TTL,
`not found` as success, reschedule, bad id), the command note tests in
`test_commands.py` (one note per accepted `sent`/`failed`/`rejected`,
`delivered` silent, bookkeeping failure harmless) and the pairing-confirmation
tests in `test_pairing.py` (`awaiting_phone_confirmation`, `confirmed:
true`/`false`, the absent-field legacy path, the approver columns).
`test_tg.py` covers `deleteMessage`, already-gone and 429,
`test_miniapp_inbox.py` covers a read setting `seen_at` and shortening the
deadline, and `test_app_assembly.py` covers the reaper task starting with the
workers. Do not report the suite as green, and do not treat these four as new
regressions.

Useful focused runs:

```bash
.venv/bin/python -m pytest tests/test_delivery.py -q
.venv/bin/python -m pytest tests/test_ingest.py tests/test_import.py -q
.venv/bin/python -m pytest tests/test_miniapp_send.py -q
.venv/bin/python -m pytest tests/test_pairing.py tests/test_devices.py -q
.venv/bin/python -m pytest tests/test_device_isolation.py -q
.venv/bin/python -m pytest tests/test_calls.py tests/test_call_actions.py -q
```

Test modules: `test_db`, `test_delivery`, `test_ingest`, `test_import`,
`test_commands`, `test_command_routing`, `test_api_state`,
`test_admin_config`, `test_devices`, `test_device_isolation`, `test_pairing`,
`test_updater`, `test_tg`, `test_security`, `test_config`, `test_phones`,
`test_miniapp_auth`, `test_miniapp_inbox`, `test_miniapp_send`,
`test_miniapp_shell`, `test_miniapp_frontend_static`, `test_app_assembly`,
`test_set_menu_script`, `test_calls`, `test_call_actions`.

There are no Android instrumentation tests and no JavaScript test runner. The
Mini App tests are Python static assertions plus API tests. If you change the
Android app, state clearly in the log that it was not built or tested on the
VPS. A change is proven to compile only by the `android-build` GitHub Actions
workflow, which must publish `sms-relay-debug.apk` on the rolling
`android-latest` release; the app itself is **not device-tested** — the screen
behaviour (role prompts, bottom navigation, diagnostics, pairing confirmation)
still needs a manual device matrix (dialer role, SMS role, locked-screen calls,
dual SIM, emergency, provider/vendor quirks, notifications).

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
3. **Old messages in the chat are full bodies.** The reaper only deletes rows
   it sent as notifications (`telegram_sent_at` set). Everything forwarded
   before that change stays in the chat, and Telegram refuses to delete a
   message older than 48 hours anyway. Do not "fix" this by backfilling
   `telegram_sent_at`: that would make the reaper try to delete history it
   cannot delete.
4. `/fallback` and `/phoneb` write backend settings that Android never reads.
   The phone's local `SettingsStore` is the real fallback configuration, and
   the Phone B fallback itself is frozen and off by default.
5. The backend accepts 3–4 digit `short:` threads; Android requires 5 digits
   to dial, so those threads are read-only in practice.
6. `ImportWorker` sends 100 items (both directions share the `_ID` cursor)
   and treats 413 as permanent; a large batch can stall the import cursor.
7. Import dedupe is a ±60 s window on identical text in one conversation, so
   two genuinely distinct messages with the same text inside a minute (a
   repeated "ok", a duplicate send) collapse into one row in either
   direction. That is the price of making a replayed import idempotent.
8. Filters match raw sender strings and case-sensitive keyword substrings;
   they are not applied to imported history.
9. The Telegram update offset is in memory; a restart can redeliver a command.
10. Fallback candidates are the 50 newest rows in 48 hours; older failures can
   starve.
11. **The reaper only runs with the workers.** `main.create_app(run_workers=
    False)` (tests, tooling) starts neither the delivery worker nor the
    reaper, so nothing is deleted in that mode. A notification whose delete
    is permanently rejected (wrong rights, chat migrated) is rescheduled every
    300 s forever — one indexed row, not a crash, but it never disappears.
    Check `telegram_delete_at` and the journal `reaper rescheduled` lines.
12. After 8 attempts any error, including `timeout`, becomes `failed`; this can
    authorize a fallback for a message Telegram may have accepted.
13. FastAPI `/docs` and `/openapi.json` are reachable through the proxy unless
    explicitly disabled.
14. **After the upgrade every phone has to pair again.** The shared phone
    token is gone; an old APK that still uses it gets a 401 on every request.
    The app keeps its Room queue and its device id, so a fresh pairing
    restores the same device row with its old history.
15. The archived `legacy` device holds the history of the previous phone. It
    is read-only: it cannot pair, cannot poll commands and cannot send, and
    the Mini App shows it as archived.
16. **`telegram_msg_id` is cleared by the reaper.** Reply-to-SMS and the
    `details:`/`showmsg:` callbacks look a message up by that id. Once the
    notification is deleted, a reply to it has nothing to resolve: the admin
    must use `/sms` or the Mini App. A delete that succeeds clears the column
    on purpose; do not keep a stale pointer "just in case".
17. The old device-less Mini App routes (`/app/api/chats…`,
    `/app/api/contacts`) always answer **410**. A client that still has the
    old bundle therefore shows an error until the new device-first bundle
    reaches it. That is intentional: a stale client fails loudly instead of
    reading a mixed inbox.
18. The pairing claim limiter counts only the *wrong* secrets, per pairing
    (and per address for an unknown pairing id). A valid poll is never
    rate-limited, because every phone shares one address behind Caddy.
19. The Telegram bot's contact search (`flow.py`) still falls back to the
    global legacy `contacts` table when the resolved device has no matching
    contact. The Mini App readers are strictly per-device; the bot picker
    can mix legacy names until it is made device-aware.
20. Starting a new pairing cancels older pending/approved pairings for the
    same device. An approved-but-unclaimed device row (`token_hash` NULL)
    may be approved again; one with an active token is refused until it is
    revoked. If the admin approves an old QR after the phone generated a
    newer code, the approval returns `410`. The app must show the current
    code; do not let two live codes coexist.
21. **Duplicate capture is an Android problem, not a backend one.** Vendor
    builds may deliver `SMS_DELIVER` and `SMS_RECEIVED` for one SMS; 1.3.3
    ignores `SMS_RECEIVED` while the app is the default SMS app. The backend
    dedupe keeps same-source repeats on purpose, so do not "fix" the duplicate
    there. Existing duplicate rows from before 1.3.3 and from the bulk
    re-import (1,039 groups / 5,039 rows, never forwarded to Telegram) are
    still in the database: **no cleanup has been performed**, and deleting
    them is a deliberate, separate data operation.
22. **Deleting a notification does not delete the message.** The row, its
    body and its `seen_at` stay; only `telegram_msg_id`/`telegram_delete_at`
    are cleared and `telegram_deleted_at` is set. A reaper bug that deletes
    chat messages must never be able to reach the history through
    `messages`.
23. **`callback_data` is capped at 64 bytes.** A call message carries
    `call:answer:<device>`, and a device id long enough to break the cap makes
    `_call_keyboard()` return `None`: the message goes out with **no** buttons,
    because Telegram rejects the whole message when a `reply_markup` is
    invalid. Do not "fix" it by truncating the device id — the bot could not
    answer for a device that does not exist.
24. **A disconnected session is live for only 60 s.** `session_is_live()`
    keeps `disconnected` for `LIVE_DISCONNECTED_GRACE_S` so the Mini App can
    render "call ended"; after that the card and the missed badge see an idle
    device. An action created against a ref in that window is still refused:
    `session_actionable()` excludes `disconnected`.
25. **`dial` has `call_ref=""`.** It is the only action without a live call;
    its `payload` is the normalised destination. The phone must therefore never
    apply `call_ref` logic to a `dial`, and a retry has to be recognised by
    `client_ref` (the partial unique index) — not by the ref.
26. **The phone must refuse a mismatched or expired ref.** The server already
    refuses `409 stale_call`, but a report and an action can cross on the
    network. `CallExecutor` compares the action's `call_ref` with the call it
    owns and reports `rejected` instead of acting on the current call. Never
    relax that check to "the call that is up".
27. **Emergency numbers are a four-way contract.**
    `CallExecutor.EmergencyNumbers` (Android), `calls.is_emergency_number()`
    (backend), `ui.isEmergencyNumber()` (Mini App) and the `Call back`
    handler in `updater.py` must agree, and the comparison is on digits with
    leading zeros stripped so `0112` resolves to `112`. A number added to one
    list and not the others is a remote-dial path for an emergency service.
28. **MMS and RCS are boundaries, not bugs.** RCS runs on Google's private
    backend and has no public API; MMS has no decoder or sender in this tree
    (`MmsReceiver` acknowledges a WAP push and ignores it). `content://sms`
    threads cannot represent a group MMS conversation. Do not promise either
    in the UI, and do not try to read `content://mms` without a PDU phase.
29. **Google's assets and identity are off limits.** The dialer/messages
    layout may follow the flow in `UI_DESIGN.md`, but Google icons, wordmarks,
    animations and code must not be copied. Use Material Symbols or original
    vector paths only.
30. **An imported or legacy call has no live session.** A `call_log` row from
    the sync is history even when `started_at` is recent; the live card reads
    `call_sessions` only. Do not synthesise a session from a log row: it would
    offer Answer for a call that already ended.
31. **A non-`dial` action claimed on the idle path is rejected, not run.**
    `CommandWorker.runIdleDialActions()` runs only `dial`; any other action it
    claims has no live call, so it reports `rejected` with `no live call` and
    the row reaches a terminal state instead of being stranded. The action was
    already consumed by the atomic claim, so it cannot be re-delivered; the
    report is the only way out. That report is **not retried**: if it fails on
    the network, `ok` goes false and the poll chain retries, but this row is
    already `delivered` and never comes back, so it stays `delivered` for ever
    and the Mini App sees no result. A verb that must work while idle still
    needs an explicit branch here rather than a wider report.
32. **A group has no single address.** `SmsThread.recipients` holds every
    resolved `recipient_ids` entry; two or more means a group, and a group's
    `address` is deliberately empty. Never take `recipients.first()` as "the
    number" — that turns a group into a private chat with one member, and the
    thread screen would then offer that member's call button and composer.
    Resolve every id in `canonical-addresses`, and label via `ThreadLabels`
    only.
33. **An MMS-only thread looks like an empty thread.** `content://sms` has no
    rows for it, so the screen infers MMS from "empty page **and** a non-blank
    provider snippet" (`ThreadActivity.isMmsOnly()`), and only after the first
    page has loaded. A blank `address` with rows is a different case: an
    unresolved canonical address, which gets its own sentence. Do not merge
    the two, and do not show a composer for either.
34. **`content://sms` is not the relay's Room queue.** The Messages screens
    read the phone's store; `MessageRecord`/`MessageDao` are the upload queue
    and must never be rendered as a conversation (and the provider rows must
    never be uploaded as live SMS — the mirror goes through `POST /import`).
    Confusing the two produces either a duplicate upload or a thread that
    disagrees with the phone's own messaging app.
35. **`sendingEnabled` is a remote-command switch.** It gates
    `CommandWorker` and `FallbackPolicy`, not the local composer. Adding it to
    `ThreadActivity` would block the owner from texting on their own phone;
    removing it from the command path would let Telegram send without consent.
    Keep it where it is.
36. **The relay mirror is best effort, by design.** The local provider row is
    written first and the `POST /import` comes after it, on a background
    thread, with its result ignored except for a category log line. Do not
    make the screen wait for it, do not retry it from the send path, and do not
    turn a mirror failure into a send failure. A durable outbound queue is a
    separate feature that does not exist yet.
37. **The provider can refuse, and a refusal is not a crash.**
    `content://sms`, `content://mms-sms/*`, `CallLog.Calls` and
    `ContactsContract` all answer `SecurityException` or a null cursor when a
    role or permission is missing. `SmsRepository` and `DialerRepository`
    return an empty result plus `failed`/`denied`; the screens draw a note.
    Every new read must do the same, and must never log a number, a name or a
    body.
38. **A Phone-home row never places a call.** `DialerHomeActivity` opens
    `ACTION_DIAL` with `tel:<number>`; `ACTION_CALL` would skip the dialpad
    and the platform's emergency handling and could dial an emergency number
    from one touch on a log row. The dialpad (`DialerActivity`) is the only
    place that routes an emergency call, and it needs no network.
39. **No Google assets.** The Messages screens and the Phone home follow the
    layout and flow in `UI_DESIGN.md`, not Google's assets: no Google icons,
    wordmarks, animations or code, and no `ACTION_CALL` shortcut that pretends
    to be Google Dialer. Use Material Symbols or original vector paths and the
    stock Material 3 theme (`Theme.SmsRelay.Messages`).
40. **The snippet and the code leave the relay over Telegram.** The
    notification carries `body_snippet()` (160 characters, whitespace
    collapsed) and the `extract_code()` result in the private admin chat. That
    is a deliberate product decision; the **full** body stays in SQLite and
    the Mini App. Do not go back to a notification with no SMS text, do not
    widen the snippet, and do not put the snippet, the code, a number or a
    name in a log line. `test_delivery.py` asserts the format.
41. **OTP extraction is a conservative heuristic.** `extract_code()` takes the
    first standalone 4–8 character token with at least one digit and skips
    dates (`_DATE_LIKE`), phone numbers (`_names_a_phone`), amounts
    (`_is_amount`), masked card/account tails (`_is_masked_digits`: `*`, `x`,
    `X`, `•`), and identifiers introduced by `card`, `a/c`, `acct`, `account`,
    `order`, `ref`, `reference`, `txn`, `transaction`, `invoice`, `bill`,
    `policy`, `ticket` (`_names_an_identifier`). A code-shaped card or order
    number must never be labelled `Code:`; an unusual OTP format can be
    missed, and the snippet still shows it. Do not widen the filters to "find
    more codes" — a wrong code line is worse than none.
42. **The phone confirmation needs a backend that speaks it.** A current APK
    sends `confirmed` and waits for `202 awaiting_phone_confirmation`; an
    older backend returns the token on the first approved poll, so the dialog
    never appears. The absent-field path is the older-APK path and must keep
    working (token issued directly): do not make it an error and do not claim
    the confirmation is enforced end-to-end.
43. **An OEM can suppress the role dialogs.** Some builds ignore the
    `ROLE_SMS`/`ROLE_DIALER` request or answer OK without changing anything.
    The Messages list shows the SMS-role card as a prominent card and the
    Phone home shows the dialer-role card, so the state is visible; the
    "Role & SMS diagnostics" dialog (long-press the Messages card, or the
    `menuDiagnostics` item) reports the SDK, whether each role is offered and
    held, the default SMS/dialer package and the permission grants. Check it
    first before hunting the app; when no role is offered the flow falls back
    to the default-apps settings.
44. **The bottom bar has exactly four items and only on the top level.**
    `menu_bottom_nav.xml` is four items (`nav_messages`, `nav_phone`,
    `nav_contacts`, `nav_settings`); five starts hiding labels.
    `ThreadActivity`, `DialerActivity` and the in-call screen have no bar on
    purpose. A tap must go through `startTopLevel(...)`, and the bar must
    restore its own selection with `selectNavSilently()` — assigning
    `selectedItemId` fires the listener and would re-enter navigation.
45. **"Unknown sender" is a readable thread, not a dropped one.**
    `SmsRepository.conversations()` repairs a one-to-one thread from its first
    SMS `ADDRESS`, and keeps the row with its real `thread_id` and
    `messages_unknown_sender` ("Unknown") when even that fails
    (`ADDRESS_FALLBACK_LIMIT = 300` reads per list). Do not filter such a
    thread out to tidy the list: the messages are real and still open.

## 11. Definition of done
- [ ] The requested behavior is implemented or the investigation is complete.
- [ ] Relevant tests pass, or the inability to run them is recorded.
- [ ] A local send appears in the thread it was sent from, and — when the phone
      is paired — the outbound bubble also appears in the Mini App. A local
      send never depends on the relay: an unpaired phone still texts, and a
      failed mirror leaves the local message and its bubble intact.
- [ ] Per-device isolation is verified: no query, route or UI path reads or
      writes conversation, contact, call or read data of another device.
- [ ] The dialer works offline, and an emergency call never depends on the
      backend: no pairing, no network, no remote action.
- [ ] A notification is a snippet plus an optional extracted code; the full
      body is still only in the relay. No snippet, code, number, name or body
      appears in a log, and the OTP extractor was not widened.
- [ ] The pairing still hands out exactly one token, and only after the phone
      user confirmed the approver (`confirmed: true`); the absent-field path
      still works for an older APK.
- [ ] Bottom navigation, role cards and the diagnostics dialog still work on
      both top-level screens, and detail screens still have no bar.
- [ ] README/guide updated if behavior, contracts, commands, env vars, or
      limitations changed.
- [ ] `docs/AGENT_LOG.md` has a new appended entry with date, agent, task,
      files, verification, risks, and follow-ups.
- [ ] No secrets, message bodies, numbers, or contact names were added to
      logs or documentation.
- [ ] No deployment, restart, push, or credential change was performed unless
      explicitly requested.
