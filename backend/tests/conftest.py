import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import create_app

TEST_ENV = {
    "TELEGRAM_BOT_TOKEN": "test-tg-token",
    "TELEGRAM_CHAT_ID": "test-chat-id",
    "ADMIN_TELEGRAM_USER_ID": "7",
    "PHONE_A_TOKEN": "device-token",
    "ADMIN_TOKEN": "admin-token",
    "UVICORN_HOST": "127.0.0.1",
    "UVICORN_PORT": "8090",
    "LOG_LEVEL": "INFO",
}


@pytest.fixture
def settings(tmp_path):
    env = dict(TEST_ENV, **{"DATABASE_PATH": os.path.join(tmp_path, "relay.db")})
    return config.load_settings(env=env)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, run_workers=False)) as test_client:
        yield test_client
