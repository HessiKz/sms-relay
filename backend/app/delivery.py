import asyncio
import logging
import random
import re
import time
from urllib.parse import quote

import app.db as db
from app import phones
from app.tg import TGError


def _html_escape(value: str) -> str:
    return (value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


log = logging.getLogger(__name__)

MIN_RETRY_AFTER = 1
MAX_RETRY_AFTER = 3600
RETRY_AFTER_FALLBACK = 60
BACKOFF_BASE = 30.0
BACKOFF_CAP = 3600.0
MAX_ATTEMPTS = 8
IDLE_DELAY = 120.0
STALE_LOCK_SECONDS = 300
ERROR_DETAIL_MAX = 200
ERROR_COOLDOWN = 5.0
_AMBIGUOUS_KINDS = frozenset({"timeout", "network", "unavailable"})

# A notification is a short heads-up: it carries the device, the sender,
# a snippet of the SMS and, when the body holds one, the code it asks
# for. The operator chose the snippet and the extracted code for the
# private admin chat; the whole body still stays in the relay, and the
# Mini App remains the place to read the message in full. There is no
# date or time line any more: the chat already stamps every message.
SNIPPET_MAX = 160
CODE_MIN_LEN = 4
CODE_MAX_LEN = 8
# A code is a standalone token: neighbouring letters or digits make it
# part of a longer word, a longer number or a phone number.
_CODE_CANDIDATE = re.compile(
    r"(?<![0-9A-Za-z])([0-9A-Za-z]{%d,%d})(?![0-9A-Za-z])"
    % (CODE_MIN_LEN, CODE_MAX_LEN))
# A day, a month or a year written around a separator is a date, not a
# code, even when one of its parts is four digits long.
_DATE_LIKE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
_CURRENCY_CHARS = frozenset("$€£¥₹")
_CURRENCY_WORDS = ("dollars", "dollar", "usd", "eur", "toman", "tomans",
                   "rial", "rials", "rs", "rupees", "rupee", "inr")
# A card or account number keeps its last digits: "*1234", "xx1234" and
# "•1234" all end in four digits that read like a code without being one.
_MASK_CHARS = frozenset("*xX•")
# A token introduced by one of these words names an identifier (a card, an
# order, a reference), not a code the reader is asked to type in.
_ID_LABEL_WORDS = frozenset({
    "card", "a/c", "acct", "account", "order", "ref", "reference",
    "txn", "transaction", "invoice", "bill", "policy", "ticket",
})
# Small words that may sit between the label and the digits, as in
# "card ending 1234", "card is 1234" or "ref no. 1234".
_ID_LINK_WORDS = frozenset({
    "ending", "ends", "no", "number", "num", "id", "last", "your", "is",
})
_ID_LABEL_WORDS_LOOKBACK = 3

REAPER_INTERVAL_S = 30.0
REAPER_BATCH = 20
REAPER_RETRY_S = 300
# A bot note ("SMS sent to ...", or an answer the bot posts) is chatter,
# not history: it is removed an hour after it was sent.
TG_NOTE_TTL_S = 3600
DEVICE_FALLBACK = "Unknown device"


def thread_url(base: str | None, thread_key: str | None,
               device_id: str | None = None) -> str | None:
    """The Mini App link of one conversation, or None when unavailable.

    The keys are percent-encoded: a thread key starts with "+", which a
    query parser would otherwise read as a space. The device id is
    appended when known so the device-first Mini App opens the right
    phone's conversation.
    """
    if not base or not thread_key:
        return None
    separator = "&" if "?" in base else "?"
    url = f"{base}{separator}thread={quote(str(thread_key), safe='')}"
    if device_id:
        url += f"&device={quote(str(device_id), safe='')}"
    return url


def _body_line(body: str) -> str:
    """The body with its whitespace runs collapsed, before it is cut."""
    return " ".join(str(body or "").split())


def body_snippet(body: str, limit: int = SNIPPET_MAX) -> str:
    """One line of the body, short enough for a chat notification.

    Whitespace runs collapse into single spaces so a multi-line SMS stays
    one line, and the text is cut to ``limit`` characters. The caller
    HTML-escapes the result.
    """
    text = _body_line(body)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _is_amount(text: str, start: int, end: int) -> bool:
    """True when the candidate is a price rather than a code."""
    before = text[:start].rstrip()
    after = text[end:].lstrip()
    if before and before[-1] in _CURRENCY_CHARS:
        return True
    if after and after[0] in _CURRENCY_CHARS:
        return True
    previous = before.rsplit(" ", 1)[-1].strip(".,;:!?()[]").lower()
    if previous in _CURRENCY_WORDS:
        return True
    following = after.split(" ", 1)[0].strip(".,;:!?()[]").lower()
    return following in _CURRENCY_WORDS


def _names_a_phone(text: str, start: int, end: int) -> bool:
    """True when the candidate sits inside a longer number (a phone number).

    A phone number is often written with spaces, dashes or dots, so one
    group of it looks standalone. The candidate is part of it only when
    its own characters fall inside the match; a code that merely shares
    digits with a number elsewhere in the SMS still counts. Only a run of
    at least 8 digits is a phone number, so a four-digit code next to a
    short number is kept.
    """
    for match in re.finditer(r"\+?\d[\d\s().-]*\d", text):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) < 8:
            continue
        if match.start() <= start and end <= match.end():
            return True
    return False


