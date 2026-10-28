import asyncio
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

import app.db as db
import app.delivery as delivery
from app.delivery import DeliveryWorker
from app.tg import TGError

FIXED_TS = 1_700_000_000
FIXED_ISO = "2023-11-14 22:13:20"


class FakeTelegram:
    def __init__(self, error=None):
        self.sent = []
        self.keyboards = []
        self._error = error
        self._n = 0

    async def send_to_chat(self, chat_id, text, *, reply_to=None, keyboard=None, parse_mode=None):
        self.sent.append((chat_id, text))
        self.keyboards.append(keyboard)
        self._n += 1
        if isinstance(self._error, BaseException):
            error, self._error = self._error, None
            raise error
        return {"ok": True, "message_id": self._n}


def _seed(conn, *, msg_id="aabbccdd11223344", sender="+15550001111", body="hi",
          received_at=FIXED_TS, parts=1, part_index=0, attempts=None, next_at=None):
    msg = {"id": msg_id, "sender": sender, "body": body, "parts": parts,
           "part_index": part_index, "received_at": received_at, "group_id": "g1"}
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
    assert "hi" in telegram.sent[0][1]
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


def test_large_body_sends_three_chunks_with_header_on_each(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    body = "x" * 9000
    msg_id = _seed(conn, body=body)
    header = f"<b>+15550001111</b>\n<i>{FIXED_ISO} UTC</i>\n\n"

    processed = asyncio.run(worker.run_once(time.time() + 5))

    assert processed == 1
    assert [chat for chat, _ in telegram.sent] == ["test-chat-id"] * 3
    chunks = [text for _, text in telegram.sent]
    assert all(text.startswith(header) for text in chunks)
    assert all(len(text) <= delivery.MAX_CHUNK_LEN for text in chunks)
    assert "".join(text[len(header):] for text in chunks) == body
    msg = _message(conn, msg_id)
    assert msg["state"] == "delivered"
    assert msg["telegram_msg_id"] == "3"
    assert db.next_jobs(conn, time.time() + 60, limit=5) == []


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


def test_last_chunk_offers_details_and_the_thread_deep_link(conn, settings):
    configured = replace(settings, miniapp_url="https://ai-barrow.ir/relay/app/")
    telegram = FakeTelegram()
    worker = _worker(conn, configured, telegram)
    msg_id = _seed(conn, msg_id="aabbccdd11223344", body="hello")

    asyncio.run(worker.run_once(time.time() + 5))

    buttons = telegram.keyboards[-1]["inline_keyboard"][0]
    assert [item["text"] for item in buttons] == ["\U0001F50D More details",
                                                  "Open chat"]
    assert buttons[0]["callback_data"] == f"details:{msg_id}"
    url = buttons[1]["web_app"]["url"]
    thread_key = _message(conn, msg_id)["thread_key"]
    assert parse_qs(urlsplit(url).query)["thread"] == [thread_key]


def test_open_chat_button_is_absent_without_a_miniapp_url(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="aabbccdd11223344", body="hello")

    asyncio.run(worker.run_once(time.time() + 5))

    buttons = telegram.keyboards[-1]["inline_keyboard"][0]
    assert [item["text"] for item in buttons] == ["\U0001F50D More details"]


def test_only_the_last_chunk_carries_the_buttons(conn, settings):
    configured = replace(settings, miniapp_url="https://ai-barrow.ir/relay/app/")
    telegram = FakeTelegram()
    worker = _worker(conn, configured, telegram)
    _seed(conn, msg_id="aabbccdd11223344", body="x" * 9000)

    asyncio.run(worker.run_once(time.time() + 5))

    assert len(telegram.keyboards) == 3
    assert telegram.keyboards[0] is None
    assert telegram.keyboards[1] is None
    assert telegram.keyboards[2]["inline_keyboard"][0][1]["text"] == "Open chat"


def test_header_format_single_part(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="aabbccdd11223344", sender="+15550001111", body="hello")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert text.startswith("<b>")
    assert "<i>" in text
    assert "hello" in text


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


# --- Phase 1: HTML escaping and entity-safe chunking --------------------

def test_body_is_html_escaped_before_sending(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    _seed(conn, msg_id="escape00-aaaa", body="<b>bank</b> & co")

    asyncio.run(worker.run_once(time.time() + 5))

    text = telegram.sent[0][1]
    assert "&lt;b&gt;bank&lt;/b&gt; &amp; co" in text
    assert "<b>bank</b>" not in text
    assert _message(conn, "escape00-aaaa")["state"] == "delivered"


def test_split_html_never_cuts_an_entity():
    assert delivery._split_html("xxxx&amp;y", 6) == ["xxxx", "&amp;y"]
    assert delivery._split_html("abc", 10) == ["abc"]
    text = "a&amp;b&lt;c&gt;d"
    for limit in range(5, len(text) + 1):
        pieces = delivery._split_html(text, limit)
        assert "".join(pieces) == text
        assert all(len(piece) <= limit for piece in pieces)


def test_chunking_never_splits_an_entity_across_chunks(conn, settings):
    telegram = FakeTelegram()
    worker = _worker(conn, settings, telegram)
    msg_id = _seed(conn, msg_id="entity00-aaaa", body="placeholder")
    row = _message(conn, msg_id)
    header = worker._header(row)
    slice_len = delivery.MAX_CHUNK_LEN - len(header)
    body = "a" * (slice_len - 1) + "&" + "b" * 20
    row["body"] = body

    chunks = worker._chunks(row)

    assert len(chunks) == 2
    assert all(len(chunk) <= delivery.MAX_CHUNK_LEN for chunk in chunks)
    pieces = [chunk[len(header):] for chunk in chunks]
    assert "".join(pieces) == delivery._html_escape(body)
    for piece in pieces:
        # no trailing fragment of &amp;/&lt;/&gt; at a chunk boundary
        assert not any(piece.endswith(fragment) for fragment in
                       ("&", "&a", "&am", "&amp"))
    assert pieces[1].startswith("&amp;")
