import asyncio
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

import app.db as db
import app.delivery as delivery
from app.delivery import DeliveryWorker, NotificationReaper
from app.tg import TGError

FIXED_TS = 1_700_000_000
FIXED_ISO = "2023-11-14 22:13:20"


class FakeTelegram:
    def __init__(self, error=None, delete_error=None, delete_gone=False):
        self.sent = []
        self.keyboards = []
        self.deleted = []
        self._error = error
        self._delete_error = delete_error
        self._delete_gone = delete_gone
        self._n = 0

    async def send_to_chat(self, chat_id, text, *, reply_to=None, keyboard=None, parse_mode=None):
        self.sent.append((chat_id, text))
        self.keyboards.append(keyboard)
        self._n += 1
        if isinstance(self._error, BaseException):
            error, self._error = self._error, None
            raise error
        return {"ok": True, "message_id": self._n}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        if isinstance(self._delete_error, BaseException):
            error, self._delete_error = self._delete_error, None
            raise error
        if self._delete_gone:
            return {"ok": True, "already_gone": True}
        return {"ok": True}


def _seed(conn, *, msg_id="aabbccdd11223344", sender="+15550001111", body="hi",
          received_at=FIXED_TS, parts=1, part_index=0, attempts=None, next_at=None,
          device_id=None):
    msg = {"id": msg_id, "sender": sender, "body": body, "parts": parts,
           "part_index": part_index, "received_at": received_at, "group_id": "g1",
           "device_id": device_id}
    assert db.record_message(conn, msg) is True
    if attempts is not None or next_at is not None:
        db.update_state(conn, msg_id, "pending", attempts or 0,
                        next_at if next_at is not None else 0)
    return msg_id


def _message(conn, msg_id):
    for row in db.page_messages(conn, limit=50, offset=0):
        if row["id"] == msg_id:
            return row
    raise AssertionError(f"message missing: {msg_id}")


async def _wait_until(predicate, timeout=2.0, step=0.02):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(step)


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(str(tmp_path / "delivery.db"))
    yield connection
    connection.close()


def _worker(conn, settings, telegram, *, batch_size=5, poke_event=None):
    return DeliveryWorker(conn, settings, telegram, batch_size=batch_size,
                          poke_event=poke_event)


def test_success_marks_delivered_records_msg_id_and_removes_job(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn)

    processed = asyncio.run(worker.run_once(time.time() + 5))

    assert processed == 1
    assert len(telegram.sent) == 1
    assert telegram.sent[0][0] == "test-chat-id"
    assert "<b>+15550001111</b>" in telegram.sent[0][1]
    msg = _message(conn, msg_id)
    assert msg["state"] == "delivered"
    assert msg["attempts"] == 1
    assert msg["telegram_msg_id"] == "1"
    assert msg["last_error"] is None
    assert db.next_jobs(conn, time.time() + 60, limit=5) == []
    assert db.counts_by_state(conn) == {"delivered": 1}


def test_timeout_marks_uncertain_with_backoff_window(conn, settings):
    telegram = FakeTelegram(error=TGError("timeout"))
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn)
    now = int(time.time()) + 5

    processed = asyncio.run(worker.run_once(now))

    assert processed == 1
    assert len(telegram.sent) == 1
    msg = _message(conn, msg_id)
    assert msg["state"] == "uncertain"
    assert msg["attempts"] == 1
    assert msg["last_error"] == "timeout"
    jobs = db.next_jobs(conn, now + 60, limit=5)
    assert len(jobs) == 1 and jobs[0]["attempts"] == 1
    backoff = jobs[0]["next_attempt_at"] - now
    assert 27 <= backoff <= 33, backoff


def test_rate_limited_reschedules_with_retry_after(conn, settings):
    telegram = FakeTelegram(error=TGError("rate_limited", retry_after=5))
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn)
    now = int(time.time()) + 5

    asyncio.run(worker.run_once(now))

    msg = _message(conn, msg_id)
    assert msg["state"] == "pending"
    assert msg["attempts"] == 1
    assert msg["telegram_msg_id"] is None
    jobs = db.next_jobs(conn, now + 60, limit=5)
    assert [j["next_attempt_at"] for j in jobs] == [now + 5]


def test_retry_cap_at_eight_attempts_fails_and_removes_job(conn, settings):
    telegram = FakeTelegram(error=TGError("unavailable"))
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn, attempts=8)
    now = int(time.time()) + 5

    asyncio.run(worker.run_once(now))

    msg = _message(conn, msg_id)
    assert msg["state"] == "failed"
    assert msg["attempts"] == 9
    assert msg["last_error"] == "unavailable"
    assert db.next_jobs(conn, now + 60, limit=5) == []
    assert db.counts_by_state(conn) == {"failed": 1}