def _is_masked_digits(text: str, start: int) -> bool:
    """True when the candidate is the visible tail of a masked number.

    ``*1234``, ``xx1234`` and ``•1234`` hide all but the last digits of a
    card or an account: the tail is an identifier, not a code.
    """
    before = text[:start].rstrip()
    return bool(before) and before[-1] in _MASK_CHARS


def _names_an_identifier(text: str, start: int) -> bool:
    """True when a label word just before the candidate names an id.

    ``Card 1234``, ``Card ending 1234``, ``Ref no. 1234`` and
    ``Order 1234`` all point at a card, an order or a reference; the
    digits label that thing instead of asking the reader to enter them.
    A few small link words may sit between the label and the digits.
    """
    words = re.findall(r"[^\s]+", text[:start].lower())
    seen = 0
    for raw in reversed(words):
        word = raw.strip(".,;:!?()[]\"'").rstrip("-/")
        if not word:
            continue
        if word in _ID_LABEL_WORDS:
            return True
        seen += 1
        if seen >= _ID_LABEL_WORDS_LOOKBACK or word not in _ID_LINK_WORDS:
            return False
    return False


def extract_code(body: str) -> str | None:
    """The first plausible one-time code in an SMS body, or None.

    The rule is deliberately conservative: a standalone 4-8 character
    token with at least one digit. Tokens that belong to a longer word or
    number (a phone number, a reference, a tracking id) are skipped, and
    so are dates, amounts, and the visible tail of a masked card or
    account number. When the shape is not recognised the notification
    simply omits the code line; the snippet still shows it.
    """
    text = " ".join(str(body or "").split())
    if not text:
        return None
    dates = [match.span() for match in _DATE_LIKE.finditer(text)]
    for match in _CODE_CANDIDATE.finditer(text):
        candidate = match.group(1)
        start, end = match.span(1)
        if not any(ch.isdigit() for ch in candidate):
            continue
        if any(lo <= start and end <= hi for lo, hi in dates):
            continue
        if _names_a_phone(text, start, end):
            continue
        if _is_amount(text, start, end):
            continue
        if _is_masked_digits(text, start):
            continue
        if _names_an_identifier(text, start):
            continue
        return candidate
    return None


