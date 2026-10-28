import asyncio
import logging
import random
import time
from urllib.parse import quote

def _html_escape(value: str) -> str:
    return (value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _split_html(text: str, limit: int):
    """Split escaped HTML without cutting an entity in half.

    The cut is backed up to the start of a trailing ``&...;`` entity, so
    a chunk never ends with ``&``, ``&am`` or similar. ``limit`` is the
    maximum length of every returned piece.
    """
    limit = max(1, int(limit))
    if len(text) <= limit:
        return [text]
    pieces = []
    while len(text) > limit:
        cut = limit
        amp = text.rfind("&", 0, cut)
        semicolon = text.rfind(";", 0, cut)
        if amp > semicolon:
            cut = amp
        if cut <= 0:
            # An entity longer than the limit: nothing safe to back up
            # to. This cannot happen with MAX_CHUNK_LEN in practice.
            cut = limit
        pieces.append(text[:cut])
        text = text[cut:]
    if text:
        pieces.append(text)
    return pieces


from datetime import datetime, timezone

import app.db as db
from app.tg import TGError

log = logging.getLogger(__name__)

MAX_CHUNK_LEN = 4090  # Telegram accepts 4096; the header is included
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


def thread_url(base: str | None, thread_key: str | None) -> str | None:
    """The Mini App link of one conversation, or None when unavailable.

    The key is percent-encoded: a thread key starts with "+", which a
    query parser would otherwise read as a space.
    """
    if not base or not thread_key:
        return None
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}thread={quote(str(thread_key), safe='')}"


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
            telegram_msg_id = None
            chunks = self._chunks(row)
            for index, chunk in enumerate(chunks):
                keyboard = None
                if index == len(chunks) - 1:
                    buttons = [{"text": "\U0001F50D More details",
                                "callback_data": f"details:{row['id']}"}]
                    thread_key = (row["thread_key"]
                                  if "thread_key" in row.keys() else None)
                    url = thread_url(self.settings.miniapp_url, thread_key)
                    if url:
                        buttons.append({"text": "Open chat",
                                        "web_app": {"url": url}})
                    keyboard = {"inline_keyboard": [buttons]}
                result = await self.telegram.send_to_chat(
                    chat_id, chunk, keyboard=keyboard, parse_mode="HTML")
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

    def _chunks(self, row):
        header = self._header(row)
        body = _html_escape(row["body"] or "")
        text = header + body
        if len(text) <= MAX_CHUNK_LEN:
            return [text]
        slice_len = max(1, MAX_CHUNK_LEN - len(header))
        return [header + piece for piece in _split_html(body, slice_len)]

    def _header(self, row):
        id8 = str(row["id"])[:8]
        seq = int(row["part_index"]) + 1
        parts = max(int(row["parts"] or 1), 1)
        ts = datetime.fromtimestamp(int(row["received_at"]), tz=timezone.utc)
        sender = (row["sender_name"] if "sender_name" in row.keys() else None) or row["sender"]
        sender = _html_escape(str(sender))
        raw_sender = _html_escape(str(row["sender"]))
        suffix = ""
        if sender != raw_sender:
            suffix = f"  <code>{raw_sender}</code>"
        return (f"<b>{sender}</b>{suffix}\n"
                f"<i>{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC</i>\n"
                f"\n")

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