def test_bad_request_stays_pending_with_backoff(conn, settings):
    detail = "telegram rejected request (status 400): Bad Request: chat not found"
    telegram = FakeTelegram(error=TGError("bad_request", detail=detail))
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn)
    now = int(time.time()) + 5

    asyncio.run(worker.run_once(now))

    msg = _message(conn, msg_id)
    assert msg["state"] == "pending"
    assert msg["attempts"] == 1
    assert msg["last_error"] == f"bad_request: {detail}"
    jobs = db.next_jobs(conn, now + 60, limit=5)
    assert 27 <= jobs[0]["next_attempt_at"] - now <= 33


def test_client_value_error_fails_immediately(conn, settings):
    telegram = FakeTelegram(error=ValueError("text must not be blank"))
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn)
    now = int(time.time()) + 5

    asyncio.run(worker.run_once(now))

    msg = _message(conn, msg_id)
    assert msg["state"] == "failed"
    assert msg["attempts"] == 1
    assert msg["last_error"] == "invalid_input: text must not be blank"
    assert db.next_jobs(conn, now + 60, limit=5) == []


def test_last_error_composed_capped_at_200_when_detail_is_200_chars(conn, settings):
    detail = "d" * 200
    telegram = FakeTelegram(error=TGError("bad_request", detail=detail))
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn)

    asyncio.run(worker.run_once(time.time() + 5))

    last_error = _message(conn, msg_id)["last_error"]
    assert len(last_error) <= 200
    assert len(last_error) == 200
    assert last_error.startswith("bad_request: ")
    assert last_error.endswith(detail[-5:])


def test_invalid_input_last_error_composed_capped_at_200(conn, settings):
    telegram = FakeTelegram(error=ValueError("v" * 200))
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn)

    asyncio.run(worker.run_once(time.time() + 5))

    last_error = _message(conn, msg_id)["last_error"]
    assert len(last_error) <= 200
    assert len(last_error) == 200
    assert last_error.startswith("invalid_input: ")


def test_large_body_sends_one_short_notification_with_a_truncated_snippet(
        conn, settings):
    """A 9 KB SMS still produces exactly one short notification.

    Only the first few characters travel: there is nothing to chunk,
    because the snippet is bounded and the full body stays in the relay.
    """
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    body = "Hello there, " + "SECRETBODY" * 900
    msg_id = _seed(conn, body=body)

    processed = asyncio.run(worker.run_once(time.time() + 5))

    assert processed == 1
    assert len(telegram.sent) == 1
    text = telegram.sent[0][1]
    assert "Hello there," in text
    # The cut is at SNIPPET_MAX characters, so the tail never travels.
    assert len(delivery.body_snippet(body)) == delivery.SNIPPET_MAX
    assert text.count("<i>") == 1
    assert len(text) < 600
    msg = _message(conn, msg_id)
    assert msg["state"] == "delivered"
    assert msg["telegram_msg_id"] == "1"
    assert db.next_jobs(conn, time.time() + 60, limit=5) == []


def test_notification_names_the_device_and_the_sender_but_no_time(
        conn, settings):
    db.activate_device(conn, "dev-pixel-0009", "Pixel 8", "android", "1.0",
                       int(time.time()))
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="notify00-aaaa", sender="+15550001111",
          body="your parcel arrives tomorrow",
          device_id="dev-pixel-0009")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "Pixel 8" in text
    assert "+15550001111" in text
    assert "your parcel arrives tomorrow" in text
    # The chat already stamps every message, so the UTC line is gone.
    assert "UTC" not in text
    assert FIXED_ISO not in text
    assert "2023-11-14" not in text


def test_notification_uses_the_device_contact_name(conn, settings):
    db.activate_device(conn, "dev-pixel-0010", "Pixel 8", "android", "1.0",
                       int(time.time()))
    db.replace_device_contacts(conn, "dev-pixel-0010",
                               [{"number": "+15550001111", "name": "Alice"}])
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn, msg_id="named00-aaaa", sender="+15550001111",
                   device_id="dev-pixel-0010")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "<b>Alice</b>" in text
    assert _message(conn, msg_id)["sender"] == "+15550001111"


def test_notification_escapes_device_and_sender(conn, settings):
    db.activate_device(conn, "dev-pixel-0011", "Pixel <8> & co", "android",
                       "1.0", int(time.time()))
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="escape01-aaaa", sender="AD<BANK>",
          body="<b>bank</b> & co", device_id="dev-pixel-0011")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "Pixel &lt;8&gt; &amp; co" in text
    assert "AD&lt;BANK&gt;" in text
    assert "<8>" not in text
    assert "<b>bank</b>" not in text


