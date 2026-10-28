import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.miniapp_auth import (MiniAppAuthError, validate_init_data)

BOT_TOKEN = "test-tg-token"
ADMIN_ID = 7
HEADER = "X-Telegram-Init-Data"
API = "/app/api/me"
NOW = 1_800_000_000


def _hash(fields, token=BOT_TOKEN):
    data_check_string = "\n".join(
        f"{key}={fields[key]}" for key in sorted(fields) if key != "hash")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode(),
                    hashlib.sha256).hexdigest()


def _user(user_id=ADMIN_ID):
    return json.dumps({"id": user_id, "first_name": "Ada Lovelace",
                       "username": "ada"}, separators=(",", ":"))


def _fields(user_id=ADMIN_ID, auth_date=None, now=None, **extra):
    if now is None:
        now = int(time.time())
    fields = {
        "query_id": "AAH-test-query",
        "user": _user(user_id),
        "auth_date": str(now if auth_date is None else auth_date),
    }
    fields.update(extra)
    return fields


def signed_init_data(user_id=ADMIN_ID, auth_date=None, now=None, **extra):
    fields = _fields(user_id=user_id, auth_date=auth_date, now=now, **extra)
    fields["hash"] = _hash(fields)
    return urlencode(fields)


def _signed_fields(fields, token=BOT_TOKEN, hash_value=None):
    fields = dict(fields)
    fields["hash"] = hash_value if hash_value is not None else _hash(
        fields, token)
    return urlencode(fields)


# --- validate_init_data unit tests --------------------------------------

def test_valid_init_data_round_trip():
    raw = signed_init_data(auth_date=NOW - 5, now=NOW, start_param="thread-42")
    result = validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)
    assert result["user"]["id"] == ADMIN_ID
    assert result["user"]["first_name"] == "Ada Lovelace"
    assert result["auth_date"] == NOW - 5
    assert result["start_param"] == "thread-42"
    assert result["query_id"] == "AAH-test-query"


def test_start_param_absent_is_none():
    raw = signed_init_data(auth_date=NOW, now=NOW)
    result = validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)
    assert result["start_param"] is None


def test_uppercase_hash_is_accepted():
    fields = dict(_fields(auth_date=NOW, now=NOW))
    raw = _signed_fields(fields, hash_value=_hash(fields).upper())
    assert validate_init_data(raw, BOT_TOKEN, 86400,
                              now=NOW)["user"]["id"] == ADMIN_ID


def test_signature_field_is_included_in_hmac():
    fields = _fields(auth_date=NOW, now=NOW, signature="sig-abc-123")
    raw = _signed_fields(fields)
    result = validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)
    assert result["user"]["id"] == ADMIN_ID

    # Signing without the signature field must not verify.
    without = dict(fields)
    del without["signature"]
    bad = _signed_fields(fields, hash_value=_hash(without))
    with pytest.raises(MiniAppAuthError):
        validate_init_data(bad, BOT_TOKEN, 86400, now=NOW)


def test_bad_hash_rejected():
    raw = signed_init_data(auth_date=NOW, now=NOW)
    tampered = raw.replace("hash=", "hash=0")
    with pytest.raises(MiniAppAuthError):
        validate_init_data(tampered, BOT_TOKEN, 86400, now=NOW)


def test_wrong_bot_token_rejected():
    raw = signed_init_data(auth_date=NOW, now=NOW)
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, "other-token", 86400, now=NOW)


def test_tampered_user_field_rejected():
    fields = _fields(auth_date=NOW, now=NOW)
    valid_hash = _hash(fields)
    fields["user"] = _user(ADMIN_ID + 1)
    raw = _signed_fields(fields, hash_value=valid_hash)
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_stale_auth_date_rejected():
    raw = signed_init_data(auth_date=NOW - 86401, now=NOW)
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_auth_date_exactly_at_max_age_accepted():
    raw = signed_init_data(auth_date=NOW - 86400, now=NOW)
    result = validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)
    assert result["auth_date"] == NOW - 86400


def test_future_auth_date_rejected():
    raw = signed_init_data(auth_date=NOW + 301, now=NOW)
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_auth_date_just_inside_future_skew_accepted():
    raw = signed_init_data(auth_date=NOW + 300, now=NOW)
    assert validate_init_data(raw, BOT_TOKEN, 86400,
                              now=NOW)["auth_date"] == NOW + 300


def test_missing_auth_date_rejected():
    fields = {"query_id": "q", "user": _user()}
    raw = _signed_fields(fields)
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_non_integer_auth_date_rejected():
    raw = _signed_fields({"user": _user(), "auth_date": "yesterday"})
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_missing_user_rejected():
    raw = _signed_fields({"query_id": "q", "auth_date": str(NOW)})
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_missing_hash_rejected():
    raw = urlencode({"user": _user(), "auth_date": str(NOW)})
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_malformed_hash_rejected():
    raw = urlencode({"user": _user(), "auth_date": str(NOW), "hash": "abc"})
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_malformed_url_encoding_rejected():
    with pytest.raises(MiniAppAuthError):
        validate_init_data("%zz", BOT_TOKEN, 86400, now=NOW)


