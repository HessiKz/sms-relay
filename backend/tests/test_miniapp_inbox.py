"""Phase 3: read-only Mini App inbox API."""

import json
import time

import pytest

import app.db as db
import app.phones as phones
from app import api, rev
from tests.miniapp_support import headers
from tests.device_support import DEVICE_ID, device_headers

DEVICE_HEADERS = device_headers()
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
    response = client.get(f"/app/api/devices/{DEVICE_ID}/chats", params=params, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()["chats"]


def _chat(client, key):
    for chat in _chats(client):
        if chat["thread_key"] == key:
            return chat
    return None


@pytest.mark.parametrize("method,path,kwargs", [
    ("get", "/app/api/me", {}),
    ("get", f"/app/api/devices/{DEVICE_ID}/chats", {}),
    ("get", f"/app/api/devices/{DEVICE_ID}/chats/989121234567/messages", {}),
    ("post", f"/app/api/devices/{DEVICE_ID}/chats/989121234567/read", {"json": {"until_ts": 0}}),
    ("get", f"/app/api/devices/{DEVICE_ID}/contacts", {}),
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
    response = client.post(f"/app/api/devices/{DEVICE_ID}/chats/989121234567/read",
                           json=["not", "an", "object"], headers=ADMIN)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "validation_error"


def test_query_and_limit_bounds_are_validated(client):
    assert client.get(f"/app/api/devices/{DEVICE_ID}/chats", params={"q": "x" * 101},
                      headers=ADMIN).status_code == 422
    assert client.get(f"/app/api/devices/{DEVICE_ID}/contacts", params={"q": "x" * 101},
                      headers=ADMIN).status_code == 422
    assert client.get(f"/app/api/devices/{DEVICE_ID}/chats", params={"limit": 201},
                      headers=ADMIN).status_code == 422
    assert client.get(f"/app/api/devices/{DEVICE_ID}/contacts", params={"limit": 101},
                      headers=ADMIN).status_code == 422
    assert client.get(f"/app/api/devices/{DEVICE_ID}/chats/989121234567/messages",
                      params={"limit": 201}, headers=ADMIN).status_code == 422


def test_timeline_merges_directions_paginates_and_404s(client):
    now = _now()
    _send(client, "m1", body="one", ts=now - 300)
    _send(client, "m2", body="two", ts=now - 200)
    _send(client, "m3", body="three", ts=now - 100)
    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-one", "09121234567", "reply one", "test",
                      thread_key=THREAD, device_id=DEVICE_ID)

    response = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
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
    page_two = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                          params={"before_ts": boundary, "limit": 10},
                          headers=ADMIN).json()
    assert [event["id"] for event in page_two["events"]] == [
        "msg:m2", "msg:m1"]
    assert page_two["has_more"] is False
    assert all(event["ts"] < boundary for event in page_two["events"])

    missing = client.get(f"/app/api/devices/{DEVICE_ID}/chats/000000/messages", headers=ADMIN)
    assert missing.status_code == 404
    assert client.post(f"/app/api/devices/{DEVICE_ID}/chats/000000/read",
                       json={"until_ts": 0}, headers=ADMIN).status_code == 404


def _insert_messages(client, count, start_ts, prefix="hist",
                     sender="09121234567", step=1):
    """Insert messages straight into the database: quicker than posting."""
    conn = client.app.state.db_conn
    for index in range(count):
        db.record_message(conn, {
            "id": f"{prefix}-{index}",
            "sender": sender,
            "body": f"body {index}",
            "received_at": start_ts + index * step,
            "state": "received",
            "device_id": DEVICE_ID,
        })
    return conn


def test_timeline_cursor_keeps_the_events_of_the_same_second(client):
    now = _now()
    for index in range(4):
        _send(client, f"same-{index}", body=f"same second {index}", ts=now)

    first = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                       params={"limit": 2}, headers=ADMIN).json()
    assert [event["id"] for event in first["events"]] == [
        "msg:same-3", "msg:same-2"]
    assert first["has_more"] is True

    cursor = first["events"][-1]
    second = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                        params={"limit": 2, "before_ts": cursor["ts"],
                                "before_id": cursor["id"]},
                        headers=ADMIN).json()
    assert [event["id"] for event in second["events"]] == [
        "msg:same-1", "msg:same-0"]
    assert second["has_more"] is False


