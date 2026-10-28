# Installing the APK and granting permissions

## 1. Get the APK onto the phone

**Option A — USB debugging (recommended)**

```bash
adb devices                       # phone must be listed
adb install -r android-app/app/build/outputs/apk/debug/app-debug.apk
```

**Option B — sideload**

Copy `android-app/app/build/outputs/apk/debug/app-debug.apk` to the phone (USB cable, SD card, or a private file
host), tap it in the file manager and allow "install unknown apps" for the
app you used (Files / Chrome). On Xiaomi, MIUI asks for the same permission
under *Settings → Apps → Special app access → Install unknown apps*.

## 2. Give the app a capture path (REQUIRED on Android 10+)

There are two ways the relay can see an incoming SMS on Android 10+:

| Path | What the user does | Reliability |
|---|---|---|
| **A — default SMS app** | accepts the system dialog, or *Settings → Apps → Default apps → SMS app → SMS Relay* | high; the platform delivers the message itself |
| **C — notification access** | *Settings → Notification access → SMS Relay* (the app has a button for it) | medium; depends on the messaging app showing a message preview |

Use A if you can. Use C if you do not want to replace your SMS app — but
the messaging app must show the message text in its notification
(previews enabled), otherwise nothing can be read.

Whichever path is active, the app shows it under *Capture path*:
`default SMS app`, `notification access only`, or a red
**no capture path active** warning. With no path active the relay is deaf.

## 2a. Make the app the default SMS app (path A)

On Android 10 and newer, the SMS permission group is **hard restricted**:
a sideloaded app cannot be granted `RECEIVE_SMS` / `READ_SMS` / `SEND_SMS`
unless the user makes it the **default SMS app**. Without that, the app
never sees an incoming SMS and cannot send the cellular fallback.

Do this on both phones:

1. Open the app once — it immediately shows the system dialog
   *"Allow SMS Relay to be your default SMS app?"* (accept it).
2. If you dismissed it: **Settings → Apps → Default apps → SMS app →
   SMS Relay**.
3. Check the app's status panel: *Permissions* must show
   `Receive SMS: ok`, `Read SMS: ok`, `Send SMS (fallback): ok`.

The app implements the minimum default-SMS-app contract (a `SENDTO`
activity and a `RESPOND_VIA_MESSAGE` service) to qualify for the role.
It does not act as a messenger: it only observes and forwards, and
hands any "send to" intent straight back.

While the app is the default SMS app it also **writes the message into
the SMS provider inbox**, so your messages still show up in your usual
messaging app exactly as before.

> If you would rather not replace your SMS app, this relay cannot work
> on Android 10+ — that is a platform restriction, not a bug in the app.

## 3. Grant the remaining permissions

The app asks for them in order when you open it:

| Permission | Why | If you deny it |
|---|---|---|
| `RECEIVE_SMS` | detect incoming SMS | no forwarding at all |
| `READ_SMS` | read the message body of an incoming SMS | sender/time only, no text |
| `SEND_SMS` | direct-SIM fallback to the iPhone | no cellular fallback |
| `READ_PHONE_STATE` | list the active SIMs (dual-SIM picker) | only one SIM can be picked |
| Notifications (Android 13+) | show relay status | UI only |

Grant them from *Settings → Apps → SMS Relay → Permissions* if you
dismissed the dialog.

## 4. Configure the app

The app is **read-only by default**. A switch on the main screen
(*Enable sending*) unlocks every outbound SMS:

- sending a new SMS to any number (composer on the main screen),
- replies that arrive from Telegram (you reply to a forwarded message in
  the chat, the phone sends the answer as an SMS),
- the cellular fallback to Phone B.

Without that switch the app only receives and forwards, and a Telegram
command that asks for an SMS is refused with a note in the chat.

Open the app and fill in:

- **Server URL**: `https://ai-barrow.ir/relay`
- **Device token**: pre-filled with the token of this phone. Only change it
  when the `PHONE_A_TOKEN` in `/etc/sms-relay/env` was rotated.
- **Forwarding**: on
- **Allow this app to send SMS**: off by default. The app is read-only
  until this is on: no composed message, no command from Telegram and no
  cellular fallback leaves the phone.
- **Fallback to Phone B**: on, plus the iPhone's number in E.164-ish form
  (`+15551234567`)
- **Send using**: the SIM card every message of this phone goes out with
  (in the "Send an SMS" section, next to the composer). It shows one entry
  per card of the phone, "SIM 1" and "SIM 2". Without a choice of its own
  the app uses the first card of the phone. The same card is used for the
  composed message, for the commands from Telegram and for the cellular
  fallback.

Then press **Test connection**: the app calls `GET /relay/health` (10 s connect timeout) and
reports the latency or the error.

With *Allow this app to send SMS* on, the screen also offers a small
composer: a number, a text and **Send SMS** - the message leaves the phone
with the selected SIM. The same switch is what makes the app execute the
`/sms` commands of the Telegram bot; while it is off such a command is
refused and the bot reports the refusal.

## 5. Make sure Android does not kill it

This is the part that decides whether the relay works in practice. On
Samsung and Xiaomi the app is killed aggressively unless you exclude it.
See [troubleshooting-samsung-xiaomi.md](troubleshooting-samsung-xiaomi.md)
for the exact menus, and do them right after installing:

1. Battery → set the app to "Unrestricted" / "No restrictions".
2. Auto-start: allow the app to start automatically.
3. Lock the app in the recent-apps screen (optional, but helps on Xiaomi).
4. Disable "Pause apps" / "Sleeping apps" for the relay.

## 6. Verify

Send an SMS to the Android number from another phone. Within a few seconds:

- the app's history shows the message,
- the Telegram chat receives the sender (contact name if known, with the raw number) and the original timestamp in UTC, followed by the text.

If the Telegram message does not arrive, check the backend:

```bash
curl -fsS -H "Authorization: Bearer $ADMIN_TOKEN" https://ai-barrow.ir/relay/status
```

`counts_by_state` shows where the message stopped (`pending` = queued,
`delivered` = sent, `uncertain` = ambiguous, `failed` = rejected).