def test_duplicate_user_field_rejected():
    raw = urlencode([("user", _user()), ("user", _user(8)),
                     ("auth_date", str(NOW)), ("hash", "0" * 64)])
    with pytest.raises(MiniAppAuthError):
        validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_user_must_be_an_object_with_integer_id():
    for bad_user in ("[1, 2]", '"text"', json.dumps({"id": "7"}),
                     json.dumps({"id": True}), json.dumps({"first_name": "x"})):
        raw = _signed_fields({"user": bad_user, "auth_date": str(NOW)})
        with pytest.raises(MiniAppAuthError):
            validate_init_data(raw, BOT_TOKEN, 86400, now=NOW)


def test_exception_message_never_contains_raw_init_data():
    good_fields = _fields(auth_date=NOW, now=NOW)
    fields = dict(good_fields)
    fields["user"] = _user(ADMIN_ID + 1)
    tampered = _signed_fields(fields, hash_value=_hash(good_fields))
    with pytest.raises(MiniAppAuthError) as excinfo:
        validate_init_data(tampered, BOT_TOKEN, 86400, now=NOW)
    message = str(excinfo.value)
    assert tampered not in message
    assert _user(ADMIN_ID + 1) not in message
    assert _user() not in message


# --- require_miniapp via /app/api/me ------------------------------------

def test_me_without_header_is_401(client):
    r = client.get(API)
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


def test_me_with_empty_header_is_401(client):
    r = client.get(API, headers={HEADER: ""})
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


def test_me_with_valid_header_returns_admin(client):
    raw = signed_init_data(auth_date=int(time.time()))
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 200
    data = r.json()
    assert data["admin"] is True
    assert data["user"] == {"id": ADMIN_ID, "first_name": "Ada Lovelace",
                            "username": "ada"}
    assert isinstance(data["auth_date"], int)


def test_me_with_bad_hash_is_401(client):
    raw = signed_init_data(auth_date=int(time.time()))
    r = client.get(API, headers={HEADER: raw.replace("hash=", "hash=0")})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_tampered_user_is_401(client):
    fields = _fields(auth_date=int(time.time()))
    valid_hash = _hash(fields)
    fields["user"] = _user(ADMIN_ID + 1)
    r = client.get(API, headers={HEADER: _signed_fields(fields,
                                                        hash_value=valid_hash)})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_stale_auth_date_is_401(client):
    raw = signed_init_data(auth_date=int(time.time()) - 86401)
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_future_auth_date_is_401(client):
    raw = signed_init_data(auth_date=int(time.time()) + 301)
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_missing_user_is_401(client):
    raw = _signed_fields({"query_id": "q", "auth_date": str(int(time.time()))})
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_missing_hash_is_401(client):
    raw = urlencode({"user": _user(), "auth_date": str(int(time.time()))})
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_malformed_url_encoding_is_401(client):
    r = client.get(API, headers={HEADER: "%zz"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_duplicate_user_field_is_401(client):
    raw = urlencode([("user", _user()), ("user", _user(8)),
                     ("auth_date", str(int(time.time()))),
                     ("hash", "0" * 64)])
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_oversized_header_is_401(client):
    r = client.get(API, headers={HEADER: "a" * (8 * 1024 + 1)})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid init data"}


def test_me_with_signature_field_succeeds(client):
    raw = signed_init_data(auth_date=int(time.time()),
                           signature="sig-abc-123")
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 200
    assert r.json()["user"]["id"] == ADMIN_ID


def test_me_with_non_admin_user_is_403(client):
    raw = signed_init_data(user_id=ADMIN_ID + 1, auth_date=int(time.time()))
    r = client.get(API, headers={HEADER: raw})
    assert r.status_code == 403
    assert r.json() == {"detail": "forbidden"}


def test_failure_limiter_trips_after_twenty_failures(client):
    for _ in range(20):
        r = client.get(API, headers={HEADER: "a" * 32})
        assert r.status_code == 401
    r = client.get(API, headers={HEADER: "a" * 32})
    assert r.status_code == 429
    assert r.json() == {"detail": "rate limited"}
    # Only failures are counted, so a correctly signed request still works.
    r = client.get(API, headers={HEADER: signed_init_data(
        auth_date=int(time.time()))})
    assert r.status_code == 200


def test_success_does_not_consume_the_failure_budget(client):
    raw = signed_init_data(auth_date=int(time.time()))
    for _ in range(25):
        assert client.get(API, headers={HEADER: raw}).status_code == 200
    assert client.get(API, headers={HEADER: "a" * 32}).status_code == 401