def test_timestamp_only_cursor_still_works_for_old_clients(client):
    now = _now()
    _send(client, "old-1", body="older", ts=now - 60)
    _send(client, "old-2", body="newer", ts=now - 10)

    page = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                      params={"before_ts": now - 10, "limit": 10},
                      headers=ADMIN).json()
    assert [event["id"] for event in page["events"]] == ["msg:old-1"]


def test_has_more_is_true_at_the_largest_page_size(client):
    now = _now()
    _insert_messages(client, 201, now - 400, prefix="many")

    page = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                      params={"limit": 200}, headers=ADMIN).json()
    assert len(page["events"]) == 200
    assert page["has_more"] is True

    cursor = page["events"][-1]
    rest = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                      params={"limit": 200, "before_ts": cursor["ts"],
                              "before_id": cursor["id"]},
                      headers=ADMIN).json()
    assert len(rest["events"]) == 1
    assert rest["has_more"] is False


def test_loading_older_pages_reaches_the_beginning_of_the_thread(client):
    now = _now()
    _insert_messages(client, 120, now - 500, prefix="walk")

    seen = []
    params = {"limit": 50}
    for _ in range(10):
        page = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                          params=params, headers=ADMIN).json()
        seen.extend(event["id"] for event in page["events"])
        if not page["has_more"]:
            break
        cursor = page["events"][-1]
        params = {"limit": 50, "before_ts": cursor["ts"],
                  "before_id": cursor["id"]}
    else:
        pytest.fail("the history did not end")

    assert len(seen) == 120
    assert len(set(seen)) == 120
    assert seen[0] == "msg:walk-119"
    assert seen[-1] == "msg:walk-0"


def test_the_timeline_holds_inbound_and_outbound_events(client):
    now = _now()
    _insert_messages(client, 2, now - 100, prefix="mixed")
    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-mixed", "09121234567", "answer", "test",
                      thread_key=THREAD, device_id=DEVICE_ID)

    page = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                      params={"limit": 50}, headers=ADMIN).json()
    assert [event["id"] for event in page["events"]] == [
        "cmd:cmd-mixed", "msg:mixed-1", "msg:mixed-0"]
    assert [event["direction"] for event in page["events"]] == [
        "out", "in", "in"]


def test_conversation_list_pages_through_every_conversation(client):
    now = _now()
    conn = client.app.state.db_conn
    for index in range(150):
        db.record_message(conn, {
            "id": f"chat-{index}",
            "sender": f"0912000{index:04d}",
            "body": f"chat {index}",
            "received_at": now - index,
            "state": "received",
            "device_id": DEVICE_ID,
        })

    first = client.get(f"/app/api/devices/{DEVICE_ID}/chats", params={"limit": 100},
                       headers=ADMIN).json()
    assert len(first["chats"]) == 100
    assert first["has_more"] is True

    cursor = first["chats"][-1]
    second = client.get(f"/app/api/devices/{DEVICE_ID}/chats",
                        params={"limit": 100, "before_ts": cursor["last_ts"],
                                "before_key": cursor["thread_key"]},
                        headers=ADMIN).json()
    assert len(second["chats"]) == 50
    assert second["has_more"] is False

    keys = [chat["thread_key"] for chat in first["chats"]]
    keys += [chat["thread_key"] for chat in second["chats"]]
    assert len(set(keys)) == 150


def test_conversation_list_cursor_without_a_key_does_not_repeat_a_row(client):
    now = _now()
    conn = client.app.state.db_conn
    # Three conversations that end in the same second.
    for index in range(3):
        db.record_message(conn, {
            "id": f"same-{index}",
            "sender": f"0912123456{index}",
            "body": f"chat {index}",
            "received_at": now,
            "state": "received",
            "device_id": DEVICE_ID,
        })

    first = client.get(f"/app/api/devices/{DEVICE_ID}/chats", params={"limit": 2},
                       headers=ADMIN).json()
    assert len(first["chats"]) == 2
    cursor = first["chats"][-1]

    # The timestamp alone is the strict cursor: the rows of that second
    # are left behind, so no conversation is handed out twice.
    strict = client.get(f"/app/api/devices/{DEVICE_ID}/chats",
                        params={"limit": 10, "before_ts": cursor["last_ts"]},
                        headers=ADMIN).json()
    assert strict["chats"] == []
    assert strict["has_more"] is False

    # With the key the page continues inside the same second.
    keyed = client.get(f"/app/api/devices/{DEVICE_ID}/chats",
                       params={"limit": 10, "before_ts": cursor["last_ts"],
                               "before_key": cursor["thread_key"]},
                       headers=ADMIN).json()
    assert [chat["thread_key"] for chat in keyed["chats"]] == [
        "989121234562"]
    assert keyed["has_more"] is False