def test_notification_falls_back_to_an_unknown_device_label(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="nodevic-aaaa", body="no device")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "Unknown device" in text
    assert _message(conn, "nodevic-aaaa")["state"] == "delivered"


def test_notification_falls_back_to_unknown_device_for_a_missing_row(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="missing1-aaaa", body="gone device",
          device_id="dev-gone-00000")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "Unknown device" in text
    assert _message(conn, "missing1-aaaa")["state"] == "delivered"


def test_notification_sets_sent_time_and_24h_deadline(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn, msg_id="deadlin0-aaaa")
    now = int(time.time()) + 5

    asyncio.run(worker.run_once(now))

    msg = _message(conn, msg_id)
    assert msg["telegram_msg_id"] == "1"
    assert msg["telegram_sent_at"] == now
    assert msg["telegram_delete_at"] == now + 86400
    assert msg["telegram_deleted_at"] is None


def test_seen_before_send_uses_the_five_minute_deadline(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn, msg_id="seenbef0-aaaa", device_id="device-a")
    # The Mini App read the thread while the notification was still queued.
    db.mark_thread_read(conn, "device-a", "15550001111", int(time.time()) + 600)
    now = int(time.time()) + 5

    asyncio.run(worker.run_once(now))

    msg = _message(conn, msg_id)
    assert msg["telegram_delete_at"] == now + 300


def test_paused_setting_skips_cycle_and_leaves_jobs_untouched(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn)
    snapshot = db.next_jobs(conn, time.time() + 10, limit=5)
    db.settings_set(conn, "paused", "1")

    processed = asyncio.run(worker.run_once(time.time() + 5))

    assert processed == 0
    assert telegram.sent == []
    msg = _message(conn, msg_id)
    assert msg["state"] == "pending"
    assert msg["attempts"] == 0
    assert [dict(row) for row in db.next_jobs(conn, time.time() + 10, limit=5)] == \
        [dict(row) for row in snapshot]


def test_batch_size_claims_and_processes_at_most_batch(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram, batch_size=5)
    for i in range(7):
        _seed(conn, msg_id=f"batch{i:04d}-aaaa", body=f"m{i}")
    now = time.time() + 5

    processed = asyncio.run(worker.run_once(now))

    assert processed == 5
    assert len(telegram.sent) == 5
    remaining = {row["msg_id"] for row in db.next_jobs(conn, now, limit=10)}
    assert remaining == {"batch0005-aaaa", "batch0006-aaaa"}


def test_last_notification_offers_the_thread_deep_link(conn, settings):
    configured = replace(settings, miniapp_url="https://ai-barrow.ir/relay/app/")
    telegram = FakeTelegram()
    worker = _worker(conn, configured, telegram)
    msg_id = _seed(conn, msg_id="aabbccdd11223344", body="hello")

    asyncio.run(worker.run_once(time.time() + 5))

    buttons = telegram.keyboards[-1]["inline_keyboard"][0]
    assert [item["text"] for item in buttons] == ["Open chat"]
    url = buttons[0]["web_app"]["url"]
    thread_key = _message(conn, msg_id)["thread_key"]
    assert parse_qs(urlsplit(url).query)["thread"] == [thread_key]


def test_deep_link_carries_the_device_id(conn, settings):
    """The device-first Mini App link names both thread and device."""
    configured = replace(settings, miniapp_url="https://ai-barrow.ir/relay/app/")
    db.activate_device(conn, "dev-pixel-0003", "Pixel 8", "android", "1.0",
                       int(time.time()))
    telegram = FakeTelegram()
    worker = _worker(conn, configured, telegram)
    msg_id = _seed(conn, msg_id="devlink0-aaaa", body="hello",
                   device_id="dev-pixel-0003")

    asyncio.run(worker.run_once(time.time() + 5))

    url = telegram.keyboards[-1]["inline_keyboard"][0][0]["web_app"]["url"]
    query = parse_qs(urlsplit(url).query)
    assert query["thread"] == [_message(conn, msg_id)["thread_key"]]
    assert query["device"] == ["dev-pixel-0003"]


def test_deep_link_omits_the_device_parameter_without_a_device(conn, settings):
    configured = replace(settings, miniapp_url="https://ai-barrow.ir/relay/app/")
    telegram = FakeTelegram()
    worker = _worker(conn, configured, telegram)
    _seed(conn, msg_id="nodevlk0-aaa", body="hello")

    asyncio.run(worker.run_once(time.time() + 5))

    url = telegram.keyboards[-1]["inline_keyboard"][0][0]["web_app"]["url"]
    assert "device=" not in url
    assert "thread=" in url


