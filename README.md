# SMS Relay

A private, single-admin bridge between several Android phones and Telegram.

Every paired phone captures incoming SMS and uploads them to a small FastAPI
backend under its own per-device token. The backend stores them in SQLite,
tags each message with the device that captured it, and posts one short
**notification** to a private Telegram chat: the device name, the sender, a
one-line snippet of the SMS and, when the body holds a plausible one, the code
it asks for, plus an “Open chat” button. Only that snippet and the extracted
code leave the relay over Telegram; the full text stays in SQLite and in the
Mini App. The same backend exposes a device-first Telegram Mini App — devices,
then the per-device chats, then a per-device thread; there is no merged
inbox — and a command queue that lets the admin
send an SMS from a specific phone through Telegram or the Mini App. If
Telegram delivery is confirmed failed, a phone can still send the original
message to a second phone over its own SIM, but
that cellular fallback is frozen: the code is kept, the bot controls that
wrote backend settings are disconnected from the phone, and the feature is
no longer being developed.

On the phone the app is also a real local **Messages client**. The launcher
is the phone's own conversation list, read from the Android SMS provider, not
from the relay's upload queue; a thread screen shows the actual messages with
in/out bubbles and day headers, and a one-to-one composer sends through the
SIM, stores the accepted message in `content://sms` and mirrors it to the
relay as outbound history, so the Mini App shows both sides of a conversation
while Telegram stays the notification channel, not a second inbox.

The same app is also the phone's **call log and dialer**, with a
Google-Dialer-style Phone home (Favorites, Recents, Contacts, Voicemail) in
front of the existing dialpad. Each phone mirrors its call log to the backend,
reports the one call it is on, and executes the call actions the admin asks
for; the Mini App shows the history and a live call card, and Telegram mirrors
a ringing or a missed call with `Answer`, `Decline` and `Call back`. Audio
never leaves the phone: the relay carries message metadata and call control,
never the audio of a call. Emergency calls are placed by the phone alone and
are never remote-controlled.

This document describes the code, not an aspiration. It was written from the
current source tree and intentionally includes the sharp edges that are
visible in the implementation.

## Topology

```
 Paired phones (Android)      VPS
┌───────────────────────┐            ┌───────────────────────┐
│ SmsReceiver /         │ HTTPS  ──► │ Caddy /relay/*        │
│ NotificationListener  │            │   → 127.0.0.1:8090    │
│   → Room queue        │            │ FastAPI, 1 worker     │
│ WorkManager upload    │ ◄──────    │ SQLite (WAL)          │
│ CommandWorker         │ /commands  │ DeliveryWorker        │
│ SmsSender             │            │ Updater (Telegram)    │
│ CallLogWorker         │ /calls/*   │ call_log + sessions   │
│ PairingController     │ /pair/*    │ call_actions          │
│ CallService (Telecom) │            │                       │
│ DialerActivity        │            │                       │
│ ShellActivity /       │            │                       │
│   ThreadActivity      │ /import    │ outbound history      │
│   → content://sms     │            │                       │
│ DialerHomeActivity    │            │                       │
└───────────────────────┘            └──────────┬────────────┘
                                                │
                                        Telegram Bot API
                                                │
                                     one private admin chat
                         (each new SMS is a short notification: device,
                         sender, a 160-character body snippet and, when
                         one is found, a `Code:` line; "Open chat" button.
                         The full body stays in the relay. A ringing or
                         missed call is one message with
                         Answer / Decline / Call back)
                                                │
                                        Telegram Mini App
                            (device-first; served by the backend, and
                            the place the full text is read; SQLite
                            keeps it too, and the calls tab and the live
                            call card live here)
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
│       ├── calls/               call log, dialer and the in-call service
│       │   ├── CallLogEntity.kt, CallLogDao.kt, CallLogDatabase.kt
│       │   │                    Room mirror of the call-log upload queue
│       │   ├── CallLogReader.kt provider cursor and call-type mapping
│       │   ├── CallLogWorker.kt forward cursor + 7-day re-read, upload
│       │   ├── DialerActivity.kt
│       │   │                    `ROLE_DIALER`-independent dialpad, `tel:`
│       │   │                    handler, call-via sheet, default-dialer
│       │   │                    candidate (`ACTION_DIAL` + `tel:`)
│       │   ├── CallService.kt   `InCallService`: events, action poll, controls
│       │   ├── CallActivity.kt  incoming/active screen, keypad, hang up
│       │   ├── CallExecutor.kt  executes an action against `Call`/Telecom
│       │   ├── CallActionGuard.kt
│       │   │                    on-disk at-most-once guard for actions
│       │   ├── CallSession.kt   state contract, `LiveCallRegistry`
│       │   └── CallDeepLinks.kt call-via catalog (tel, WhatsApp, Telegram …)
│       ├── messages/            read model of the phone's own SMS store
│       │   ├── SmsRepository.kt conversations, threads, unread, sent insert
│       │   ├── SmsThread.kt, SmsMessage.kt
│       │   │                    provider row shapes (group = several addresses)
│       │   └── SmsObserver.kt   content observer behind the lists
│       ├── ui/                  activities (XML views)
│       │   ├── ShellActivity.kt the launcher: the phone's conversations,
│       │   │                    SMS-role card, bottom bar, diagnostics
│       │   ├── ThreadActivity.kt
│       │   │                    one thread, bubbles, day headers, composer
│       │   ├── DialerHomeActivity.kt
│       │   │                    Phone home: Favorites, Recents, Contacts,
│       │   │                    Voicemail, search, dialpad FAB, role card
│       │   ├── messages/        conversation/thread adapters, labels, dates
│       │   ├── dialer/          Phone-home adapters and provider reads
│       │   └── MainActivity.kt  setup: pairing, permissions, roles,
│       │                        the pairing confirmation dialog
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
│   │   ├── importer.py          POST /import (historical inbox + sent)
│   │   ├── api.py               device/admin REST API
│   │   ├── calls.py             device call log, live session, actions,
│   │   │                        Mini App call routes, Telegram call mirrors
│   │   ├── delivery.py          notification worker, reaper, retries
│   │   ├── tg.py                Telegram Bot API client and error taxonomy
│   │   ├── updater.py           Telegram long-poll bot, call callbacks
│   │   ├── flow.py              in-memory button conversation
│   │   ├── tg_ui.py             keyboards and phone-number helpers
│   │   ├── miniapp.py           Mini App JSON API (device-scoped)
│   │   ├── miniapp_auth.py      Telegram initData validation
│   │   ├── rev.py               device-scoped Mini App revision counter
│   │   └── static/app/          Mini App shell, CSS and ES modules
│   └── tests/                   785 tests across 25 modules, 781 passing
├── deploy/                      systemd, Caddy, installer, backup/restore
├── docs/
│   ├── AGENT_GUIDE.md           implementation map and change recipes
│   ├── AGENT_LOG.md             append-only work log
│   ├── CALLS_DESIGN.md          calls contract (already implemented)
│   └── UI_DESIGN.md             local SMS client / dialer UI direction
└── scripts/                     memory probe and Mini App menu helper
```

## Runtime model

The backend is deliberately a **single Uvicorn process**:

- one SQLite connection with WAL mode, `BEGIN IMMEDIATE` writes, and a
  process-wide write lock;
- one `DeliveryWorker` task that drains the `jobs` table and posts one short
  notification per inbound SMS;
- one `NotificationReaper` task that deletes a notification from the chat once
  its deadline passed (see below) and the bot's one-hour chatter notes; it is
  started together with the delivery worker and only when workers are enabled;
