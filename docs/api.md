# REST API reference

Base URL: `https://ai-barrow.ir/relay` (Caddy strips `/relay` before the
request reaches the app). The backend itself listens on `127.0.0.1:8090`.

Authentication:

- `Authorization: Bearer <PHONE_A_TOKEN>` — device token (ingest and import)
- `Authorization: Bearer <ADMIN_TOKEN>` — admin token (everything else)

Tokens are compared in constant time. After 10 failed attempts with the
same token within 60 seconds the client receives `429`.

Error bodies never contain message content.

## GET /health

Liveness probe. No authentication.

```json
{"ok": true}
```

## POST /sms

Ingest one received SMS. **Device token required.**

```json
{
  "id": "3f1c9a0e-6c1a-4a54-9a4a-1f0d2b3c4d5e",
  "sender": "+15551234567",
  "body": "Your code is 123456",
  "received_at": 1767225600,
  "parts": 1,
  "part_index": 0,
  "thread_id": "optional-conversation-id"
}
```

| Field | Rules |
|---|---|
| `id` | required, 1-64 chars, `[A-Za-z0-9._-]`. Must be unique — it is the idempotency key. |
| `sender` | required, 1-64 chars |
| `body` | required, 1-16000 chars |
| `received_at` | required, epoch seconds (int or numeric string), within ±24 h of now |
| `parts` | optional, ≥ 1 (total parts of a multipart message) |
| `part_index` | optional, ≥ 0 |
| `thread_id` | optional, ≤ 64 chars |
| `source` | optional, ≤ 16 chars, default `sms` (`sms` or `notification`). Same-text captures from different sources within 120 s are treated as one message. |

Responses:

| Status | Body | When |
|---|---|---|
| 200 | `{"accepted": true, "duplicate": false, "state": "pending"}` | stored and queued |
| 200 | `{"accepted": true, "duplicate": true, "state": "delivered"}` | same `id` seen before — nothing is sent again |
| 200 | `{"accepted": true, "duplicate": false, "state": "received", "paused": true}` | forwarding is paused; stored, not queued |
| 200 | `{"accepted": false, "reason": "blocked"}` | sender/body matched a filter — nothing is stored |
| 401 | `{"detail": "unauthorized"}` | missing/invalid token |
| 413 | `{"detail": "payload too large"}` | body over 512 KiB |
| 422 | `{"detail": {"error": "validation_error", "fields": [...]}}` | invalid fields (no values echoed) |

Delivery states: `received` (stored, not queued), `pending` (queued),
`delivered` (Telegram confirmed), `failed` (the retry budget of 8 attempts
is exhausted or Telegram rejected the message outright), `uncertain`
(timeout/ambiguous — the outcome is unknown and a retry may follow).

## POST /import

Bulk-import historical SMS from the phone's inbox. **Device token required.**
Opt-in: the app only calls this after the user confirms. Imported rows are
stored with `state: "received"` and `source: "import"` — they are visible in
the Mini App but are **never forwarded to Telegram**.

```json
{
  "items": [
    {
      "provider_id": 4312,
      "sender": "+15551234567",
      "body": "Old message",
      "received_at": 1609459200,
      "sub_id": 1
    }
  ]
}
```

| Field | Rules |
|---|---|
| `items` | required, 1-200 entries per request |
| `provider_id` | optional, inbox row id (≤ 64 chars). Makes the import idempotent. |
| `id` | optional alias for `provider_id` when the client has no stable row id |
| `sender` | required, 1-64 chars |
| `body` | required, 1-16000 chars |
| `received_at` | required, epoch seconds (int or numeric string), ≥ 2000-01-01 and at most 24 h in the future. Unlike `/sms`, old timestamps are expected. |
| `sub_id` | optional, 0-99, default 0 |

Idempotency is layered:

- Each item becomes a deterministic `imp-<hash>` id and is inserted with
  `INSERT OR IGNORE`, so replaying a request after a crash changes nothing.
