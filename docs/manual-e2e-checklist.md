# Manual end-to-end test checklist

Run through this once after installing the app and before relying on the
relay. Tick each box. Anything that fails is a real defect — check
[troubleshooting-samsung-xiaomi.md](troubleshooting-samsung-xiaomi.md).

## Preparation

- [ ] VPS: `systemctl is-active sms-relay-api` → `active`
- [ ] VPS: `curl -fsS https://ai-barrow.ir/relay/health` → `{"ok":true}`
- [ ] Phone: app installed, `RECEIVE_SMS` / `READ_SMS` / `SEND_SMS` granted
- [ ] Phone: the app **is the default SMS app** (Android 10+ requirement —
      the permission panel in the app must show all three as "ok")
- [ ] App: server URL `https://ai-barrow.ir/relay`, device token filled in
- [ ] App: *Test connection* → success
- [ ] Telegram: the bot replies to `/start` in a private chat

## Mini App

- [ ] `curl -sI https://ai-barrow.ir/relay/app` → 200, CSP with
      `frame-ancestors`, **no** `X-Frame-Options`, `cache-control: no-store`
- [ ] `/relay/app` opened from the bot menu on iOS, Android and Desktop/Web
      → inbox loads, theme follows Telegram
- [ ] Open the URL in a normal browser → "Open from Telegram" splash, no data
- [ ] Reopen from the bot after an old session → still works (fresh initData)
- [ ] Chat list shows conversations with names/numbers, last message and
      unread badges; search filters and clears
- [ ] Thread shows inbound SMS and outbound commands interleaved, newest first;
      "load older" paginates
- [ ] Deep link under a relayed SMS opens the matching thread
- [ ] `t.me/<bot>?startapp=thread_<number>` opens the same thread (if used)
- [ ] Send to an existing contact → bubble queued; status becomes `sent`
      after the phone polls, then `delivered` if the carrier reports it
- [ ] Send to a never-seen number → Telegram confirmation popup; cancel sends
      nothing; confirm queues exactly one SMS
- [ ] Retry a failed send → the same SMS is not duplicated (same `client_ref`)
- [ ] 7 sends within a minute → 429 `rate_limited` (minute) and a readable error
- [ ] A message containing `<script>alert(1)</script>` renders as text
- [ ] Another Telegram account cannot open the data (403/401, no content)
- [ ] An expired `initData` (older than `MINIAPP_AUTH_MAX_AGE`) gets "Not signed in"
- [ ] `journalctl -u sms-relay-api | grep -F "<initData substring>"` → no output

## Android release 1.2.1

- [ ] Install the new APK over 1.1.0; app data and settings survive
- [ ] Enter a command from Telegram → status `sent`; with a carrier report,
      the Mini App shows `delivered`
- [ ] Dual SIM: pick card 2 in the Mini App → the SMS leaves from card 2;
      remove/disable card 2 → it fails as `sim_unavailable` and does **not**
      silently use card 1
- [ ] `GET /relay/app/api/status` shows this device with `version` 1.2.1,
      `sending` and the SIM list; the heartbeat refreshes each minute
- [ ] WorkManager shows one waiting command poll (no multiplying chain)
- [ ] "Import old messages": cancel the confirmation → nothing happens;
      confirm → progress counts rise; imported history appears in the Mini App
- [ ] Import runs again immediately → all rows report as already present
      (idempotent), no second Telegram message
- [ ] Imported rows never appear in the Telegram chat
- [ ] Import without the default-SMS role → clear message, import does not start

## Normal forwarding

- [ ] Send an SMS to Phone A from a second phone → the Telegram chat
      receives the sender (contact name if known, with the raw number)
      and the original timestamp in UTC, followed by the text
- [ ] The app's history shows the message with state *sent*
- [ ] `GET /relay/status` shows `delivered` ≥ 1
- [ ] Send a long message (> 160 chars) → it arrives in one or more
      chunks, each repeating the same sender/timestamp header
- [ ] Send a message with special characters (`& < > " ' emoji`) → the
      text arrives unchanged
- [ ] Send a message from a number with a non-Latin display name → sender
      shown correctly

## Idempotency / duplicates

- [ ] Replay the same message id with `curl` (device token) → response
      `duplicate: true`, and **no** second Telegram message
- [ ] `GET /relay/messages` shows only one row for that id

## Offline queue and fallback

- [ ] Turn on airplane mode (or disable Wi-Fi and mobile data) on Phone A
- [ ] Send 3 SMS → they stay queued in the app
- [ ] Restore the network → all 3 arrive in Telegram (order may vary)
- [ ] With the fallback enabled, make the backend confirm a delivery
      failure for a queued message (e.g. remove the bot from the chat) →
      after the next upload sweep a fallback SMS arrives at Phone B
      containing the original sender, timestamp and full body
- [ ] A 4xx rejection of the upload (e.g. a body over the API limit) also
      triggers the fallback: the backend stored nothing for that message
- [ ] The fallback SMS is not repeated endlessly (check the hourly limit)
- [ ] The app's history shows the fallback message as `fallback:sent` only
      after the radio confirmed it
- [ ] The same message is not fallback-sent twice

## Telegram remote control

- [ ] `/status` → shows paused state, counts and queue
- [ ] `/pause` → reply "Forwarding paused."
- [ ] With the relay paused, send an SMS → **no** Telegram message, and
      the message is stored (`received`)
- [ ] `/resume` → reply "Forwarding resumed. Resumed N messages." and the
      held message is delivered
- [ ] `/filters` → shows the current lists
- [ ] `/filters block +1555xxxxxxx` → a message from that number is
      dropped (`accepted: false, reason: "blocked"` on the API)
- [ ] `/filters unblock +1555xxxxxxx` → forwarding works again
- [ ] `/filters keyword wrongcode` → a message containing "wrongcode" is
      dropped
- [ ] `/fallback off` → no fallback SMS even when the backend reports a
      failed delivery
- [ ] `/fallback on` → fallback works again
- [ ] `/phoneb +15551234567` → reply shows the masked number
- [ ] A command from a **different** Telegram account → no reply, no
      effect
- [ ] A command in a **group** → ignored

## Admin API

- [ ] `GET /relay/messages` without a token → 401
- [ ] `GET /relay/messages` with the **device** token → 401
- [ ] `GET /relay/messages` with the admin token → 200 with items
- [ ] `GET /relay/messages?limit=500` → 422
- [ ] `PUT /relay/admin/settings` with an unknown key → 422
- [ ] 11 wrong-token requests → the last one returns 429

## Persistence and restart

- [ ] `sudo systemctl restart sms-relay-api` → the service comes back
      `active` and `/relay/health` works again
- [ ] History and settings survive the restart
- [ ] `sudo systemctl stop sms-relay-api` → sending a new SMS leaves the
      app queueing (no data loss) and the queue drains after the start

## Resource and stability

- [ ] `sudo scripts/measure_memory.sh` → relay RSS well below 200 MB
- [ ] `journalctl -k | grep -i oom` → no OOM kills
- [ ] `journalctl -u sms-relay-api --since "1 hour ago"` → no repeated
      error loops; the worker is quiet when idle

## Privacy

- [ ] `journalctl -u sms-relay-api | grep -F "<part of a test message>"`
      → no output (bodies are not logged)
- [ ] `sudo deploy/purge-history.sh --all` → history gone, `/relay/status`
      shows zero messages

## Documentation of the result

Note anything that failed, with the phone model, Android/MIUI version and
the relevant log excerpt, so it can be fixed rather than rediscovered.
