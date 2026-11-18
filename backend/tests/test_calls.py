"""The call log: sync, isolation, pagination, the live session and the
timeline of a conversation.

A call belongs to one device, exactly like a message. Every test here
either proves a rule of the sync protocol or proves that a second device
cannot see, overwrite or guess the calls of the first one.
"""

import asyncio
import time
from dataclasses import replace

from fastapi.testclient import TestClient

import app.db as db
from app import rev
from app.main import create_app
from app.tg import TGError
from tests.device_support import (DEVICE_ID, DEVICE_TOKEN, SECOND_DEVICE_ID,
                                  device_headers, pair_device, seed_device)
from tests.miniapp_support import headers as admin_headers

DEVICE_HEADERS = device_headers()
ADMIN = admin_headers()
NUMBER = "09121234567"
THREAD = "989121234567"


def _now() -> int:
    return int(time.time())


# A fixed base for the default item, so a replayed batch is byte for byte
# the same batch instead of drifting with the clock.
_BASE = _now()


def _item(**overrides) -> dict:
    item = {"provider_id": "100", "number": NUMBER, "kind": "incoming",
            "started_at": _BASE - 60, "duration_s": 12, "sub_id": 1,
            "phone_account": "Personal", "is_read": False}
    item.update(overrides)
    return item


def _sync(client, items, headers=DEVICE_HEADERS):
    return client.post("/calls/sync", json={"items": items}, headers=headers)


