import asyncio
import logging
import random
import time
from datetime import datetime, timezone
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

# A notification is a short heads-up, not a copy of the SMS: it carries
# the device, the sender and the time, and a button into the Mini App.
REAPER_INTERVAL_S = 30.0
REAPER_BATCH = 20
REAPER_RETRY_S = 300
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
        """The one-line notification of an inbound SMS.

        The body never leaves the relay: the admin gets the device, the
        sender and the time, and opens the Mini App for the content.
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
                log.warning("sms-relay delivery device lookup failed",
                            exc_info=True)
        sender = self._sender_label(row)
        ts = datetime.fromtimestamp(int(row["received_at"]), tz=timezone.utc)
        return (f"\U0001F4E5 <b>New SMS</b>\n"
                f"\U0001F4F1 <b>{_html_escape(device)}</b>\n"
                f"<b>{_html_escape(sender)}</b>\n"
                f"<i>{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC</i>")

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
    """Deletes Telegram notifications that have outlived their deadline.

    A notification is removed 5 minutes after the Mini App read the
    message, or 24 hours after it was sent when it was never read. The
    worker is cheap: one indexed query per interval, and a failure on
    one notification never stops the others.
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
