# Troubleshooting: Samsung and Xiaomi

Background reliability is decided by the phone, not by this project. Both
Samsung (One UI) and Xiaomi (MIUI/HyperOS) aggressively freeze apps that
are not visible to the user, and both ship with vendor-specific battery
"protections" that are not part of the Android standard. The relay app
cannot bypass any of that — it can only ask you to disable the restrictions.

## First: make it the default SMS app

On Android 10+ (A51 and 11T) the SMS permissions are hard restricted. If
the app is not the default SMS app it receives **no** SMS at all and
cannot send the fallback SMS. Symptom: the app shows `Receive SMS: failed`
and nothing ever appears in its history.

Fix: *Settings → Apps → Default apps → SMS app → SMS Relay*, then reopen
the app and check the permission panel.

If you do not want to change the default SMS app, grant **notification
access** instead (*Settings → Notification access → SMS Relay*). The
relay then reads the incoming message from your messaging app's
notification — which requires that app to show a message preview.

## Symptoms and causes

| Symptom | Usual cause |
|---|---|
| No message appears in the app at all | app killed / battery optimisation / permission revoked / "SMS delivered" broadcast not received |
| Message in the app but never forwarded | no internet, backend down, wrong device token, filters dropped it |
| Forwarded late (minutes/hours) | Doze, app frozen, network lost and restored |
| Fallback SMS never sent | `SEND_SMS` not granted, no cellular signal, fallback disabled, rate limit reached |
| Works until the phone reboots | auto-start not allowed (Xiaomi), no boot receiver permission |
| Works until the app is swiped away | "Pause apps" / sleeping apps / swipe-to-kill behaviour |

## Samsung (One UI 5-7)

1. **Settings → Apps → SMS Relay → Battery** → *Unrestricted* (not
   "Optimised" or "Never sleeping apps" only).
2. **Settings → Battery → Background usage limits → Never sleeping apps**
   → add *SMS Relay*.
3. **Settings → Apps → SMS Relay → Mobile data** → allow background data
   usage if you use a metered connection.
4. **Settings → Notifications → Do not disturb** → allow the app if you
   want status notifications.
5. **Settings → Device care → Battery → Background usage limits** → make
   sure the app is not in "Never used" cleanup lists.
6. Optional: **Settings → Lock Screen and Lockout** → "Lock app in Recent
   Apps" (keeps it out of the swipe-to-dismiss cleanup path).

## Xiaomi (MIUI 12-14 / HyperOS, Xiaomi 11T)

1. **Settings → Apps → Manage apps → SMS Relay** → *Autostart* → enable.
2. **Settings → Apps → Manage apps → SMS Relay → Other permissions** →
   enable *Autostart*, *Background activity* and (if listed) *Battery
   saver*.
3. **Settings → Battery → Battery saver → App battery saver** → set the
   app to *No restrictions*.
4. **Settings → Apps → SMS Relay → Notifications** → allow all
   notifications and set "Floating notifications" if you want a status
   indicator.
5. MIUI "Second space" or "Dual apps" clones get their own battery
   settings — check the clone, not the original.
6. Lock the app in the recent-apps screen (the padlock icon) to reduce
   swipe-away cleanup.

> On MIUI the autostart screen is reached with
> `adb shell am start -n com.miui.securitycenter/com.miui.permcenter.autostart.AutoStartManagementActivity`
> if the menu is hard to find. The app in this repository offers the same
> deep link from its diagnostics section.

## General Android checks

- **Force-stop**: after a force-stop, Android does not deliver broadcasts
  to the app until it is launched again. This is a platform guarantee, not
  a bug. Relaunch the app after force-stopping.
- **Doze / App standby**: the system batches network wake-ups. The app
  requests a battery-optimisation exemption; if the user denies it,
  uploads can be delayed until the next doze maintenance window.
- **Data saver**: if "Data saver" is on, background traffic of other apps
  is blocked — the upload can be postponed.
- **VPN / private DNS**: if the phone uses a VPN that blocks the relay
  domain, uploads fail. The app's *Test connection* button shows this.
- **Dual SIM**: the incoming SMS may arrive on SIM 2 while the fallback
  goes out on SIM 1 (or vice versa). Pick the card explicitly in the app:
  "Send using" in the "Send an SMS" section. Every message of the app
  (the composer, the commands from Telegram, the fallback) goes out with
  that card; without a choice the app uses the first card of the phone.
  The history shows the card of every received message ("via SIM 1").
- **Carrier**: if the incoming number is short-coded (e.g. a bank OTP), the
  carrier may deliver it as a "flash SMS" (Class 0) with a different
  intent; some devices do not pass those to normal receivers. The relay
  may therefore miss one-time codes from some banks — this is a carrier
  and device behaviour, not something the app can fix.

## Diagnostics inside the app

The app shows:

- which permissions are granted / denied,
- whether the app is exempt from battery optimisation,
- a link to the autostart screen (Xiaomi; the app cannot read the autostart permission, so it only shows the deep link),
- the current queue length,
- the result of the last *Test connection* call.

## Backend side checks

```bash
systemctl --no-pager status sms-relay-api
journalctl -u sms-relay-api -n 50 --no-pager
curl -fsS -H "Authorization: Bearer $ADMIN_TOKEN" https://ai-barrow.ir/relay/status
```

`counts_by_state` tells you where messages stopped:

| State | Meaning | Action |
|---|---|---|
| `received` | stored while paused | `/resume` |
| `pending` | queued for delivery | check the journal for Telegram errors |
| `delivered` | Telegram confirmed | nothing to do |
| `uncertain` | timeout, outcome unknown | may be delivered twice; no action needed |
| `failed` | Telegram rejected it (e.g. chat id wrong) | fix the chat id, then re-send from the app |

## Still not working?

1. Send a test SMS from a second phone.
2. Watch the app's history: does the message arrive at all?
3. If yes: tap *Test connection* — a failed probe means network/DNS/token
   trouble, not SMS trouble.
4. If the probe works but nothing is forwarded: check `/relay/status` and
   the journal for `rate_limited` (Telegram 429) or `bad_request`
   (wrong chat id).
5. If the message never reaches the app: it is an Android/OEM
   background-restriction problem — work through the lists above.