- An item whose thread, text and timestamp (±60 s) match an existing row is
  counted as a duplicate; this covers messages already captured live.

Response:

```json
{"accepted": true, "imported": 12, "duplicates": 3, "skipped": 0}
```

| Status | When |
|---|---|
| 200 | batch processed; counts describe the outcome |
| 401 | missing/invalid device token |
| 413 | body over 512 KiB |
| 422 | invalid item (timestamp, length, batch size) |

Import does not wake the Telegram delivery worker. Storage grows with the
imported history; `deploy/purge-history.sh` covers these rows like any other.

## GET /messages

Paginated history. **Admin token required.**

Query parameters: `limit` (default 50, max 200), `offset` (≥ 0),
`state` (optional, one of the five states).

```json
{
  "items": [
    {
      "id": "3f1c9a0e-...",
      "sender": "+15551234567",
      "body": "Your code is 123456",
      "parts": 1,
      "part_index": 0,
      "received_at": 1767225600,
      "state": "delivered",
      "telegram_msg_id": "42",
      "attempts": 1,
      "last_error": null,
      "updated_at": 1767225601
    }
  ],
  "next_offset": 50
}
```

`next_offset` is `null` on the last page.

## GET /sms-state

Delivery state of specific message ids. Device **or** admin token accepted.
Used by the Android app to learn that the backend confirmed a failure for
a message — that, or a 4xx rejection of the upload, authorises a cellular
fallback SMS.

Query parameter: `ids` — comma separated, at most 50 ids, each ≤ 64 chars.

```json
{
  "states": {
    "3f1c9a0e-...": {"state": "failed", "attempts": 8, "last_error": "bad_request: ..."}
  }
}
```

## GET /relay/commands

SMS commands for the phone to execute with its own SIM. **Device token
only.**

```json
{"commands": [{"id": "9f2c...", "target": "+15551234567", "body": "on my way",
                "source": "miniapp", "created_at": 1767225600, "sub_id": 1}]}
```

The app polls this and reports the outcome back. Commands are created when
you reply to a forwarded message in the Telegram chat, with
`/sms <number> <text>`, or from the Mini App.

Optional request header `X-Device-Info` carries a small heartbeat:

```json
{"id": "installation-uuid", "version": "1.2.1", "sending": true,
 "sims": [{"sub_id": 1, "slot": 0, "label": "Personal"}]}
```

It is advisory: malformed JSON is ignored, and the heartbeat is what the
Mini App shows in `GET /app/api/status`. It contains no token, no phone
number and no contact name.

## POST /relay/commands/{id}/result

Reports the outcome of a command. **Device token only.**

```json
{"status": "sent", "detail": ""}
```

`status` is one of:

| Status | Meaning |
|---|---|
| `sent` | The phone's radio accepted the SMS |
| `delivered` | A carrier delivery report confirmed delivery |
| `failed` | The radio or the carrier reported a failure |
| `rejected` | The phone refused (sending disabled, unknown contact, …) |

`delivered`, `failed` and `rejected` are terminal. A late or duplicate
report is answered with `{"accepted": false}` and changes nothing. The
backend posts the outcome to the admin's Telegram chat for `sent`, `failed`
and `rejected`; a `delivered` report only updates the status, so the chat is
not spammed for every carrier receipt.

## POST /relay/contacts

Uploads the phone's contact list so the bot can offer names instead of
numbers. **Device token only.** The list is replaced on every upload.

```json
{"contacts": [{"number": "+989120000003", "name": "Alice"}]}
```

At most 3000 entries; entries without a name or number are dropped.

## GET /relay/status

Queue and state summary. `queue` is the total number of queued delivery jobs (the whole backlog, not only the jobs that are due right now). Device **or** admin token accepted.

```json
{
  "paused": false,
  "counts_by_state": {"pending": 1, "delivered": 24},
  "queue": 1,
  "version": "0.1.0"
}
```

## POST /admin/pause

**Admin token required.** Stops forwarding; incoming messages are stored as
`received`.

```json
{"paused": true}
```

