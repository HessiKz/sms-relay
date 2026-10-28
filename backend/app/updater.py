import asyncio
import logging
import re
import datetime
import time
import uuid

import app.db as db
from app.flow import (MODE_IDLE, MODE_NUMBER, MODE_SEARCH, MODE_TEXT, Flow)
from app.delivery import _html_escape
from app.tg import TGError
from app.tg_ui import button, keyboard, mask_number, normalize_number

log = logging.getLogger(__name__)

PARSE_ERROR_SLEEP = 5.0
QUEUE_LIMIT = 1000
PHONE_RE = re.compile(r"^\+?[0-9]{5,20}$")

HELP_TEXT = (
    "\U0001F4E8 SMS Relay\n\n"
    "I forward the SMS your phone receives to this chat, and I can send "
    "SMS from your phone too.\n\n"
    "TAP A BUTTON\n"
    "\U0001F4E8 Send an SMS  - pick a contact or type a number\n"
    "\U0001F4F7 Contacts     - browse your contacts\n"
    "\U0001F6D1 Status       - what is queued and delivered\n\n"
    "TYPING IS ALSO FINE\n"
    "/sms +989120000003 hello   - send straight away\n"
    "/sms Alice               - search your contacts\n"
    "/status  /pause  /resume   - forwarding on/off\n"
    "/filters                  - who gets through\n"
    "/phoneb +989055961896      - the number for fallback SMS\n"
    "/fallback on|off           - fallback SMS on/off\n\n"
    "REPLIES\n"
    "Answer a forwarded message here and it goes back to the sender as "
    "a real SMS.\n\n"
    "The bot only answers you, in this private chat, and it never runs "
    "shell commands."
)
UNKNOWN_HINT = "Unknown command. Send /help for the command list."
FILTERS_USAGE = ("Usage: /filters | /filters allow|block|keyword PATTERN"
                 " | /filters unallow|unblock|unkeyword PATTERN")
FALLBACK_USAGE = "Usage: /fallback on|off"
PHONEB_USAGE = "Usage: /phoneb +1234567890 (5-20 digits, optional leading +)"
SMS_USAGE = ("Usage: /sms +1234567890 message text\n"
             "You can also just reply to a forwarded message to answer it "
             "by SMS.")
FILTER_KINDS = ("allow", "block", "keyword")
COMMANDS = frozenset({"start", "help", "status", "pause", "resume",
                      "filters", "fallback", "phoneb", "sms"})
FILTER_REMOVAL_SUBS = frozenset({"unallow", "unblock", "unkeyword"})


def _admin_ids(settings):
    raw = getattr(settings, "admin_telegram_user_id", None)
    try:
        return {int(raw)}
    except (TypeError, ValueError):
        return set()


def _strip_at(pattern: str) -> str:
    return pattern[1:] if pattern.startswith("@") else pattern


