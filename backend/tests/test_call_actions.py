"""Call actions: the Mini App creates them and the phone claims and reports
them.

The rules under test are the ones that keep a remote action from doing
the wrong thing: the TTL, the idempotent ``client_ref``, a stale
``call_ref`` that is refused instead of applied to the next call, the
absolute refusal of an emergency dial, and the atomic claim that makes
sure two consumers never run one action.
"""

import time

import app.db as db
from tests.device_support import (DEVICE_ID, SECOND_DEVICE_ID, device_headers,
                                  pair_device)
from tests.miniapp_support import headers as admin_headers

DEVICE_HEADERS = device_headers()
ADMIN = admin_headers()
NUMBER = "09121234567"
REF = "ref-live-1"


def _now() -> int:
    return int(time.time())


def _event(client, **body):
    payload = {"call_ref": REF, "state": "ringing",
               "direction": "incoming", "number": NUMBER,
               "sim_label": "SIM 1"}
    payload.update(body)
    return client.post("/calls/events", json=payload, headers=DEVICE_HEADERS)


def _act(client, *, action="answer", call_ref=REF, payload=None,
         client_ref="abcdefgh", device_id=DEVICE_ID, headers=ADMIN):
    body = {"call_ref": call_ref, "action": action, "client_ref": client_ref}
    if payload is not None:
        body["payload"] = payload
    return client.post(f"/app/api/devices/{device_id}/calls/actions",
                       json=body, headers=headers)