- one `Updater` task that long-polls Telegram and drives the admin bot;
- an in-memory, device-scoped revision counter that wakes Mini App long
  polls and names the device and thread that changed;
- an in-memory Telegram update offset;
- in-memory authentication-failure and pairing rate limiters.

The calls feature adds **no backend task**. A ringing call is a state report
and a bounded Telegram send inside the request; a live call is a phone that
polls `GET /calls/actions?wait=…`; an idle phone polls the same route from
`CommandWorker` every 5 s. Nothing in the backend loops over calls.

Running multiple workers would split or duplicate all of that state. Do not do
it without redesigning those components.

Each phone is the durable capture edge for its own messages: an SMS is written
to Room before any upload is attempted. The backend is the durable delivery
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
| `received` | held while delivery is paused, or imported history | no |
| `pending` | queued for Telegram delivery | yes |
| `delivered` | Telegram accepted the notification | no |
| `uncertain` | ambiguous failure (timeout/network/Telegram unavailable); still retrying | yes |
| `failed` | terminal failure; this is what can authorize the frozen cellular fallback | no |

Retries use exponential backoff from 30 seconds to one hour, with jitter, and
a maximum of 8 attempts. A Telegram `429` honors `retry_after`.

A delivered message is a **notification**, not a copy of the SMS. The chat
receives the device name, the sender's contact name (or the number), one line
of the body, the extracted code when there is one, and one inline button:

```
📥 New SMS
📱 <device name>
<contact name or number>
Code: <b>482913</b>                  ← only when a 4–8 character code is found
<i>Your verification code is 482913. Do not share it with anyone.</i>
[ Open chat ]        ← Telegram web_app button, device/thread deep link
```

The snippet is the body with its whitespace runs collapsed into single spaces,
cut to **160 characters**; there is no date or time line, because the chat
already stamps every message. `extract_code()` looks for the first standalone
4–8 character token that contains at least one digit and skips the shapes that
only look like a code: a masked card or account tail (`*1234`, `x1234`,
`•1234`), a token introduced by an identifier label (`card`, `a/c`, `acct`,
`account`, `order`, `ref`, `reference`, `txn`, `transaction`, `invoice`,
`bill`, `policy`, `ticket`), a date, an amount, or a number that is part of a
phone number. When the snippet already shows the whole code in context the
`Code:` line is dropped, and when the shape is not recognised the line is
simply absent — the snippet still shows the text.

The full body stays in SQLite and in the Mini App; only the snippet and the
extracted code leave the relay over Telegram. There is no logging of either:
the log rule below is unchanged. Messages forwarded by the old
body-forwarding code before this change are still full-text messages in the
chat; nothing rewrites or retracts them.

Each notification carries a deletion deadline (`messages.telegram_delete_at`):
**+24 h** from the send by default, or **now + 5 min** when the Mini App has
already read the message (`messages.seen_at` is set). `NotificationReaper`
runs every **30 s**, takes up to 20 due rows, and calls `deleteMessage`; on
success it clears `telegram_msg_id` and `telegram_delete_at` and stamps
`telegram_deleted_at` — a "message to delete not found" answer counts as
success. A transient failure pushes the deadline back by **300 s** instead of
retrying in a tight loop. A message row the reaper cannot map to a Telegram id
is cleared, not retried forever.

The same reaper deletes the bot's chatter notes — the “✅ SMS sent to …”, the
failure and the rejection notes posted by `api.py`, and the bot's own replies
posted by `updater.py`. Those rows live in `tg_messages`
(`chat_id`, `message_id`, `kind`, `delete_at`, `deleted_at`, `created_at`)
because a note is not an SMS notification and has no `messages` row. Their TTL
is `TG_NOTE_TTL_S = 3600`: an hour after they were sent, `_reap_notes()` reads
`due_tg_messages()` and calls `deleteMessage` for each. A "message to delete
not found" answer is a success, a transient `TGError` reschedules the row by
300 s, a permanent `bad_request` or an id that is not an int is cleared, and
one bad row never stops the batch. The chat id comes from the row itself, so a
note sent to another chat is still deleted there.

### The `messages` row

One row per inbound SMS, live or imported. Besides the message itself
(`id`, `device_id`, `thread_key`, `sender`, `sender_name`, `body`,
`received_at`, `source`, `state`, `attempts`, `last_error`), the notification
lifecycle is stored on the same row:

| Column | Meaning |
|---|---|
| `telegram_msg_id` | Telegram message id of the last notification sent for this row; the reply-to-SMS lookup and the edit callbacks use it. Cleared once the notification is deleted. |
| `telegram_sent_at` | when the notification was accepted by Telegram. NULL for imported history and for rows forwarded by the old body-forwarding code, which is what keeps the reaper away from them. |
| `seen_at` | when the Mini App read this message. Set by the thread-read route, never moved back. |
| `telegram_delete_at` | when the reaper should delete the notification: `telegram_sent_at + 86400` by default, or `now + 300` when the message was already read. Pulled forward, never pushed back, by a read. |
| `telegram_deleted_at` | when the notification was confirmed gone (or answered "not found"). |

All four new columns are additive and nullable, added by an idempotent
`_ensure_column` migration; an existing database keeps every row.
`idx_messages_tg_delete` is a partial index on `telegram_delete_at`, so the
reaper's scan does not read the history.

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

### Calls: history, live session and remote actions

Calls are a fourth stream next to messages, commands and contacts, and they
use three tables with three different lifetimes:

| Table | Lifetime | What it holds |
|---|---|---|
| `call_log` | permanent history | one row per call the phone's provider knows: number, `number_key`, cached name, `kind` (`incoming`, `outgoing`, `missed`, `rejected`, `blocked`, `voicemail`, `unknown`), `started_at`, `duration_s`, `sub_id`, `phone_account`, `is_read`, `content_hash`, `present`. |
| `call_sessions` | ephemeral, primary key `device_id` | the one call the device is on: `call_ref`, `number`, `number_key`, `direction`, `state` (`ringing`, `dialing`, `active`, `held`, `disconnected`), `started_at`, `answered_at`, `ended_at`, `muted`, `speaker`, `sim_label`, `updated_at`. |
| `call_actions` | one row per remote control | `call_ref`, `action` (`answer`, `reject`, `hangup`, `mute`, `unmute`, `speaker_on`, `speaker_off`, `hold`, `unhold`, `dtmf`, `dial`), `payload`, `status` (`pending`, `delivered`, `done`, `failed`, `expired`, `rejected`), `expires_at`, `client_ref`, `result`. |

A device has **one primary call**. Call waiting is out of scope: a second
incoming call is left to the network, so `call_ref` stays unambiguous. A
`call_sessions` row is never history — the phone writes the finished call to
its own call log, and `CallLogWorker` uploads it as a `call_log` row.

`call_ref` is the phone-generated UUID of the current call and the only thing
an action may target. A session that describes a call nobody is on is not
"live", and the freshness rule decides that in one place
(`calls.session_is_live`):

| State | Fresh for |
|---|---|
| `ringing`, `dialing` | 2 minutes since the last update — a ring is over in seconds |
| `active`, `held` | 12 hours — covers any real call, not a phone left on a desk |
| `disconnected` | 60 seconds, still *shown* but no longer actionable |

The 60 s window exists so the Mini App can render "call ended" before the
card disappears. A phone that dies mid-call therefore stops offering
Answer/Hang up after the window instead of for ever.

