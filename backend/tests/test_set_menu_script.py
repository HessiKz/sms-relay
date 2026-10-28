"""Tests for the optional bot-menu helper script."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2]
          / "scripts" / "set_miniapp_menu.py")


def _load():
    spec = importlib.util.spec_from_file_location("set_miniapp_menu", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_menu_payload_sets_a_web_app_button():
    module = _load()
    payload = module.menu_payload("https://ai-barrow.ir/relay/app/")
    assert payload == {
        "menu_button": {
            "type": "web_app",
            "text": "Inbox",
            "web_app": {"url": "https://ai-barrow.ir/relay/app/"},
        }
    }


def test_menu_payload_rejects_plain_http():
    module = _load()
    with pytest.raises(ValueError):
        module.menu_payload("http://ai-barrow.ir/relay/app/")


def test_menu_payload_can_restore_the_default_button():
    module = _load()
    assert module.menu_payload(None, clear=True) == {
        "menu_button": {"type": "default"}
    }
