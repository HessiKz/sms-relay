# Security and privacy

This system handles private SMS content. It is a personal tool, but the
data path still crosses three parties: the phone, your VPS, and Telegram.

## Data flow

```
SMS  →  Phone A (Room queue)  →  HTTPS  →  VPS (SQLite)  →  Telegram Bot API  →  Telegram servers  →  your chat
                                    ↘ direct-SIM fallback: Phone A  →  carrier  →  Phone B

Telegram WebView (admin only)  →  HTTPS + signed initData header  →  VPS (SQLite)
```

The Mini App path adds no new third party: the WebView is Telegram's, and
the request goes straight to your VPS.

Each hop can see the content:

- **Your VPS** stores the full text in `/var/lib/sms-relay/sms.db`.
- **Telegram** receives the full text through the Bot API. Telegram bots
  are **not** end-to-end encrypted: the message is delivered to the
  cloud, and the recipient client fetches it from there. Anyone with
  access to the bot token (or to the account) can read the messages.
  Telegram's own privacy policy applies.
- **Your carrier** sees the fallback SMS in transit, like any SMS.

If the SMS content is sensitive enough that "Telegram servers can read
it" is unacceptable, do not use this relay for that content. Use a
self-hosted end-to-end encrypted channel (e.g. a Signal bridge or a
Matrix homeserver you control) instead — and remember the same argument
applies to the VPS-side database, which is only as safe as your host.

## Credentials

| Secret | Where it lives | Protection |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `/etc/sms-relay/env` | 0640 root:smsrelay; grants full control of the bot |
| `ADMIN_TOKEN` | `/etc/sms-relay/env` | 0640 root:smsrelay; grants full read access to the message history |
| `PHONE_A_TOKEN` | `/etc/sms-relay/env` + the phone | 0640 root:smsrelay; grants message injection |
| `ADMIN_TELEGRAM_USER_ID` | `/etc/sms-relay/env` | not secret, but decides who can run commands |

Rules that hold everywhere:

- Never commit these values. `.gitignore` excludes `.env` and
  `*.db*`; the repository only contains `deploy/env.template` and
  `.env.example` with placeholders.
- Never paste them into chat logs, issue trackers or screenshots.
- Rotate a token that leaks: generate a new one, update the file,
  `systemctl restart sms-relay-api`, and update the phone.
- The bot token in the OpenCode bot's own env file is shared by two
  applications; treat a leak of one as a leak of both and rotate both.

## Access control

- The API is bound to `127.0.0.1`; only Caddy can reach it.
- Caddy terminates TLS; the domain must have a valid certificate.
- Two separate tokens: the device token can only ingest, the admin token
  can read history and change settings. There is no "phone token" that
  can read the history.
- The Telegram command interface only answers the configured numeric user
  id in a private chat. Group and channel messages are ignored. The bot
  executes no shell commands and has no generic "eval" path.
- Failed authentications are rate limited (10 per token per minute, then
  HTTP 429).

## Mini App trust boundary

The Mini App is served from the same origin as the API, at
`https://ai-barrow.ir/relay/app/`. Loading the shell is public; reading any
message is not.

- Every `/app/api/*` request carries `X-Telegram-Init-Data`, the signed
  payload Telegram injects into the WebView. The server verifies the HMAC
  with `TELEGRAM_BOT_TOKEN`, checks the `auth_date` age (default 24 h, no
  more than 5 minutes in the future), and requires the Telegram user id to
  equal `ADMIN_TELEGRAM_USER_ID`.
- The header is capped at 8 KiB; malformed, stale, unsigned or non-admin
  payloads get `401`/`403`. Repeated failures are rate limited (20 per IP
  per minute, then `429`).
- No cookie is used, CORS is not enabled, and no bearer token is ever sent
  to the browser. The `initData` value is never logged and never embedded
  in an error message. It is a short-lived credential, not a permanent
  secret.
- The public URL being known changes nothing by itself: without a valid
  admin `initData` the API returns no data. Old `initData` from a browser
  history or screenshot expires after the TTL.
- Sending from the Mini App is capped server-side
  (`MINIAPP_SEND_PER_MINUTE`, `MINIAPP_SEND_DAILY_CAP`) and a never-seen
  number needs an explicit confirmation. Caps apply to the admin account,
  not to the browser.
- The only third-party script is `https://telegram.org/js/telegram-web-app.js`.
  CSP restricts `script-src`, `connect-src` and `frame-ancestors`; no
  `innerHTML` is used, so SMS text is always rendered as text.
- `X-Frame-Options` is intentionally absent for `/relay/app`; the CSP
  `frame-ancestors` directive is what allows the Telegram web clients to
  embed it. Adding XFO site-wide would break the Web/Desktop Mini App.

The historical import is opt-in. Once run, the phone's old SMS are readable
by the admin account in the Mini App like any other message. Imported rows
are not sent to Telegram and are stored with `source='import'`.

## Data retention

- The VPS keeps every message until you delete it. Use
  `deploy/purge-history.sh --older-than N` and the cron entry from
  [backup-restore.md](backup-restore.md).
- Backups are unencrypted on the same disk — encrypt them externally if
  that matters, and delete old ones.
- The phone keeps a local Room history; clear it from the app.
- Deleting a message from the Telegram chat does not delete the copy in
  the VPS database or in the backups.

## Logging

The backend logs message ids, states and error kinds. It never logs
message bodies, senders, tokens or Telegram chat ids. The API returns
validation errors without echoing the offending values, and request
bodies are never written to disk by the service.

## Android side

- The app requests `RECEIVE_SMS`, `READ_SMS`, `SEND_SMS` and
  `READ_PHONE_STATE` at runtime; nothing is granted silently.
- On Android 10+ the user has to make the app the **default SMS app**,
  which grants it the full SMS permission group. That means the app can
  read every incoming SMS on the phone — it is the relay's purpose, but
  it is a real capability, and the app is therefore built to store
  messages only locally and to forward them over TLS.
- The device token is stored in the app's private preferences.
- The optional historical import reads `content://sms/inbox` only after the
  user confirms a dialog, requires the default-SMS role, and sends rows with
  the device token. Imported rows are marked `source='import'`; the server
  never forwards them to Telegram. The import only runs when the user taps
  the button; there is no background backfill.
- The fallback SMS is an ordinary carrier SMS: it can be charged, it is
  visible to the carrier, and it is not encrypted.
- A debug-signed APK is fine for personal use, but it is not suitable
  for distribution — sign it properly if you share the project.

## Honest threat model

Protects against: casual interception on the network (TLS), unauthorized
API use (bearer tokens), and bot takeover via chat commands (admin id
gate).

Does **not** protect against: a compromised VPS, a leaked bot token, a
malicious carrier, Telegram's own access to message content, or an
attacker with physical access to the phone.