def _snippet_shows_code(snippet: str, code: str, *,
                        truncated: bool = False) -> bool:
    """True when the snippet already carries the whole code.

    The explicit ``Code:`` line exists so a code the snippet cut away is
    still readable, so a code the snippet shows in full is not repeated.
    ``truncated`` says the snippet is only the head of a longer body: a
    token that touches the cut may continue past it, so it does not count
    as shown.
    """
    if not snippet or not code:
        return False
    pattern = (r"(?<![0-9A-Za-z])" + re.escape(code) + r"(?![0-9A-Za-z])")
    for match in re.finditer(pattern, snippet):
        if truncated and match.end() >= len(snippet):
            continue
        return True
    return False


def _snippet_is_cut(body: str) -> bool:
    """True when the snippet of this body is only its first part."""
    return len(_body_line(body)) > SNIPPET_MAX


class DeliveryWorker:
    def __init__(self, db_conn, settings, telegram, *, batch_size=5,
                 poke_event=None):
        self.db_conn = db_conn
        self.settings = settings
        self.telegram = telegram
        self.batch_size = max(1, int(batch_size))
        self.poke_event = poke_event if poke_event is not None else asyncio.Event()

    def poke(self):
        self.poke_event.set()

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.run_once()
                if stop.is_set():
                    break
                if self.poke_event.is_set():
                    self.poke_event.clear()
                    continue
                if self._is_paused():
                    # Nothing may be sent while paused. Sleeping the full
                    # idle window avoids a hot loop over the queued jobs.
                    delay = IDLE_DELAY
                else:
                    delay = self._idle_delay()
                if delay <= 0:
                    # Work is due: yield to the event loop once (so the API
                    # stays responsive) and claim it in the next cycle.
                    await asyncio.sleep(0)
                    continue
                stop_wait = asyncio.ensure_future(stop.wait())
                poke_wait = asyncio.ensure_future(self.poke_event.wait())
                try:
                    await asyncio.wait({stop_wait, poke_wait}, timeout=delay,
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in (stop_wait, poke_wait):
                        if not waiter.done():
                            waiter.cancel()
                    await asyncio.gather(stop_wait, poke_wait,
                                         return_exceptions=True)
            except Exception as exc:
                log.warning("sms-relay delivery cycle aborted kind=%s",
                            type(exc).__name__)
                if stop.is_set():
                    break
                await asyncio.sleep(ERROR_COOLDOWN)

    async def run_once(self, now=None):
        now = int(time.time()) if now is None else int(now)
        if self._is_paused():
            return 0
        processed = 0
        for msg_id, job_attempts in self._claim(now):
            processed += 1
            row = self._fetch_message(msg_id)
            if row is None:
                log.warning("sms-relay delivery dropped msg_id=%s kind=orphan_job",
                            msg_id)
                self._delete_job(msg_id)
                continue
            await self._deliver(row, job_attempts + 1, now)
        return processed

    def _is_paused(self):
        return db.settings_get(self.db_conn, "paused") == "1"

    def _claim(self, now, batch_size=None):
        batch = self.batch_size if batch_size is None else batch_size

        def fn():
            stale_before = now - STALE_LOCK_SECONDS
            rows = self.db_conn.execute(
                "SELECT msg_id, attempts FROM jobs"
                " WHERE next_attempt_at <= ? AND (locked_at IS NULL OR locked_at <= ?)"
                " ORDER BY next_attempt_at, msg_id LIMIT ?",
                (now, stale_before, batch),
            ).fetchall()
            claimed = [(row["msg_id"], int(row["attempts"])) for row in rows]
            for msg_id, _ in claimed:
                self.db_conn.execute(
                    "UPDATE jobs SET locked_at = ? WHERE msg_id = ?",
                    (now, msg_id),
                )
            return claimed

        return db._write_tx(self.db_conn, fn)

    def _fetch_message(self, msg_id):
        return self.db_conn.execute(
            "SELECT * FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()

    def _delete_job(self, msg_id):
        db._write_tx(self.db_conn,
                     lambda: self.db_conn.execute(
                         "DELETE FROM jobs WHERE msg_id = ?", (msg_id,)))

    async def _deliver(self, row, attempts, now):
        msg_id = row["id"]
        chat_id = self.settings.telegram_chat_id
        try:
            text = self._notification_text(row)
            thread_key = (row["thread_key"]
                          if "thread_key" in row.keys() else None)
            device_id = (row["device_id"]
                         if "device_id" in row.keys() else None)
            keyboard = None
            url = thread_url(self.settings.miniapp_url, thread_key, device_id)
            if url:
                keyboard = {"inline_keyboard": [[
                    {"text": "Open chat", "web_app": {"url": url}}]]}
            result = await self.telegram.send_to_chat(
                chat_id, text, keyboard=keyboard, parse_mode="HTML")
            value = result.get("message_id") if isinstance(result, dict) else None
            if not isinstance(value, int):
                raise TGError("api_error", detail="response missing message_id")
            telegram_msg_id = str(value)
        except ValueError as exc:
            self._record_outcome(msg_id, "failed", attempts, now, None,
                                 f"invalid_input: {str(exc)[:ERROR_DETAIL_MAX]}"
                                 [:ERROR_DETAIL_MAX])
            log.warning("sms-relay delivery failed msg_id=%s kind=invalid_input",
                        msg_id)
        except TGError as exc:
            state, next_attempt_at, telegram_msg_id = self._classify(attempts, exc, now)
            self._record_outcome(msg_id, state, attempts, next_attempt_at,
                                 telegram_msg_id, self._bounded_error(exc))
            log.warning("sms-relay delivery failed msg_id=%s kind=%s",
                        msg_id, exc.kind)
        else:
            db.update_state(self.db_conn, msg_id, "delivered", attempts, now,
                            telegram_msg_id=telegram_msg_id, last_error=None)
            try:
                db.mark_message_notified(self.db_conn, msg_id,
                                         telegram_msg_id, now)
            except Exception as exc:
                # The notification is already in the chat; a failed
                # deadline bookkeeping must not fail the delivery.
                log.warning("sms-relay notify bookkeeping failed msg_id=%s"
                            " kind=%s", msg_id, type(exc).__name__)
            log.info("sms-relay delivered msg_id=%s attempts=%d", msg_id, attempts)

    def _record_outcome(self, msg_id, state, attempts, next_attempt_at,
                        telegram_msg_id, last_error):
        db.update_state(self.db_conn, msg_id, state, attempts, next_attempt_at,
                        telegram_msg_id=telegram_msg_id, last_error=last_error)

    def _classify(self, attempts, exc, now):
        if attempts >= MAX_ATTEMPTS:
            return "failed", now, None
        if exc.kind == "rate_limited":
            retry_after = exc.retry_after
            delay = RETRY_AFTER_FALLBACK
            if isinstance(retry_after, int) and retry_after >= MIN_RETRY_AFTER:
                delay = min(retry_after, MAX_RETRY_AFTER)
            return "pending", now + delay, None
        state = "uncertain" if exc.kind in _AMBIGUOUS_KINDS else "pending"
        return state, now + self._backoff(attempts), None

    def _backoff(self, attempts):
        base = min(BACKOFF_BASE * (2 ** (attempts - 1)), BACKOFF_CAP)
        jittered = random.uniform(0.9 * base, 1.1 * base)
        return max(MIN_RETRY_AFTER, int(round(jittered)))

    def _bounded_error(self, exc):
        detail = exc.detail
        if detail and len(detail) <= ERROR_DETAIL_MAX:
            return f"{exc.kind}: {detail}"[:ERROR_DETAIL_MAX]
        return exc.kind

    def _notification_text(self, row):
        """The notification of an inbound SMS.

        The operator chose to lift the old "the body never leaves the
        relay over Telegram" rule for the private admin chat: the
        notification carries the device, the sender, an extracted code
        when the body holds one, and a one-line snippet of the body. The
        full body still stays in the relay: the Mini App is where the
        message is read, and only the snippet is forwarded.

        The ingest filters decide which messages reach delivery at all,
        so there is deliberately no second spam filter here.
        """
        device = DEVICE_FALLBACK
        device_id = row["device_id"] if "device_id" in row.keys() else None
        if device_id:
            try:
                found = db.device_by_id(self.db_conn, device_id)
                name = found.get("name") if found else None
                if name:
                    device = str(name)
            except Exception:
                # A failed lookup must never block the delivery: the
                # notification falls back to the generic device label.
                log.warning("sms-relay delivery device lookup failed",
                            exc_info=True)
        sender = self._sender_label(row)
        body = str(row["body"] or "") if "body" in row.keys() else ""
        code = extract_code(body)
        snippet = body_snippet(body)
        shown = _snippet_shows_code(snippet, code or "",
                                    truncated=_snippet_is_cut(body))
        lines = [f"\U0001F4E5 <b>New SMS</b>",
                 f"\U0001F4F1 <b>{_html_escape(device)}</b>",
                 f"<b>{_html_escape(sender)}</b>"]
        if code and not shown:
            lines.append(f"Code: <b>{_html_escape(code)}</b>")
        if snippet:
            lines.append(f"<i>{_html_escape(snippet)}</i>")
        return "\n".join(lines)

    def _sender_label(self, row):
        """The contact name of the thread, otherwise the sender number.

        The device's own contact list wins over the name the phone
        attached to the SMS, which in turn wins over the sender number.
        """
        thread_key = row["thread_key"] if "thread_key" in row.keys() else None
        device_id = row["device_id"] if "device_id" in row.keys() else None
        if thread_key:
            try:
                name = db.device_contact_name_for_key(
                    self.db_conn, device_id, thread_key)
                if name:
                    return str(name)
            except Exception:
                log.warning("sms-relay delivery contact lookup failed",
                            exc_info=True)
        name = row["sender_name"] if "sender_name" in row.keys() else None
        if name:
            return str(name)
        if thread_key and not thread_key.startswith("raw:"):
            return phones.display_number(thread_key)
        return str(row["sender"])

    def _idle_delay(self):
        """Seconds until the next claimable job is due.

        The scan must use the same predicate as _claim(): a job that is
        locked but not yet stale is owned by an in-flight attempt and is
        not claimable. Ignoring that would return 0 and spin the event
        loop at 100% CPU.
        """
        now = int(time.time())
        stale_before = now - STALE_LOCK_SECONDS
        with db._WRITE_LOCK:
            row = self.db_conn.execute(
                "SELECT next_attempt_at FROM jobs"
                " WHERE next_attempt_at <= ? AND (locked_at IS NULL OR locked_at <= ?)"
                " ORDER BY next_attempt_at, msg_id LIMIT 1",
                (now + 4000, stale_before),
            ).fetchone()
        if row is None:
            return IDLE_DELAY
        delay = float(row["next_attempt_at"]) - now
        if delay <= 0:
            # Claimable and already due: the next cycle picks it up. The
            # caller yields to the event loop before continuing.
            return 0.0
        return float(min(IDLE_DELAY, delay))


class NotificationReaper:
    """Deletes Telegram messages that have outlived their deadline.

    Two kinds of messages are removed. An SMS notification goes 5
    minutes after the Mini App read the message, or 24 hours after it was
    sent when it was never read; those deadlines live in ``messages``. A
    bot note created by this relay (an "SMS sent" confirmation, a reply)
    goes an hour after it was sent; those rows live in ``tg_messages``,
    because a note is not an SMS notification and has no message row.

    The worker is cheap: two indexed queries per interval, and a failure
    on one message never stops the others.
    """

    def __init__(self, db_conn, settings, telegram, *,
                 interval=REAPER_INTERVAL_S, batch_size=REAPER_BATCH):
        self.db_conn = db_conn
        self.settings = settings
        self.telegram = telegram
        self.interval = max(1.0, float(interval))
        self.batch_size = max(1, int(batch_size))

    def poke(self):
        """Compatibility hook: the reaper only polls, nothing to signal."""
        return None

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                log.warning("sms-relay reaper cycle aborted kind=%s",
                            type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    async def run_once(self, now=None):
        now = int(time.time()) if now is None else int(now)
        removed = await self._reap_notifications(now)
        removed += await self._reap_notes(now)
        return removed

    async def _reap_notifications(self, now):
        chat_id = self.settings.telegram_chat_id
        removed = 0
        for row in db.due_notifications(self.db_conn, now,
                                        limit=self.batch_size):
            msg_id = row["id"]
            raw = row["telegram_msg_id"]
            try:
                message_id = int(raw)
            except (TypeError, ValueError):
                # A row that cannot name its Telegram message would be
                # retried forever: clear it instead.
                log.warning("sms-relay reaper dropped msg_id=%s kind=bad_id",
                            msg_id)
                db.mark_notification_deleted(self.db_conn, msg_id, now)
                continue
            try:
                await self.telegram.delete_message(chat_id, message_id)
            except ValueError:
                log.warning("sms-relay reaper dropped msg_id=%s kind=bad_input",
                            msg_id)
                db.mark_notification_deleted(self.db_conn, msg_id, now)
            except TGError as exc:
                log.warning("sms-relay reaper rescheduled msg_id=%s kind=%s",
                            msg_id, exc.kind)
                db.reschedule_notification(self.db_conn, msg_id, now,
                                           REAPER_RETRY_S)
            except Exception as exc:
                log.warning("sms-relay reaper rescheduled msg_id=%s kind=%s",
                            msg_id, type(exc).__name__)
                db.reschedule_notification(self.db_conn, msg_id, now,
                                           REAPER_RETRY_S)
            else:
                db.mark_notification_deleted(self.db_conn, msg_id, now)
                removed += 1
        return removed

    async def _reap_notes(self, now):
        """Delete the bot notes whose hour is up.

        A note is tracked in ``tg_messages``, so the chat id comes from
        its own row: a note may have been sent to a different chat than
        the configured one. A "not found" answer is a success (the note
        is already gone), and a transient failure pushes the deadline
        back instead of spinning on it.
        """
        removed = 0
        for row in db.due_tg_messages(self.db_conn, now,
                                      limit=self.batch_size):
            row_id = row["id"]
            raw = row["message_id"]
            try:
                message_id = int(raw)
            except (TypeError, ValueError):
                # A row that cannot name its message would be retried
                # forever: clear it instead.
                log.warning("sms-relay reaper dropped note=%s kind=bad_id",
                            row_id)
                db.mark_tg_message_deleted(self.db_conn, row_id, now)
                continue
            try:
                await self.telegram.delete_message(row["chat_id"], message_id)
            except ValueError:
                log.warning("sms-relay reaper dropped note=%s kind=bad_input",
                            row_id)
                db.mark_tg_message_deleted(self.db_conn, row_id, now)
            except TGError as exc:
                if exc.kind == "bad_request":
                    # Telegram refused the delete for good (a message that
                    # can never be deleted). Retrying it would spin
                    # forever, so the row is cleared instead. A "not
                    # found" answer is already a success in the client.
                    log.warning("sms-relay reaper dropped note=%s"
                                " kind=bad_request", row_id)
                    db.mark_tg_message_deleted(self.db_conn, row_id, now)
                else:
                    log.warning("sms-relay reaper rescheduled note=%s kind=%s",
                                row_id, exc.kind)
                    db.reschedule_tg_message(self.db_conn, row_id, now,
                                             REAPER_RETRY_S)
            except Exception as exc:
                log.warning("sms-relay reaper rescheduled note=%s kind=%s",
                            row_id, type(exc).__name__)
                db.reschedule_tg_message(self.db_conn, row_id, now,
                                         REAPER_RETRY_S)
            else:
                db.mark_tg_message_deleted(self.db_conn, row_id, now)
                removed += 1
        return removed