class Updater:
    """Telegram long-poll loop that turns admin DM messages into commands."""

    def __init__(self, db_conn, settings, telegram, *, poll_timeout=30,
                 poke_event=None, on_change=None):
        self.db_conn = db_conn
        self.settings = settings
        self.telegram = telegram
        self.poll_timeout = poll_timeout
        self.poke_event = poke_event
        self.on_change = on_change
        self._on_change = on_change
        self.admin_ids = _admin_ids(settings)
        self.last_update_id = None
        self.flow = Flow(db_conn, settings.miniapp_url)

    async def run(self, stop: asyncio.Event) -> None:
        self.last_update_id = None
        while not stop.is_set():
            try:
                updates = await self.telegram.get_updates(
                    self._next_offset(), self.poll_timeout)
            except asyncio.CancelledError:
                raise
            except TGError as exc:
                log.warning("sms-relay updater poll failed kind=%s", exc.kind)
                await self._sleep(stop, self.poll_timeout)
                continue
            except Exception as exc:
                log.warning("sms-relay updater cycle aborted kind=%s",
                            type(exc).__name__)
                await self._sleep(stop, PARSE_ERROR_SLEEP)
                continue
            await self._process_all(updates)
            await asyncio.sleep(0)

    async def _process_all(self, updates) -> None:
        for update in updates:
            try:
                await self._process(update)
            except asyncio.CancelledError:
                raise
            except TGError as exc:
                log.warning("sms-relay updater update skipped kind=%s",
                            exc.kind)
            except Exception as exc:
                log.warning("sms-relay updater update skipped kind=%s",
                            type(exc).__name__)
        self._advance(updates)

    def _advance(self, updates) -> None:
        for update in updates:
            if not isinstance(update, dict):
                continue
            raw = update.get("update_id")
            if isinstance(raw, int) and (self.last_update_id is None
                                         or raw > self.last_update_id):
                self.last_update_id = raw

    def _next_offset(self):
        if self.last_update_id is None:
            return None
        return self.last_update_id + 1

    async def _sleep(self, stop, delay) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    async def _process(self, update) -> None:
        if not isinstance(update, dict):
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._process_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return
        sender_id = sender.get("id")
        if not isinstance(sender_id, int) or sender_id not in self.admin_ids:
            return
        if chat.get("type") != "private":
            return
        chat_id = chat.get("id")
        if chat_id is None or chat_id == "":
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        # A reply to one of our relayed messages means: send this text
        # back to the original sender as an SMS.
        reply_to = message.get("reply_to_message")
        if isinstance(reply_to, dict):
            replied_id = reply_to.get("message_id")
            if isinstance(replied_id, int):
                if await self._handle_reply(replied_id, text, chat_id):
                    return
        session = self.flow.session(chat_id)
        if session.mode != MODE_IDLE:
            if await self._continue_conversation(session, text, chat_id):
                return

        tokens = text.strip().split()
        if not tokens:
            return
        word = tokens[0].split("@", 1)[0].lower()
        cmd = word[1:] if word.startswith("/") else ""
        log.info("sms-relay updater command kind=%s",
                 cmd if cmd in COMMANDS else "unknown")
        await self._dispatch(cmd, tokens, chat_id)


    # --- the guided conversation ----------------------------------------
    async def _continue_conversation(self, session, text, chat_id) -> bool:
        """Handle a plain message while a conversation is running."""
        if session.mode == MODE_NUMBER:
            number, name = self.flow.resolve_target(text)
            if number is None:
                await self._reply(
                    chat_id,
                    "That does not look like a phone number.\n\n"
                    "Send it like +989120000003 (or 09120000003).",
                    keyboard=self.flow.sms_menu())
                return True
            session.mode = MODE_TEXT
            session.target = number
            session.target_name = name
            await self._reply(
                chat_id,
                f"Sending to {name or number}.\n\nNow type the message:",
                keyboard=keyboard([button("⬅️ Cancel", "cancel")]))
            return True

        if session.mode == MODE_TEXT:
            body = text.strip()
            if not body:
                await self._reply(chat_id, "The message was empty, nothing sent.")
                return True
            await self._queue_sms(session.target, body, chat_id)
            self.flow.reset(chat_id)
            return True

        if session.mode == MODE_SEARCH:
            contacts = db.search_contacts(self.db_conn, text, limit=8)
            if not contacts:
                await self._reply(
                    chat_id,
                    f"No contact matches \"{text}\".\n\n"
                    "Try another word, or use a phone number instead.",
                    keyboard=self.flow.sms_menu())
                return True
            await self._reply(
                chat_id,
                "Who do you want to write to?",
                keyboard=self.flow.contact_keyboard(chat_id, query=text))
            return True

        return False

    async def _process_callback(self, callback) -> None:
        """A button was tapped."""
        message = callback.get("message")
        chat = (message or {}).get("chat") or {}
        sender = callback.get("from") or {}
        sender_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(sender_id, int) or sender_id not in self.admin_ids:
            return
        if chat.get("type") != "private" or chat_id is None:
            return
        data = callback.get("data")
        if not isinstance(data, str):
            return

        session = self.flow.session(chat_id)
        if data == "cancel":
            self.flow.reset(chat_id)
            await self._reply(chat_id, "Cancelled.",
                              keyboard=self.flow.main_menu())
            return

        if data.startswith("details:"):
            msg_id = data.split(":", 1)[1]
            row = db.message_by_telegram_id(self.db_conn, msg_id)
            if row is None:
                return
            ts = datetime.datetime.fromtimestamp(
                int(row["received_at"]), tz=datetime.timezone.utc)
            sender = row.get("sender_name") or row["sender"]
            text = (
                f"<b>{_html_escape(str(sender))}</b>\n"
                f"<i>{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC</i>\n\n"
                f"{_html_escape(str(row['body']))}\n\n"
                f"<b>SIM</b>: {row.get('sub_id') or 'unknown'}\n"
                f"<b>Relay id</b>: <code>{row['id']}</code>\n"
                f"<b>Attempts</b>: {row['attempts']}\n"
                f"<b>State</b>: {row['state']}"
            )
            await self.telegram.edit_message_text(
                chat_id, int(msg_id), text,
                keyboard={"inline_keyboard": [[
                    {"text": "\U0001F4AC Show message",
                     "callback_data": f"showmsg:{msg_id}"}]]},
                parse_mode="HTML")
            return

        if data.startswith("showmsg:"):
            msg_id = data.split(":", 1)[1]
            row = db.message_by_telegram_id(self.db_conn, msg_id)
            if row is None:
                return
            ts = datetime.datetime.fromtimestamp(
                int(row["received_at"]), tz=datetime.timezone.utc)
            sender = row.get("sender_name") or row["sender"]
            text = (
                f"<b>{_html_escape(str(sender))}</b>\n"
                f"<i>{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC</i>\n\n"
                f"{_html_escape(str(row['body']))}"
            )
            await self.telegram.edit_message_text(
                chat_id, int(msg_id), text,
                keyboard={"inline_keyboard": [[
                    {"text": "\U0001F50D More details",
                     "callback_data": f"details:{msg_id}"}]]},
                parse_mode="HTML")
            return

        if data == "menu:home":
            self.flow.reset(chat_id)
            await self._reply(chat_id, "What would you like to do?",
                              keyboard=self.flow.main_menu())
            return

        if data == "menu:help":
            await self._reply(chat_id, HELP_TEXT, keyboard=self.flow.main_menu())
            return

        if data == "menu:status":
            await self._reply(chat_id, self._status_text(),
                              keyboard=self.flow.main_menu())
            return

        if data == "menu:contacts":
            await self._reply(chat_id, self._contacts_overview(),
                              keyboard=self.flow.contact_keyboard(chat_id))
            return

        if data == "menu:sms":
            session.mode = MODE_IDLE
            await self._reply(chat_id, "How do you want to send it?",
                              keyboard=self.flow.sms_menu())
            return

        if data == "sms:contacts":
            await self._reply(chat_id, "Who do you want to write to?",
                              keyboard=self.flow.contact_keyboard(chat_id))
            return

        if data == "sms:search":
            session.mode = MODE_SEARCH
            session.results = []
            await self._reply(
                chat_id,
                "Type the name or number of the contact you are looking for.",
                keyboard=keyboard([button("⬅️ Cancel", "cancel")]))
            return

        if data == "sms:number":
            session.mode = MODE_NUMBER
            session.target = ""
            session.target_name = ""
            await self._reply(
                chat_id,
                "Send me the phone number (for example +989120000003).",
                keyboard=keyboard([button("⬅️ Cancel", "cancel")]))
            return

        if data == "menu:pause":
            paused = db.settings_get(self.db_conn, "paused") == "1"
            if paused:
                db.settings_set(self.db_conn, "paused", "0")
                moved = db.resume_received(self.db_conn)
                self._poke()
                await self._reply(
                    chat_id,
                    f"\u2705 Forwarding resumed. {moved} queued message(s) sent.",
                    keyboard=self.flow.main_menu())
            else:
                db.settings_set(self.db_conn, "paused", "1")
                self._poke()
                await self._reply(
                    chat_id,
                    "\u23F8 Forwarding paused. New messages are held until you resume.",
                    keyboard=self.flow.main_menu())
            return

        if data.startswith("contacts:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                page = 0
            session.query = session.query
            await self._reply(
                chat_id,
                self._contacts_overview(),
                keyboard=self.flow.contact_keyboard(chat_id,
                                                   query=session.query,
                                                   page=page))
            return

        if data.startswith("contact:"):
            try:
                index = int(data.split(":", 1)[1])
            except ValueError:
                return
            contact = self.flow.contact_at(chat_id, index)
            if contact is None:
                await self._reply(chat_id, "That contact is gone, pick another one.",
                                  keyboard=self.flow.contact_keyboard(chat_id))
                return
            session.mode = MODE_TEXT
            session.target = contact["number"]
            session.target_name = contact["name"]
            await self._reply(
                chat_id,
                f"Sending to {contact['name']} ({mask_number(contact['number'])}).\n\n"
                "Now type the message:",
                keyboard=keyboard([button("⬅️ Cancel", "cancel")]))
            return

    def _notify_change(self) -> None:
        """Tell the Mini App that outbound history changed."""
        if callable(self._on_change):
            self._on_change()

    async def _queue_sms(self, target, body, chat_id) -> None:
        """Hand an SMS to the phone."""
        command_id = uuid.uuid4().hex
        db.create_command(self.db_conn, command_id, target, body, "telegram_sms")
        self._notify_change()
        await self._reply(
            chat_id,
            f"\U0001F4E8 Sent to the phone: it will text {mask_number(target)}.\n\n"
            "Message:\n" + body[:200],
            keyboard=self.flow.main_menu())

    def _contacts_overview(self) -> str:
        total = db.contact_count(self.db_conn)
        if total == 0:
            return ("Your phone has not sent its contact list yet.\n\n"
                    "Open the app and press \"Sync contacts\", then try again.")
        return (f"{total} contact(s) available. Tap one, or use "
                "\U0001F50D Search contacts.")

    async def _dispatch(self, cmd, tokens, chat_id) -> None:
        if cmd in ("start", "help"):
            await self._reply(chat_id, HELP_TEXT, keyboard=self.flow.main_menu())
        elif cmd == "menu":
            await self._reply(chat_id, "What would you like to do?",
                              keyboard=self.flow.main_menu())
        elif cmd == "contacts":
            await self._reply(chat_id, self._contacts_overview(),
                              keyboard=self.flow.contact_keyboard(chat_id))
        elif cmd == "status":
            await self._reply(chat_id, self._status_text())
        elif cmd == "pause":
            db.settings_set(self.db_conn, "paused", "1")
            self._poke()
            await self._reply(chat_id, "Forwarding paused.")
        elif cmd == "resume":
            db.settings_set(self.db_conn, "paused", "0")
            moved = db.resume_received(self.db_conn)
            self._poke()
            await self._reply(
                chat_id, f"Forwarding resumed. Resumed {moved} messages.")
        elif cmd == "filters":
            await self._filters_cmd(tokens, chat_id)
        elif cmd == "fallback":
            await self._fallback_cmd(tokens, chat_id)
        elif cmd == "sms":
            await self._sms_cmd(tokens, chat_id)
        elif cmd == "resume2":
            pass
        elif cmd == "phoneb":
            await self._phoneb_cmd(tokens, chat_id)
        else:
            await self._reply(chat_id, UNKNOWN_HINT)

    async def _filters_cmd(self, tokens, chat_id) -> None:
        args = tokens[1:]
        if not args:
            await self._reply(chat_id, self._filters_text())
            return
        sub = args[0].lower()
        if sub in FILTER_KINDS:
            if len(args) != 2 or not args[1]:
                await self._reply(chat_id, FILTERS_USAGE)
                return
            pattern = args[1] if sub == "keyword" else _strip_at(args[1])
            if not pattern:
                await self._reply(chat_id, FILTERS_USAGE)
                return
            if not self._pattern_allowed(sub, pattern):
                await self._reply(chat_id, FILTERS_USAGE)
                return
            db.filters_add(self.db_conn, sub, pattern)
            await self._reply(chat_id, f"Filter added: {sub} {pattern}")
        elif sub in FILTER_REMOVAL_SUBS:
            if len(args) != 2 or not args[1]:
                await self._reply(chat_id, FILTERS_USAGE)
                return
            kind = sub[2:]
            pattern = args[1] if kind == "keyword" else _strip_at(args[1])
            if not pattern:
                await self._reply(chat_id, FILTERS_USAGE)
                return
            db.filters_remove(self.db_conn, kind, pattern)
            await self._reply(chat_id, "Filter removed.")
        else:
            await self._reply(chat_id, FILTERS_USAGE)

    def _pattern_allowed(self, kind: str, pattern: str) -> bool:
        """Apply the same bounds the REST API enforces, so a chat command
        cannot build an unbounded filter list."""
        try:
            db.clean_filter_patterns([pattern])
        except ValueError:
            return False
        existing = db.filters_all(self.db_conn).get(kind, set())
        if pattern in existing:
            return True
        return len(existing) < db.FILTER_MAX_PATTERNS

    async def _handle_reply(self, replied_id: int, text: str,
                            chat_id) -> bool:
        """Turn a reply to a relayed SMS into an SMS command.

        Returns True when the reply was consumed.
        """
        original = db.find_by_telegram_id(self.db_conn, replied_id)
        if original is None:
            return False
        # The sender may be stored as a contact NAME; the phone needs a
        # number, so resolve it through the uploaded contact list.
        target = original["sender"]
        name = original.get("sender_name") or ""
        resolved, resolved_name = self.flow.resolve_target(target)
        if resolved:
            target = resolved
            name = resolved_name
        command_id = uuid.uuid4().hex
        db.create_command(self.db_conn, command_id, target, text,
                          "telegram_reply", replied_id)
        self._notify_change()
        await self._reply(
            chat_id,
            f"\U0001F4E8 Your reply goes out as an SMS to "
            f"{name or mask_number(target)}.",
            keyboard=self.flow.main_menu())
        return True

    async def _sms_cmd(self, tokens, chat_id) -> None:
        """Send an SMS: the menu, or a direct command."""
        args = tokens[1:]
        session = self.flow.session(chat_id)

        if not args:
            session.mode = MODE_IDLE
            await self._reply(
                chat_id,
                "Send an SMS from your phone.\n\n"
                "Pick a contact from your list, or type any number.",
                keyboard=self.flow.sms_menu())
            return

        # /sms <number> <text> -> send it right away
        number, name = self.flow.resolve_target(args[0])
        if number is None:
            contacts = db.search_contacts(self.db_conn, args[0], limit=8)
            if contacts:
                await self._reply(
                    chat_id,
                    f"Did you mean one of these?",
                    keyboard=self.flow.contact_keyboard(chat_id, query=args[0]))
                return
            await self._reply(
                chat_id,
                f"\"{args[0]}\" is neither a contact nor a phone number.\n\n"
                "Use the buttons, or write it like /sms +989120000003 hello",
                keyboard=self.flow.sms_menu())
            return

        body = " ".join(args[1:]).strip()
        if not body:
            session.mode = MODE_TEXT
            session.target = number
            session.target_name = name
            await self._reply(
                chat_id,
                f"Sending to {name or number}.\n\nNow type the message:",
                keyboard=keyboard([button("⬅️ Cancel", "cancel")]))
            return
        await self._queue_sms(number, body, chat_id)

    async def _fallback_cmd(self, tokens, chat_id) -> None:
        arg = tokens[1].lower() if len(tokens) > 1 else ""
        if arg == "on":
            db.settings_set(self.db_conn, "fallback_enabled", "1")
            await self._reply(chat_id, "Fallback enabled.")
        elif arg == "off":
            db.settings_set(self.db_conn, "fallback_enabled", "0")
            await self._reply(chat_id, "Fallback disabled.")
        else:
            await self._reply(chat_id, FALLBACK_USAGE)

    async def _phoneb_cmd(self, tokens, chat_id) -> None:
        if len(tokens) == 2 and PHONE_RE.fullmatch(tokens[1]):
            value = tokens[1]
            db.settings_set(self.db_conn, "phone_b", value)
            lead = "+" if value.startswith("+") else ""
            await self._reply(chat_id, f"Phone B updated: {lead}****{value[-4:]}")
        else:
            await self._reply(chat_id, PHONEB_USAGE)

    def _status_text(self) -> str:
        counts = db.counts_by_state(self.db_conn)
        paused = db.settings_get(self.db_conn, "paused") == "1"
        queue = len(db.next_jobs(self.db_conn, int(time.time()), QUEUE_LIMIT))
        lines = ["Status:", f"paused: {'yes' if paused else 'no'}"]
        for state in db.STATES:
            lines.append(f"{state}: {counts.get(state, 0)}")
        lines.append(f"queue: {queue}")
        return "\n".join(lines)

    def _filters_text(self) -> str:
        filters = db.filters_all(self.db_conn)
        lines = ["Filters:"]
        for kind in FILTER_KINDS:
            patterns = sorted(filters.get(kind, ()))
            lines.append(f"{kind}: {', '.join(patterns) if patterns else '(none)'}")
        return "\n".join(lines)

    async def _reply(self, chat_id, text, keyboard=None) -> None:
        await self.telegram.send_to_chat(chat_id, text, keyboard=keyboard)
        log.info("sms-relay updater reply sent")

    def _poke(self) -> None:
        if self.poke_event is not None:
            self.poke_event.set()