`call_actions` has its own TTL: `answer`, `reject` and `hangup` expire after
**20 s**, everything else (`mute`, `speaker_*`, `hold`, `dtmf`, `dial`) after
**300 s**. `GET /calls/actions` marks the rows it returns `delivered` in the
same transaction, so the idle poll of `CommandWorker` and the live poll of
`CallService` can never run the same action twice. An action whose TTL passed
is `expired` and is never handed out.

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
   App never asks for the camera itself), opens the t.me link, or types the
   six-character code shown on the phone. A bare code is resolved to its
   pairing by `POST /app/api/pair/lookup` (admin token, rate limited), which
   only considers live pending/approved pairings and answers `409` rather
   than guessing when two of them share the code. The Mini App then shows a
   preview of the pending pairing before the admin approves it. Approval
   creates or reactivates the device but still issues no token.
4. The phone polls `POST /pair/claim` every two seconds (longer after a
   network error) with its claim secret. While approval is pending the answer
   is `202` with `"status": "pending"`. Once the admin approved, an approval is
   **not** a token: the phone sends `"confirmed": null` while it polls and the
   backend answers `202` with `"status": "awaiting_phone_confirmation"`, the
   `device` object, and `approved_by` — an object with `name` and `username`,
   each nullable. The phone then shows a **non-cancelable** dialog naming the
   server URL, the device and the approver; only when the user taps **Pair**
   does it claim with `"confirmed": true`, and the token is returned
   **exactly once** and written into the encrypted store right away. Tapping
   Cancel, dismissing the dialog or a missing screen claims with
   `"confirmed": false`: the pairing is cancelled, the admin's approval is
   withdrawn and no token is ever issued. An APK that does not send the field
   at all is the legacy path and still gets the token straight away, so an old
   build does not break.

The approver shown on the phone is stored on the pairing itself:
`pairings.approved_by_name` and `pairings.approved_by_username` are additive,
nullable columns filled at approval time from the Telegram admin's Mini App
profile (`first_name`, `username`). A missing name or username is valid — the
dialog then says "Approved in the Mini App".

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
already paired); re-pairing a revoked device reactivates it. Starting a new
code cancels any older pending or approved-but-unclaimed code for the same
device, so an old QR cannot be approved after the phone has moved on. A
device row that never claimed its token can be approved again; an actively
paired device must be revoked first.

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
5. `DeliveryWorker` claims the job and builds one short notification: the
   device name that tells the admin which phone it came from, the sender
   (the device's own contact name wins, then the name the phone attached to
   the SMS, then the number), a `Code:` line when `extract_code()` finds a
   plausible one, a 160-character one-line snippet of the body, and an “Open
   chat” button that deep-links to that device/thread in the Mini App. The
   full body is not included, but the snippet and the extracted code are —
   that is the one place where SMS text leaves the relay over Telegram. A
   failed device or contact lookup must never block delivery: the notification
   falls back to `Unknown device` and to the raw sender.
6. On success the message becomes `delivered` and records the notification
   id and its delete deadline (`mark_message_notified`). On a definitive
   error it is retried. On exhaustion it becomes `failed`.
7. The admin opens the thread in the Mini App. The read route sets
   `messages.seen_at` for the messages it covers and pulls the notification
   deadline of that device/thread forward to now + 5 min.
8. `NotificationReaper` deletes the notification once the deadline passed and
   clears the pointer. See “Message states” above for the timing.

### 3. Paused delivery

`/pause`, the pause button, or `POST /admin/pause` sets `settings.paused=1`.
New live messages are stored as `received` with no job. They remain visible
in the Mini App under their device. `/resume` or the pause button sets the
flag back to `0` and calls `db.resume_received()`, which moves `received`
rows to `pending` — but **never imported history**. An import is a replay of
the phone's own inbox, not a message to forward, so `resume_received()`
excludes `source='import'`.

### 4. Historical import

The Android app reads the phone's SMS store (`content://sms`), not just the
inbox: received and sent rows live in the same provider table, so one cursor
covers both folders. Each item carries a `direction` (`in` for received,
`out` for sent), and the phone uploads them in batches of 100 items (the
backend accepts up to 200). MMS is not imported.

Both directions are history, and both are scoped to the authenticating
device only: an import never touches another device's history.

- **Inbound** (`direction='in'`, also the default, so an older client keeps
  working) is stored in `messages` with `source='import'`,
  `state='received'` and a deterministic `imp-<hash>` id.
- **Sent** (`direction='out'`) is stored in `sms_commands` with
  `source='import'` and `status='sent'`. It is therefore outbound history:
  it is never pending, never handed to a phone by `GET /commands`, and never
  forwarded to Telegram. Its `target` is the other side of the conversation
  (the recipient, as the provider reports it). The local composer in
  `ThreadActivity` reuses exactly this path after it writes the phone's own
  `content://sms` row, which is how a message typed on the phone reaches the
  Mini App without becoming a command.

Neither direction enters the Telegram delivery queue, neither is resumed, and
neither is queued for the phone; the only effect is that old conversations
become visible in that device's Mini App threads, on both sides.

Dedupe is server-side, per device: an item is skipped when the same
conversation already has the same text within ±60 s, and a sent item that
matches an existing `sms_commands` row (whatever its source) is treated as
the same message. An imported sent row is terminal history, so it never
authorizes a fallback and never becomes a status message.

The first import after upgrading to APK 1.3.2 re-reads the provider from the
start, because the old inbox-only cursor cannot say which sent rows were
already seen. That replay is safe: server-side dedupe skips the inbound rows
the backend already has, and the only cost is one longer import run.

### 5. Send from Telegram

A `/sms` command or a reply to a notification creates an `sms_commands`
row bound to one device:

- A reply goes to the device that received the original message. The
  notification is still bound to its `messages` row through
  `telegram_msg_id`, so replying to it works exactly as it did when the chat
  carried the whole body — but only while the notification is still in the
  chat.
  Once the reaper deletes it, there is no message left to reply to and the
  admin sends `/sms` or uses the Mini App instead.
- If that device is revoked or archived, the bot refuses instead of guessing.
- A plain `/sms` goes to the configured `default_device_id`; when no default
  is set but exactly one device is active, that device is used. With several
  active devices and no default, the bot asks the admin to pick a default in
  the Mini App (there is no broadcast).

The phone polls `GET /commands` every few seconds and receives only its own
commands, writes a local guard row, sends through its SIM, and reports the
result. The backend notifies the Telegram chat for `sent`, `failed`, and
`rejected`; `delivered` is silent. Each of those notes is recorded in
`tg_messages` and deleted again after an hour by the `NotificationReaper`.

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

### 8. Call-log sync

1. `CallLogWorker` walks `content://call_log/calls` forwards with an `_ID`
   cursor (`SettingsStore.callLogCursor`), oldest first, in pages of 100.
2. It also re-reads the last **7 days** by `DATE`, because the provider
   rewrites `DURATION`, `TYPE` and `IS_READ` when a call ends or the user
   opens the call log. That is why a re-sync can update a row instead of
   inserting a new one.
3. Each page goes to `POST /calls/sync` as 1–200 items. The backend ids a row
   deterministically: `call-<sha256(device_id, "pid:"+provider_id)>`, or the
   number and `started_at` when there is no provider id. A replayed batch is
   therefore a no-op.
4. the body is validated **one item at a time**. An unknown `kind`, an
   impossible `started_at` or an overlong name is counted in `skipped` and the
   rest of the page is stored — one bad row must never cost the phone the
   whole batch.
5. The answer is `{inserted, updated, unchanged, skipped}`. A row is updated
   only when its `content_hash` (or its stored fields) changed, and
   `is_read` never moves backwards: a Mini App read must not be undone by the
   phone's next sync.
