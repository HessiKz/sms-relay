"""Phase 3: read-only Mini App inbox API."""

import json
import time

import pytest

import app.db as db
import app.phones as phones
from app import api
from tests.miniapp_support import headers

DEVICE_HEADERS = {"Authorization": "Bearer device-token"}
ADMIN = headers()
THREAD = "989121234567"  # 09121234567 in international digits


def _now():
    return int(time.time())


def _send(client, msg_id="m1", sender="09121234567", body="hello",
          ts=None, sender_name=None):
    payload = {
        "id": msg_id,
        "sender": sender,
        "body": body,
        "received_at": _now() if ts is None else ts,
    }
    if sender_name is not None:
        payload["sender_name"] = sender_name
    response = client.post("/sms", json=payload, headers=DEVICE_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _upload_contacts(client, contacts):
    response = client.post("/contacts", json={"contacts": contacts},
                           headers=DEVICE_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _chats(client, q=None):
    params = {"q": q} if q is not None else None
    response = client.get("/app/api/chats", params=params, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()["chats"]


def _chat(client, key):
    for chat in _chats(client):
        if chat["thread_key"] == key:
            return chat
    return None


@pytest.mark.parametrize("method,path,kwargs", [
    ("get", "/app/api/me", {}),
    ("get", "/app/api/chats", {}),
    ("get", "/app/api/chats/989121234567/messages", {}),
    ("post", "/app/api/chats/989121234567/read", {"json": {"until_ts": 0}}),
    ("get", "/app/api/contacts", {}),
    ("get", "/app/api/status", {}),
    ("get", "/app/api/events", {"params": {"rev": 0, "timeout": 0}}),
])
def test_every_endpoint_returns_401_without_header(client, method, path,
                                                   kwargs):
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_me_keeps_its_phase_two_shape(client):
    response = client.get("/app/api/me", headers=ADMIN)
    assert response.status_code == 200
    data = response.json()
    assert data["admin"] is True
    assert data["user"] == {"id": 7, "first_name": "Ada Lovelace",
                            "username": "ada"}
    assert isinstance(data["auth_date"], int)


def test_chats_shows_new_sms_with_contact_name_and_unread(client):
    _upload_contacts(client, [{"number": "09121234567", "name": "Alice"}])
    _send(client, body="Hello from Alice", ts=_now() - 10,
          sender_name="Alice Sender")

    chat = _chat(client, THREAD)
    assert chat is not None
    assert chat["display"] == "+989121234567"
    assert chat["name"] == "Alice"  # contact beats sender_name
    assert chat["last_body"] == "Hello from Alice"
    assert chat["last_direction"] == "in"
    assert chat["unread"] == 1
    assert isinstance(chat["last_ts"], int)


def test_local_number_search_finds_the_thread(client):
    _send(client, body="local search", ts=_now())
    assert [chat["thread_key"] for chat in _chats(client, "09121234567")] == [
        THREAD]
    assert [chat["thread_key"] for chat in _chats(client, "0912 123 4567")] == [
        THREAD]
    assert _chats(client, "nosuchquery") == []


def test_chat_search_treats_like_wildcards_literally(client):
    _upload_contacts(client, [{"number": "09120000001", "name": "under_score"}])
    _send(client, "plain", sender="09120000002", body="plain text", ts=_now() - 3)
    _send(client, "pct", sender="09990000000", body="100% done", ts=_now() - 2)
    _send(client, "underscore", sender="09120000001", body="hi", ts=_now() - 1)

    percent = [chat["thread_key"] for chat in _chats(client, "%")]
    assert percent == [phones.thread_key("09990000000")]

    underscore = [chat["thread_key"] for chat in _chats(client, "_")]
    assert underscore == [phones.thread_key("09120000001")]


def test_malformed_json_body_is_422_not_401(client):
    _send(client)
    response = client.post("/app/api/chats/989121234567/read",
                           json=["not", "an", "object"], headers=ADMIN)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "validation_error"


def test_query_and_limit_bounds_are_validated(client):
    assert client.get("/app/api/chats", params={"q": "x" * 101},
                      headers=ADMIN).status_code == 422
    assert client.get("/app/api/contacts", params={"q": "x" * 101},
                      headers=ADMIN).status_code == 422
    assert client.get("/app/api/chats", params={"limit": 201},
                      headers=ADMIN).status_code == 422
    assert client.get("/app/api/contacts", params={"limit": 101},
                      headers=ADMIN).status_code == 422
    assert client.get("/app/api/chats/989121234567/messages",
                      params={"limit": 201}, headers=ADMIN).status_code == 422


def test_timeline_merges_directions_paginates_and_404s(client):
    now = _now()
    _send(client, "m1", body="one", ts=now - 300)
    _send(client, "m2", body="two", ts=now - 200)
    _send(client, "m3", body="three", ts=now - 100)
    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-one", "09121234567", "reply one", "test",
                      thread_key=THREAD)

    response = client.get(f"/app/api/chats/{THREAD}/messages",
                          params={"limit": 2}, headers=ADMIN)
    assert response.status_code == 200
    first = response.json()
    assert first["thread_key"] == THREAD
    assert first["display"] == "+989121234567"
    assert first["name"] == "+989121234567"
    assert [event["id"] for event in first["events"]] == [
        "cmd:cmd-one", "msg:m3"]
    assert first["has_more"] is True

    outbound = first["events"][0]
    assert outbound["direction"] == "out"
    assert outbound["status"] == "pending"
    assert outbound["result"] is None
    assert outbound["sub_id"] == 0
    for key in ("id", "direction", "ts", "body", "status", "result",
                "sub_id", "sender_name"):
        assert key in outbound
    assert first["events"][1]["direction"] == "in"

    boundary = first["events"][-1]["ts"]
    page_two = client.get(f"/app/api/chats/{THREAD}/messages",
                          params={"before_ts": boundary, "limit": 10},
                          headers=ADMIN).json()
    assert [event["id"] for event in page_two["events"]] == [
        "msg:m2", "msg:m1"]
    assert page_two["has_more"] is False
    assert all(event["ts"] < boundary for event in page_two["events"])

    missing = client.get("/app/api/chats/000000/messages", headers=ADMIN)
    assert missing.status_code == 404
    assert client.post("/app/api/chats/000000/read",
                       json={"until_ts": 0}, headers=ADMIN).status_code == 404


def test_read_marks_thread_and_later_inbound_is_unread(client):
    now = _now()
    _send(client, "read-1", body="first", ts=now - 60)
    assert _chat(client, THREAD)["unread"] == 1

    wanted = now + 100000
    response = client.post(f"/app/api/chats/{THREAD}/read",
                           json={"until_ts": wanted}, headers=ADMIN)
    assert response.status_code == 200
    data = response.json()
    assert data["thread_key"] == THREAD
    assert data["last_read_at"] <= _now()  # clamped, not the client value
    assert data["last_read_at"] >= now - 60
    assert _chat(client, THREAD)["unread"] == 0

    _send(client, "read-2", body="second", ts=_now() + 2)
    assert _chat(client, THREAD)["unread"] == 1

    # Reading again with an older cursor never moves the marker backwards.
    older = client.post(f"/app/api/chats/{THREAD}/read",
                        json={"until_ts": 0}, headers=ADMIN).json()
    assert older["last_read_at"] == data["last_read_at"]


def test_contacts_endpoint_shapes_and_wildcard_escaping(client):
    _upload_contacts(client, [
        {"number": "09121234567", "name": "Alice"},
        {"number": "09120000001", "name": "under_score"},
        {"number": "09120000002", "name": "100% Hal"},
    ])

    response = client.get("/app/api/contacts", headers=ADMIN)
    assert response.status_code == 200
    contacts = response.json()["contacts"]
    assert [contact["name"] for contact in contacts] == [
        "100% Hal", "Alice", "under_score"]
    alice = contacts[1]
    assert alice["number"] == "09121234567"
    assert alice["number_key"] == THREAD
    assert alice["thread_key"] == THREAD
    assert alice["display"] == "+989121234567"

    percent = client.get("/app/api/contacts", params={"q": "%"},
                         headers=ADMIN).json()["contacts"]
    assert [contact["name"] for contact in percent] == ["100% Hal"]

    underscore = client.get("/app/api/contacts", params={"q": "_"},
                            headers=ADMIN).json()["contacts"]
    assert [contact["name"] for contact in underscore] == ["under_score"]

    limited = client.get("/app/api/contacts", params={"limit": 1},
                         headers=ADMIN).json()["contacts"]
    assert len(limited) == 1


def test_status_phone_shape_before_and_after_heartbeat(client):
    data = client.get("/app/api/status", headers=ADMIN).json()
    assert data["paused"] is False
    assert data["queue"] == 0
    assert data["counts_by_state"] == {}
    assert data["version"] == api.VERSION
    assert data["phone"] == {
        "last_seen": None, "online": False, "app_version": None,
        "sending": None, "sims": [],
    }

    info = {"id": "phone-a", "version": "1.2.0", "sending": True,
            "sims": [{"sub_id": 1, "slot": 0, "label": "SIM 1"}]}
    client.get("/commands", headers=dict(DEVICE_HEADERS,
                                         **{"X-Device-Info": json.dumps(info)}))

    phone = client.get("/app/api/status", headers=ADMIN).json()["phone"]
    assert phone["last_seen"] is not None
    assert phone["online"] is True
    assert phone["app_version"] == "1.2.0"
    assert phone["sending"] is True
    assert phone["sims"] == [{"sub_id": 1, "slot": 0, "label": "SIM 1"}]


def test_events_revision_bumps_for_sms_and_accepted_result(client):
    first = client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                       headers=ADMIN).json()
    assert first == {"rev": 0, "changed": False}

    _send(client, "event-1", body="bump")
    after_sms = client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                           headers=ADMIN).json()
    assert after_sms == {"rev": 1, "changed": True}
    unchanged = client.get("/app/api/events", params={"rev": 1, "timeout": 0},
                           headers=ADMIN).json()
    assert unchanged == {"rev": 1, "changed": False}

    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-event", "09121234567", "out", "test",
                      thread_key=THREAD)
    client.app.state.tg = None  # no Telegram note during this test
    result = client.post("/commands/cmd-event/result", json={"status": "sent"},
                         headers=DEVICE_HEADERS)
    assert result.json() == {"accepted": True, "status": "sent"}
    after_result = client.get("/app/api/events",
                              params={"rev": 1, "timeout": 0},
                              headers=ADMIN).json()
    assert after_result == {"rev": 2, "changed": True}
    assert client.get("/app/api/events", params={"rev": 2, "timeout": 0},
                      headers=ADMIN).json() == {"rev": 2, "changed": False}


def test_duplicate_and_blocked_messages_do_not_bump_revision(client):
    _send(client, "dup-1", body="once")
    _send(client, "dup-1", body="once")  # same id, ignored insert
    conn = client.app.state.db_conn
    db.filters_add(conn, "block", "+15550001111")
    blocked = client.post("/sms", json={
        "id": "blocked-1", "sender": "+15550001111", "body": "nope",
        "received_at": _now()}, headers=DEVICE_HEADERS)
    assert blocked.json() == {"accepted": False, "reason": "blocked"}
    assert client.get("/app/api/events", params={"rev": 1, "timeout": 0},
                      headers=ADMIN).json() == {"rev": 1, "changed": False}


def test_message_text_is_returned_verbatim_as_json(client):
    probe = '<img src=x onerror=alert(1)>"><script>alert(2)</script>'
    _send(client, "xss-1", body=probe, ts=_now())

    response = client.get(f"/app/api/chats/{THREAD}/messages", headers=ADMIN)
    assert response.headers["content-type"].startswith("application/json")
    events = response.json()["events"]
    assert events[0]["body"] == probe
    assert _chat(client, THREAD)["last_body"] == probe
