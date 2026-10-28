#!/usr/bin/env python3
"""Set (or clear) the bot menu button that opens the relay Mini App.

BotFather's /setmenubutton is the preferred route. This script does the
same thing from a terminal without anyone copying the bot token by hand:
the token is read from the root-only env file and is never printed.

Usage (the env file is root-only, so run it with sudo):

    sudo python3 scripts/set_miniapp_menu.py \
        --env-file /etc/sms-relay/env \
        --url https://ai-barrow.ir/relay/app/

    sudo python3 scripts/set_miniapp_menu.py \
        --env-file /etc/sms-relay/env --clear
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

API_BASE = "https://api.telegram.org"
MAX_BUTTON_TEXT = 64


def read_value(path: str, key: str) -> str:
    """Read one KEY=value pair without ever echoing the line."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text.startswith(key + "="):
                continue
            return text.partition("=")[2].strip()
    return ""


def menu_payload(url: str | None, text: str = "Inbox",
                 clear: bool = False) -> dict:
    if clear:
        return {"menu_button": {"type": "default"}}
    if not url or urlsplit(url).scheme != "https":
        raise ValueError("the menu button URL must be an https URL")
    if not 1 <= len(text) <= MAX_BUTTON_TEXT:
        raise ValueError(f"the button text must be 1..{MAX_BUTTON_TEXT} chars")
    return {
        "menu_button": {
            "type": "web_app",
            "text": text,
            "web_app": {"url": url},
        }
    }


def set_menu_button(token: str, payload: dict, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(
        f"{API_BASE}/bot{token}/setChatMenuButton",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read(64_000).decode("utf-8"))
    return body if isinstance(body, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set the Telegram bot menu button to the Mini App.")
    parser.add_argument("--env-file", default="/etc/sms-relay/env")
    parser.add_argument("--url", default=None,
                        help="Mini App URL; defaults to MINIAPP_URL")
    parser.add_argument("--text", default="Inbox")
    parser.add_argument("--clear", action="store_true",
                        help="restore the default menu button")
    args = parser.parse_args(argv)

    token = read_value(args.env_file, "TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is missing from the env file.", file=sys.stderr)
        return 2

    url = args.url or read_value(args.env_file, "MINIAPP_URL") or None
    try:
        payload = menu_payload(url, args.text, args.clear)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        body = set_menu_button(token, payload)
    except urllib.error.HTTPError as exc:
        description = ""
        try:
            data = json.loads(exc.read(16_000).decode("utf-8"))
            description = str(data.get("description") or "")[:200]
        except Exception:
            description = ""
        print(f"Telegram refused the menu button (HTTP {exc.code})"
              + (f": {description}" if description else ""), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"request failed ({type(exc.reason).__name__})", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"request failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    if body.get("ok") is not True:
        print("Telegram did not confirm the menu button.", file=sys.stderr)
        return 1
    if args.clear:
        print("Bot menu button cleared.")
    else:
        print(f"Bot menu button now opens {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