6. One changed request bumps `rev` once, scoped to the affected thread when
   exactly one conversation changed and to the device otherwise.
7. A permanent 4xx rejection moves the cursor past the row and marks it
   failed; `429`, `5xx`, a network error or a `401` stops the run **without**
   moving the cursor, so the next run repeats it. The periodic safety net runs
   every 15 minutes.

### 9. Live call events

1. `CallService` is the app's `InCallService`. While the app holds
   `ROLE_DIALER` Android binds it; it registers a `Call.Callback` on the
   primary call.
2. Every Telecom transition is reported to `POST /calls/events` with the
   `call_ref`, `state`, `direction`, and optionally the number, SIM label,
   mute/speaker flags and timestamps. Events are posted on one serialised
   thread, so `ringing` cannot overtake `active`.
3. `db.call_session_set` merges an event of the same `call_ref` (an omitted
   field keeps the stored value, so a mute update does not erase the caller)
   and replaces the row for a new `call_ref`. `disconnected` stamps
   `ended_at` when the phone did not send one.
4. The state change is committed **before** any Telegram call. The mirror is
   bounded (2 s) and best-effort: a slow Telegram is a lost notification, not
   a failed state report, and the phone gets its `200` either way.
5. Two transitions are mirrored: the first `ringing` event of an *incoming*
   call, and the `disconnected` event of an incoming call nobody answered. A
   replayed `ringing` of the same `call_ref` is deduplicated — one ring, one
   message.

### 10. Remote call actions

1. The admin taps a button in the Mini App live card (or a Telegram call
   message). The backend writes a `call_actions` row with `status='pending'`.
2. `dial` is the only action with an empty `call_ref`: it has no call yet, its
   payload is the destination, normalised by `phones.normalize_target`, and an
   emergency number is refused with `422 emergency_number`. Every other action
   names the live `call_ref`; a ref that is not the live call is `409
   stale_call`, never applied to the next call.
3. The phone polls `GET /calls/actions?wait=0..25`. `CallService` uses a 10 s
   long poll with a 1 s reconnect while a call is live and runs every action;
   `CommandWorker` asks with `wait=0` every 5 s when idle, executes only a
   `dial`, and skips the queue entirely while `LiveCallRegistry` says a call
   is up. Any other action claimed on the idle path is reported `rejected`
   (`no live call`), because there is no session that could run it (see known
   issue 16).
4. The claim and the `delivered` transition are one transaction, so the two
   pollers never race. The phone then writes the action id to
   `CallActionGuard` on disk *before* touching Telecom, executes it, and
   reports `done`/`failed`/`rejected` to `POST /calls/actions/{id}/result`.
5. A duplicate or late report answers `accepted: false` with the current
   status, so the phone stops retrying. An action of another device is a
   `404`, never a hint that it exists.

### 11. Telegram Answer / Decline / Call back

A ringing incoming call posts one message: `📞 Incoming call`, the device
name, the caller (contact name, else the number, else `Unknown number`) and
the UTC time, with `Answer` / `Decline` and an `Open calls` Mini App button.
A missed call posts `📞 Missed call` with `Call back` and `Open calls`.

The callback data is `call:answer:<device>`, `call:reject:<device>` or
`call:callback:<device>` — it names the **device**, never the call. The bot
resolves the live session when the button is tapped, so a message that sat in
the chat for an hour cannot answer the next call. `Answer`/`Decline` need a
session that is still actionable; `Call back` needs the ended session the
message was about, because the number comes from that session and never from
the message text. The tap becomes an action with `client_ref = "tg:<callback
id>"`, so Telegram delivering one tap twice creates one action. A stale tap
answers "That call has ended" and drops the buttons.

### 12. Mini App live card and calls tab

`#/d/<device>/calls` is the call history of one device, newest first, with a
two-half cursor (`before_ts` + `before_id`) and a search field. A `Chats |
Calls` tab sits on the device screen; `GET /app/api/devices` already carries
`call` (the live session shaped exactly like the live endpoint) and `missed`
(the unread missed count), so the device list shows the badge without an extra
request. The live card offers Answer, Decline, Hang up, Mute, Speaker and a
keypad; a call row opens the existing thread when `number_key` exists, with
`Call back` and a `Call via…` sheet. Any call change bumps `rev` device-wide,
so the existing `/app/api/events` long poll wakes the card.

## HTTP surface

Device routes require a per-device token issued by QR pairing:
`Authorization: Bearer sr_…`. Admin routes require
`Authorization: Bearer $ADMIN_TOKEN`; "either" accepts both. Any other
credential on a device route — including a token from a server reset or an
old shared-token APK — is answered with `401`.

| Method and path | Auth | Purpose |
|---|---|---|
| `POST /pair/start` | public, rate limited | begin pairing; returns pairing id, scan code, QR payload, expiry |
| `POST /pair/claim` | public (claim secret) | phone polls; `202 {"status":"pending"}` = waiting for the admin, `202 awaiting_phone_confirmation` = the approval is shown to the phone's user (with `device` and `approved_by`), `200` = the token, exactly once. `confirmed` absent = legacy path (token straight away), `null` = still polling, `true` = the user tapped Pair, `false` = cancelled |
| `POST /sms` | device | ingest one live SMS |
| `POST /import` | device | ingest historical SMS (received and sent) in batches for that device |
| `GET /sms-state?ids=...` | either | delivery states for Android's fallback sweep |
| `GET /messages` | admin | paged message history, optionally filtered by device |
| `GET /commands` | device | that device's pending outbound commands + heartbeat |
| `POST /commands/{id}/result` | device | report outbound command status |
| `POST /calls/sync` | device | upsert a page of that device's call log; `{inserted, updated, unchanged, skipped}` |
| `POST /calls/events` | device | report the state of the one live call |
| `GET /calls/actions?wait=0..25` | device | claim that device's pending call actions, at most 10; `wait` holds the request open |
| `POST /calls/actions/{id}/result` | device | report `done` / `failed` / `rejected` for one action |
| `POST /contacts` | device | replace the contact list of that device |
| `GET /contacts` | admin | read contacts |
| `GET /status` | either | pause flag, queue depth, counts, device status |
| `POST /admin/pause` | admin | pause delivery (new live messages are held as `received`) |
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
| `POST /app/api/pair/lookup` | resolve the six-character code shown on the phone to its live pairing; `404` unknown, `409` ambiguous |
| `POST /app/api/pair/approve` | approve the pairing shown on the phone |
| `POST /app/api/pair/cancel` | cancel a pending pairing |
| `GET /app/api/devices/{id}/chats` | the conversations of one device |
| `GET/POST /app/api/devices/{id}/chats/{thread}/messages` | thread history / queue a send on that device |
| `POST /app/api/devices/{id}/chats/{thread}/read` | move the read marker of one thread; sets `seen_at` and pulls that thread's notification deletions to now + 5 min |
| `GET /app/api/devices/{id}/contacts` | the contacts of one device |
| `GET /app/api/devices/{id}/calls?limit&before_ts&before_id&q` | the call log of one device, newest first, two-half cursor |
| `GET /app/api/devices/{id}/calls/live` | the current `call_sessions` row, or `{"call": null}` |
| `POST /app/api/devices/{id}/calls/actions` | `{call_ref, action, payload?, client_ref}`; `409 stale_call`, `422 emergency_number` |
| `POST /app/api/devices/{id}/calls/read` | `{until_ts}`; clears the missed badge, answers the remaining `missed` |
| `GET /app/api/status` | relay header state + device summaries (no conversations) |
| `GET /app/api/events` | the long poll; names the device and thread that changed |
| `GET/POST /app/api/chats…`, `GET /app/api/contacts` | removed routes; always `410` |

