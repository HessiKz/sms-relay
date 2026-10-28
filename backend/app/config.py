import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    admin_telegram_user_id: int
    phone_a_token: str
    admin_token: str
    database_path: str
    uvicorn_host: str = "127.0.0.1"
    uvicorn_port: int = 8090
    log_level: str = "INFO"
    miniapp_url: str | None = None
    miniapp_bot_username: str | None = None
    miniapp_auth_max_age: int = 86400
    miniapp_send_per_minute: int = 6
    miniapp_send_daily_cap: int = 50


def _require(env: dict, name: str) -> str:
    value = env.get(name, "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required setting: {name}")
    return value.strip()


def _coerce_int(env: dict, name: str) -> int:
    value = env.get(name, "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required setting: {name}")
    try:
        return int(value.strip())
    except ValueError:
        raise ValueError(f"invalid integer for setting: {name}") from None


def _optional_str(env: dict, name: str) -> str | None:
    value = env.get(name, "")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid string for setting: {name}")
    return value.strip() or None


def _optional_positive_int(env: dict, name: str, default: int) -> int:
    value = env.get(name, "")
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if not isinstance(value, str):
        raise ValueError(f"invalid integer for setting: {name}")
    try:
        parsed = int(value.strip())
    except ValueError:
        raise ValueError(f"invalid integer for setting: {name}") from None
    if parsed <= 0:
        raise ValueError(f"invalid integer for setting: {name}")
    return parsed


def read_env_file(path: str) -> dict:
    parsed = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"invalid env-file line: {line}")
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip()
    return parsed


def load_settings(env: dict | None = None, env_file: str | None = None) -> Settings:
    if env is None:
        env = read_env_file(env_file) if env_file is not None else dict(os.environ)
    return Settings(
        telegram_bot_token=_require(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require(env, "TELEGRAM_CHAT_ID"),
        admin_telegram_user_id=_coerce_int(env, "ADMIN_TELEGRAM_USER_ID"),
        phone_a_token=_require(env, "PHONE_A_TOKEN"),
        admin_token=_require(env, "ADMIN_TOKEN"),
        database_path=_require(env, "DATABASE_PATH"),
        uvicorn_host=env.get("UVICORN_HOST", "127.0.0.1").strip() or "127.0.0.1",
        uvicorn_port=int(env.get("UVICORN_PORT", "8090").strip() or "8090"),
        log_level=env.get("LOG_LEVEL", "INFO").strip() or "INFO",
        miniapp_url=_optional_str(env, "MINIAPP_URL"),
        miniapp_bot_username=_optional_str(env, "MINIAPP_BOT_USERNAME"),
        miniapp_auth_max_age=_optional_positive_int(
            env, "MINIAPP_AUTH_MAX_AGE", 86400),
        miniapp_send_per_minute=_optional_positive_int(
            env, "MINIAPP_SEND_PER_MINUTE", 6),
        miniapp_send_daily_cap=_optional_positive_int(
            env, "MINIAPP_SEND_DAILY_CAP", 50),
    )