def test_thread_url_percent_encodes_and_keeps_an_existing_query(conn, settings):
    assert delivery.thread_url("https://x/relay/app", "+15550001111",
                               "dev id 1") == \
        "https://x/relay/app?thread=%2B15550001111&device=dev%20id%201"
    assert delivery.thread_url("https://x/relay/app?src=btn", "+15550001111") == \
        "https://x/relay/app?src=btn&thread=%2B15550001111"
    assert delivery.thread_url("https://x/relay/app", "+15550001111") == \
        "https://x/relay/app?thread=%2B15550001111"
    assert delivery.thread_url("https://x/relay/app", None, "dev") is None
    assert delivery.thread_url(None, "+1555", "dev") is None


def test_open_chat_button_is_absent_without_a_miniapp_url(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="aabbccdd11223344", body="hello")

    asyncio.run(worker.run_once(time.time() + 5))

    assert telegram.keyboards[-1] is None
    assert len(telegram.sent) == 1


def test_open_chat_button_opens_the_device_thread(conn, settings):
    configured = replace(settings, miniapp_url="https://ai-barrow.ir/relay/app/")
    db.activate_device(conn, "dev-pixel-0004", "Pixel 8", "android", "1.0",
                       int(time.time()))
    telegram = FakeTelegram()
    worker = _worker(conn, configured, telegram)
    msg_id = _seed(conn, msg_id="openchat-aaaa", body="hello",
                   device_id="dev-pixel-0004")

    asyncio.run(worker.run_once(time.time() + 5))

    assert len(telegram.sent) == 1
    buttons = telegram.keyboards[-1]["inline_keyboard"][0]
    assert [item["text"] for item in buttons] == ["Open chat"]
    query = parse_qs(urlsplit(buttons[0]["web_app"]["url"]).query)
    assert query["thread"] == [_message(conn, msg_id)["thread_key"]]
    assert query["device"] == ["dev-pixel-0004"]


def test_header_format_single_part(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="aabbccdd11223344", sender="+15550001111", body="hello")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert text.startswith("📥 <b>New SMS</b>")
    assert "<i>hello</i>" in text


def test_device_label_shows_the_device_name(conn, settings):
    """An SMS from a known device carries a compact `📱 <b>name</b>` line.

    The device name is user-controlled, so it is HTML-escaped like the
    rest of the notification.
    """
    db.activate_device(conn, "dev-pixel-0001", "Pixel <8>", "android", "1.0",
                       int(time.time()))
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="label000-aaaa", body="captured",
          device_id="dev-pixel-0001")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "📱 <b>Pixel &lt;8&gt;</b>" in text
    assert _message(conn, "label000-aaaa")["state"] == "delivered"


def test_notification_is_one_message_per_part(conn, settings):
    """A multi-part SMS produces one notification per captured part."""
    db.activate_device(conn, "dev-pixel-0002", "Pixel 8", "android", "1.0",
                       int(time.time()))
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="part0000-aaaa", body="x" * 9000, parts=3,
          part_index=0, device_id="dev-pixel-0002")
    _seed(conn, msg_id="part0001-aaaa", body="y" * 9000, parts=3,
          part_index=1, device_id="dev-pixel-0002")

    asyncio.run(worker.run_once(time.time() + 5))

    assert len(telegram.sent) == 2
    assert all("📱 <b>Pixel 8</b>" in text for _, text in telegram.sent)
    # Each part is its own short snippet: the whole 9 KB never travels.
    assert all("x" * 200 not in text and "y" * 200 not in text
               for _, text in telegram.sent)


def test_run_once_with_no_work_returns_zero(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)

    processed = asyncio.run(worker.run_once())

    assert processed == 0
    assert telegram.sent == []


@pytest.mark.anyio
async def test_poke_wakes_idle_run_prompt(conn, settings):
    telegram = FakeTelegram()
    worker = DeliveryWorker(conn, settings, telegram, poke_event=asyncio.Event())
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.sleep(0.1)
        assert telegram.sent == []
        _seed(conn, msg_id="poke0000-aaaa", body="poked")
        worker.poke()

        await _wait_until(lambda: len(telegram.sent) == 1)

        assert "<b>" in telegram.sent[0][1]
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert not task.cancelled()
    assert task.done()


@pytest.mark.anyio
async def test_run_survives_transient_db_error_and_recovers(conn, settings,
                                                            monkeypatch):
    telegram = FakeTelegram()
    worker = DeliveryWorker(conn, settings, telegram, poke_event=asyncio.Event())
    stop = asyncio.Event()
    original = db.settings_get
    state = {"fail": True, "failures": 0}

    def flaky_settings_get(c, key):
        if state["fail"]:
            state["failures"] += 1
            raise RuntimeError("transient db error")
        return original(c, key)

    monkeypatch.setattr(db, "settings_get", flaky_settings_get)
    monkeypatch.setattr(delivery, "ERROR_COOLDOWN", 0.05)

    task = asyncio.create_task(worker.run(stop))
    try:
        await _wait_until(lambda: state["failures"] >= 2)
        assert telegram.sent == []
        state["fail"] = False
        _seed(conn, msg_id="recov0000-aaaa", body="recovered")
        worker.poke()

        await _wait_until(lambda: telegram.sent != [])

        assert len(telegram.sent) == 1
        assert "<b>" in telegram.sent[0][1]
        assert _message(conn, "recov0000-aaaa")["state"] == "delivered"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert task.done() and not task.cancelled()
    assert task.exception() is None