Both lists are paged with a cursor of two halves, so nothing is skipped when
two events share a second:

- `GET /app/api/devices/{id}/chats/{thread}/messages`
  `?before_ts=<ts>&before_id=<event id>` returns the next older page of the
  conversation. `before_ts` alone keeps working for older clients. `has_more`
  stays true until the real beginning of the thread.
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

The suite is 785 tests across 25 modules: **781 passed, 3 failed, 1 error**.
The four non-passing tests are the known pre-existing failures (two
contact-search tests, one chat-search test, and one shutdown-timing error):

- `tests/test_commands.py::test_search_by_name_suggests_contacts`
- `tests/test_commands.py::test_contacts_are_uploaded_and_searchable`
- `tests/test_db.py::test_chat_search_matches_name_number_and_body_and_escapes_wildcards`
- `tests/test_app_assembly.py::test_shutdown_is_prompt_when_updater_blocked_in_long_poll`

Run the service locally:

```bash
cd backend
.venv/bin/python -m app.main --env-file /path/to/env --host 127.0.0.1 --port 8090
```

`GET /health` returns `{"ok":true}`. The delivery worker and the
notification reaper start with the app whenever workers are enabled;
`RELAY_DISABLE_UPDATER=1` only skips the Telegram updater.

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
available, so the UI never lies about the default-SMS role. The manifest
also carries the MMS/WAP-push part of the role contract (`RECEIVE_MMS`,
`RECEIVE_WAP_PUSH`, and a `WAP_PUSH_DELIVER` receiver); the relay does not
implement MMS, and the receiver exists so Android and OEM role pickers list
the app as a messaging app.

The same role is what lets the launcher read the phone's conversations: with
the SMS role the provider returns rows, without it the list shows the prominent
SMS-role card and an empty store. Notification access alone is enough for relay
capture but not for the local Messages screens. Both roles are requested from
the visible cards on the two top-level screens (and again from the setup screen,
`ui/MainActivity.kt`, which the **Settings** item of the bottom bar reopens);
the "Role & SMS diagnostics" dialog reports which one is missing.

### Calls on the phone

The `calls/` package is its own stream and its own set of roles:

- `DialerActivity` is the handler of `ACTION_DIAL` and `tel:` links and the
  default-dialer candidate, so another app can still hand it a number. It has
  a dialpad, a SIM spinner, the `Call via…` sheet and an **Emergency**
  passthrough that goes straight to the platform with no SIM choice and no
  backend call. It works with no pairing and no network: that is a hard rule,
  not a nicety. It is **not** the launcher any more: `ShellActivity` is, and
  the Phone home (`DialerHomeActivity`) opens this dialpad from its FAB. The
  setup screen is the **Settings** item of the bottom bar of either home.
- `CallService` is the `InCallService` of a default dialer. Android binds it
  only while the app holds `ROLE_DIALER`; it reports events, long-polls
  actions and draws the in-call UI. It keeps one primary call and deliberately
  ignores call waiting.
- `CallActivity` is the full-screen in-call screen (`showWhenLocked`,
  `turnScreenOn`, `singleTask`): answer, decline, mute, speaker, hold, a DTMF
  keypad and hang up. `CallService` also raises a notification as a second
  path for when the platform refuses to start the activity.
- `CallExecutor` applies one action to the `Call`/Telecom objects;
  `CallActionGuard` writes the action id to a private preferences list
  *before* the radio is touched, so a redelivered action is refused rather
  than executed twice. `CallSession` holds the state contract and
  `LiveCallRegistry`, which tells `CommandWorker` a call owns the action
  queue.
- `CallLogReader` reads `content://call_log/calls`; `CallLogWorker` uploads it
  and keeps `SettingsStore.callLogCursor` and `callLogCursorFormat`, mirroring
  the import cursor. `CallLogEntity`/`CallLogDao`/`CallLogDatabase` are a
  durable upload queue: a call is written to Room before it is sent, and the
  row is never deleted by a migration, so a lost connection cannot lose a
  call.
- `CallDeepLinks` is the call-via catalog (phone, WhatsApp, Telegram, Signal,
  Viber, Skype, Bale, Rubika, FaceTime). An `https` link is always offered
  because a browser can open it; a custom scheme is offered only when the
  package manager says something handles it, because a dead button is worse
  than a missing one.

Permissions added for calls: `READ_CALL_LOG`, `WRITE_CALL_LOG`, `CALL_PHONE`
and `ANSWER_PHONE_CALLS`, all runtime permissions. The call log is a
permission group of its own and the setup screen's `Calls` card asks for it
explicitly; nothing is uploaded before it is granted.

Android **1.3.3 (versionCode 8)**, commit `149504e`, stops the dual-broadcast
duplicate: some vendor builds (MIUI among them) deliver both `SMS_DELIVER`
and `SMS_RECEIVED` to the default SMS app for one message, and both copies
were stored because the local dedupe deliberately keeps same-source repeats.
`SmsReceiver` now ignores `SMS_RECEIVED` while the app holds the default-SMS
role, so `SMS_DELIVER` is the only copy. The non-default case (notification
listener) and the cross-path dedupe are unchanged.

The 1.3.3 build was produced by GitHub Actions run `43960881846`, which
finished clean and published `sms-relay-debug.apk` as the rolling
`android-latest` asset (URL above).

