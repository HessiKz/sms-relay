import asyncio
import time

import pytest

import app.db as db
from app.tg import TGError
from app.updater import (FALLBACK_USAGE, FILTERS_USAGE, HELP_TEXT,
                         PARSE_ERROR_SLEEP, PHONEB_USAGE, UNKNOWN_HINT, Updater)

ADMIN_ID = 7
CHAT_ID = 555
OTHER_CHAT_ID = 888


class FakeTelegram:
    def __init__(self, script=None):
        self._script = list(script or [])
        self.sent = []
        self.calls = []

    async def get_updates(self, offset, timeout):
        self.calls.append({"offset": offset, "timeout": timeout})
        await asyncio.sleep(0)
        item = self._script.pop(0) if self._script else []
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_to_chat(self, chat_id, text, reply_to=None, keyboard=None):
        self.sent.append((chat_id, text))
        return {"ok": True, "message_id": len(self.sent)}


class FailingTelegram(FakeTelegram):
    async def send_to_chat(self, chat_id, text, reply_to=None, keyboard=None):
        raise TGError("timeout", detail="send failed")


def _msg(update_id, text=None, *, user=ADMIN_ID, chat_type="private",
         chat_id=CHAT_ID, with_text=True):
    message = {"message_id": update_id,
               "chat": {"id": chat_id, "type": chat_type}}
    if user is not None:
        message["from"] = {"id": user}
    if with_text:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def _seed(conn, msg_id, state=None, body="body-text"):
    msg = {"id": msg_id, "sender": "+15550001111", "body": body,
           "received_at": int(time.time()), "parts": 1, "part_index": 0}
    if state is not None:
        msg["state"] = state
    db.record_message(conn, msg)


async def _wait_until(predicate, timeout=2.0, step=0.02):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(step)


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(str(tmp_path / "updater.db"))
    yield connection
    connection.close()


async def _drive(fake, updater, ready):
    stop = asyncio.Event()
    task = asyncio.create_task(updater.run(stop))
    try:
        await _wait_until(ready)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
    assert task.done() and not task.cancelled()


def _updater(fake, conn, settings, *, poll_timeout=30, poke_event=None):
    return Updater(conn, settings, fake, poll_timeout=poll_timeout,
                   poke_event=poke_event)


@pytest.mark.anyio
@pytest.mark.parametrize(("user", "chat_type"),
                         [(999, "private"), (ADMIN_ID, "group"),
                          (ADMIN_ID, "supergroup"), (ADMIN_ID, "channel")])
async def test_unauthorized_messages_are_ignored(conn, settings, user, chat_type):
    fake = FakeTelegram(script=[[_msg(1, "/pause", user=user, chat_type=chat_type)]])
    poke = asyncio.Event()
    updater = _updater(fake, conn, settings, poke_event=poke)

    await _drive(fake, updater, lambda: len(fake.calls) >= 2)

    assert fake.sent == []
    assert db.settings_get(conn, "paused") is None
    assert not poke.is_set()


@pytest.mark.anyio
async def test_message_without_sender_is_ignored(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/pause", user=None)]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.calls) >= 2)

    assert fake.sent == []
    assert db.settings_get(conn, "paused") is None


@pytest.mark.anyio
async def test_message_without_text_is_ignored(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, with_text=False), _msg(2, "   ")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.calls) >= 2)

    assert fake.sent == []