def test_cursor_parameters_are_validated(client):
    _send(client, "cursor-1", body="one")
    assert client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                      params={"before_id": "msg:cursor-1"},
                      headers=ADMIN).status_code == 422
    assert client.get(f"/app/api/devices/{DEVICE_ID}/chats", params={"before_key": "989121234567"},
                      headers=ADMIN).status_code == 422
    assert client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
                      params={"before_id": "x" * 200, "before_ts": 1},
                      headers=ADMIN).status_code == 422


def test_read_marks_thread_and_later_inbound_is_unread(client):
    now = _now()
    _send(client, "read-1", body="first", ts=now - 60)
    assert _chat(client, THREAD)["unread"] == 1

    wanted = now + 100000
    response = client.post(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/read",
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
    older = client.post(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/read",
                        json={"until_ts": 0}, headers=ADMIN).json()
    assert older["last_read_at"] == data["last_read_at"]


def test_contacts_endpoint_shapes_and_wildcard_escaping(client):
    _upload_contacts(client, [
        {"number": "09121234567", "name": "Alice"},
        {"number": "09120000001", "name": "under_score"},
        {"number": "09120000002", "name": "100% Hal"},
    ])

    response = client.get(f"/app/api/devices/{DEVICE_ID}/contacts", headers=ADMIN)
    assert response.status_code == 200
    contacts = response.json()["contacts"]
    assert [contact["name"] for contact in contacts] == [
        "100% Hal", "Alice", "under_score"]
    alice = contacts[1]
    assert alice["number"] == "09121234567"
    assert alice["number_key"] == THREAD
    assert alice["thread_key"] == THREAD
    assert alice["display"] == "+989121234567"

    percent = client.get(f"/app/api/devices/{DEVICE_ID}/contacts", params={"q": "%"},
                         headers=ADMIN).json()["contacts"]
    assert [contact["name"] for contact in percent] == ["100% Hal"]

    underscore = client.get(f"/app/api/devices/{DEVICE_ID}/contacts", params={"q": "_"},
                            headers=ADMIN).json()["contacts"]
    assert [contact["name"] for contact in underscore] == ["under_score"]

    limited = client.get(f"/app/api/devices/{DEVICE_ID}/contacts", params={"limit": 1},
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
    # The device list is part of the status, and it never carries the
    # token of a device.
    assert [row["id"] for row in data["devices"]] == [DEVICE_ID]
    assert "token_hash" not in json.dumps(data["devices"])

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
    assert first == {"rev": 0, "changed": False, "threads": []}

    _send(client, "event-1", body="bump")
    after_sms = client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                           headers=ADMIN).json()
    assert after_sms == {
        "rev": 1, "changed": True,
        "threads": [{"device_id": DEVICE_ID, "thread_key": THREAD}]}
    unchanged = client.get("/app/api/events", params={"rev": 1, "timeout": 0},
                           headers=ADMIN).json()
    assert unchanged == {"rev": 1, "changed": False, "threads": []}

    conn = client.app.state.db_conn
    db.create_command(conn, "cmd-event", "09121234567", "out", "test",
                      thread_key=THREAD, device_id=DEVICE_ID)
    client.app.state.tg = None  # no Telegram note during this test
    result = client.post("/commands/cmd-event/result", json={"status": "sent"},
                         headers=DEVICE_HEADERS)
    assert result.json() == {"accepted": True, "status": "sent"}
    after_result = client.get("/app/api/events",
                              params={"rev": 1, "timeout": 0},
                              headers=ADMIN).json()
    assert after_result == {
        "rev": 2, "changed": True,
        "threads": [{"device_id": DEVICE_ID, "thread_key": THREAD}]}
    assert client.get("/app/api/events", params={"rev": 2, "timeout": 0},
                      headers=ADMIN).json() == {"rev": 2, "changed": False,
                                               "threads": []}


def test_events_report_the_conversation_that_changed(client):
    _send(client, "scope-1", body="one", sender="09121234567")
    _send(client, "scope-2", body="two", sender="09120000009")

    payload = client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                          headers=ADMIN).json()
    assert payload["changed"] is True
    assert sorted(payload["threads"], key=lambda item: item["thread_key"]) == [
        {"device_id": DEVICE_ID, "thread_key": "989120000009"},
        {"device_id": DEVICE_ID, "thread_key": "989121234567"},
    ]


