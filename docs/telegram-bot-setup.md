# Telegram bot setup

The relay needs a **dedicated** Telegram bot of its own.

> Do not reuse the token of the bot that already runs on this host
> (`opencode-telegram-bot.service`). Telegram allows only one
> `getUpdates` consumer per token: a second poller receives
> `409 Conflict` and terminates the first one. The relay's command
> interface and the existing bot would fight over the token, and the
> existing bot would crash-loop.

## Creating the relay bot

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name
   and a username → copy the token.
2. Open a **private** chat with the new bot and press **Start** (Telegram
   requires this before a bot may message you).
3. Get your numeric user id, for example from
   [@userinfobot](https://t.me/userinfobot).
4. Write the three values into `/etc/sms-relay/env`:

```bash
sudo nano /etc/sms-relay/env
```

```
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=987654321
ADMIN_TELEGRAM_USER_ID=987654321
```

In a private chat with a bot, the chat id equals your user id, so
`TELEGRAM_CHAT_ID` and `ADMIN_TELEGRAM_USER_ID` are the same number.

## Writing the values without leaking them

Never put a token into a command line: `ps` shows the arguments of every
process on the box, and the shell history keeps them. Use a small Python
snippet that reads the value from the environment and writes the file:

```bash
read -rsp "Bot token: " TOKEN; echo
sudo python3 - "$TOKEN" <<'PY'
import os
import sys

token = sys.argv[1]
path = "/etc/sms-relay/env"
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()
out = []
for line in lines:
    if line.startswith("TELEGRAM_BOT_TOKEN="):
        out.append("TELEGRAM_BOT_TOKEN=" + token)
    elif line.startswith("TELEGRAM_CHAT_ID="):
        out.append("TELEGRAM_CHAT_ID=" + os.environ.get("CHAT_ID", ""))
    elif line.startswith("ADMIN_TELEGRAM_USER_ID="):
        out.append("ADMIN_TELEGRAM_USER_ID=" + os.environ.get("CHAT_ID", ""))
    else:
        out.append(line)
with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
os.chmod(path, 0o600)
PY
unset TOKEN
```

Then generate the two API tokens and paste them into the same file:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # PHONE_A_TOKEN
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # ADMIN_TOKEN
```

Restart the relay afterwards:

```bash
sudo systemctl restart sms-relay-api.service
```

## Mini App menu button

The web inbox is described in [miniapp.md](miniapp.md). To expose it:

1. Add the public URL to `/etc/sms-relay/env` and restart:

   ```
   MINIAPP_URL=https://ai-barrow.ir/relay/app/
   ```

2. Set the persistent menu button, either in BotFather with
   `/setmenubutton` or with the helper (it reads the token itself and never
   prints it):

   ```bash
   sudo python3 scripts/set_miniapp_menu.py \
       --env-file /etc/sms-relay/env --url https://ai-barrow.ir/relay/app/
   ```

`web_app` buttons work only in the private chat with the bot. The bot still
ignores commands from groups and from other accounts, so the inbox stays
admin-only. `--clear` restores the default menu button.

## Commands

The relay answers only in a **private** chat with the configured admin
user id. Group and channel messages are ignored silently.

| Command | Effect |
|---|---|
| `/start`, `/help` | command list |
| `/status` | paused state, per-state message counts, queue depth |
| `/pause` | stop forwarding new messages (they queue up as `received`) |
| `/resume` | resume forwarding and enqueue everything held while paused |
| `/filters` | show allow / block / keyword filters |
| `/filters allow\|block\|keyword PATTERN` | add a filter (`@` is stripped for allow/block) |
| `/filters unallow\|unblock\|unkeyword PATTERN` | remove a filter |
| `/fallback on\|off` | enable/disable the direct-SIM fallback flag |
| `/phoneb +1234567890` | set the iPhone number used for fallback SMS |
| `/sms +1234567890 text` | send an SMS to any number via the phone's SIM |
| *reply to a forwarded message* | send the reply back to the original sender as an SMS |

Filter policy: a blocked sender is always dropped; if an allowlist exists
only allowlisted senders are forwarded; keyword filters drop messages whose
body contains the keyword. The same bounds as the REST API apply (at most
100 patterns per kind, 1-64 characters each).

The bot never executes shell commands and ignores anything that is not one
of the commands above.

Sending SMS is refused while the app has *Enable sending* switched off —
the app reports the refusal and the bot says so in the chat.

## The button flow

Every choice is a button, so nothing has to be remembered:

```
/sms  ->  [ 📍 Pick a contact ]  [ ✏️ Custom number ]
              |                       |
        your contact list        "send me the number"
              |                       |
        tap a contact  ->  "now type the message"  ->  sent
```

* **Pick a contact** shows the names your phone uploaded (press *Sync
  contacts* in the app). Numbers are masked, names are not. The list is
  paged eight at a time — use **▶️ Next** / **◀️ Previous**.
* **Search contacts** takes a name or a number.
* **Custom number** accepts every format: `+989120000003`, `09120000003`,
  `009891234567`, with spaces, and Persian numerals.
* Replying to a forwarded message sends the answer back to the original
  sender — including when the sender was stored as a contact *name*.

## Using the API directly

```bash
curl -fsS -H "Authorization: Bearer $ADMIN_TOKEN" https://ai-barrow.ir/relay/status
curl -fsS -H "Authorization: Bearer $ADMIN_TOKEN" https://ai-barrow.ir/relay/messages?limit=20
```

## Troubleshooting

- **No replies to commands**: the chat id/user id does not match, the
  message was not in a private chat, or the relay service is not running
  (`systemctl status sms-relay-api`). Check the journal:
  `journalctl -u sms-relay-api -n 50`.
- **`409 Conflict` in the journal**: another process is polling the same
  bot token. Only one relay instance may run, and the relay must not
  share a token with the OpenCode bot:
  `systemctl show -p MainPID sms-relay-api` and
  `journalctl -u opencode-telegram-bot -n 20`.
- **"bot is not a member of the group" style errors**: the chat id points
  at a group; use your private chat instead.