@pytest.mark.anyio
async def test_run_terminates_cleanly_on_stop(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="stop0000-aaaa", body="done")
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        await _wait_until(lambda: len(telegram.sent) == 1)
        assert _message(conn, "stop0000-aaaa")["state"] == "delivered"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert task.done() and not task.cancelled()
    assert task.exception() is None


def test_idle_delay_ignores_locked_but_fresh_jobs(conn, settings):
    """A locked job must not make the worker spin on the event loop.

    The idle scan uses the same predicate as the claim: a job that is
    locked and not yet stale belongs to an in-flight attempt. Returning 0
    for it would busy-loop the whole process and starve the API.
    """
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="locked01-aaaa", body="locked")
    claimed = worker._claim(int(time.time()))
    assert [item[0] for item in claimed] == ["locked01-aaaa"]
    delay = worker._idle_delay()
    assert delay > 0, "a locked job must not produce a zero idle delay"


def test_idle_delay_is_zero_when_work_is_claimable(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="ready0001-aaaa", body="ready")
    assert worker._idle_delay() == 0.0


@pytest.mark.anyio
async def test_paused_worker_does_not_spin(conn, settings):
    """While paused the worker must idle instead of spinning over the
    queued jobs (the queued rows stay claimable, so a zero idle delay
    would burn a core)."""
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="paused001-aaaa", body="held")
    db.settings_set(conn, "paused", "1")

    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.sleep(0.3)
        assert telegram.sent == []
        assert task.done() is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert task.exception() is None


# --- The body travels as one short snippet, plus an extracted code -------
#
# The operator lifted the old "the body never leaves the relay over
# Telegram" rule for the private admin chat: the notification carries a
# bounded snippet and, when the body holds one, a code. The full body
# still stays in the relay and is what the Mini App shows.

def test_body_snippet_is_one_line_cut_to_the_limit():
    assert delivery.body_snippet("line one\nline two") == "line one line two"
    assert delivery.body_snippet("  a \t b  ") == "a b"
    assert delivery.body_snippet("x" * 500) == "x" * delivery.SNIPPET_MAX
    assert delivery.body_snippet("x" * 160) == "x" * 160
    assert delivery.body_snippet("") == ""
    assert delivery.body_snippet(None) == ""
    # No trailing space survives the cut.
    assert delivery.body_snippet("a" * 159 + " b c") == "a" * 159


def test_notification_carries_the_snippet_html_escaped(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="escape00-aaaa", body="<b>bank</b> & co")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "&lt;b&gt;bank&lt;/b&gt; &amp; co" in text
    # The raw markup is never forwarded, so Telegram cannot render it.
    assert "<b>bank</b>" not in text
    assert _message(conn, "escape00-aaaa")["state"] == "delivered"


def test_notification_shows_the_code_above_the_snippet(conn, settings):
    """A code the snippet cut away gets its own line, above the snippet."""
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    prefix = ("Thank you for using our service. This message confirms your "
              "request and explains what happens next in a few short "
              "sentences, so please read it carefully before you close it. ")
    body = prefix + "Your verification code is 483920 and it expires soon."
    assert len(" ".join(body.split())) > delivery.SNIPPET_MAX
    _seed(conn, msg_id="otp00000-aaaa", body=body)

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "Code: <b>483920</b>" in text
    assert text.index("Code: <b>483920</b>") < text.index("<i>")
    assert text.count("483920") == 1


def test_notification_omits_the_code_line_without_a_code(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="nocode00-aaaa",
          body="See you at the usual place tomorrow.")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "Code:" not in text
    assert "See you at the usual place tomorrow." in text


def test_notification_does_not_repeat_a_code_the_snippet_already_shows(
        conn, settings):
    """A short SMS shows its code once, in the snippet."""
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="once0000-aaaa", body="Your code is 44821")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert text.count("44821") == 1
    assert "Code:" not in text


def test_notification_code_line_is_escaped(conn, settings):
    """A code is alphanumeric, and the line still goes through the escaper."""
    assert delivery._html_escape("AB12CD") == "AB12CD"
    assert delivery._html_escape("A&B<2>") == "A&amp;B&lt;2&gt;"
    assert delivery._snippet_shows_code("code AB12CD here", "AB12CD") is True
    assert delivery._snippet_shows_code("code AB12CDX here", "AB12CD") is False
    assert delivery._snippet_shows_code("", "AB12CD") is False