def test_events_with_an_unknown_scope_ask_for_a_full_reload(client):
    # A change without a device is global: the scope of the change is
    # unknown and the client has to reload everything.
    conn = client.app.state.db_conn
    db.record_message(conn, {"id": "scope-import", "sender": "09121234567",
                             "body": "imported", "received_at": _now(),
                             "source": "import", "state": "received"})
    rev.bump_rev(client.app)

    payload = client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                          headers=ADMIN).json()
    assert payload["changed"] is True
    assert payload["threads"] is None


def test_events_with_a_thread_but_without_a_device_are_unknown(client):
    # A thread belongs to a device, so a scope without a device cannot be
    # resolved and the client has to reload everything.
    rev.bump_rev(client.app, None, THREAD)

    payload = client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                          headers=ADMIN).json()
    assert payload["changed"] is True
    assert payload["threads"] is None


def test_events_report_a_device_wide_change(client):
    # An import touches every conversation of the device.
    rev.bump_rev(client.app, DEVICE_ID, None)

    payload = client.get("/app/api/events", params={"rev": 0, "timeout": 0},
                          headers=ADMIN).json()
    assert payload["threads"] == [{"device_id": DEVICE_ID,
                                    "thread_key": None}]


def test_events_with_a_stale_revision_asks_for_a_reload(client):
    # The counter lives in memory: after a restart the client is ahead of
    # the server and must not wait for a change that already happened.
    _send(client, "stale-1", body="one")
    ahead = client.get("/app/api/events", params={"rev": 99, "timeout": 0},
                        headers=ADMIN).json()
    assert ahead["changed"] is True
    assert ahead["threads"] is None


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
                      headers=ADMIN).json() == {"rev": 1, "changed": False,
                                               "threads": []}


def test_message_text_is_returned_verbatim_as_json(client):
    probe = '<img src=x onerror=alert(1)>"><script>alert(2)</script>'
    _send(client, "xss-1", body=probe, ts=_now())

    response = client.get(f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages", headers=ADMIN)
    assert response.headers["content-type"].startswith("application/json")
    events = response.json()["events"]
    assert events[0]["body"] == probe
    assert _chat(client, THREAD)["last_body"] == probe


def test_the_device_less_routes_are_gone(client):
    """The old, mixed routes answer with 410 and never with data."""
    _send(client, "gone-1", body="hello")
    gone = [
        ("get", "/app/api/chats", {}),
        ("get", "/app/api/chats/989121234567/messages", {}),
        ("post", "/app/api/chats/989121234567/messages",
         {"json": {"body": "hi", "client_ref": "ref-gone-01"}}),
        ("post", "/app/api/chats/989121234567/read", {"json": {"until_ts": 0}}),
        ("get", "/app/api/contacts", {}),
    ]
    for method, path, kwargs in gone:
        response = getattr(client, method)(path, headers=ADMIN, **kwargs)
        assert response.status_code == 410, path
        assert response.json()["detail"]["error"] == "moved"


def test_an_unknown_device_is_404(client):
    _send(client, "unknown-1", body="hello")
    missing = "/app/api/devices/nope"
    assert client.get(f"{missing}/chats", headers=ADMIN).status_code == 404
    assert client.get(f"{missing}/chats/989121234567/messages",
                      headers=ADMIN).status_code == 404
    assert client.get(f"{missing}/contacts", headers=ADMIN).status_code == 404
    assert client.post(f"{missing}/chats/989121234567/read",
                       json={"until_ts": 0}, headers=ADMIN).status_code == 404
    assert client.post(f"{missing}/chats/989121234567/messages",
                       json={"body": "hi", "client_ref": "ref-unknown-1"},
                       headers=ADMIN).status_code == 404
