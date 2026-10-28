"""Tests for the SMS command queue, reply handling and de-duplication."""

import time
from dataclasses import replace

import pytest

import app.db as db
import app.updater as updater_module
from app.updater import SMS_USAGE, Updater

ADMIN_ID = 7
CHAT_ID = 555


class FakeTelegram:
    def __init__(self, script=None):
        self._script = list(script or [])
        self.sent = []
        self.updates = []

    async def get_updates(self, offset, timeout):
        self.updates.append({"offset": offset})
        item = self._script.pop(0) if self._script else []
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_to_chat(self, chat_id, text, reply_to=None, keyboard=None):
        self.sent.append((chat_id, text, keyboard))
        return {"ok": True, "message_id": len(self.sent)}


def _msg(update_id, text=None, *, user=ADMIN_ID, chat_type="private",
         chat_id=CHAT_ID, reply_to_message=None):
    message = {"message_id": update_id,
               "chat": {"id": chat_id, "type": chat_type}}
    if user is not None:
        message["from"] = {"id": user}
    if text is not None:
        message["text"] = text
    if reply_to_message is not None:
        message["reply_to_message"] = reply_to_message
    return {"update_id": update_id, "message": message}


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(str(tmp_path / "commands.db"))
    yield connection
    connection.close()


def _updater(conn, settings, fake):
    return Updater(conn, settings, fake)


async def _drain(updater, fake, expected=1):
    """Run one update cycle by feeding a single update."""
    await updater._process_all(fake.updates if fake.updates else [])


@pytest.mark.anyio
async def test_reply_to_relayed_message_creates_sms_command(conn, settings):
    """Replying in Telegram to a forwarded SMS must queue an SMS back."""
    # A message that was delivered to Telegram, with the id the bot used.
    db.record_message(conn, {"id": "orig-1", "sender": "+15551230000",
                             "body": "hello there", "received_at": int(time.time()),
                             "state": "pending"})
    db.update_state(conn, "orig-1", "delivered", 1, int(time.time()),
                    telegram_msg_id="4242")

    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    update = _msg(1, "sure, here is the answer",
                  reply_to_message={"message_id": 4242})
    await updater._process(update)

    commands = db.pending_commands(conn)
    assert len(commands) == 1
    assert commands[0]["target"] == "+15551230000"
    assert commands[0]["body"] == "sure, here is the answer"
    assert commands[0]["source"] == "telegram_reply"
    assert any("goes out as an SMS" in text for _, text, _ in fake.sent)


@pytest.mark.anyio
async def test_reply_to_unknown_message_is_not_a_command(conn, settings):
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "hello", reply_to_message={"message_id": 999}))
    assert db.pending_commands(conn) == []


@pytest.mark.anyio
async def test_sms_command_queues_any_number(conn, settings):
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms +15559876543 meet me at 7"))
    commands = db.pending_commands(conn)
    assert len(commands) == 1
    assert commands[0]["target"] == "+15559876543"
    assert commands[0]["body"] == "meet me at 7"
    assert commands[0]["source"] == "telegram_sms"


@pytest.mark.anyio
async def test_sms_command_requires_number_and_text(conn, settings):
    """/sms alone opens the menu; a bad target explains itself."""
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms"))
    assert db.pending_commands(conn) == []
    assert fake.sent
    assert "Send an SMS" in fake.sent[0][1]

    fake2 = FakeTelegram()
    updater2 = _updater(conn, settings, fake2)
    await updater2._process(_msg(2, "/sms not-a-number hello"))
    assert db.pending_commands(conn) == []
    assert fake2.sent and "neither a contact nor a phone number" in fake2.sent[0][1]


@pytest.mark.anyio
async def test_sms_command_from_stranger_is_ignored(conn, settings):
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms +15559876543 let me in", user=999))
    assert db.pending_commands(conn) == []
    assert fake.sent == []


def test_command_lifecycle(conn):
    db.create_command(conn, "cmd-1", "+15551230000", "hi", "telegram_sms")
    pending = db.pending_commands(conn)
    assert len(pending) == 1 and pending[0]["id"] == "cmd-1"
    db.finish_command(conn, "cmd-1", "sent", "ok")
    assert db.pending_commands(conn) == []
    stored = db.command(conn, "cmd-1")
    assert stored["status"] == "sent" and stored["result"] == "ok"