## POST /admin/resume

**Admin token required.** Resumes forwarding and enqueues everything that
was held.

```json
{"paused": false, "resumed": 3}
```

## GET /admin/settings

**Admin token required.**

```json
{
  "paused": false,
  "fallback_enabled": false,
  "phone_b": null,
  "fallback_min_gap_s": 30,
  "fallback_max_per_hour": 10
}
```

## PUT /admin/settings

**Admin token required.** Strict whitelist; unknown keys → 422.

| Key | Type |
|---|---|
| `paused` | boolean |
| `fallback_enabled` | boolean |
| `phone_b` | string matching `^\+?[0-9]{5,20}$` or `null` |
| `fallback_min_gap_s` | integer 1-3600 |
| `fallback_max_per_hour` | integer 1-50 |

Omitted keys keep their current value. The response is the same shape as
`GET /admin/settings`.

## GET /admin/filters

**Admin token required.**

```json
{"allow": [], "block": ["+15550000000"], "keyword": ["verification code"]}
```

## PUT /admin/filters

**Admin token required.** Replaces the listed kinds completely; omitted
kinds stay unchanged.

```json
{"block": ["+15550000000", "+15551111111"], "keyword": ["verification code"]}
```

Each list holds at most 100 patterns, each 1-64 characters.

## Mini App API (`/app/api/*`)

Every route below requires the header `X-Telegram-Init-Data` with the raw
Telegram WebApp `initData`. The signature is verified against
`TELEGRAM_BOT_TOKEN`, the `auth_date` must be within
`MINIAPP_AUTH_MAX_AGE` (default 24 h), and the Telegram user id must equal
`ADMIN_TELEGRAM_USER_ID`. There is no cookie and no bearer token in the
browser. See [miniapp.md](miniapp.md#authentication) for the threat model
and CSP.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| `GET` | `/app/api/me` | – | `{"user": {...}, "admin": true, "auth_date": …}` |
| `GET` | `/app/api/chats` | `q` (≤ 100 chars), `limit` (1–200) | `{"chats": [{thread_key, display, name, last_ts, last_body, unread, …}]}` |
| `GET` | `/app/api/chats/{thread}/messages` | `before_ts`, `limit` (1–200) | `{"thread_key", "display", "name", "events", "has_more"}` |
| `POST` | `/app/api/chats/{thread}/messages` | `{"body", "client_ref", "sub_id", "confirm_unknown"}` | command id/status; `duplicate: true` on replay |
| `POST` | `/app/api/chats/{thread}/read` | `{"until_ts"}` | `{"thread_key", "last_read_at"}` |
| `GET` | `/app/api/contacts` | `q`, `limit` (1–100) | `{"contacts": [{number, name, thread_key, display}]}` |
| `GET` | `/app/api/status` | – | paused, queue, counts, phone heartbeat, version |
| `GET` | `/app/api/events` | `rev`, `timeout` (0–25 s) | `{"rev", "changed"}` long poll |

Send errors:

| Status | Error | Meaning |
|---|---|---|
| 409 | `unknown_number` | New number; retry with `confirm_unknown: true` |
| 409 | `client_ref_conflict` | The same `client_ref` was used for another thread |
| 422 | `invalid_destination` / `unsendable_thread` | The thread cannot receive SMS |
| 422 | `validation_error` | Body over 1600 chars, blank body, bad `client_ref` |
| 429 | `rate_limited` (`window: minute|day`) | Server-side send caps |

`thread` may be a canonical number (`98912…`), a shortcode (`short:110`) or
a number typed in the new-conversation dialog (`+98 912 …`); all spellings
land in the same thread. A `client_ref` is 8–64 chars of `[A-Za-z0-9._-]`
and must be reused verbatim on retry.

## Telegram command interface

The same operations are available from the private chat with the bot:
`/status`, `/pause`, `/resume`, `/filters`, `/fallback on|off`,
`/phoneb +1555…`. See
[telegram-bot-setup.md](telegram-bot-setup.md).
