# Telegram Mini App inbox

The Mini App is a messenger-style web inbox for the relay. It is mounted at
`https://ai-barrow.ir/relay/app/` and opens inside Telegram from the bot's
menu button, from the "Open chat" button under a relayed SMS, or from a
`t.me/<bot>?startapp=...` link.

It is **admin-only**: the backend accepts requests only from the Telegram
account whose numeric id equals `ADMIN_TELEGRAM_USER_ID`. SMS text is never
sent to a browser without a valid, fresh Telegram signature.

## Architecture

```
Telegram WebView
  └─ static shell  /relay/app (Caddy → FastAPI)
       ├─ index.html + assets/app.css + ES modules
       └─ fetch → /relay/app/api/*  with X-Telegram-Init-Data
                        │
                        ├─ FastAPI (127.0.0.1:8090)
                        ├─ SQLite (messages, sms_commands, thread_reads, …)
                        └─ in-process revision counter + asyncio.Event

Android phone ── GET /relay/commands ──→ FastAPI
              ── POST /relay/commands/{id}/result ──→ FastAPI ──→ Telegram
              ── POST /relay/import (opt-in history) ──→ FastAPI
```

There is **no Node build step**. The shell is plain HTML, one stylesheet and
six ES modules:

| File | Role |
|---|---|
| `index.html` | Two-pane shell, search, composer, new-conversation dialog |
| `assets/js/telegram.js` | Defensive wrapper around the Telegram WebApp SDK |
| `assets/js/api.js` | `fetch` wrapper: header auth, no cookies, no cache |
| `assets/js/store.js` | State, loading, sending, revision long poll |
| `assets/js/ui.js` | Text-only DOM rendering (no `innerHTML`) |
| `assets/js/app.js` | Bootstrap, hash routing, composer, deep links |

Every user-visible string is rendered with `textContent`/DOM nodes. There is
no `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write` in the
bundle, and the only third-party URL is `https://telegram.org/js/telegram-web-app.js`.

## Authentication

`initData` is the string Telegram injects into `window.Telegram.WebApp.initData`.
The browser sends it on every request:

```
X-Telegram-Init-Data: <raw initData>
```

The backend (`app/miniapp_auth.py`):

1. Caps the field at 8 KiB and rejects duplicate keys (max 64 pairs).
2. Computes `secret = HMAC_SHA256("WebAppData", TELEGRAM_BOT_TOKEN)` and
   verifies `hash` over the URL-decoded, alphabetically sorted `key=value`
   lines (excluding `hash`).
3. Requires `auth_date` within `MINIAPP_AUTH_MAX_AGE` (default 24 h) and no
   more than 5 minutes in the future.
4. Parses `user` and requires `user.id == ADMIN_TELEGRAM_USER_ID`; anyone
   else gets `403`.
5. Rejects failed attempts after 20 failures per IP in 60 s with `429`.

The raw `initData` is never logged and never included in an error message.
It is a short-lived credential: if the Mini App stays open past the TTL,
requests return `401` and the UI asks the user to reopen it from the bot.

Why a header instead of cookies:

- The Telegram Web iframe has an opaque origin; third-party cookie rules
  make a cookie session unreliable.
- Headers do not appear in URLs, access logs or `Referer`.
- `fetch` can set them; `EventSource` cannot, which is why live updates use
  a `fetch`-based long poll instead of SSE.

CORS is deliberately not configured: the page is served from the same
origin as the API, and no other origin is allowed to call it.

## Endpoints