def test_the_notification_never_logs_the_body_or_the_code(conn, settings, caplog):
    caplog.set_level("DEBUG")
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    body = "Your verification code is 483920. Do not share it."
    _seed(conn, msg_id="nolog000-aaaa", body=body)

    asyncio.run(worker.run_once(time.time() + 5))

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "483920" not in text
    assert "Do not share" not in text
    assert body not in text


def test_extract_code_skips_masked_and_labelled_identifiers():
    # Card/account tails are masked digits, not codes.
    assert delivery.extract_code(
        "Card *1234 debited Rs.5000 on 12/03/2026") is None
    assert delivery.extract_code("Card ending 1234") is None
    assert delivery.extract_code("Card is 1234") is None
    assert delivery.extract_code("A/c **9876 credited") is None
    assert delivery.extract_code("Acct xx4321 debited") is None
    # A label word before the digits names an identifier too.
    assert delivery.extract_code("Order 5511 has shipped") is None
    assert delivery.extract_code("Ref no. 7722") is None
    assert delivery.extract_code("Txn 998877 completed") is None


def test_extract_code_keeps_real_codes_next_to_masked_numbers():
    assert delivery.extract_code("Your OTP is 123456") == "123456"
    assert delivery.extract_code("Use 4821 to log in") == "4821"
    assert delivery.extract_code("Your code is A1B2C3") == "A1B2C3"
    assert delivery.extract_code("123456") == "123456"
    # A code that follows a masked number in the same SMS still counts.
    assert delivery.extract_code(
        "Card *1234: your code is 5612") == "5612"


def test_extract_code_skips_dates_amounts_and_phone_numbers():
    assert delivery.extract_code("Your code is 4921") == "4921"
    assert delivery.extract_code("G-731944 is your Google code") == "731944"
    assert delivery.extract_code("Use AB12CD at the till") == "AB12CD"
    # A date is not a code, even when a part of it is four digits.
    assert delivery.extract_code("Booked for 2024-11-14") is None
    # An amount is not a code.
    assert delivery.extract_code("Paid 120000 dollars") is None
    # A phone number is not a code, and a standalone code is not lost
    # because it shares digits with a number elsewhere in the SMS.
    assert delivery.extract_code("Call +989121234567 for help") is None
    assert delivery.extract_code(
        "Your code: 123456. Call +989121234567 if this was not you."
    ) == "123456"
    assert delivery.extract_code("") is None
    assert delivery.extract_code(None) is None


# --- Reaper --------------------------------------------------------------

def _notified(conn, msg_id, *, sent_at, delete_at, msg_number="11",
              state="delivered"):
    conn.execute(
        "UPDATE messages SET state = ?, telegram_msg_id = ?,"
        " telegram_sent_at = ?, telegram_delete_at = ? WHERE id = ?",
        (state, msg_number, sent_at, delete_at, msg_id))
    return msg_id


def _reaper(conn, settings, telegram):
    return NotificationReaper(conn, settings, telegram)


def test_reaper_deletes_a_due_notification_and_clears_the_fields(conn, settings):
    telegram = FakeTelegram()
    _seed(conn, msg_id="due00000-aaaa", device_id="device-a")
    _notified(conn, "due00000-aaaa", sent_at=1000, delete_at=1100)
    now = 1200

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(now))

    assert removed == 1
    assert telegram.deleted == [("test-chat-id", 11)]
    msg = _message(conn, "due00000-aaaa")
    assert msg["telegram_deleted_at"] == now
    assert msg["telegram_msg_id"] is None
    assert msg["telegram_delete_at"] is None


def test_reaper_ignores_not_due_notifications(conn, settings):
    telegram = FakeTelegram()
    _seed(conn, msg_id="notdue00-aaaa")
    _notified(conn, "notdue00-aaaa", sent_at=1000, delete_at=5000)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(1200))

    assert removed == 0
    assert telegram.deleted == []
    assert _message(conn, "notdue00-aaaa")["telegram_msg_id"] == "11"


def test_reaper_ignores_rows_without_a_telegram_message_id(conn, settings):
    telegram = FakeTelegram()
    _seed(conn, msg_id="noid0000-aaaa")
    conn.execute(
        "UPDATE messages SET telegram_delete_at = ? WHERE id = ?",
        (1000, "noid0000-aaaa"))

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(1200))

    assert removed == 0
    assert telegram.deleted == []