def test_pending_commands_are_bounded(conn):
    for index in range(30):
        db.create_command(conn, f"cmd-{index}", "+15551230000", "hi", "telegram_sms")
    assert len(db.pending_commands(conn, limit=20)) == 20


def test_find_recent_duplicate_detects_the_same_message(conn):
    now = int(time.time())
    db.record_message(conn, {"id": "a", "sender": "+1555", "body": "same",
                             "received_at": now, "state": "pending",
                             "source": "notification"})
    found = db.find_recent_duplicate(conn, "+1555", "same", now)
    assert found is not None and found["id"] == "a"
    assert db.find_recent_duplicate(conn, "+1555", "different", now) is None
    assert db.find_recent_duplicate(conn, "+1555", "same", now + 5000) is None


def test_same_source_is_not_a_duplicate(conn):
    """A genuine repeat through the same capture path is a new message."""
    now = int(time.time())
    db.record_message(conn, {"id": "a", "sender": "+1555", "body": "same",
                             "received_at": now, "state": "pending",
                             "source": "sms"})
    assert db.find_recent_duplicate(conn, "+1555", "same", now,
                                    source="sms") is None
    assert db.find_recent_duplicate(conn, "+1555", "same", now,
                                    source="notification") is not None


def test_find_by_telegram_id(conn):
    db.record_message(conn, {"id": "b", "sender": "+1555", "body": "x",
                             "received_at": int(time.time()), "state": "pending"})
    db.update_state(conn, "b", "delivered", 1, int(time.time()),
                    telegram_msg_id="77")
    found = db.find_by_telegram_id(conn, 77)
    assert found is not None and found["id"] == "b"
    assert db.find_by_telegram_id(conn, 78) is None


def test_sender_normalisation_makes_two_spellings_equal():
    assert db.normalize_sender("0912 000 0001") == "+989120000001"
    assert db.normalize_sender("+989120000001") == "+989120000001"
    assert db.normalize_sender("00989120000001") == "+989120000001"
    assert db.normalize_sender("989120000001") == "+989120000001"
    assert db.normalize_sender("09120000001") == "+989120000001"


def test_duplicate_is_detected_across_sender_spellings(conn):
    """The broadcast and the notification spell the sender differently."""
    now = int(time.time())
    db.record_message(conn, {"id": "x1", "sender": "+989120000001",
                             "body": "same text", "received_at": now,
                             "state": "pending", "source": "notification"})
    found = db.find_recent_duplicate(conn, "0912 000 0001", "same text", now + 3)
    assert found is not None and found["id"] == "x1"
    # a different number is not a duplicate
    assert db.find_recent_duplicate(conn, "+989120000002", "same text", now) is None


# --- contacts + the guided bot flow -------------------------------------

def test_contacts_are_uploaded_and_searchable(conn, settings):
    db.replace_contacts(conn, [
        {"number": "+989120000002", "name": "Alice"},
        {"number": "+989120000001", "name": "Bob"},
    ])
    assert db.contact_count(conn) == 2
    found = db.search_contacts(conn, "dela")
    assert len(found) == 1 and found[0]["name"] == "Alice"
    assert db.resolve_contact(conn, "Alice") == "+989120000002"


def test_flow_resolves_a_contact_name_to_a_number(conn, settings):
    db.replace_contacts(conn, [{"number": "+989120000002", "name": "Alice"}])
    from app.flow import Flow
    flow = Flow(conn)
    number, name = flow.resolve_target("Alice")
    assert number == "+989120000002" and name == "Alice"


def test_flow_normalizes_local_and_persian_numbers(conn, settings):
    from app.flow import Flow
    flow = Flow(conn)
    assert flow.resolve_target("09120000003")[0] == "+989120000003"
    assert flow.resolve_target("00989120000003")[0] == "+989120000003"
    assert flow.resolve_target("+989120000003")[0] == "+989120000003"
    assert flow.resolve_target("۰۹۱۲۰۰۰۰۰۰۳")[0] == "+989120000003"
    assert flow.resolve_target("not a number")[0] is None