@pytest.mark.anyio
async def test_pause_sets_setting_replies_and_pokes(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/pause")]])
    poke = asyncio.Event()
    updater = _updater(fake, conn, settings, poke_event=poke)

    await _drive(fake, updater, lambda: len(fake.sent) >= 1)

    assert db.settings_get(conn, "paused") == "1"
    assert fake.sent == [(CHAT_ID, "Forwarding paused.")]
    assert poke.is_set()
    assert updater.last_update_id == 1


@pytest.mark.anyio
async def test_pause_accepts_case_and_bot_name_suffix(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/PAUSE@relay_bot")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert db.settings_get(conn, "paused") == "1"
    assert fake.sent == [(CHAT_ID, "Forwarding paused.")]


@pytest.mark.anyio
async def test_resume_moves_received_returns_count_and_pokes(conn, settings):
    _seed(conn, "resume0001-aaaa", state="received", body="alpha")
    _seed(conn, "resume0002-bbbb", state="received", body="bravo")
    db.settings_set(conn, "paused", "1")
    fake = FakeTelegram(script=[[_msg(1, "/resume")]])
    poke = asyncio.Event()
    updater = _updater(fake, conn, settings, poke_event=poke)

    await _drive(fake, updater, lambda: len(fake.sent) >= 1)

    assert db.settings_get(conn, "paused") == "0"
    assert db.counts_by_state(conn) == {"pending": 2}
    assert len(db.next_jobs(conn, int(time.time()) + 60, limit=10)) == 2
    assert fake.sent == [(CHAT_ID, "Forwarding resumed. Resumed 2 messages.")]
    assert poke.is_set()


@pytest.mark.anyio
async def test_status_reply_is_counts_only(conn, settings):
    _seed(conn, "status0001-aaaa", body="pending-body")
    _seed(conn, "status0002-bbbb", state="received", body="received-body")
    fake = FakeTelegram(script=[[_msg(1, "/status")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    text = fake.sent[0][1]
    assert "paused: no" in text
    assert "pending: 1" in text
    assert "received: 1" in text
    assert "delivered: 0" in text
    assert "queue: 1" in text
    assert "pending-body" not in text
    assert "received-body" not in text


@pytest.mark.anyio
async def test_status_reply_after_pause(conn, settings):
    db.settings_set(conn, "paused", "1")
    fake = FakeTelegram(script=[[_msg(1, "/status")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert "paused: yes" in fake.sent[0][1]
    assert "queue: 0" in fake.sent[0][1]


@pytest.mark.anyio
async def test_replies_go_to_sender_chat_not_configured_chat(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/status", chat_id=OTHER_CHAT_ID)]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert fake.sent[0][0] == OTHER_CHAT_ID


@pytest.mark.anyio
async def test_filters_show_empty_lists(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/filters")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert fake.sent == [(CHAT_ID, "Filters:\nallow: (none)\nblock: (none)\n"
                                  "keyword: (none)")]


@pytest.mark.anyio
async def test_filters_add_strips_at_and_keeps_keyword_as_is(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/filters block @SPAM"),
                                 _msg(2, "/filters allow @BETA"),
                                 _msg(3, "/filters keyword PayPal"),
                                 _msg(4, "/filters keyword @weird")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 4)

    assert db.filters_all(conn) == {"allow": {"BETA"}, "block": {"SPAM"},
                                    "keyword": {"PayPal", "@weird"}}
    assert all(text.startswith("Filter added") for _, text in fake.sent)


@pytest.mark.anyio
async def test_filters_remove_each_kind(conn, settings):
    db.filters_add(conn, "block", "SPAM")
    db.filters_add(conn, "allow", "X")
    db.filters_add(conn, "keyword", "K")
    fake = FakeTelegram(script=[[_msg(1, "/filters unblock @SPAM"),
                                 _msg(2, "/filters unallow X"),
                                 _msg(3, "/filters unkeyword K")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 3)

    assert db.filters_all(conn) == {"allow": set(), "block": set(),
                                    "keyword": set()}
    assert all(text == "Filter removed." for _, text in fake.sent)


@pytest.mark.anyio
async def test_filters_unblock_empty_after_strip_returns_usage_hint(
        conn, settings):
    db.filters_add(conn, "block", "SPAM")
    fake = FakeTelegram(script=[[_msg(1, "/filters unblock @")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert fake.sent == [(CHAT_ID, FILTERS_USAGE)]
    assert db.filters_all(conn) == {"allow": set(), "block": {"SPAM"},
                                    "keyword": set()}


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["/filters bright X", "/filters block",
                                  "/filters unblock", "/filters keyword"])
async def test_filters_bad_usage_returns_usage_hint(conn, settings, text):
    fake = FakeTelegram(script=[[_msg(1, text)]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert fake.sent == [(CHAT_ID, FILTERS_USAGE)]
    assert db.filters_all(conn) == {"allow": set(), "block": set(),
                                    "keyword": set()}


@pytest.mark.anyio
async def test_fallback_toggles_on_and_off(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/fallback on"),
                                 _msg(2, "/FALLBACK off"),
                                 _msg(3, "/fallback banana")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 3)

    assert fake.sent == [(CHAT_ID, "Fallback enabled."),
                         (CHAT_ID, "Fallback disabled."),
                         (CHAT_ID, FALLBACK_USAGE)]
    assert db.settings_get(conn, "fallback_enabled") == "0"


@pytest.mark.anyio
async def test_phoneb_valid_sets_masked_reply(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/phoneb +15551234567")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert db.settings_get(conn, "phone_b") == "+15551234567"
    assert fake.sent == [(CHAT_ID, "Phone B updated: +****4567")]


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["/phoneb", "/phoneb 1234",
                                  "/phoneb +123x99", "/phoneb +123456789012345678901234",
                                  "/phoneb +1 2345"])
async def test_phoneb_invalid_returns_usage_hint(conn, settings, text):
    fake = FakeTelegram(script=[[_msg(1, text)]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert fake.sent == [(CHAT_ID, PHONEB_USAGE)]
    assert db.settings_get(conn, "phone_b") is None


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["/start", "/help", "/HELP", "/help@relay_bot"])
async def test_start_and_help_reply_static_text(conn, settings, text):
    fake = FakeTelegram(script=[[_msg(1, text)]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert fake.sent == [(CHAT_ID, HELP_TEXT)]
    assert "/status" in HELP_TEXT
    assert "/pause" in HELP_TEXT
    assert "/resume" in HELP_TEXT
    assert "/filters" in HELP_TEXT
    assert "shell" in HELP_TEXT


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["/frobnicate", "/", "hello there"])
async def test_unknown_messages_get_one_line_hint(conn, settings, text):
    fake = FakeTelegram(script=[[_msg(1, text)]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.sent) >= 1)

    assert fake.sent == [(CHAT_ID, UNKNOWN_HINT)]


@pytest.mark.anyio
async def test_poll_survives_tg_error_and_tracks_offset(conn, settings):
    fake = FakeTelegram(script=[TGError("timeout"), [_msg(1, "/status")],
                                [_msg(2, "/pause")]])
    updater = _updater(fake, conn, settings, poll_timeout=0.05)

    await _drive(fake, updater,
                 lambda: len(fake.calls) >= 4 and len(fake.sent) >= 2)

    assert [call["offset"] for call in fake.calls[:4]] == [None, None, 2, 3]
    assert fake.calls[0]["timeout"] == 0.05
    assert db.settings_get(conn, "paused") == "1"


@pytest.mark.anyio
async def test_poll_survives_unexpected_error_and_logs_kind_only(conn, settings,
                                                                 monkeypatch,
                                                                 caplog):
    monkeypatch.setattr("app.updater.PARSE_ERROR_SLEEP", 0.05)
    fake = FakeTelegram(script=[RuntimeError("boom-secret"), [_msg(1, "/help")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.calls) >= 3)

    assert fake.sent == [(CHAT_ID, HELP_TEXT)]
    messages = [record.getMessage() for record in caplog.records
                if record.name == "app.updater"]
    assert any("RuntimeError" in message for message in messages)
    assert all("boom-secret" not in message for message in messages)
    assert PARSE_ERROR_SLEEP == 5.0


@pytest.mark.anyio
async def test_reply_failure_does_not_kill_cycle(conn, settings):
    fake = FailingTelegram(script=[[_msg(1, "/pause"), _msg(2, "/resume")]])

    await _drive(fake, _updater(fake, conn, settings, poll_timeout=0.05),
                 lambda: len(fake.calls) >= 3)

    assert db.settings_get(conn, "paused") == "0"
    assert fake.sent == []


@pytest.mark.anyio
async def test_run_terminates_cleanly_on_stop(conn, settings):
    fake = FakeTelegram(script=[[_msg(1, "/status")]])

    await _drive(fake, _updater(fake, conn, settings),
                 lambda: len(fake.calls) >= 2)

    assert "paused: no" in fake.sent[0][1]
    assert fake.sent[0][0] == CHAT_ID
    assert len(fake.sent) == 1