Historical import keeps its own contract: the phone reads
`content://sms` (`TYPE IN (INBOX, SENT)`) with one `_ID` cursor, sends
batches of 100, and the backend stores received rows as `messages` and sent
rows as terminal `sms_commands` history. Neither direction is forwarded to
Telegram. See [Historical import](#4-historical-import).

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
  address and per pairing, and the number of open pairings is capped. An
  approval only makes the phone *ask*: the token is issued after the user on
  the phone confirmed the approver the backend showed (`confirmed: true`); a
  refusal cancels the pairing and issues nothing.
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
- **Call metadata is as sensitive as SMS.** The call log holds numbers, names
  and call times; a live session holds the number of the person calling right
  now. No number, name or duration is ever logged: the call code logs ids,
  states and error kinds only, and the same rule applies to the Android code
  (`CallLogReader`, `CallLogWorker`, `CallService`). Call rows are
  device-scoped exactly like messages: the device comes from the bearer token,
  never from the body.
- **Emergency numbers are never remote-controlled.** `110`, `112`, `115`,
  `125`, `911`, `999` and `000` are refused by the backend (`422
  emergency_number`), by the Telegram `Call back` button and by the Mini App,
  which hides `Call back` and `Call via…` for them. The comparison is on the
  digits with leading zeros stripped, so `0112` cannot slip through. An
  emergency call is placed on the phone, by a human, with the local dialer and
  no network.
- **A stale `call_ref` is refused.** An action names the phone-generated ref
  of the current call; when that call is over the backend answers `409
  stale_call` and the phone refuses a mismatched or expired ref as well. An
  action can therefore never land on the next call.
- **The Telegram callback data is capped at 64 bytes.** A device id long
  enough to break the cap produces a message with **no** buttons instead of a
  `reply_markup` Telegram would reject (a rejected keyboard fails the whole
  message). `call_ref` is deliberately not part of the callback data.
- The SQLite database contains full SMS bodies in plaintext. Protect the file
  and its backups accordingly. The notification carries a 160-character snippet
  of the body and the extracted code (when there is one) in addition to the
  device name, the sender and a Mini App button; the **full** body is never
  sent to Telegram and stays in SQLite, and the Mini App is where the
  message is read in full. Mini App requests and
  responses still carry the body inside the Telegram WebView, so Telegram's
  servers can see it there; the channel is server-mediated, not end-to-end
  encrypted.
- No SMS body, snippet, extracted code, token, claim secret, or pairing payload
  is ever logged.

## Local Messages and dialer UI

The app is a usable phone app on its own: the launcher is the phone's own
conversation list, and the Phone home is a Google-Dialer-style hub. Both read
the platform providers directly. The relay's Room queue is the upload queue;
it is never rendered as a conversation.

### Screen map

| Screen | Activity | Reads | Writes |
|---|---|---|---|
| Messages list (launcher) | `ui/ShellActivity.kt` | `content://mms-sms/conversations?simple=true`, `content://mms-sms/canonical-addresses`, an unread scan over `content://sms` | nothing |
| One conversation | `ui/ThreadActivity.kt` | `content://sms` for one `THREAD_ID`, 500 rows a page | marks the thread read; one sent row per accepted send |
| Phone home | `ui/DialerHomeActivity.kt` | `CallLog.Calls`, `ContactsContract`, `TelecomManager` (voicemail number) | deletes one call-log row with `WRITE_CALL_LOG` |
| Dialpad | `calls/DialerActivity.kt` | SIM list from `callCapablePhoneAccounts` | places a call only when the user presses the call button |
| Setup | `ui/MainActivity.kt` | `SettingsStore`, `RoleState` | pairing, permissions, roles, base URL |

`ShellActivity` is the **only** `ACTION_MAIN` + `LAUNCHER` activity. Its
extended FAB opens the system contact picker
(`ContactsContract.CommonDataKinds.Phone`) and goes straight to a thread for
the picked number. `ThreadActivity` is unexported and is reached from the list,
the picker or the platform's restore. `DialerHomeActivity` is unexported too
and is reached from the bottom bar; its dialpad FAB opens `DialerActivity`.

Both Messages screens show three honest states instead of one empty list: the
app does not hold the default-SMS role (a prominent card that requests it), the
relay is not paired (a banner that opens setup), and a genuinely empty list.
`SmsObserver` watches `content://sms` and the conversations URI, so an
incoming SMS appears without a manual refresh; a `SecurityException` or a null
cursor from the provider is reported as an empty read plus a failure flag, not
a crash.

### Bottom navigation

`ui/ShellActivity.kt` and `ui/DialerHomeActivity.kt` share one Material 3
bottom bar (`res/menu/menu_bottom_nav.xml`, included as a real row from
`res/layout/view_bottom_nav.xml`) with four items — **Messages**,
**Phone**, **Contacts**, **Settings** (`nav_messages`, `nav_phone`,
`nav_contacts`, `nav_settings`). Four, never five: `BottomNavigationView` keeps
the labels of all four at any width. Selecting an item starts the top-level
activity (`DialerHomeActivity` with `TAB_RECENTS` or `TAB_CONTACTS`, or
`MainActivity` for Settings) through `startTopLevel`, so a tap can never leave
two copies of the same page stacked; the bar then re-selects its own item
silently (`selectNavSilently`), because `selectedItemId` would fire the
listener again. `ThreadActivity`, `DialerActivity` and the in-call screen have
**no** bar: they are detail screens reached from a list, and a tab bar there
would imply a top level that does not exist.

### Role cards, diagnostics and unknown senders

- **The SMS role is a prominent card, not a footnote.** While the app is not
  the default SMS app the conversation list is empty by design, so
  `ShellActivity.renderBanners()` draws the role card with
  `MessagesBanner.prominentCard` and the full role flow in `requestSmsRole()`:
  the `RoleManager.ROLE_SMS` intent on API 29+, `ACTION_CHANGE_DEFAULT` with
  `EXTRA_PACKAGE_NAME` on API 26–28, and `ACTION_MANAGE_DEFAULT_APPS_SETTINGS`
  as the fallback when the phone offers neither. The result code is never
  trusted — `handleSmsRoleResult()` re-reads `RoleState.isDefaultSmsApp()`,
  because a vendor build can answer OK and change nothing.
- **The dialer role is requested first, the permissions after it.** The Phone
  home shows a visible set-as-default-dialer card ("Make SMS Relay your
  dialer"; `renderRoleCard()`, with a different sentence when the phone offers
  no dialer role at all).
  `requestDialerRole()` launches `RoleState.dialerRoleIntent()`, and only
  afterwards — or immediately when the intent could not be shown — does it
  fall back to the default-apps screen and `askRolePermissions()`, which asks
  for the missing `CALL_PHONE`, `ANSWER_PHONE_CALLS` and `READ_CALL_LOG`
  permissions together. The permissions are deliberately **not** a gate on the
  role dialog: a single "no" used to hide the dialog for good, and the dialpad
  works without them anyway.
- **"Role & SMS diagnostics" is the first thing to check.** A long press on
  the Messages role card, or the `menuDiagnostics` menu item, opens
  `RoleState.diagnosticsReport()` in a scrollable, selectable dialog: Android
  SDK, whether the SMS and dialer roles are offered and held, the current
  default SMS and dialer packages, this app's package, and whether
  `READ_SMS`, `READ_CALL_LOG` and `READ_CONTACTS` are granted. It is built
  from the same helpers the screens use, so it cannot disagree with the card
  next to it, and it holds settings and
  permissions only — never a number, a name or a message.
- **A conversation whose address cannot be resolved still opens.**
  `SmsRepository.conversations()` falls back to the `ADDRESS` of the thread's
  first `content://sms` row when `canonical-addresses` returns nothing for a
  one-to-one thread (at most `ADDRESS_FALLBACK_LIMIT = 300` such reads per
  list). When that also fails, the row is kept with its real `thread_id` and
  `ThreadLabels` shows **"Unknown"**; the thread itself still opens. The old
  behaviour dropped the thread from the list, which hid messages that were
  perfectly readable.

### Providers

| Purpose | URI | Notes |
|---|---|---|
| Conversation list | `content://mms-sms/conversations?simple=true` | projection `_id, date, recipient_ids, snippet`, sort `date DESC` |
| Recipient resolution | `content://mms-sms/canonical-addresses` | `recipient_ids` is a space-separated list; every id is resolved and cached per call |
| Thread messages | `content://sms` | selection `thread_id = ?`, newest first, `MESSAGE_LIMIT = 500` per page |
| Unread counts | `content://sms` | `read = 0 AND type = INBOX`, `_id DESC LIMIT 2000`, so a phone that never marked anything read cannot stall the list |
| Sent insert | `content://sms` | `ADDRESS`, `BODY`, `DATE`, `DATE_SENT`, `TYPE=SENT`, `READ=1`, `SEEN=1`, `SUBSCRIPTION_ID` when known |

Names come from the relay's local `ContactsRepository` map, cached for 60 s
and only while the contacts permission is held. Every id in `recipient_ids` is
resolved: that is what makes a group a group instead of a private chat with
whoever the provider listed first.

A thread page is the newest 500 messages read `DATE DESC` and then reversed,
plus one extra row as proof that older messages exist ("Load older messages").
The older page keys on date **and** `_id`, because two messages of one thread
can share a second and a date-only key would drop one of them.

There is no visual verification of any of this on the host that wrote it: the
VPS has no JDK, Gradle or emulator. The `android-build` workflow proves the
code compiles; the layout, the insets and the provider behaviour on a vendor
build still need a real phone.

### A local send, and its mirror

1. The composer hands the text to `SmsSender.send(...)` with no explicit
   subscription id, so the phone's default or user-chosen SIM is used exactly
   as the fallback does. Nothing is written before the radio accepts the
   message.
2. On `Sent`, `SmsRepository.insertSent(...)` writes one `content://sms` row
   with the values in the table above and returns its `_id` and `THREAD_ID`.
   The provider assigns the thread from the address, so the conversation
   appears in the list by itself.
3. The thread is reloaded, so the outbound bubble is real store content.
4. Best effort, after the local row exists: `RelayClient.importBatch(listOf(
   ImportMessage(..., direction="out")))` posts to `/import`. The backend
   stores an outbound import as an `sms_commands` row with `source='import'`,
   `status='sent'`, so it is history: never pending, never handed to a phone,
   never forwarded to Telegram. Its only effect is that the Mini App shows the
   outbound bubble.
5. A failed mirror never undoes the local SMS and is only logged by category
   (network, refused). A dropped mirror is missing history, never a lost
   message; a durable outbound queue is a follow-up, not part of this phase.
6. If the provider refuses the insert after the radio accepted the message
   (`SecurityException`, a null URI), the screen says **"Sent, but this phone
   did not store it"** and the composer is still cleared. It never draws a
   bubble that does not exist.

The local composer is deliberately **not** gated by
`SettingsStore.sendingEnabled`. That switch exists so a remote command
(Telegram, the Mini App) cannot make the phone send without its owner present;
a person typing on this screen and tapping Send is that consent. The
permission and the radio are the only gates.

### Groups, MMS and RCS

| Case | Behaviour |
|---|---|
| Group thread (two or more recipients) | labelled from first names, at most two, then `+n`; `Group (n)` when no member is a known contact. Never one member's number, and the header hides the call button. Composer replaced by "Group sending is not supported yet." |
| MMS-only thread | `content://sms` has no rows for it, so the screen shows the provider snippet as a placeholder and says MMS cannot be displayed yet. No composer. |
| Mixed thread | the SMS rows are shown normally, plus the note "Some messages in this conversation are MMS". |
| Unresolved canonical address | the thread is copied from its first SMS `ADDRESS`; when even that fails it is listed as "Unknown" with its real `thread_id` and still opens. The thread screen shows its own note ("no number to send to") and hides the composer, and it is not blamed on MMS. |
| RCS | impossible: Google's private backend, no public API. |

### Roles and launchers

- `ShellActivity` is the launcher. It needs neither `ROLE_DIALER` nor a
  pairing: an unpaired phone still reads its own SMS store.
- Both top-level screens request their role from a visible card, never from a
  hidden menu item: the Messages list shows the SMS-role card and the Phone
  home shows the dialer-role card. A phone whose OEM suppresses the role
  dialog fails visibly, and the "Role & SMS diagnostics" dialog says which
  piece is missing (see above).
- `DialerActivity` is no longer the launcher. It keeps `ACTION_DIAL` and
  `tel:` and stays the default-dialer candidate next to `CallService`; the
  setup screen is reached from the bottom bar of both homes.
- `DialerHomeActivity` never places a call. A row opens `ACTION_DIAL` with the
  number in the field; the user presses the call button on the dialpad. That
  is the rule that keeps an emergency number in a log row from starting a call
  from one touch. Recents read `CallLog.Calls` (200 newest), Contacts and
  Favorites read `ContactsContract`, and Voicemail is one honest sentence with
  a `TelecomManager` carrier-voicemail number: a call placeholder, not visual
  voicemail.
- A refused permission is a state of its own in every tab, with a note that
  requests it; a provider failure is another. A private caller with no number
  gets one toast instead of an empty dialpad.
- `CallService`, `CallActivity` and the `ROLE_DIALER` request are unchanged.

The Phone home and the Messages screens use the Material 3 theme
`Theme.SmsRelay.Messages` (day/night, dynamic colour where the platform offers
it) with original assets. Google icons, wordmarks, animations and code are not
used; the visual blueprint is [`docs/UI_DESIGN.md`](docs/UI_DESIGN.md).

## Calls, MMS and RCS boundaries

The app is a working local Messages client, call log and dialer over the
platform's own telephony and provider stores; it is not a reimplementation of
Google's messaging stack. What is deliberately out of reach:

| Boundary | Reality |
|---|---|
| **RCS** | Impossible. RCS runs on Google's private Jibe backend and has no public API; the app sends and reads SMS only. |
| **MMS** | Not implemented. `MmsReceiver` acknowledges a WAP push and ignores it; the manifest keeps the MMS part of the default-SMS role only so Android and OEM role pickers list the app as a messaging app. |
| **Group threads** | Read only. A thread with several `recipient_ids` is labelled from first names or `Group (n)`, never from one member's number, and this phase cannot send to it. `content://sms` has no group MMS conversation at all, so an MMS thread is shown as a provider snippet at best, never as a decoded conversation. |
| **Google spam, call screening, visual voicemail** | Proprietary services with no public API. The dialer shows an explicit placeholder instead of promising them. |
| **Google Messages/Dialer visual identity** | The UI direction copies **layout and flow only**, with original assets. Google icons, wordmarks, animations and code are not copied and must not be; the visual blueprint is in [`docs/UI_DESIGN.md`](docs/UI_DESIGN.md). |
| **The in-call UI needs `ROLE_DIALER`** | `CallService` is an `InCallService`; Android only binds it while the app is the default dialer. `MainActivity` requests the role and `RoleState` reports whether it was actually granted. Until then the dialpad still places calls and the platform draws the in-call screen. |
| **No on-device verification here** | There is no emulator, JDK or Gradle on the VPS. Telephony behaviour — dual SIM, emergency dialling, ringing over the lock screen, OEM role pickers — needs a real phone and a manual matrix. |

Audio never leaves the phone. The relay has no `CAPTURE_AUDIO_OUTPUT`
permission and no path to one: the Mini App and the bot send controls, never
carry a conversation.

## Known code-level issues

These are confirmed from the implementation and should be considered before
relying on the affected behavior.

1. **Old full-text messages stay in the chat.** Messages forwarded before the
   notification change are still complete SMS bodies in Telegram, and the
   notification itself now carries a 160-character snippet plus the extracted
   code. The reaper only tracks notifications it sent itself
   (`telegram_sent_at`), so it never touches the old messages, and Telegram
   refuses to delete messages older than 48 hours anyway. Deleting those
   messages is a manual chat action.
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
8. **Notification deletion depends on the bot's rights.** `deleteMessage`
   only works for messages the bot itself posted, in a chat where it is
   allowed to delete, and never older than 48 hours. A rejected delete is
   rescheduled every 300 s, so a notification whose deletion is impossible
   keeps one indexed row due forever instead of disappearing silently; a
   message the admin already deleted by hand is answered "not found" and is
   treated as success. The same rules apply to the `tg_messages` notes, which
   additionally treat a permanent `bad_request` as done instead of retrying
   it forever.
9. **Eight ambiguous failures become `failed`.** After the attempt limit, a
   timeout/network error becomes terminal, which can authorize a fallback
   even though Telegram may have accepted the notification.
10. **FastAPI's default `/docs` and `/openapi.json` are enabled.** They are
    reachable through `/relay/docs` and `/relay/openapi.json` without auth
    unless disabled at the app or proxy level.
11. **The Telegram bot's contact search can still use the legacy list.**
    `flow.py` falls back to the global `contacts` table when the resolved
    device has no matching contact. The Mini App is strictly per-device;
    the bot's name picker can mix legacy names until it is made
    device-aware.
12. **Duplicate rows from the pre-fix capture and the bulk re-import remain.**
    One live pair came from the vendor dual `SMS_DELIVER`/`SMS_RECEIVED`
    broadcast (fixed in Android 1.3.3, commit `149504e`), and the bulk
    re-import left 1,039 duplicate groups (5,039 rows) that were never
    forwarded to Telegram. The Android guard stops new duplicates; the
    existing rows were **not** cleaned up.
13. **Call waiting is out of scope.** The relay tracks one primary call per
    device. A second call is left to the network: the app neither answers nor
    rejects it, which is what keeps `call_ref` unambiguous. Conference and
    merge are not implemented either.
14. **A call row is written when the call ends.** `CallLogWorker` has a 15 min
    periodic net and a 7-day `DATE` re-read for `DURATION`/`TYPE`/`IS_READ`,
    so a call that is still up appears in the history only after it ends. The
    live call is the session, not a `call_log` row.
15. **`dial` has no `call_ref`.** It is the only action with an empty ref, so
    an idempotency retry after a timeout must be recognised by `client_ref`.
    A `dial` whose payload is not a destination is reported `rejected`
    (`invalid destination`); a Telecom refusal is `failed` (`the phone
    refused`). Neither touches the radio twice.
16. **A non-`dial` action claimed while the phone is idle is rejected, not
    run.** `CommandWorker.runIdleDialActions()` executes only `dial`; any other
    action it claims from `GET /calls/actions` has no live call to act on, so
    it reports `rejected` with `no live call` and the row reaches a terminal
    state. This is a race between a Mini App tap and a call that ended in the
    same second: the tap then does nothing, but the action ends `rejected`
    with a reason instead of hanging in `delivered`.
    **Residual limitation:** that report is not retried. If it fails on the
    network, `ok` goes false and the poll chain retries, but the row is
    already `delivered` and will not be handed out again, so the action stays
    `delivered` for ever and the Mini App never sees a result. Treat a stuck
    `delivered` action as a lost report before hunting a bug elsewhere.
17. **A sent message is mirrored best effort.** The local composer writes the
    row to `content://sms` first and only then posts it to `/import` as
    `direction='out'`. A mirror that fails (unreachable server, a 401, a
    rejected batch) is logged by category and otherwise dropped: the message
    is on the phone and in the Mini App only if that POST succeeded. There is
    no durable outbound queue yet, so a send made while the relay is down is
    missing from the Mini App for ever; the message itself is not lost.
18. **A provider insert can leave a hole in the thread.** If the radio accepts
    the message and the provider then refuses the insert (the app lost the
    SMS role, a vendor provider error), the screen says "Sent, but this phone
    did not store it" and does not mirror anything. The thread shows no
    outbound bubble even though the SMS went out.
19. **The snippet and the code leave the relay over Telegram.** This is a
    deliberate product decision, not an oversight: the private admin chat
    receives up to 160 characters of every new SMS plus the extracted code.
    Do not go back to a notification with no SMS text, and do not widen it
    either — the full body must stay in SQLite and the Mini App. The chat is
    server-mediated, never end-to-end encrypted.
20. **The OTP extraction is a heuristic.** `extract_code()` is deliberately
    conservative: it skips masked tails (`*1234`, `x1234`, `•1234`),
    identifier labels (`card`, `account`, `order`, `ref`, `txn`, `invoice`,
    `bill`, `policy`, `ticket`), dates, amounts and numbers inside phone
    numbers. A code-shaped card or order number must **not** be labelled a
    code, and an unusual OTP format (spaced digits, a code inside a URL, a
    non-Latin script) can be missed: the snippet still shows it, so the code
    line is a convenience, never the contract. Do not loosen the filters to
    "find more codes" — a wrong `Code:` line is worse than none.
21. **The phone confirmation needs a current backend.** A new APK sends
    `confirmed` and waits for `202 awaiting_phone_confirmation`; an older
    backend answers with the token directly on the first approved poll, so the
    dialog never appears and the token is stored without the phone's user
    agreeing. The field being absent is the older-APK path and keeps working
    on purpose — do not make it an error, and do not claim the confirmation is
    enforced end-to-end.
22. **An OEM can suppress the role dialogs.** Some vendor builds ignore the
    `ROLE_SMS` / `ROLE_DIALER` request or answer OK and change nothing, but
    the cards make that visible instead of silent. Check "Role & SMS
    diagnostics" first: it reports the SDK, whether each role is offered and
    held, the current default SMS and dialer packages, and the relevant
    permission grants. When no role is offered at all, the app falls back to
    the default-apps settings and says so.
23. **"Unknown sender" rows still open.** When `canonical-addresses` returns
    nothing and the thread's first SMS row has no `ADDRESS` either, the row is
    kept with its real `thread_id` and labelled from
    `messages_unknown_sender` ("Unknown"). The messages are readable; only the
    name and the number are missing. Do not filter such a thread out of the
    list to "tidy" it — that hides real messages.

## Testing

The backend suite is the primary automated safety net: **785 collected — 781
passed, 3 failed, 1 error** across 25 modules. The four non-passing tests are
the known pre-existing failures listed below and are unchanged by the calls
and messages-UI work. It uses fakes for Telegram, so no real message is sent.
The Android app has no instrumentation tests, and the Mini App has only
Python-side static assertions.

Run:

```bash
cd backend
.venv/bin/python -m pytest tests -q
```

For any change, run at least the directly affected test module; run the full
suite before deployment. The calls modules have their own:
`tests/test_calls.py` (sync, live session, freshness, Mini App routes) and
`tests/test_call_actions.py` (action creation, TTL, claim, result, expiry).

The Android app is **not built on the VPS**: there is no JDK, Gradle or
emulator there. Every Android change must be built by the `android-build`
GitHub Actions workflow, which must finish clean and publish
`sms-relay-debug.apk` on the rolling `android-latest` release:

<https://github.com/HessiKz/sms-relay/releases/download/android-latest/sms-relay-debug.apk>

Do not hand out an Actions artifact URL and do not commit an APK. A dialer
change additionally needs manual on-device testing (stock Android, a Samsung
and a Xiaomi build, dual SIM, no SIM, an emergency number, and an incoming
call while the phone is locked). The Messages and Phone-home screens, the
bottom bar and the role/diagnostics flow are compiled by the `android-build`
workflow only: they have never run on a device or an emulator here, so their
behaviour is unverified. A manual device matrix is still required for the
dialer role prompt, the SMS role flow, calls over the lock screen, dual-SIM
sends, emergency dialling, provider and vendor quirks (`content://sms` insert
permissions, contacts visibility) and notification behaviour.

## Documentation and agent rules

- [AGENTS.md](AGENTS.md) — binding rules for every coding agent. The most
  important rule: **append an entry to `docs/AGENT_LOG.md` before finishing any
  task.**
- [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) — module-by-module implementation
  map, invariants, change recipes, testing, and known traps.
- [docs/CALLS_DESIGN.md](docs/CALLS_DESIGN.md) — the calls contract: tables,
  TTLs, the device and Mini App routes, the Telegram callbacks, and the
  Android package.
- [docs/UI_DESIGN.md](docs/UI_DESIGN.md) — the local SMS-client and dialer UI
  direction: information architecture, provider data plan, phases and risks.
- [docs/AGENT_LOG.md](docs/AGENT_LOG.md) — append-only history of agent work.

If code and documentation disagree, inspect the code and update this document
as part of the change. Documentation drift is a bug.