def _calls(client, device_id=DEVICE_ID, headers=ADMIN, **params):
    response = client.get(f"/app/api/devices/{device_id}/calls",
                          params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _second_device(client) -> str:
    """A second phone, paired through the real flow."""
    return pair_device(client, device_id=SECOND_DEVICE_ID, name="Phone B")


def _event(client, **body):
    payload = {"call_ref": "ref-a", "state": "ringing",
               "direction": "incoming", "number": NUMBER,
               "sim_label": "SIM 1"}
    payload.update(body)
    return client.post("/calls/events", json=payload, headers=DEVICE_HEADERS)


def _act_answer(client, call_ref="ref-a"):
    """Ask the phone to answer the live call, as the Mini App does."""
    return client.post(
        f"/app/api/devices/{DEVICE_ID}/calls/actions",
        json={"call_ref": call_ref, "action": "answer",
              "client_ref": "abcdefgh"},
        headers=ADMIN)


# --- the database helpers -------------------------------------------------


def test_call_log_id_is_deterministic_and_device_scoped(tmp_path):
    # The id helper is pure, so the connection is only there to prove the
    # signature is stable across a real database.
    conn = db.open_db(str(tmp_path / "x.db"))
    assert conn is not None
    item = {"provider_id": "42", "number": NUMBER, "started_at": 1_700_000_000}
    first = db.call_log_id(DEVICE_ID, item)
    assert first == db.call_log_id(DEVICE_ID, dict(item))
    assert first.startswith("call-")
    assert len(first) == len("call-") + 32
    # Two phones routinely reuse the same provider row id.
    assert db.call_log_id(SECOND_DEVICE_ID, item) != first
    # A row without a provider id still has a stable id.
    fallback = {"number": NUMBER, "started_at": 1_700_000_000}
    assert db.call_log_id(DEVICE_ID, fallback) == db.call_log_id(
        DEVICE_ID, dict(fallback))
    assert db.call_log_id(DEVICE_ID, fallback) != first
    # There is no id without a provider id and without a timestamp.
    assert db.call_log_id(DEVICE_ID, {"number": NUMBER}) is None


def test_record_call_items_counts_insert_update_unchanged(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    item = _item()
    assert db.record_call_items(conn, DEVICE_ID, [item]) == {
        "inserted": 1, "updated": 0, "unchanged": 0, "skipped": 0}
    assert db.record_call_items(conn, DEVICE_ID, [item]) == {
        "inserted": 0, "updated": 0, "unchanged": 1, "skipped": 0}
    # A different content_hash is the phone telling us the row changed.
    changed = dict(item, content_hash="hash-2", duration_s=99)
    assert db.record_call_items(conn, DEVICE_ID, [changed]) == {
        "inserted": 0, "updated": 1, "unchanged": 0, "skipped": 0}
    assert db.page_calls(conn, DEVICE_ID)[0]["duration_s"] == 99
    # An identical hash is a replay.
    assert db.record_call_items(conn, DEVICE_ID, [changed]) == {
        "inserted": 0, "updated": 0, "unchanged": 1, "skipped": 0}
    # An invalid row is skipped, never fatal.
    assert db.record_call_items(conn, DEVICE_ID, [
        {"kind": "not-a-kind", "started_at": 1},
        {"kind": "missed"},
    ]) == {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 2}


def test_record_call_items_never_undoes_a_read_marker(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    item = _item(kind="missed")
    db.record_call_items(conn, DEVICE_ID, [item])
    assert db.count_missed_calls(conn, DEVICE_ID) == 1
    assert db.mark_calls_read(conn, DEVICE_ID, _now()) == 1
    assert db.count_missed_calls(conn, DEVICE_ID) == 0
    # The phone still has the row as unread; a re-sync must not reopen it.
    db.record_call_items(conn, DEVICE_ID, [dict(item, content_hash="h2")])
    assert db.count_missed_calls(conn, DEVICE_ID) == 0


def test_record_call_items_never_raises_on_one_hostile_item(tmp_path):
    """A single unusable row must not roll back the rest of the batch."""
    conn = db.open_db(str(tmp_path / "x.db"))
    good = _item()
    hostile = [
        None,
        "not-a-dict",
        [],
        42,
        {"kind": "missed", "started_at": {"nested": 1}},
        {"kind": "missed", "started_at": "x"},
        {"kind": "missed", "started_at": True},
        {"kind": ["missed"], "started_at": _now()},
        {"kind": "missed", "started_at": 10 ** 30},
    ]
    counts = db.record_call_items(conn, DEVICE_ID, [good, *hostile])
    assert counts == {"inserted": 1, "updated": 0, "unchanged": 0,
                      "skipped": len(hostile)}
    assert len(db.page_calls(conn, DEVICE_ID)) == 1


def test_record_call_items_coerces_odd_but_usable_types(tmp_path):
    """Non-string numbers and names are coerced, not treated as fatal."""
    conn = db.open_db(str(tmp_path / "x.db"))
    counts = db.record_call_items(conn, DEVICE_ID, [{
        "provider_id": 7, "number": 989121234567, "kind": "incoming",
        "started_at": _now(), "duration_s": "30", "cached_name": 99,
        "sub_id": None, "phone_account": None, "is_read": "yes",
    }])
    assert counts == {"inserted": 1, "updated": 0, "unchanged": 0,
                      "skipped": 0}
    row = db.page_calls(conn, DEVICE_ID)[0]
    assert row["number"] == "989121234567"
    assert row["cached_name"] == "99"
    assert row["duration_s"] == 0
    assert row["provider_id"] == "7"


def test_record_call_items_bounds_an_unstorable_number(tmp_path):
    """A value SQLite cannot bind is clamped, not an aborted transaction."""
    conn = db.open_db(str(tmp_path / "x.db"))
    counts = db.record_call_items(conn, DEVICE_ID, [
        _item(provider_id="huge", duration_s=10 ** 30),
        _item(provider_id="huge-sub", sub_id=10 ** 30),
        _item(provider_id="huge-raw", raw_type=10 ** 30),
        _item(provider_id="huge-stamp", started_at=10 ** 30),
        _item(provider_id="ok"),
    ])
    assert counts == {"inserted": 4, "updated": 0, "unchanged": 0,
                      "skipped": 1}
    rows = {row["id"]: row for row in db.page_calls(conn, DEVICE_ID)}
    assert len(rows) == 4
    assert {row["duration_s"] for row in rows.values()} == {0, 12}


def test_page_calls_cursor_never_skips_the_same_second(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    now = 1_700_000_000
    items = [{"provider_id": str(index), "number": NUMBER, "kind": "incoming",
              "started_at": now, "duration_s": index}
             for index in range(5)]
    db.record_call_items(conn, DEVICE_ID, items)

    page = db.page_calls(conn, DEVICE_ID, limit=2)
    assert len(page) == 2
    older = db.page_calls(conn, DEVICE_ID, limit=2,
                          before_ts=page[-1]["started_at"],
                          before_id=page[-1]["id"])
    assert len(older) == 2
    oldest = db.page_calls(conn, DEVICE_ID, limit=2,
                           before_ts=older[-1]["started_at"],
                           before_id=older[-1]["id"])
    assert len(oldest) == 1
    seen = [row["id"] for row in page + older + oldest]
    assert len(seen) == len(set(seen)) == 5
    # The timestamp alone is the strict cursor: no row is handed out twice.
    assert db.page_calls(conn, DEVICE_ID, limit=10, before_ts=now) == []


def test_call_name_and_missed_helpers_are_device_scoped(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.replace_device_contacts(conn, DEVICE_ID,
                               [{"number": "+989121234567", "name": "Ali"}])
    db.record_call_items(conn, DEVICE_ID, [_item(kind="missed")])
    assert db.call_name_for_key(conn, DEVICE_ID, THREAD) == "Ali"
    assert db.call_name_for_key(conn, SECOND_DEVICE_ID, THREAD) is None
    assert db.count_missed_calls(conn, DEVICE_ID) == 1
    assert db.count_missed_calls(conn, SECOND_DEVICE_ID) == 0
    # An answered call is not a missed one.
    db.record_call_items(conn, DEVICE_ID, [
        _item(provider_id="101", kind="incoming")])
    assert db.count_missed_calls(conn, DEVICE_ID) == 1
    assert db.mark_calls_read(conn, SECOND_DEVICE_ID, _now()) == 0


# --- the device sync route ------------------------------------------------


def test_sync_inserts_updates_and_reports_unchanged(client):
    assert _sync(client, [_item()]).json() == {
        "inserted": 1, "updated": 0, "unchanged": 0, "skipped": 0}
    assert _sync(client, [_item()]).json() == {
        "inserted": 0, "updated": 0, "unchanged": 1, "skipped": 0}
    assert _sync(client, [_item(content_hash="h2", duration_s=40)]).json() == {
        "inserted": 0, "updated": 1, "unchanged": 0, "skipped": 0}
    rows = _calls(client)["calls"]
    assert len(rows) == 1
    assert rows[0]["duration_s"] == 40
    assert rows[0]["thread_key"] == THREAD
    assert rows[0]["display"] == "+989121234567"
    assert rows[0]["is_read"] is False


def test_sync_batch_bounds(client):
    assert _sync(client, []).status_code == 422
    assert _sync(client, [_item(provider_id=str(i)) for i in range(201)]
                 ).status_code == 422
    # Exactly 200 is allowed.
    response = _sync(client, [_item(provider_id=str(i)) for i in range(200)])
    assert response.status_code == 200, response.text
    assert response.json()["inserted"] == 200


def test_the_body_itself_must_be_usable(client):
    # These are body-level failures: there is nothing to count per item.
    assert client.post("/calls/sync", json={},
                       headers=DEVICE_HEADERS).status_code == 422
    assert client.post("/calls/sync", json=[],
                       headers=DEVICE_HEADERS).status_code == 422
    assert client.post("/calls/sync", json={"items": "nope"},
                       headers=DEVICE_HEADERS).status_code == 422
    assert client.post("/calls/sync", json={"items": []},
                       headers=DEVICE_HEADERS).status_code == 422


def test_one_bad_item_does_not_lose_the_good_ones(client):
    good_one = _item(provider_id="1")
    good_two = _item(provider_id="2", started_at=_BASE - 30)
    bad = _item(provider_id="3", kind="telepathy")
    response = _sync(client, [good_one, bad, good_two])
    assert response.status_code == 200, response.text
    assert response.json() == {"inserted": 2, "updated": 0,
                               "unchanged": 0, "skipped": 1}
    stored = {row["id"] for row in _calls(client)["calls"]}
    assert len(stored) == 2


def test_each_kind_of_bad_item_is_skipped_without_losing_the_rest(client):
    cases = [
        _item(provider_id="bad-kind", kind="telepathy"),
        _item(provider_id="bad-future", started_at=_now() + 86400 + 120),
        _item(provider_id="bad-old", started_at=946684799),
        _item(provider_id="bad-name", cached_name="n" * 200),
        _item(provider_id="bad-number", number="9" * 100),
        _item(provider_id="bad-duration", duration_s=-1),
        _item(provider_id="bad-sub", sub_id=500),
        {"provider_id": "no-started-at", "kind": "missed"},
        "not-even-an-object",
        None,
    ]
    for index, bad in enumerate(cases):
        good = _item(provider_id=f"ok-{index}")
        response = _sync(client, [good, bad])
        assert response.status_code == 200, bad
        body = response.json()
        assert body["skipped"] == 1, bad
        assert body["inserted"] == 1, bad
    stored = _calls(client, limit=200)["calls"]
    assert len(stored) == len(cases)
    assert {row["id"] for row in stored}  # every good row landed


def test_a_rejected_batch_still_answers_200(client):
    response = _sync(client, [_item(kind="telepathy")])
    assert response.status_code == 200
    assert response.json() == {"inserted": 0, "updated": 0,
                               "unchanged": 0, "skipped": 1}
    assert _calls(client)["calls"] == []


def test_sync_rejects_a_blank_body_and_bad_auth(client):
    assert client.post("/calls/sync", json={},
                       headers=DEVICE_HEADERS).status_code == 422
    assert client.post("/calls/sync", json={"items": [_item()]}
                       ).status_code == 401


def test_sync_bumps_the_revision_once_for_one_thread(client):
    before = client.app.state.rev
    _sync(client, [_item(), _item(provider_id="101", started_at=_now() - 30)])
    assert client.app.state.rev > before
    assert rev.changed_threads(client.app, before) == [
        {"device_id": DEVICE_ID, "thread_key": THREAD}]
    # A replayed batch does not move the counter.
    before = client.app.state.rev
    _sync(client, [_item()])
    assert client.app.state.rev == before


def test_sync_of_a_private_number_is_a_device_wide_change(client):
    before = client.app.state.rev
    response = _sync(client, [_item(provider_id="200", number="")])
    assert response.json()["inserted"] == 1
    assert client.app.state.rev > before
    assert rev.changed_threads(client.app, before) == [
        {"device_id": DEVICE_ID, "thread_key": None}]
    row = _calls(client)["calls"][0]
    assert row["thread_key"] is None
    assert row["number"] == ""
    assert row["display"] == ""


def test_a_second_device_cannot_see_or_overwrite_the_calls_of_the_first(
        client):
    _sync(client, [_item()])
    ours = _calls(client)["calls"]
    assert len(ours) == 1

    _second_device(client)
    other_headers = device_headers(pair_device(
        client, device_id="device-c", name="Phone C"))
    # The second phone syncs the same provider id: its own row, not ours.
    assert _sync(client, [_item(duration_s=77)],
                 headers=other_headers).json()["inserted"] == 1
    mine = _calls(client)["calls"]
    assert len(mine) == 1
    assert mine[0]["id"] == ours[0]["id"]
    assert mine[0]["duration_s"] == 12

    theirs = _calls(client, "device-c")["calls"]
    assert len(theirs) == 1
    assert theirs[0]["id"] != ours[0]["id"]
    assert theirs[0]["duration_s"] == 77

    # A missing device id is simply not a device of this relay.
    assert client.get("/app/api/devices/nope/calls",
                      headers=ADMIN).status_code == 404


def test_sync_keeps_the_contact_name_of_the_same_device(client):
    client.post("/contacts", json={"contacts": [
        {"number": "+989121234567", "name": "Ali"}]}, headers=DEVICE_HEADERS)
    _sync(client, [_item()])
    row = _calls(client)["calls"][0]
    assert row["name"] == "Ali"
    assert row["display"] == "+989121234567"


# --- pagination through the API -------------------------------------------


def test_calls_page_cursor_walks_the_whole_list(client):
    now = _now()
    items = [_item(provider_id=str(index), started_at=now - index,
                   content_hash=None)
             for index in range(5)]
    _sync(client, items)

    first = _calls(client, limit=2)
    assert len(first["calls"]) == 2 and first["has_more"] is True
    second = _calls(client, limit=2, before_ts=first["calls"][-1]["started_at"],
                    before_id=first["calls"][-1]["id"])
    assert len(second["calls"]) == 2
    seen = [row["id"] for row in first["calls"] + second["calls"]]
    assert len(seen) == len(set(seen)) == 4
    last = _calls(client, limit=2, before_ts=second["calls"][-1]["started_at"],
                  before_id=second["calls"][-1]["id"])
    assert len(last["calls"]) == 1
    assert last["has_more"] is False


def test_calls_sharing_a_second_are_not_skipped(client):
    now = _now()
    _sync(client, [_item(provider_id=str(index), started_at=now)
                   for index in range(3)])
    page = _calls(client, limit=2)
    assert len(page["calls"]) == 2
    rest = _calls(client, limit=2, before_ts=now, before_id=page["calls"][-1]["id"])
    assert len(rest["calls"]) == 1
    # The timestamp on its own never repeats a row.
    strict = _calls(client, limit=10, before_ts=now)
    assert strict["calls"] == []


def test_calls_query_and_limit_validation(client):
    _sync(client, [_item()])
    assert len(_calls(client, q="0912")["calls"]) == 1
    assert len(_calls(client, q="Ali")["calls"]) == 0
    assert len(_calls(client, limit=1)["calls"]) == 1
    response = client.get(f"/app/api/devices/{DEVICE_ID}/calls",
                          params={"limit": "500"}, headers=ADMIN)
    assert response.status_code == 422
    response = client.get(f"/app/api/devices/{DEVICE_ID}/calls",
                          params={"limit": "0"}, headers=ADMIN)
    assert response.status_code == 422
    response = client.get(f"/app/api/devices/{DEVICE_ID}/calls",
                          params={"before_id": "x"}, headers=ADMIN)
    assert response.status_code == 422
    response = client.get(f"/app/api/devices/{DEVICE_ID}/calls",
                          params={"q": "x" * 101}, headers=ADMIN)
    assert response.status_code == 422
    assert client.get(f"/app/api/devices/{DEVICE_ID}/calls"
                      ).status_code == 401


# --- the live session -----------------------------------------------------


def test_call_events_upsert_one_session_and_bump_the_revision(client):
    before = client.app.state.rev
    assert _event(client).json() == {"ok": True}
    assert client.app.state.rev > before
    assert rev.changed_threads(client.app, before) == [
        {"device_id": DEVICE_ID, "thread_key": None}]

    live = client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()
    assert live["device_id"] == DEVICE_ID
    session = live["call"]
    assert session["call_ref"] == "ref-a"
    assert session["state"] == "ringing"
    assert session["number_key"] == THREAD
    assert session["display"] == "+989121234567"
    assert session["sim_label"] == "SIM 1"
    assert session["ended_at"] is None

    # A mute update merges instead of erasing the caller.
    assert _event(client, state="active", muted=True, number=None).json() == {
        "ok": True}
    live = client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"]
    assert live["state"] == "active"
    assert live["number"] == NUMBER
    assert live["muted"] is True
    assert live["answered_at"] is not None

    assert _event(client, state="disconnected", number=None).json() == {
        "ok": True}
    live = client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"]
    assert live["state"] == "disconnected"
    assert live["ended_at"] is not None


def test_call_event_validation(client):
    assert _event(client, state="dreaming").status_code == 422
    assert _event(client, direction="sideways").status_code == 422
    assert _event(client, call_ref="").status_code == 422
    assert _event(client, call_ref="x" * 65).status_code == 422
    assert _event(client, state="ringing", direction="incoming",
                  started_at=-5).status_code == 422
    assert client.post("/calls/events", json={"state": "ringing"},
                       headers=DEVICE_HEADERS).status_code == 422


def test_call_event_accepts_int_flags(client):
    """The Android client sends 0/1 for the toggles, not JSON booleans."""
    response = client.post("/calls/events", json={
        "call_ref": "ref-int", "state": "active", "direction": "outgoing",
        "number": NUMBER, "muted": 1, "speaker": 0,
    }, headers=DEVICE_HEADERS)
    assert response.status_code == 200, response.text
    live = client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"]
    assert live["muted"] is True
    assert live["speaker"] is False

    response = client.post("/calls/events", json={
        "call_ref": "ref-int", "state": "active", "direction": "outgoing",
        "muted": 0, "speaker": 1,
    }, headers=DEVICE_HEADERS)
    assert response.status_code == 200, response.text
    live = client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"]
    assert live["muted"] is False
    assert live["speaker"] is True
    # The caller is still there: an int flag is a merge, not a reset.
    assert live["number"] == NUMBER


def test_a_ringing_session_goes_stale_and_cannot_be_answered(client):
    assert _event(client).status_code == 200

    def fn():
        client.app.state.db_conn.execute(
            "UPDATE call_sessions SET updated_at = ? WHERE device_id = ?",
            (_now() - 121, DEVICE_ID))

    db._write_tx(client.app.state.db_conn, fn)
    assert client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"] is None

    devices = client.get("/app/api/devices", headers=ADMIN).json()["devices"]
    summary = next(row for row in devices if row["id"] == DEVICE_ID)
    assert summary["call"] is None

    response = _act_answer(client)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_call"


def test_a_recent_ringing_session_still_works(client):
    assert _event(client).status_code == 200

    def fn():
        client.app.state.db_conn.execute(
            "UPDATE call_sessions SET updated_at = ? WHERE device_id = ?",
            (_now() - 60, DEVICE_ID))

    db._write_tx(client.app.state.db_conn, fn)
    assert client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"]["state"] == "ringing"
    assert _act_answer(client).status_code == 200


def test_a_recent_dialing_session_is_live_but_not_answerable(client):
    # ``dialing`` is an outgoing call the phone is placing: it is live, but
    # there is nothing to answer yet.
    assert _event(client, state="dialing", direction="outgoing",
                  number=None).status_code == 200

    def fn():
        client.app.state.db_conn.execute(
            "UPDATE call_sessions SET updated_at = ? WHERE device_id = ?",
            (_now() - 60, DEVICE_ID))

    db._write_tx(client.app.state.db_conn, fn)
    assert client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"]["state"] == "dialing"
    assert _act_answer(client).status_code == 409


def test_an_active_session_lives_for_hours_but_not_a_day(client):
    assert _event(client, state="active").status_code == 200

    def age(seconds):
        def run():
            client.app.state.db_conn.execute(
                "UPDATE call_sessions SET updated_at = ? WHERE device_id = ?",
                (_now() - seconds, DEVICE_ID))
        return run

    # Three hours into a call is odd, but possible.
    db._write_tx(client.app.state.db_conn, age(3 * 3600))
    assert client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"]["state"] == "active"
    # Half a day later the phone cannot still be on that call.
    db._write_tx(client.app.state.db_conn, age(13 * 3600))
    assert client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"] is None
    assert _act_answer(client).status_code == 409


def test_session_is_live_is_the_one_rule():
    import app.calls as calls

    now = 1_700_000_000
    assert calls.session_is_live(None, now) is False
    assert calls.session_is_live(
        {"state": "ringing", "updated_at": now - 119}, now) is True
    assert calls.session_is_live(
        {"state": "ringing", "updated_at": now - 121}, now) is False
    assert calls.session_is_live(
        {"state": "dialing", "updated_at": now - 121}, now) is False
    assert calls.session_is_live(
        {"state": "active", "updated_at": now - 11 * 3600}, now) is True
    assert calls.session_is_live(
        {"state": "held", "updated_at": now - 13 * 3600}, now) is False
    assert calls.session_is_live(
        {"state": "disconnected", "updated_at": now - 10,
         "ended_at": now - 10}, now) is True
    assert calls.session_is_live(
        {"state": "disconnected", "updated_at": now - 300,
         "ended_at": now - 300}, now) is False


def test_device_summary_carries_the_live_call_and_the_missed_count(client):
    _sync(client, [_item(kind="missed"),
                   _item(provider_id="101", started_at=_now() - 30)])
    assert _event(client).status_code == 200

    devices = client.get("/app/api/devices", headers=ADMIN).json()["devices"]
    summary = next(row for row in devices if row["id"] == DEVICE_ID)
    assert summary["missed"] == 1
    assert summary["call"]["call_ref"] == "ref-a"
    assert summary["call"]["state"] == "ringing"

    # A session that ended long ago is not a live call any more.
    db.call_session_set(client.app.state.db_conn, DEVICE_ID, "ref-old",
                        "disconnected", direction="incoming",
                        number=NUMBER, ended_at=_now() - 300)
    devices = client.get("/app/api/devices", headers=ADMIN).json()["devices"]
    summary = next(row for row in devices if row["id"] == DEVICE_ID)
    assert summary["call"] is None


def test_live_endpoint_of_an_idle_device_is_null(client):
    assert client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json() == {
        "device_id": DEVICE_ID, "call": None}
    assert client.get("/app/api/devices/nope/calls/live",
                      headers=ADMIN).status_code == 404


# --- the thread timeline --------------------------------------------------


def test_thread_timeline_merges_call_events(client):
    _sync(client, [_item(provider_id="1", kind="missed", started_at=_now() - 60,
                         duration_s=0)])
    client.post("/sms", json={"id": "m1", "sender": NUMBER,
                              "body": "hello", "received_at": _now() - 30},
                headers=DEVICE_HEADERS)
    response = client.get(
        f"/app/api/devices/{DEVICE_ID}/chats/{THREAD}/messages",
        headers=ADMIN)
    assert response.status_code == 200, response.text
    events = response.json()["events"]
    call_events = [event for event in events if event["kind"] == "call"]
    assert len(call_events) == 1
    call = call_events[0]
    assert call["id"].startswith("call:")
    assert call["event_id"] == call["id"]
    assert call["call_kind"] == "missed"
    assert call["duration_s"] == 0
    assert call["number"] == NUMBER
    assert call["direction"] == "in"
    # The call is older than the SMS, so it sorts after it.
    assert events[-1]["id"] == call["id"]


def test_thread_timeline_call_cursor_does_not_skip_a_second(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    now = 1_700_000_000
    db.record_call_items(conn, DEVICE_ID, [
        {"provider_id": str(index), "number": NUMBER, "kind": "outgoing",
         "started_at": now, "duration_s": index} for index in range(3)])
    page = db.thread_timeline(conn, DEVICE_ID, THREAD, limit=2)
    assert len(page) == 2
    assert all(event["id"].startswith("call:") for event in page)
    rest = db.thread_timeline(
        conn, DEVICE_ID, THREAD, before_ts=page[-1]["ts"],
        before_id=page[-1]["id"], limit=2)
    assert len(rest) == 1
    assert rest[0]["id"] not in [event["id"] for event in page]
    # A private number never joins a thread.
    db.record_call_items(conn, DEVICE_ID, [
        {"provider_id": "p", "number": "", "kind": "incoming",
         "started_at": now + 5}])
    assert len(db.thread_timeline(conn, DEVICE_ID, THREAD)) == 3


# --- the Telegram mirror --------------------------------------------------


class FakeTelegram:
    """A Telegram stand-in that records what the call notification sends."""

    def __init__(self, error=None, delay=None):
        self.sent = []
        self.keyboards = []
        self.parse_modes = []
        self._error = error
        self._delay = delay
        self._n = 0

    async def send_to_chat(self, chat_id, text, *, reply_to=None,
                           keyboard=None, parse_mode=None):
        self.sent.append((chat_id, text))
        self.keyboards.append(keyboard)
        self.parse_modes.append(parse_mode)
        self._n += 1
        if self._delay is not None:
            await asyncio.sleep(self._delay)
        if isinstance(self._error, BaseException):
            error, self._error = self._error, None
            raise error
        return {"ok": True, "message_id": self._n}

    def buttons(self, index=0):
        keyboard_value = self.keyboards[index] or {}
        return [item for row in keyboard_value.get("inline_keyboard", [])
                for item in row]


def _install_tg(client, fake) -> FakeTelegram:
    client.app.state.tg = fake
    return fake


def _ring(client, **body):
    """A ringing event, with a ref of its own so a test can ring twice."""
    return _event(client, **body)


def test_a_ringing_call_sends_one_message_with_both_buttons(client):
    fake = _install_tg(client, FakeTelegram())

    assert _ring(client).status_code == 200

    assert len(fake.sent) == 1
    chat_id, text = fake.sent[0]
    assert chat_id == "test-chat-id"
    assert "Incoming call" in text
    assert "Phone A" in text           # the device name
    assert "09121234567" in text or "+989121234567" in text
    assert "UTC" in text
    assert fake.parse_modes == ["HTML"]

    labels = [item["text"] for item in fake.buttons()]
    assert labels == ["Answer", "Decline"]
    data = [item["callback_data"] for item in fake.buttons()]
    assert data == [f"call:answer:{DEVICE_ID}", f"call:reject:{DEVICE_ID}"]
    assert all(len(value.encode()) <= 64 for value in data)
    # No Mini App is configured in this environment.
    assert "web_app" not in str(fake.keyboards[0])


def test_two_ringing_retries_of_the_same_call_send_one_message(client):
    fake = _install_tg(client, FakeTelegram())

    _ring(client)
    _ring(client)          # the phone reconnected mid-ring
    _ring(client, state="ringing", number=None)

    assert len(fake.sent) == 1


def test_a_ringing_replay_after_the_call_went_active_is_not_sent_again(
        client):
    fake = _install_tg(client, FakeTelegram())

    _ring(client)
    _event(client, state="active", number=None)
    fake.sent.clear()

    _ring(client)          # a drained offline queue replays the ring

    assert fake.sent == []


def test_an_outgoing_dialing_call_sends_no_ringing_message(client):
    fake = _install_tg(client, FakeTelegram())

    _event(client, state="dialing", direction="outgoing")

    assert fake.sent == []


def test_the_ringing_message_uses_the_contact_name_when_known(client):
    fake = _install_tg(client, FakeTelegram())
    client.post("/contacts", json={"contacts": [
        {"number": NUMBER, "name": "Ada Lovelace"}]}, headers=DEVICE_HEADERS)

    _ring(client)

    assert "Ada Lovelace" in fake.sent[0][1]
    # The full number is not needed once the contact is known.
    assert "09121234567" not in fake.sent[0][1]


def test_a_long_device_id_sends_the_message_without_buttons(client):
    """64 bytes is a hard Telegram cap: an overlong id must not break the
    message, so the keyboard is dropped and the text still goes out."""
    long_id = "d" * 55                     # "call:answer:" + id > 64 bytes
    seed_device(client.app.state.db_conn, long_id, "sr-" + long_id,
                name="Long Phone")
    fake = _install_tg(client, FakeTelegram())

    response = client.post("/calls/events", json={
        "call_ref": "ref-long", "state": "ringing", "direction": "incoming",
        "number": NUMBER}, headers=device_headers("sr-" + long_id))

    assert response.status_code == 200, response.text
    assert len(fake.sent) == 1
    assert "Incoming call" in fake.sent[0][1]
    assert fake.keyboards == [None]


def test_the_mini_app_button_uses_the_configured_base(settings):
    configured = replace(settings, miniapp_url="https://relay.example/app")
    with TestClient(create_app(configured, run_workers=False)) as client:
        seed_device(client.app.state.db_conn, DEVICE_ID, DEVICE_TOKEN)
        fake = _install_tg(client, FakeTelegram())

        _ring(client)

        buttons = fake.buttons()
        assert buttons[-1]["text"] == "Open calls"
        assert buttons[-1]["web_app"]["url"] == (
            f"https://relay.example/app?device={DEVICE_ID}")


def test_a_missed_call_sends_one_message_with_call_back(client):
    fake = _install_tg(client, FakeTelegram())

    _ring(client)
    fake.sent.clear()
    fake.keyboards.clear()

    assert _event(client, state="disconnected", number=None
                  ).status_code == 200

    assert len(fake.sent) == 1
    text = fake.sent[0][1]
    assert "Missed call" in text
    assert "Phone A" in text
    assert fake.parse_modes[-1] == "HTML"
    assert [item["text"] for item in fake.buttons()] == ["Call back"]
    assert fake.buttons()[0]["callback_data"] == f"call:callback:{DEVICE_ID}"


def test_an_answered_call_is_not_a_missed_call(client):
    fake = _install_tg(client, FakeTelegram())

    _ring(client)
    _event(client, state="active", number=None)
    fake.sent.clear()

    _event(client, state="disconnected", number=None)

    assert fake.sent == []


def test_an_outgoing_call_is_never_a_missed_call(client):
    fake = _install_tg(client, FakeTelegram())

    _event(client, direction="outgoing", state="dialing")
    _event(client, direction="outgoing", state="disconnected", number=None)

    assert fake.sent == []


def test_a_duplicate_disconnected_event_sends_nothing(client):
    fake = _install_tg(client, FakeTelegram())

    _ring(client)
    _event(client, state="disconnected", number=None)
    assert len(fake.sent) == 2      # the ring and the missed call
    fake.sent.clear()

    _event(client, state="disconnected", number=None)

    assert fake.sent == []


def test_a_disconnect_without_a_ring_is_not_a_missed_call(client):
    """The first thing we ever see is the end: an import, not a ring."""
    fake = _install_tg(client, FakeTelegram())

    _event(client, state="disconnected", direction="incoming", number=None)

    assert fake.sent == []


def test_a_telegram_failure_does_not_fail_the_call_event(client):
    _install_tg(client, FakeTelegram(error=TGError("timeout")))

    response = _ring(client)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    # The state change happened even though the message did not.
    live = client.get(f"/app/api/devices/{DEVICE_ID}/calls/live",
                      headers=ADMIN).json()["call"]
    assert live["state"] == "ringing"


def test_a_slow_telegram_is_bounded_and_does_not_fail_the_event(client):
    _install_tg(client, FakeTelegram(delay=5.0))

    started = time.monotonic()
    response = _ring(client)
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    # 2 s cap, with room for the test client itself.
    assert elapsed < 3.5


def test_a_telegram_outage_is_logged_by_kind_only(client, caplog):
    _install_tg(client, FakeTelegram(error=TGError("bad_request")))

    _ring(client)

    messages = [record.getMessage() for record in caplog.records
                if record.name == "app.calls"]
    assert any("bad_request" in message for message in messages)
    # The number, the device name and the caller never reach the log.
    for message in messages:
        assert NUMBER not in message
        assert "09121234567" not in message
        assert "Phone A" not in message
