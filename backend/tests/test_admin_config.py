import app.db as db

ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}
DEVICE_HEADERS = {"Authorization": "Bearer device-token"}
SETTING_KEYS = {"paused", "fallback_enabled", "phone_b",
                "fallback_min_gap_s", "fallback_max_per_hour"}


def test_admin_settings_endpoints_require_admin(client):
    r = client.get("/admin/settings", headers=DEVICE_HEADERS)
    assert r.status_code == 401
    r = client.get("/admin/settings", headers={})
    assert r.status_code == 401
    r = client.put("/admin/settings", json={"paused": True}, headers=DEVICE_HEADERS)
    assert r.status_code == 401
    r = client.put("/admin/settings", json={"paused": True}, headers={})
    assert r.status_code == 401


def test_admin_filters_endpoints_require_admin(client):
    r = client.get("/admin/filters", headers=DEVICE_HEADERS)
    assert r.status_code == 401
    r = client.get("/admin/filters", headers={})
    assert r.status_code == 401
    r = client.put("/admin/filters", json={"allow": ["x"]}, headers=DEVICE_HEADERS)
    assert r.status_code == 401
    r = client.put("/admin/filters", json={"allow": ["x"]}, headers={})
    assert r.status_code == 401


def test_get_settings_defaults(client):
    r = client.get("/admin/settings", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == {
        "paused": False,
        "fallback_enabled": False,
        "phone_b": None,
        "fallback_min_gap_s": 30,
        "fallback_max_per_hour": 10,
    }
    assert set(r.json()) == SETTING_KEYS


def test_put_settings_updates_all_fields_and_persists(client):
    body = {
        "paused": True,
        "fallback_enabled": True,
        "phone_b": "+15551234567",
        "fallback_min_gap_s": 120,
        "fallback_max_per_hour": 25,
    }
    r = client.put("/admin/settings", json=body, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == body
    data = client.get("/admin/settings", headers=ADMIN_HEADERS).json()
    assert data == body
    conn = client.app.state.db_conn
    assert db.settings_get(conn, "paused") == "1"
    assert db.settings_get(conn, "fallback_enabled") == "1"
    assert db.settings_get(conn, "fallback_min_gap_s") == "120"
    assert db.settings_get(conn, "fallback_max_per_hour") == "25"


def test_put_settings_partial_update_preserves_other_keys(client):
    r = client.put("/admin/settings", json={"paused": True}, headers=ADMIN_HEADERS)
    assert r.json()["paused"] is True
    r = client.put("/admin/settings", json={"fallback_min_gap_s": 60},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = client.get("/admin/settings", headers=ADMIN_HEADERS).json()
    assert data == {
        "paused": True,
        "fallback_enabled": False,
        "phone_b": None,
        "fallback_min_gap_s": 60,
        "fallback_max_per_hour": 10,
    }


def test_put_settings_unknown_key_422(client):
    r = client.put("/admin/settings", json={"bogus": True}, headers=ADMIN_HEADERS)
    assert r.status_code == 422
    data = client.get("/admin/settings", headers=ADMIN_HEADERS).json()
    assert data["paused"] is False


def test_put_settings_bool_requires_json_boolean(client):
    for bad in ("1", 1, "true", 0, None):
        r = client.put("/admin/settings", json={"paused": bad},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 422, bad
    r = client.put("/admin/settings", json={"paused": False}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert db.settings_get(client.app.state.db_conn, "paused") == "0"


def test_put_settings_phone_patterns(client):
    for bad in ["1", "1234", "1" * 21, "+12x456", "12 345", "55-123", "", "abc"]:
        r = client.put("/admin/settings", json={"phone_b": bad},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 422, bad
    for good in ["12345", "1" * 20, "+15551234567", "00445551234567"]:
        r = client.put("/admin/settings", json={"phone_b": good},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 200, good
        assert client.get("/admin/settings", headers=ADMIN_HEADERS).json()[
            "phone_b"] == good


def test_put_settings_phone_type_422(client):
    r = client.put("/admin/settings", json={"phone_b": 15551234567},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 422


def test_put_settings_phone_null_clears(client):
    client.put("/admin/settings", json={"phone_b": "+15551234567"},
               headers=ADMIN_HEADERS)
    r = client.put("/admin/settings", json={"phone_b": None}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert client.get("/admin/settings", headers=ADMIN_HEADERS).json()[
        "phone_b"] is None


def test_put_settings_gap_bounds(client):
    for bad in (0, 3601, -30):
        r = client.put("/admin/settings", json={"fallback_min_gap_s": bad},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 422, bad
    for good in (1, 3600):
        r = client.put("/admin/settings", json={"fallback_min_gap_s": good},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 200, good


def test_put_settings_hourly_limit_bounds(client):
    for bad in (0, 51, -1):
        r = client.put("/admin/settings", json={"fallback_max_per_hour": bad},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 422, bad
    for good in (1, 50):
        r = client.put("/admin/settings", json={"fallback_max_per_hour": good},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 200, good


def test_put_settings_int_fields_type_422(client):
    for key in ("fallback_min_gap_s", "fallback_max_per_hour"):
        for bad in ("30", 30.5, True, None):
            r = client.put("/admin/settings", json={key: bad},
                           headers=ADMIN_HEADERS)
            assert r.status_code == 422, (key, bad)


def test_get_filters_defaults_empty(client):
    r = client.get("/admin/filters", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"allow": [], "block": [], "keyword": []}


def test_put_filters_replaces_kind_sorted_and_deduped(client):
    r = client.put("/admin/filters", json={"allow": ["zz", "aa", "aa", "mm"]},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["allow"] == ["aa", "mm", "zz"]
    conn = client.app.state.db_conn
    assert db.filters_all(conn)["allow"] == {"aa", "mm", "zz"}


def test_put_filters_replacement_removes_previous_entries(client):
    client.put("/admin/filters", json={"block": ["old1", "old2"]},
               headers=ADMIN_HEADERS)
    r = client.put("/admin/filters", json={"block": ["new"]}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["block"] == ["new"]
    conn = client.app.state.db_conn
    assert db.filters_all(conn)["block"] == {"new"}


def test_put_filters_partial_replace_leaves_other_kinds_intact(client):
    client.put("/admin/filters", json={"allow": ["+1"], "keyword": ["stop"]},
               headers=ADMIN_HEADERS)
    r = client.put("/admin/filters", json={"block": ["+2"]}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"allow": ["+1"], "block": ["+2"], "keyword": ["stop"]}


def test_put_filters_empty_list_clears_only_that_kind(client):
    client.put("/admin/filters", json={"allow": ["+1"], "keyword": ["stop"]},
               headers=ADMIN_HEADERS)
    r = client.put("/admin/filters", json={"keyword": []}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"allow": ["+1"], "block": [], "keyword": []}


def test_put_filters_unknown_kind_422(client):
    r = client.put("/admin/filters", json={"banned": ["x"]}, headers=ADMIN_HEADERS)
    assert r.status_code == 422
    conn = client.app.state.db_conn
    assert db.filters_all(conn) == {"allow": set(), "block": set(),
                                    "keyword": set()}


def test_put_filters_pattern_bounds(client):
    conn = client.app.state.db_conn
    long_ok = "a" * 64
    too_long = "a" * 65
    r = client.put("/admin/filters", json={"keyword": [long_ok]},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["keyword"] == [long_ok]
    for bad in [too_long, [f"p{n}" for n in range(101)]]:
        r = client.put("/admin/filters", json={"keyword": bad},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 422, bad
    assert client.get("/admin/filters", headers=ADMIN_HEADERS).json()[
        "keyword"] == [long_ok]
    r = client.put("/admin/filters", json={"keyword": [f"p{n}" for n in range(100)]},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["keyword"]) == 100
    r = client.put("/admin/filters", json={"keyword": [f"p{n}" for n in range(101)]},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 422
    assert len(db.filters_all(conn)["keyword"]) == 100


def test_put_filters_patterns_trimmed(client):
    r = client.put("/admin/filters", json={"keyword": ["  stop  ", " a ", "b"]},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["keyword"] == ["a", "b", "stop"]


def test_put_filters_bad_patterns_422(client):
    for bad in ("", "   ", "x" * 65):
        r = client.put("/admin/filters", json={"keyword": [bad]},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 422, bad
    r = client.put("/admin/filters", json={"keyword": ["ok", 123]},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 422
    r = client.put("/admin/filters", json={"allow": "not-a-list"},
                   headers=ADMIN_HEADERS)
    assert r.status_code == 422
