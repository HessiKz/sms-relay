# Calls: design contract (dialer replacement)

Status: **approved direction, in progress**. The operator chose:

- the phone is the **daily driver**;
- the app **replaces the system dialer** (`ROLE_DIALER`, `InCallService`);
- the **Mini App is the primary surface**, Telegram mirrors it;
- **no remote audio**: the Mini App controls a call, it never carries it.

This file is the contract between the backend, the Android app and the Mini
App. Code is the source of truth once written; update this file with the code.

## Scope

In scope: call log (history, missed, duration, SIM), contacts for callers,
answer/reject/hang up, mute, speaker, hold, DTMF, dial (SIM + call-via deep
links), live call state in the Mini App, Telegram notifications with inline
actions, per-device isolation, call log in the conversation thread.

Out of scope (v1): conference/merge, call waiting beyond the primary call,
call recording, voicemail playback, remote audio, VoIP/SIP.

## Hard rules

1. **The dialer works offline.** Placing an emergency call must never depend on
   the backend, the network or a pairing. If the relay is down, the phone still
   dials.
2. **Emergency numbers are never remote-controlled.** The backend and the phone
   refuse to create or execute a remote `dial` action for 110, 112, 115, 125,
   911, 999, 000 and friends. Emergency calls are placed by the local UI only.
3. **No remote audio.** Never request `CAPTURE_AUDIO_OUTPUT`; never promise
   listening or talking.
4. **Device isolation.** Every call row and action carries `device_id`; the
   device comes from the bearer token, never from the body.
5. **No numbers, names or durations in logs.** Log ids and state only.
6. **At most once.** A call action guard row is written before the Telecom call
   is touched; a stale `call_ref` is refused, never applied to the next call.
7. **Additive migrations.** New tables only; never drop the message queue.
8. **The SMS path keeps working.** Calls are a fourth stream next to messages,
   commands and contacts.

## Canonical keys

- `number_key = phones.thread_key(number)` — the same `989…` / `short:…` /
  `raw:…` key used by SMS, so calls and SMS share one conversation.
- A private/withheld number stores `number = ""`, `number_key = NULL`; it is
  shown but never merged into a thread.
- `call_ref` is a phone-generated UUID per call; it is valid until disconnect.
  It is the only thing an action may target.

## Database (additive)

```sql
call_log(
  id TEXT PRIMARY KEY,              -- call-<sha256(device_id, provider_id|digest)>
  device_id TEXT NOT NULL,
  provider_id TEXT,
  number TEXT NOT NULL DEFAULT '',
  number_key TEXT,
  cached_name TEXT,
  kind TEXT NOT NULL,               -- incoming|outgoing|missed|rejected|blocked|voicemail|unknown
  raw_type INTEGER,
  started_at INTEGER NOT NULL,
  duration_s INTEGER NOT NULL DEFAULT 0,
  sub_id INTEGER NOT NULL DEFAULT 0,
  phone_account TEXT,
  is_read INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT,
  present INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
-- index (device_id, started_at DESC, id DESC), (device_id, number_key, started_at DESC)

call_sessions(
  device_id TEXT PRIMARY KEY,       -- one primary call per device
  call_ref TEXT NOT NULL,
  number TEXT NOT NULL DEFAULT '',
  number_key TEXT,
  direction TEXT NOT NULL,          -- incoming|outgoing
  state TEXT NOT NULL,              -- ringing|dialing|active|held|disconnected
  started_at INTEGER NOT NULL,
  answered_at INTEGER,
  ended_at INTEGER,
  muted INTEGER NOT NULL DEFAULT 0,
  speaker INTEGER NOT NULL DEFAULT 0,
  sim_label TEXT,
  updated_at INTEGER NOT NULL
);

call_actions(
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  call_ref TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,             -- answer|reject|hangup|mute|unmute|speaker_on|speaker_off|hold|unhold|dtmf|dial
  payload TEXT,
  status TEXT NOT NULL,             -- pending|delivered|done|failed|expired|rejected
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  client_ref TEXT,
  result TEXT,
  finished_at INTEGER
);
-- unique (client_ref) where not null; index (device_id, status, created_at)
```

TTL: `answer`/`reject`/`hangup` 20 s; everything else 300 s. `dial` 300 s.
An expired action is `expired` and never delivered.

## Device API (bearer token)

| Route | Body | Answer |
|---|---|---|
| `POST /calls/sync` | `{items:[…]}` 1–200 | `{inserted, updated, unchanged, skipped}` |
| `GET /calls/actions?wait=0..25` | — | `{actions:[…]}`, at most 10 |
| `POST /calls/actions/{id}/result` | `{status, detail}` | `{accepted, status}` |
| `POST /calls/events` | `{call_ref, state, direction, number?, sim_label?, muted?, speaker?, started_at?, ended_at?}` | `{ok:true}` |