def _claim(client, wait=0, headers=DEVICE_HEADERS):
    response = client.get("/calls/actions", params={"wait": wait},
                           headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["actions"]


def _report(client, action_id, status="done", detail=None,
            headers=DEVICE_HEADERS):
    body = {"status": status}
    if detail is not None:
        body["detail"] = detail
    return client.post(f"/calls/actions/{action_id}/result", json=body,
                       headers=headers)


# --- creating an action ---------------------------------------------------


def test_answer_needs_a_live_call_and_has_a_short_ttl(client):
    # No live call yet: the ref is stale, never guessed.
    assert _act(client).status_code == 409
    assert _act(client).json()["detail"]["error"] == "stale_call"

    assert _event(client).status_code == 200
    created = _act(client)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["action"] == "answer"
    assert body["call_ref"] == REF
    assert body["status"] == "pending"
    assert body["duplicate"] is False
    assert 0 < body["expires_at"] - body["created_at"] <= 20


def test_a_slow_action_wins_the_longer_ttl(client):
    _event(client, state="active")
    body = _act(client, action="mute").json()
    assert 20 < body["expires_at"] - body["created_at"] <= 300
    dial = _act(client, action="dial", call_ref="", payload="09121234567",
                client_ref="dialref01").json()
    assert 20 < dial["expires_at"] - dial["created_at"] <= 300


def test_client_ref_is_idempotent_and_scoped_to_one_device_and_action(client):
    _event(client)
    first = _act(client).json()
    repeat = _act(client).json()
    assert repeat["id"] == first["id"]
    assert repeat["duplicate"] is True

    # The same ref with another action is a conflict, never a second action.
    assert _act(client, action="reject").status_code == 409
    assert _act(client, action="reject").json()["detail"]["error"] == (
        "client_ref_conflict")

    # And the same ref on the second phone is a conflict as well.
    other_token = pair_device(client, device_id=SECOND_DEVICE_ID, name="Phone B")
    other_headers = device_headers(other_token)
    assert _event(client, call_ref="ref-b",
                  headers=other_headers).status_code == 200
    conflict = _act(client, call_ref="ref-b", device_id=SECOND_DEVICE_ID)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "client_ref_conflict"


def test_a_stale_call_ref_is_refused(client):
    _event(client)
    assert _act(client, call_ref="ref-other").status_code == 409
    assert _act(client, call_ref="ref-other").json()["detail"]["error"] == (
        "stale_call")
    # A call that already ended is stale too.
    _event(client, state="disconnected")
    assert _act(client).status_code == 409
    # An empty ref is only valid for a dial.
    assert _act(client, call_ref="").status_code == 409


def test_dial_while_idle_is_allowed_and_normalised(client):
    # No live call at all: dialling is exactly what the Mini App does then.
    created = _act(client, action="dial", call_ref="",
                   payload="+98 912 123 4567", client_ref="dialref02")
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["call_ref"] == ""
    assert body["payload"] == "989121234567"
    assert body["status"] == "pending"

    # A shortcode keeps its normalised form as well.
    short = _act(client, action="dial", call_ref="", payload="1100",
                 client_ref="dialref03")
    assert short.status_code == 200
    assert short.json()["payload"] == "short:1100"


def test_an_emergency_dial_is_refused(client):
    for number in ("112", "911", "999", "000", "110", "115", "125"):
        response = _act(client, action="dial", call_ref="", payload=number,
                        client_ref=f"dial{number}")
        assert response.status_code == 422, number
        assert response.json()["detail"] == {"error": "emergency_number"}
    # A local spelling and a non-ASCII spelling reach the same number.
    for number in ("0112", " 112 ", "۱۱۲"):
        response = _act(client, action="dial", call_ref="", payload=number,
                        client_ref="dial-local")
        assert response.status_code == 422, number
        assert response.json()["detail"] == {"error": "emergency_number"}
    # Nothing was stored for the refused dials.
    assert _claim(client) == []


def test_dial_validation(client):
    assert _act(client, action="dial", call_ref="", payload="",
                client_ref="dialempty").status_code == 422
    assert _act(client, action="dial", call_ref="", payload="bank",
                client_ref="dialbank").status_code == 422
    assert _act(client, action="dial", call_ref="", payload="12",
                client_ref="dialshort").status_code == 422
    assert _act(client, action="teleport", client_ref="dialbad"
                ).status_code == 422
    assert _act(client, action="dial", call_ref=REF, payload=NUMBER,
                client_ref="dialref").status_code == 422


def test_action_requests_need_a_valid_body_and_an_active_device(client):
    assert _act(client, client_ref="").status_code == 422
    assert _act(client, client_ref="x" * 65).status_code == 422
    assert _act(client, device_id="nope").status_code == 404
    # The Mini App credential is the only way in.
    assert client.get(f"/app/api/devices/{DEVICE_ID}/calls/live").status_code == 401
    assert client.post(f"/app/api/devices/{DEVICE_ID}/calls/actions",
                       json={"call_ref": REF, "action": "answer",
                             "client_ref": "abcdefgh"}).status_code == 401


# --- claiming an action ---------------------------------------------------


def test_claim_marks_delivered_exactly_once(client):
    _event(client)
    created = _act(client).json()
    first = _claim(client)
    assert [row["id"] for row in first] == [created["id"]]
    assert set(first[0]) == {"id", "call_ref", "action", "payload",
                             "expires_at"}
    # The second consumer gets nothing: the action is already delivered.
    assert _claim(client) == []


def test_claim_never_hands_out_an_expired_action(client):
    _event(client)
    created = _act(client).json()
    conn = client.app.state.db_conn

    def fn():
        conn.execute("UPDATE call_actions SET expires_at = ? WHERE id = ?",
                     (_now() - 1, created["id"]))

    db._write_tx(conn, fn)
    assert _claim(client) == []
    stored = db.call_action(conn, created["id"])
    assert stored["status"] == "expired"


def test_expire_call_actions_marks_only_pending_ones(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.call_action_create(conn, DEVICE_ID, "answer", call_ref="r",
                          now=1000, expires_at=1010)
    delivered = db.call_action_create(conn, DEVICE_ID, "mute", call_ref="r",
                                      now=1000, expires_at=1010)
    db.claim_call_actions(conn, DEVICE_ID, now=1005)
    assert db.expire_call_actions(conn, now=2000) == 0
    assert db.call_action(conn, delivered["id"])["status"] == "delivered"
    pending = db.call_action_create(conn, DEVICE_ID, "hold", call_ref="r",
                                    now=1000, expires_at=1010)
    assert db.expire_call_actions(conn, now=2000) == 1
    assert db.call_action(conn, pending["id"])["status"] == "expired"


def test_claim_waits_for_an_action(client):
    started = time.monotonic()
    assert _claim(client, wait=1) == []
    assert time.monotonic() - started >= 0.9


def test_wait_parameter_is_bounded(client):
    response = client.get("/calls/actions", params={"wait": 26},
                          headers=DEVICE_HEADERS)
    assert response.status_code == 422
    response = client.get("/calls/actions", params={"wait": "soon"},
                          headers=DEVICE_HEADERS)
    assert response.status_code == 422


def test_a_second_device_cannot_claim_the_actions_of_the_first(client):
    _event(client)
    created = _act(client).json()
    other_token = pair_device(client, device_id=SECOND_DEVICE_ID, name="Phone B")
    other_headers = device_headers(other_token)
    assert _claim(client, headers=other_headers) == []
    # ... and it cannot report on it either.
    assert _report(client, created["id"],
                   headers=other_headers).status_code == 404
    # Its own live call never makes the ref of another phone answerable.
    assert _event(client, call_ref="ref-b",
                  headers=other_headers).status_code == 200
    stale = _act(client, call_ref=REF, device_id=SECOND_DEVICE_ID,
                 client_ref="otherref01")
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "stale_call"
    # The owner still can.
    assert _claim(client)[0]["id"] == created["id"]


# --- reporting a result ---------------------------------------------------


def test_result_is_forward_only(client):
    _event(client)
    created = _act(client).json()
    _claim(client)
    first = _report(client, created["id"], "done", "answered")
    assert first.status_code == 200
    assert first.json() == {"accepted": True, "status": "done"}
    # A duplicate or late report is accepted with the current status.
    again = _report(client, created["id"], "failed")
    assert again.status_code == 200
    assert again.json() == {"accepted": False, "status": "done"}
    assert db.call_action(client.app.state.db_conn,
                          created["id"])["status"] == "done"


def test_result_unknown_action_and_bad_status(client):
    assert _report(client, "ca-does-not-exist").status_code == 404
    _event(client)
    created = _act(client).json()
    assert _report(client, created["id"], "maybe").status_code == 422
    assert _report(client, created["id"], "done", 5).status_code == 422


def test_result_of_a_pending_action_is_accepted(client):
    # A phone that executed an action before the claim is still right.
    _event(client)
    created = _act(client).json()
    assert _report(client, created["id"], "failed", "no radio").json() == {
        "accepted": True, "status": "failed"}
    assert _claim(client) == []


# --- unread calls ---------------------------------------------------------


def test_read_endpoint_clears_the_missed_badge(client):
    now = _now()
    client.post("/calls/sync", json={"items": [
        {"provider_id": "1", "number": NUMBER, "kind": "missed",
         "started_at": now - 60},
        {"provider_id": "2", "number": NUMBER, "kind": "missed",
         "started_at": now - 30},
    ]}, headers=DEVICE_HEADERS)
    devices = client.get("/app/api/devices", headers=ADMIN).json()["devices"]
    assert next(row for row in devices if row["id"] == DEVICE_ID)["missed"] == 2

    before = client.app.state.rev
    response = client.post(f"/app/api/devices/{DEVICE_ID}/calls/read",
                           json={"until_ts": now - 45}, headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"device_id": DEVICE_ID, "read": 1, "missed": 1}
    assert client.app.state.rev > before

    response = client.post(f"/app/api/devices/{DEVICE_ID}/calls/read",
                           json={"until_ts": now}, headers=ADMIN)
    assert response.json()["missed"] == 0
    # Reading nothing new does not bump the revision again.
    before = client.app.state.rev
    client.post(f"/app/api/devices/{DEVICE_ID}/calls/read",
                json={"until_ts": now}, headers=ADMIN)
    assert client.app.state.rev == before

    assert client.post("/app/api/devices/nope/calls/read",
                       json={"until_ts": now}, headers=ADMIN).status_code == 404
    assert client.post(f"/app/api/devices/{DEVICE_ID}/calls/read",
                       json={"until_ts": -1}, headers=ADMIN).status_code == 422
    assert client.post(f"/app/api/devices/{DEVICE_ID}/calls/read",
                       json={}).status_code == 401


def test_read_endpoint_never_moves_into_the_future(client):
    now = _now()
    client.post("/calls/sync", json={"items": [
        {"provider_id": "1", "number": NUMBER, "kind": "missed",
         "started_at": now}]}, headers=DEVICE_HEADERS)
    family = client.app.state.db_conn
    response = client.post(f"/app/api/devices/{DEVICE_ID}/calls/read",
                           json={"until_ts": now + 86400}, headers=ADMIN)
    assert response.status_code == 200
    row = db.page_calls(family, DEVICE_ID)[0]
    assert row["is_read"] == 1


# --- logging hygiene ------------------------------------------------------


def test_no_number_or_name_reaches_the_log(client, caplog):
    caplog.set_level("DEBUG")
    client.post("/calls/sync", json={"items": [
        {"provider_id": "1", "number": NUMBER, "kind": "missed",
         "cached_name": "Very Secret Name", "started_at": _now() - 10,
         "content_hash": "h1"}]}, headers=DEVICE_HEADERS)
    _event(client)
    created = _act(client).json()
    _claim(client)
    _report(client, created["id"], "done")
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert NUMBER not in text
    assert "989121234567" not in text
    assert "Very Secret Name" not in text