def test_flow_contact_buttons_point_at_the_shown_list(conn, settings):
    from app.flow import Flow
    db.replace_contacts(conn, [
        {"number": "+989120000002", "name": "Alice"},
        {"number": "+989120000001", "name": "Bob"},
    ])
    flow = Flow(conn)
    kb = flow.contact_keyboard("chat-1")
    rows = kb["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "contact:0"
    picked = flow.contact_at("chat-1", 0)
    assert picked["name"] == "Alice"


# --- the button driven flow ---------------------------------------------

def _tap(data, chat_id=CHAT_ID):
    return {"update_id": 9, "callback_query": {
        "id": "cb", "from": {"id": ADMIN_ID},
        "message": {"chat": {"id": chat_id, "type": "private"}},
        "data": data}}


@pytest.mark.anyio
async def test_sms_menu_offers_two_choices(conn, settings):
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms"))
    text, keyboard = fake.sent[-1][1], fake.sent[-1][2]
    assert "Pick a contact" in text
    labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
    assert any("contact" in label.lower() for label in labels)
    assert any("number" in label.lower() for label in labels)


def test_main_menu_offers_the_inbox_only_when_configured(conn, settings):
    from app.flow import Flow
    without = Flow(conn).main_menu()
    labels = [b["text"] for row in without["inline_keyboard"] for b in row]
    assert "\U0001F4E8 Open inbox" not in labels

    configured = Flow(conn, "https://ai-barrow.ir/relay/app/").main_menu()
    first = configured["inline_keyboard"][0][0]
    assert first["text"] == "\U0001F4E8 Open inbox"
    assert first["web_app"]["url"] == "https://ai-barrow.ir/relay/app/"


@pytest.mark.anyio
async def test_start_menu_uses_the_configured_miniapp_url(conn, settings):
    configured = replace(settings, miniapp_url="https://ai-barrow.ir/relay/app/")
    fake = FakeTelegram()
    updater = _updater(conn, configured, fake)

    await updater._process(_msg(1, "/start"))

    keyboard = fake.sent[-1][2]
    button = keyboard["inline_keyboard"][0][0]
    assert button["web_app"]["url"] == "https://ai-barrow.ir/relay/app/"


@pytest.mark.anyio
async def test_button_flow_contact_then_text_queues_the_sms(conn, settings):
    db.replace_contacts(conn, [{"number": "+989120000002", "name": "Alice"}])
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)

    await updater._process(_tap("sms:contacts"))
    buttons = [b["text"] for row in fake.sent[-1][2]["inline_keyboard"] for b in row]
    assert any("Alice" in label for label in buttons)

    await updater._process(_tap("contact:0"))
    assert "Alice" in fake.sent[-1][1]

    await updater._process(_msg(2, "hi there"))
    commands = db.pending_commands(conn)
    assert len(commands) == 1
    assert commands[0]["target"] == "+989120000002"
    assert commands[0]["body"] == "hi there"


@pytest.mark.anyio
async def test_button_flow_custom_number_normalises_local_format(conn, settings):
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)

    await updater._process(_tap("sms:number"))
    await updater._process(_msg(1, "09120000003"))
    await updater._process(_msg(2, "hello"))
    commands = db.pending_commands(conn)
    assert len(commands) == 1
    assert commands[0]["target"] == "+989120000003"


@pytest.mark.anyio
async def test_number_button_answers_a_nonsense_target(conn, settings):
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_tap("sms:number"))
    await updater._process(_msg(1, "not a number"))
    assert db.pending_commands(conn) == []
    assert "does not look like a phone number" in fake.sent[-1][1]


@pytest.mark.anyio
async def test_search_by_name_suggests_contacts(conn, settings):
    db.replace_contacts(conn, [{"number": "+989120000001", "name": "Bob"}])
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_msg(1, "/sms Mam"))
    text, keyboard = fake.sent[-1][1], fake.sent[-1][2]
    assert "Bob" in text
    assert keyboard is not None


@pytest.mark.anyio
async def test_contact_list_can_be_paged(conn, settings):
    """The contact list must offer a way to reach the next page."""
    from app.flow import Flow
    db.replace_contacts(conn, [
        {"number": f"+9893900{i:04d}", "name": f"Person {i:02d}"} for i in range(20)
    ])
    flow = Flow(conn)
    first = flow.contact_keyboard("chat-1")
    labels = [b["text"] for row in first["inline_keyboard"] for b in row]
    assert any("Next" in label for label in labels), labels
    assert any("Person 00" in label for label in labels)

    page2 = flow.contact_keyboard("chat-1", page=1)
    labels2 = [b["text"] for row in page2["inline_keyboard"] for b in row]
    assert any("Person 08" in label for label in labels2)
    assert any("Previous" in label for label in labels2)

    # the button of the second page points at the contact of that page
    index = next(i for i, b in enumerate(page2["inline_keyboard"][0]))
    picked = flow.contact_at("chat-1", 0)
    assert picked["name"] == "Person 08"


