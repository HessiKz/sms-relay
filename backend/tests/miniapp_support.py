"""Shared helpers for Mini App tests.

The token matches ``tests/conftest.py`` so signed headers validate against
the app built by the ``client`` fixture.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

BOT_TOKEN = "test-tg-token"
ADMIN_ID = 7
INIT_DATA_HEADER = "X-Telegram-Init-Data"


def _hash(fields, token=BOT_TOKEN):
    data_check_string = "\n".join(
        f"{key}={fields[key]}" for key in sorted(fields) if key != "hash")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode(),
                    hashlib.sha256).hexdigest()


def signed_init_data(user_id=ADMIN_ID, auth_date=None, start_param=None):
    """A URL-encoded, correctly signed initData value for the test bot."""
    fields = {
        "query_id": "AAH-test-query",
        "user": json.dumps({"id": user_id, "first_name": "Ada Lovelace",
                            "username": "ada"}, separators=(",", ":")),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
    }
    if start_param is not None:
        fields["start_param"] = start_param
    fields["hash"] = _hash(fields)
    return urlencode(fields)


def headers(user_id=ADMIN_ID, auth_date=None, start_param=None):
    """Request headers carrying a fresh, signed Mini App credential."""
    return {INIT_DATA_HEADER: signed_init_data(
        user_id=user_id, auth_date=auth_date, start_param=start_param)}