def test_reaper_ignores_imported_history(conn, settings):
    """Imported history has no notification and no job to reap."""
    telegram = FakeTelegram()
    db.record_message(conn, {
        "id": "imported-aaaa", "sender": "+15550002222", "body": "old",
        "parts": 1, "part_index": 0, "received_at": 900, "state": "received",
        "source": "import", "device_id": "device-a"})

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(1200))

    assert removed == 0
    assert telegram.deleted == []


def test_reaper_treats_already_deleted_as_success(conn, settings):
    telegram = FakeTelegram(delete_gone=True)
    _seed(conn, msg_id="gone0000-aaaa")
    _notified(conn, "gone0000-aaaa", sent_at=1000, delete_at=1100)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(1200))

    assert removed == 1
    msg = _message(conn, "gone0000-aaaa")
    assert msg["telegram_deleted_at"] == 1200
    assert msg["telegram_msg_id"] is None


def test_reaper_reschedules_a_failed_delete(conn, settings):
    telegram = FakeTelegram(delete_error=TGError("network"))
    _seed(conn, msg_id="retry000-aaaa")
    _notified(conn, "retry000-aaaa", sent_at=1000, delete_at=1100)
    now = 1200

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(now))

    assert removed == 0
    msg = _message(conn, "retry000-aaaa")
    assert msg["telegram_msg_id"] == "11"
    assert msg["telegram_delete_at"] == now + delivery.REAPER_RETRY_S
    assert msg["telegram_deleted_at"] is None


def test_one_failed_delete_does_not_stop_the_others(conn, settings):
    telegram = FakeTelegram(delete_error=TGError("api_error"))
    _seed(conn, msg_id="batch000-aaaa")
    _seed(conn, msg_id="batch001-aaaa")
    _notified(conn, "batch000-aaaa", sent_at=1000, delete_at=1100, msg_number="21")
    _notified(conn, "batch001-aaaa", sent_at=1000, delete_at=1101, msg_number="22")

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(1200))

    # Only the first raise is armed; the second row still gets deleted.
    assert removed == 1
    assert [mid for _, mid in telegram.deleted] == [21, 22]
    assert _message(conn, "batch000-aaaa")["telegram_msg_id"] == "21"
    assert _message(conn, "batch001-aaaa")["telegram_deleted_at"] == 1200


@pytest.mark.anyio
async def test_reaper_run_terminates_cleanly_on_stop(conn, settings):
    telegram = FakeTelegram()
    reaper = NotificationReaper(conn, settings, telegram, interval=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(reaper.run(stop))
    try:
        await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert task.done() and not task.cancelled()
    assert task.exception() is None


def test_reaper_is_idempotent_after_a_delete(conn, settings):
    telegram = FakeTelegram()
    _seed(conn, msg_id="twice000-aaaa")
    _notified(conn, "twice000-aaaa", sent_at=1000, delete_at=1100)
    reaper = _reaper(conn, settings, telegram)

    assert asyncio.run(reaper.run_once(1200)) == 1
    assert asyncio.run(reaper.run_once(1200)) == 0
    assert telegram.deleted == [("test-chat-id", 11)]


def test_bookkeeping_failure_does_not_undo_a_successful_send(conn, settings,
                                                             monkeypatch):
    """The notification is already in the chat when bookkeeping runs.

    A failure there must not mark the message failed or re-queue it: the
    admin would get the notification twice.
    """
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn, msg_id="bookke00-aaaa")

    def boom(c, i, t, n):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(db, "mark_message_notified", boom)

    processed = asyncio.run(worker.run_once(time.time() + 5))

    assert processed == 1
    assert len(telegram.sent) == 1
    msg = _message(conn, msg_id)
    assert msg["state"] == "delivered"
    assert msg["telegram_msg_id"] == "1"
    assert db.next_jobs(conn, time.time() + 60, limit=5) == []


# --- The bot notes the relay creates are deleted after an hour -----------
#
# An "SMS sent" note is a bot message, not an SMS notification, so it has
# no row in ``messages``. It is tracked in ``tg_messages`` and removed by
# the same reaper.

def _note(conn, *, chat_id="test-chat-id", message_id=42,
          kind="command_result", delete_at=2000, created_at=1000):
    return db.record_tg_message(conn, chat_id, message_id, kind, delete_at,
                                created_at)


def _note_row(conn, row_id):
    return db._asdict(conn.execute("SELECT * FROM tg_messages WHERE id = ?",
                                   (row_id,)).fetchone())


def test_a_note_is_recorded_with_its_deletion_deadline(conn):
    row_id = _note(conn, message_id=77, delete_at=5000, created_at=1400)

    row = _note_row(conn, row_id)
    assert row["chat_id"] == "test-chat-id"
    assert row["message_id"] == 77
    assert row["kind"] == "command_result"
    assert row["delete_at"] == 5000
    assert row["created_at"] == 1400
    assert row["deleted_at"] is None