`call_sync` item: `{provider_id, number, cached_name, kind, raw_type,
started_at, duration_s, sub_id, phone_account, is_read, content_hash}`.

Sync semantics: deterministic id, upsert when `content_hash` differs, bump rev
once per request with scope `(device_id, number_key)` only when a row changed.

`GET /calls/actions` marks returned rows `delivered` in the same transaction,
so two consumers never run one action.

## Mini App API (initData)

| Route | Purpose |
|---|---|
| `GET /app/api/devices/{id}/calls?limit&before_ts&before_id&q` | history, two-half cursor |
| `GET /app/api/devices/{id}/calls/live` | the current `call_sessions` row |
| `POST /app/api/devices/{id}/calls/actions` | `{call_ref, action, payload?, client_ref}` → action |
| `POST /app/api/devices/{id}/calls/read` | `{until_ts}` → unread badge |

`_device_summary` gains `call` (live session or null) and `missed` (unread
missed count). Any call change bumps `rev.bump_rev(app, device_id, None)`, so
the existing `/app/api/events` long poll wakes the Mini App.

## Telegram

- Ringing: one message to `TELEGRAM_CHAT_ID` with the device, `display`/name and
  inline buttons `Answer` / `Decline` (`callback_data` `call:answer:<device>:<call_ref>`).
  Editing/deleting the message after the action is optional.
- Missed: `Missed call from X on <device>` with `Call back` and `Open calls`.
- `Call back` creates a `dial` action (never for an emergency number).
- The bot never broadcasts to another device: the callback names the device.

## Android

New package `dev.smsrelay.calls`:

| File | Responsibility |
|---|---|
| `CallLogEntity.kt`, `CallLogDao.kt`, `CallLogDatabase.kt` | Room queue, upload state |
| `CallLogReader.kt` | `content://call_log/calls` cursor, type mapping |
| `CallLogWorker.kt` | cursor sync + rolling recent window, upload |
| `CallService.kt` | `InCallService`: lifecycle, events, action poll, controls |
| `CallActivity.kt` + layouts | incoming/active UI (answer, decline, mute, speaker, keypad, hangup) |
| `DialerActivity.kt` | `ACTION_DIAL` handler, dialpad, SIM choice, call-via sheet |
| `CallDeepLinks.kt` | capability catalog (`tel`, WhatsApp, Telegram, Signal, Viber, Bale, Rubika, FaceTime) |
| `CallActions.kt` | guard row, TTL/ref checks, report |

Manifest: `ACTION_DIAL` activity with `CATEGORY_DEFAULT`; `InCallService` with
`BIND_INCALL_SERVICE`, `android.telecom.InCallService`, metadata
`IN_CALL_SERVICE_UI=true`, `IN_CALL_SERVICE_RINGING=true`; `CallActivity`
full-screen, `showWhenLocked`, `turnScreenOn`. Permissions: `READ_CALL_LOG`,
`WRITE_CALL_LOG`, `CALL_PHONE`, `ANSWER_PHONE_CALLS`, `READ_PHONE_STATE`
(already), `READ_CONTACTS` (already).

Action fetch: `CommandWorker` (5 s) calls `GET /calls/actions?wait=0`; while a
call is live, `CallService` runs `GET /calls/actions?wait=15` with a 1 s
reconnect. One atomic `delivered` transition means the two never race.

Call log sync: `_ID` ASC cursor in `SettingsStore.callLogCursor`, batch 100,
plus a rolling 7-day `DATE` re-read for duration/type updates; `_ID` format
version in `SettingsStore.callLogCursorFormat` (mirror the import cursor).

## Mini App

- Route `#/d/<device>/calls`; a `Chats | Calls` tab on the device screen.
- Live call card at the top of the device and calls screens: Answer, Decline,
  Hang up, Mute, Speaker, Keypad.
- A call row opens the existing thread when `number_key` exists, with a
  "Call back" action and a "Call via…" sheet.
- Thread timeline gains `call:<id>` events next to `msg:` and `cmd:`.
- `#/d/<device>/calls` shows missed/incoming/outgoing, duration, SIM, name.
- All links are `https` where possible (`wa.me`, `t.me`, `signal.me`); custom
  schemes are best-effort and never promised.

## Verification

- Backend: `cd backend && /tmp/opencode/venv311/bin/python -m pytest
  tests/test_calls.py tests/test_call_actions.py tests/test_device_isolation.py -q`.
- Full suite before handoff; the four known pre-existing failures stay.
- Android: built only by the `android-build` GitHub Actions workflow. A local
  Gradle build is not available on this host. Manual telephony matrix is
  required before release (stock Android, Samsung, Xiaomi, dual-SIM, no SIM,
  emergency number, incoming while locked).
- Mini App: Python static assertions plus API tests; no JS test runner.