@pytest.mark.anyio
async def test_next_page_button_queues_the_right_contact(conn, settings):
    db.replace_contacts(conn, [
        {"number": f"+9893900{i:04d}", "name": f"Person {i:02d}"} for i in range(20)
    ])
    fake = FakeTelegram()
    updater = _updater(conn, settings, fake)
    await updater._process(_tap("sms:contacts"))
    await updater._process(_tap("contacts:1"))
    await updater._process(_tap("contact:0"))
    # the bot now asks for the text
    await updater._process(_msg(3, "hello"))
    commands = db.pending_commands(conn)
    assert len(commands) == 1
    assert commands[0]["target"] == "+98939000008"
    assert commands[0]["body"] == "hello"


# --- Phase 1: command results and the device poll ------------------------

DEVICE_HEADERS = {"Authorization": "Bearer device-token"}


def _queue(client, command_id="cmd-api", **kwargs):
    db.create_command(client.app.state.db_conn, command_id,
                      kwargs.pop("target", "+15551230000"),
                      kwargs.pop("body", "hi"), "miniapp", **kwargs)


def _result(client, command_id, status, detail=None):
    payload = {"status": status}
    if detail is not None:
        payload["detail"] = detail
    return client.post(f"/commands/{command_id}/result", json=payload,
                       headers=DEVICE_HEADERS)


def test_result_endpoint_sent_then_delivered_posts_one_note(client):
    _queue(client)
    fake = FakeTelegram()
    client.app.state.tg = fake

    r = _result(client, "cmd-api", "sent")
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "status": "sent"}
    assert len(fake.sent) == 1 and "SMS sent" in fake.sent[0][1]

    r = _result(client, "cmd-api", "delivered")
    assert r.json() == {"accepted": True, "status": "delivered"}
    assert len(fake.sent) == 1  # delivered is silent

    r = _result(client, "cmd-api", "sent")
    assert r.status_code == 200
    assert r.json() == {"accepted": False, "status": "delivered"}
    assert len(fake.sent) == 1

    r = _result(client, "cmd-api", "delivered")
    assert r.json() == {"accepted": False, "status": "delivered"}
    stored = db.command(client.app.state.db_conn, "cmd-api")
    assert stored["status"] == "delivered"
    assert stored["finished_at"] is not None


def test_result_endpoint_accepts_delivered_before_sent(client):
    _queue(client)
    fake = FakeTelegram()
    client.app.state.tg = fake

    assert _result(client, "cmd-api", "delivered").json() == {
        "accepted": True, "status": "delivered"}
    assert fake.sent == []
    assert _result(client, "cmd-api", "sent").json() == {
        "accepted": False, "status": "delivered"}
    assert fake.sent == []


def test_result_endpoint_failed_and_rejected_post_one_note(client):
    _queue(client, "cmd-fail")
    _queue(client, "cmd-reject")
    fake = FakeTelegram()
    client.app.state.tg = fake

    assert _result(client, "cmd-fail", "failed", "boom").json() == {
        "accepted": True, "status": "failed"}
    assert any("failed" in text and "boom" in text
               for _, text, _ in fake.sent)
    assert _result(client, "cmd-reject", "rejected").json() == {
        "accepted": True, "status": "rejected"}
    assert any("sending is disabled" in text for _, text, _ in fake.sent)
    assert len(fake.sent) == 2


def test_result_endpoint_unknown_id_404(client):
    assert _result(client, "missing", "sent").status_code == 404


def test_result_endpoint_rejects_unknown_status(client):
    _queue(client)
    r = _result(client, "cmd-api", "pending")
    assert r.status_code == 422
    assert db.command(client.app.state.db_conn, "cmd-api")["status"] == "pending"


def test_command_poll_returns_sub_id_and_thread_key(client):
    _queue(client, "cmd-poll", target="09121234567", sub_id=3,
           client_ref="ref-poll")
    data = client.get("/commands", headers=DEVICE_HEADERS).json()
    assert len(data["commands"]) == 1
    command = data["commands"][0]
    assert command["id"] == "cmd-poll"
    assert command["sub_id"] == 3
    assert command["thread_key"] == "989121234567"
    assert command["target"] == "09121234567"
