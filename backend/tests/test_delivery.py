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


def test_large_body_sends_one_short_notification_without_the_body(conn, settings):
    """A 9 KB SMS still produces exactly one short notification.

    The body is never forwarded, so there is nothing to chunk: the
    notification only says who wrote, on which phone and when.
    """
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    body = "SECRETBODY" * 900
    msg_id = _seed(conn, body=body)

    processed = asyncio.run(worker.run_once(time.time() + 5))

    assert processed == 1
    assert len(telegram.sent) == 1
    text = telegram.sent[0][1]
    assert "SECRETBODY" not in text
    assert len(text) < 500
    assert f"<i>{FIXED_ISO} UTC</i>" in text
    msg = _message(conn, msg_id)
    assert msg["state"] == "delivered"
    assert msg["telegram_msg_id"] == "1"
    assert db.next_jobs(conn, time.time() + 60, limit=5) == []


def test_notification_names_the_device_the_sender_and_the_time(conn, settings):
    db.activate_device(conn, "dev-pixel-0009", "Pixel 8", "android", "1.0",
                       int(time.time()))
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="notify00-aaaa", sender="+15550001111",
          body="the body must stay in the relay",
          device_id="dev-pixel-0009")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "Pixel 8" in text
    assert "+15550001111" in text
    assert f"{FIXED_ISO} UTC" in text
    assert "the body must stay in the relay" not in text


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
    assert "<i>" in text
    assert "hello" not in text


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
    assert all("x" * 10 not in text and "y" * 10 not in text
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


# --- The notification payload never carries the SMS body -----------------

def test_body_is_never_forwarded(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="escape00-aaaa", body="<b>bank</b> & co")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "bank" not in text
    assert "&lt;b&gt;" not in text
    assert _message(conn, "escape00-aaaa")["state"] == "delivered"


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
