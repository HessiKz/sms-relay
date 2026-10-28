import pytest
from app.config import load_settings

REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "t:abc", "TELEGRAM_CHAT_ID": "123",
    "ADMIN_TELEGRAM_USER_ID": "123", "PHONE_A_TOKEN": "pat", "ADMIN_TOKEN": "adm",
    "DATABASE_PATH": "/tmp/x.db",
}

def test_loads_all_from_env():
    s = load_settings(env=REQUIRED)
    assert s.telegram_chat_id == "123" and s.admin_telegram_user_id == 123
    assert s.uvicorn_host == "127.0.0.1" and s.uvicorn_port == 8090

@pytest.mark.parametrize("missing", list(REQUIRED.keys()))
def test_missing_required_raises(missing):
    env = {k: v for k, v in REQUIRED.items() if k != missing}
    with pytest.raises(ValueError):
        load_settings(env=env)

def test_empty_value_raises():
    env = dict(REQUIRED); env["PHONE_A_TOKEN"] = ""
    with pytest.raises(ValueError):
        load_settings(env=env)

def test_bad_admin_id_raises():
    env = dict(REQUIRED); env["ADMIN_TELEGRAM_USER_ID"] = "abc"
    with pytest.raises(ValueError):
        load_settings(env=env)


# --- Phase 1: optional Mini App settings --------------------------------

def test_miniapp_settings_have_defaults():
    s = load_settings(env=dict(REQUIRED))
    assert s.miniapp_url is None
    assert s.miniapp_bot_username is None
    assert s.miniapp_auth_max_age == 86400
    assert s.miniapp_send_per_minute == 6
    assert s.miniapp_send_daily_cap == 50


def test_miniapp_settings_are_parsed_and_stripped():
    env = dict(REQUIRED)
    env.update({
        "MINIAPP_URL": "  https://ai-barrow.ir/relay/app/  ",
        "MINIAPP_BOT_USERNAME": "  my_bot  ",
        "MINIAPP_AUTH_MAX_AGE": " 7200 ",
        "MINIAPP_SEND_PER_MINUTE": "3",
        "MINIAPP_SEND_DAILY_CAP": "25",
    })
    s = load_settings(env=env)
    assert s.miniapp_url == "https://ai-barrow.ir/relay/app/"
    assert s.miniapp_bot_username == "my_bot"
    assert s.miniapp_auth_max_age == 7200
    assert s.miniapp_send_per_minute == 3
    assert s.miniapp_send_daily_cap == 25


def test_empty_miniapp_values_fall_back_to_defaults():
    env = dict(REQUIRED)
    env.update({
        "MINIAPP_URL": "   ",
        "MINIAPP_BOT_USERNAME": "",
        "MINIAPP_AUTH_MAX_AGE": "",
        "MINIAPP_SEND_PER_MINUTE": " ",
        "MINIAPP_SEND_DAILY_CAP": "",
    })
    s = load_settings(env=env)
    assert s.miniapp_url is None
    assert s.miniapp_bot_username is None
    assert s.miniapp_auth_max_age == 86400
    assert s.miniapp_send_per_minute == 6
    assert s.miniapp_send_daily_cap == 50


@pytest.mark.parametrize("name,value", [
    ("MINIAPP_AUTH_MAX_AGE", "abc"),
    ("MINIAPP_AUTH_MAX_AGE", "0"),
    ("MINIAPP_AUTH_MAX_AGE", "-5"),
    ("MINIAPP_SEND_PER_MINUTE", "0"),
    ("MINIAPP_SEND_PER_MINUTE", "1.5"),
    ("MINIAPP_SEND_DAILY_CAP", "-1"),
])
def test_invalid_miniapp_integers_raise(name, value):
    env = dict(REQUIRED)
    env[name] = value
    with pytest.raises(ValueError):
        load_settings(env=env)
