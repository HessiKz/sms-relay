"""Where a command ends up: the device of the source, or the default one."""

import time

import pytest

import app.db as db
from app.updater import Updater
from tests.device_support import DEVICE_ID, SECOND_DEVICE_ID, seed_device

ADMIN_ID = 7
CHAT_ID = 555


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def get_updates(self, offset, timeout):
        return []

    async def send_to_chat(self, chat_id, text, reply_to=None, keyboard=None):
        self.sent.append((chat_id, text, keyboard))
        return {"ok": True, "message_id": len(self.sent)}


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(str(tmp_path / "routing.db"))
    seed_device(connection, DEVICE_ID)
    yield connection
    connection.close()


def _updater(conn, settings, fake):
    return Updater(conn, settings, fake)


def _msg(update_id, text=None, reply_to_message=None):
    message = {"message_id": update_id, "chat": {"id": CHAT_ID, "type": "private"},
               "from": {"id": ADMIN_ID}}
    if text is not None:
        message["text"] = text
    if reply_to_message is not None:
        message["reply_to_message"] = reply_to_message
    return {"update_id": update_id, "message": message}


def _second_device(conn):
    seed_device(conn, SECOND_DEVICE_ID, "sr_second-token", name="Phone B")


@pytest.mark.anyio
async def test_a_reply_goes_to_the_device_of_the_original_message(conn, settings):
    _second_device(conn)
    db.record_message(conn, {"id": "orig-1", "sender": "+15551230000",
                             "body": "hello there",
                             "received_at": int(time.time()),
                             "state": "pending", "device_id": SECOND_DEVICE_ID})
    db.update_state(conn, "orig-1", "delivered", 1, int(time.time()),
                    telegram_msg_id="4242")
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "sure", reply_to_message={"message_id": 4242}))

    commands = db.pending_commands(conn, SECOND_DEVICE_ID)
    assert len(commands) == 1
    assert commands[0]["target"] == "+15551230000"
    assert commands[0]["device_id"] == SECOND_DEVICE_ID
    assert db.pending_commands(conn, DEVICE_ID) == []


@pytest.mark.anyio
async def test_a_reply_to_a_revoked_device_queues_nothing(conn, settings):
    db.record_message(conn, {"id": "orig-1", "sender": "+15551230000",
                             "body": "hello there",
                             "received_at": int(time.time()),
                             "state": "pending", "device_id": DEVICE_ID})
    db.update_state(conn, "orig-1", "delivered", 1, int(time.time()),
                    telegram_msg_id="4242")
    db.revoke_device(conn, DEVICE_ID, int(time.time()))
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "sure", reply_to_message={"message_id": 4242}))

    assert db.pending_commands(conn, DEVICE_ID) == []
    assert fake.sent
    assert "not paired any more" in fake.sent[-1][1]


@pytest.mark.anyio
async def test_a_reply_to_the_archived_device_queues_nothing(conn, settings):
    db.record_message(conn, {"id": "orig-1", "sender": "+15551230000",
                             "body": "hello there",
                             "received_at": int(time.time()),
                             "state": "pending", "device_id": "legacy"})
    db.update_state(conn, "orig-1", "delivered", 1, int(time.time()),
                    telegram_msg_id="4242")
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "sure", reply_to_message={"message_id": 4242}))

    assert db.pending_commands(conn, "legacy") == []
    assert fake.sent
    assert "not paired any more" in fake.sent[-1][1]


@pytest.mark.anyio
async def test_a_reply_resolves_the_name_of_the_device(conn, settings):
    seed_device(conn, DEVICE_ID)
    db.replace_device_contacts(conn, DEVICE_ID,
                               [{"number": "+15551230000", "name": "Alice"}])
    db.record_message(conn, {"id": "orig-1", "sender": "Alice",
                             "body": "hello there",
                             "received_at": int(time.time()),
                             "state": "pending", "device_id": DEVICE_ID})
    db.update_state(conn, "orig-1", "delivered", 1, int(time.time()),
                    telegram_msg_id="4242")
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "sure", reply_to_message={"message_id": 4242}))

    commands = db.pending_commands(conn, DEVICE_ID)
    assert len(commands) == 1
    assert commands[0]["target"] == "+15551230000"


@pytest.mark.anyio
async def test_sms_without_a_device_explains_itself(conn, settings):
    db.revoke_device(conn, DEVICE_ID, int(time.time()))
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms +15559876543 meet me at 7"))
    assert db.pending_commands(conn, DEVICE_ID) == []
    assert fake.sent
    assert "No device is paired" in fake.sent[-1][1]


@pytest.mark.anyio
async def test_sms_with_several_devices_needs_a_default(conn, settings):
    _second_device(conn)
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms +15559876543 meet me at 7"))
    assert db.pending_commands(conn, DEVICE_ID) == []
    assert db.pending_commands(conn, SECOND_DEVICE_ID) == []
    assert fake.sent
    assert "Several devices are paired" in fake.sent[-1][1]


@pytest.mark.anyio
async def test_sms_uses_the_default_device(conn, settings):
    _second_device(conn)
    db.settings_set(conn, "default_device_id", SECOND_DEVICE_ID)
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms +15559876543 meet me at 7"))
    assert db.pending_commands(conn, DEVICE_ID) == []
    commands = db.pending_commands(conn, SECOND_DEVICE_ID)
    assert len(commands) == 1
    assert commands[0]["target"] == "+15559876543"
    assert commands[0]["device_id"] == SECOND_DEVICE_ID


@pytest.mark.anyio
async def test_sms_with_one_device_uses_that_device(conn, settings):
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms +15559876543 meet me at 7"))
    commands = db.pending_commands(conn, DEVICE_ID)
    assert len(commands) == 1
    assert commands[0]["device_id"] == DEVICE_ID