All `/app/api/*` routes require a valid admin `initData` header unless noted.
Full request/response schemas are in [api.md](api.md#mini-app-api).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/app/api/me` | Signed-in Telegram user (display only) |
| `GET` | `/app/api/chats?q=&limit=` | Thread list with last message and unread count |
| `GET` | `/app/api/chats/{thread}/messages?before_ts=&limit=` | Merged inbound/outbound timeline |
| `POST` | `/app/api/chats/{thread}/messages` | Queue an SMS (idempotent `client_ref`) |
| `POST` | `/app/api/chats/{thread}/read` | Mark the thread read up to a timestamp |
| `GET` | `/app/api/contacts?q=&limit=` | Uploaded contacts for "new conversation" |
| `GET` | `/app/api/status` | Paused flag, queue, counts, phone heartbeat |
| `GET` | `/app/api/events?rev=&timeout=25` | Long poll for inbox changes |
| `POST` | `/import` | Historical SMS import (**device token**, opt-in) |

The browser never learns any bearer token. The device token and admin token
stay on the server and the phone.

## Threads and status

A thread key is the canonical phone number (`98912…`), a shortcode
(`short:110`), or an alphanumeric sender (`raw:…`). Inbound `messages` and
outbound `sms_commands` are merged on `thread_key` and ordered by timestamp.

Outbound statuses:

| Status | Meaning |
|---|---|
| `pending` | Queued; the phone has not polled it yet (≤ 5 seconds normally) |
| `sent` | The phone's radio accepted the SMS |
| `delivered` | A carrier delivery report arrived — the only source of this state |
| `failed` | Radio or carrier reported a failure |
| `rejected` | The phone refused (e.g. sending disabled, unknown contact) |

`delivered` is never inferred. If a carrier sends no report the command stays
`sent`, which is the honest answer. Late or duplicate transitions are
ignored; the bot is notified only for `sent`, `failed` and `rejected`.

Sending rules:

- A number that has never been seen needs `confirm_unknown: true`; otherwise
  the API returns `409 {"error": "unknown_number"}` and the UI asks first.
- Caps are server-side and apply to every send: `MINIAPP_SEND_PER_MINUTE`
  (default 6) and `MINIAPP_SEND_DAILY_CAP` (default 50).
- `client_ref` (8–64 chars, generated once per draft) makes retries
  idempotent. The UI keeps the exact same ref and body on "Retry"; the
  server returns the existing command instead of creating a second SMS.
- The chosen SIM (`sub_id`) travels with the command. The phone never
  silently falls back to another card when that card is gone.

## Live updates

`app.state.rev` is an in-process counter. Every genuine change (new SMS,
new command, status transition, read cursor, import) bumps it and wakes all
waiters on `app.state.rev_event`. The browser keeps a long poll open for up
to 25 seconds; on change it refreshes the chat list and open thread, then
polls again. On network errors it backs off 1 s → 2 s → 5 s → 15 s. The poll
stops while the document is hidden and restarts on `visibilitychange`.

This is intentionally single-process state. The service runs one Uvicorn
worker; a multi-worker deployment would need a shared revision source.

## UI behaviour

- Chat list: newest first, contact name over sender name over number,
  unread badge, search box (debounced 250 ms).
- Thread: newest messages first, "load older" pagination with `before_ts`,
  pending outgoing bubbles, and per-command status.
- Composer: message length and SMS part estimate (160 GSM-7 / 70 UCS-2 per
  part), SIM picker, retry of the last failed send with the same
  `client_ref`.
- New conversation: number or contact name, message, SIM, unknown-number
  confirmation through Telegram's popup (native `confirm` outside Telegram).
- BackButton is wired to return to the list; closing confirmation is on
  while a draft exists.
- Theme variables come from `themeParams`; outside Telegram the page is a
  read-only browser view with a clear "open from Telegram" splash.

## Deep links

| Source | Example |
|---|---|
| Bot "Open chat" button | `https://ai-barrow.ir/relay/app/?thread=989121234567` |
| `startapp` | `https://t.me/<bot>?startapp=thread_989121234567` |
| Menu button | `https://ai-barrow.ir/relay/app/` |

The client accepts `thread:` and `thread_` prefixes and checks, in order:
the `thread` query parameter, `tgWebAppStartParam`, then
`initDataUnsafe.start_param` / the raw `initData`. Thread keys are
percent-encoded, so a leading `+` survives query parsing. An unknown thread
falls back to the chat list.

## BotFather and menu setup

1. Set `MINIAPP_URL=https://ai-barrow.ir/relay/app/` in
   `/etc/sms-relay/env` and restart the service. This enables the
   "Open inbox" (`web_app`) button in the bot's main menu and the "Open chat"
   button under relayed SMS.
2. Set the persistent menu button, either in BotFather with
   `/setmenubutton` (choose the bot, paste the URL, choose a title) or with:

   ```bash
   sudo python3 scripts/set_miniapp_menu.py \
       --env-file /etc/sms-relay/env --url https://ai-barrow.ir/relay/app/
   ```

   The script reads the token from the env file and never prints it. Use
   `--clear` to restore the default menu button.
3. Optional: set `MINIAPP_BOT_USERNAME` if you use `t.me/<bot>?startapp=…`
   links in tooling. The menu buttons themselves only need `MINIAPP_URL`.

`web_app` inline buttons work only in private chats. The bot refuses
commands from other users and from groups, so the Mini App remains
admin-only even if its URL is known.

## CSP and iframe notes

The shell is served with:

```
Content-Security-Policy: default-src 'self';
  script-src 'self' https://telegram.org;
  style-src 'self'; img-src 'self' data:; connect-src 'self';
  frame-ancestors 'self' https://web.telegram.org https://*.telegram.org;
  base-uri 'self'; form-action 'none'; object-src 'none'
```

- `frame-ancestors` must include Telegram's web clients; **do not** add
  `X-Frame-Options: DENY` (or any XFO) for `/relay/app` — it would break the
  Web and Desktop clients.
- `script-src` allows `https://telegram.org` for the WebApp SDK. No other
  third-party script, style, font or image is loaded.
- `base-uri 'self'` is required because the shell uses
  `<base href="/relay/app/">`; `'none'` would make the browser ignore it.
- `/app` and `/app/api/*` are `Cache-Control: no-store`; static assets are
  `no-cache`.

A future site-wide CSP or XFO added in Caddy would override or conflict with
these headers. Check the `/relay/app` headers after any Caddy change.

## Historical import (opt-in)

The Android app has an "Import old messages" button. It reads
`content://sms/inbox` in batches of 100 (keyset pagination on `_ID`) and
POSTs to `/import` with the device token. Imported rows are stored as
`state='received'`, `source='import'` and are **never queued for Telegram**.
The import is opt-in and requires the default-SMS role.

Risks, accepted for v1:

- The import overlap heuristic is ±60 s on thread + text; two genuinely
  identical messages received a minute apart may collapse into one.
- Imported history grows the database and the backups.
- The entire historical inbox becomes readable in the Mini App. Anyone who
  can open the bot (the admin account) can read years of SMS. Only run the
  import if that is acceptable.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "Not signed in" splash | Opened outside Telegram, stale `initData` (> TTL), or a different Telegram account. Reopen from the bot. |
| 401 on every call | Bot token changed on the server; restart after updating `TELEGRAM_BOT_TOKEN`. |
| 403 | Signed in as a non-admin account; check `ADMIN_TELEGRAM_USER_ID`. |
| Empty chat list | No inbound SMS yet, or the DB was purged. Check `GET /relay/app/api/status`. |
| Send stays `pending` | The phone has not polled. Check the app is running (1.2.1+ polls every 5 seconds), sending is enabled, and the device heartbeat is fresh in `/app/api/status`. |
| Send stays `sent` | The carrier sent no delivery report. This is expected on some networks. |
| "Connection problem" repeatedly | Caddy/API down, TLS problem, or `/relay` not routed to the API. |
| Blank page but `/relay/app` returns 200 | CSP blocked a resource; check the browser console and the CSP header. |
| Deep link opens the list | The thread has no messages (404) or the client dropped the query; try `startapp`. |
| Menu button missing | Set it in BotFather or run `scripts/set_miniapp_menu.py`. |

## Performance notes

- One process, one SQLite connection, WAL mode. The Mini App adds no worker,
  no extra connection and no background task beyond the existing delivery
  and Telegram updaters.
- Long polls are cheap: each waiter is one `asyncio.Event` wait, capped at
  25 s. The memory target stays well under 200 MB on the 1–2 GB VPS.
- The static bundle is a few tens of KB; no build step, no source maps.