def test_a_note_that_cannot_name_a_message_is_not_recorded(conn):
    """A missing message id must never create a row that spins forever."""
    assert _note(conn, message_id=None) == 0
    assert _note(conn, message_id="not-a-number") == 0
    assert _note(conn, chat_id=None) == 0
    assert _note(conn, chat_id="") == 0
    assert db.due_tg_messages(conn, 10 ** 9) == []


def test_the_reaper_deletes_a_due_note(conn, settings):
    telegram = FakeTelegram()
    row_id = _note(conn, message_id=77, delete_at=2000)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(2500))

    assert removed == 1
    assert telegram.deleted == [("test-chat-id", 77)]
    row = _note_row(conn, row_id)
    assert row["deleted_at"] == 2500


def test_the_reaper_ignores_a_note_that_is_not_due(conn, settings):
    telegram = FakeTelegram()
    row_id = _note(conn, message_id=78, delete_at=9000)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(2500))

    assert removed == 0
    assert telegram.deleted == []
    assert _note_row(conn, row_id)["deleted_at"] is None


def test_the_reaper_reschedules_a_failed_note_delete(conn, settings):
    telegram = FakeTelegram(delete_error=TGError("network"))
    row_id = _note(conn, message_id=79, delete_at=2000)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(2500))

    assert removed == 0
    row = _note_row(conn, row_id)
    assert row["deleted_at"] is None
    assert row["delete_at"] == 2500 + delivery.REAPER_RETRY_S


def test_the_reaper_clears_a_note_whose_id_is_bad(conn, settings):
    """A row that cannot name its message would be retried forever."""
    telegram = FakeTelegram()
    row_id = _note(conn, message_id=80, delete_at=2000)
    conn.execute("UPDATE tg_messages SET message_id = ? WHERE id = ?",
                 ("not-a-number", row_id))

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(2500))

    assert removed == 0
    assert telegram.deleted == []
    assert _note_row(conn, row_id)["deleted_at"] == 2500


def test_the_reaper_clears_a_note_telegram_refuses(conn, settings):
    """A permanent rejection must not be retried forever either."""
    telegram = FakeTelegram(delete_error=TGError("bad_request",
                                                 detail="message can't be deleted"))
    row_id = _note(conn, message_id=81, delete_at=2000)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(2500))

    assert removed == 0
    assert _note_row(conn, row_id)["deleted_at"] == 2500


def test_the_reaper_treats_a_missing_note_as_success(conn, settings):
    telegram = FakeTelegram(delete_gone=True)
    row_id = _note(conn, message_id=82, delete_at=2000)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(2500))

    assert removed == 1
    assert _note_row(conn, row_id)["deleted_at"] == 2500


def test_the_reaper_never_deletes_an_untracked_message(conn, settings):
    """Only the notes this relay recorded are deleted.

    A message with no row anywhere - a message the admin wrote, or one
    the bot sent before this feature existed - is never touched.
    """
    telegram = FakeTelegram()
    _note(conn, message_id=83, delete_at=9000)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(2500))

    assert removed == 0
    assert telegram.deleted == []


def test_a_deleted_note_is_not_deleted_twice(conn, settings):
    telegram = FakeTelegram()
    row_id = _note(conn, message_id=84, delete_at=2000)
    reaper = _reaper(conn, settings, telegram)

    assert asyncio.run(reaper.run_once(2500)) == 1
    assert asyncio.run(reaper.run_once(2500)) == 0
    assert telegram.deleted == [("test-chat-id", 84)]
    assert _note_row(conn, row_id)["deleted_at"] == 2500


def test_an_sms_notification_is_not_tracked_as_a_bot_note(conn, settings):
    """A delivered notification already has a messages row: no second row.

    Tracking it twice would make the reaper try to delete the same
    Telegram message from two places.
    """
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="twice-note-aaa", body="hi")

    asyncio.run(worker.run_once(time.time() + 5))

    assert len(telegram.sent) == 1
    assert _message(conn, "twice-note-aaa")["telegram_msg_id"] == "1"
    assert conn.execute(
        "SELECT COUNT(*) FROM tg_messages").fetchone()[0] == 0


def test_the_reaper_deletes_both_kinds_of_message_in_one_pass(conn, settings):
    telegram = FakeTelegram()
    _seed(conn, msg_id="both0000-aaaa")
    _notified(conn, "both0000-aaaa", sent_at=1000, delete_at=1100,
              msg_number=90)
    _note(conn, message_id=91, delete_at=1100)

    removed = asyncio.run(_reaper(conn, settings, telegram).run_once(2500))

    assert removed == 2
    assert telegram.deleted == [("test-chat-id", 90), ("test-chat-id", 91)]
