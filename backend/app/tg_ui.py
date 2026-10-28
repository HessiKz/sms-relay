"""Helpers for Telegram inline keyboards and phone-number normalisation.

The bot is driven by buttons: every choice the user can make is a button,
so nothing has to be typed and every step is obvious.
"""

from __future__ import annotations

import re

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_MAP = {ord(ch): str(i) for i, ch in enumerate(PERSIAN_DIGITS)}
_DIGIT_MAP.update({ord(ch): str(i) for i, ch in enumerate(ARABIC_DIGITS)})

PHONE_RE = re.compile(r"^\+?[0-9]{5,20}$")


def to_ascii_digits(text: str) -> str:
    """Convert Persian/Arabic numerals to ASCII digits."""
    return (text or "").translate(_DIGIT_MAP)


def normalize_number(raw: str) -> str | None:
    """Turn what a human typed into a number the phone can dial.

    Accepts: "+989120000003", "09120000003", "00989120000003",
    "989120000003", "0912 000 0003" and Persian numerals. Returns None
    when the input cannot be a phone number.
    """
    if not raw:
        return None
    text = to_ascii_digits(raw).strip()
    text = re.sub(r"[\s\-()]", "", text)
    if not text:
        return None
    if text.startswith("+"):
        digits = text[1:]
    elif text.startswith("00"):
        digits = text[2:]
    else:
        digits = text
    if not digits.isdigit():
        return None
    if digits.startswith("98") and len(digits) >= 12:
        return "+" + digits
    if digits.startswith("0") and 10 <= len(digits) <= 12:
        return "+98" + digits[1:]
    if len(digits) < 5:
        return None
    return "+" + digits


def is_phone_number(raw: str) -> bool:
    return normalize_number(raw) is not None


def button(text: str, callback_data: str) -> dict:
    return {"text": text, "callback_data": callback_data}


def keyboard(*rows: list[dict]) -> dict:
    """Build an inline keyboard from rows of buttons."""
    return {"inline_keyboard": [list(row) for row in rows if row]}


def web_app_button(text: str, url: str) -> dict:
    """A button that opens a Telegram Mini App.

    The Bot API only accepts these in a private chat between the user and
    the bot, which is exactly where the relay talks to its admin.
    """
    return {"text": text, "web_app": {"url": url}}


def mask_number(number: str) -> str:
    """Show a number without exposing all of it: +98*******1896."""
    digits = number.lstrip("+")
    if len(digits) <= 4:
        return number
    return "+" + digits[:-4].replace(digits[:-4], "*" * (len(digits) - 4)) + digits[-4:]


def contact_button(name: str, number: str, index: int) -> dict:
    """A contact as a button: the name, with the number behind it."""
    label = name if len(name) <= 24 else name[:23] + "…"
    return button(f"{label} · {mask_number(number)}", f"contact:{index}")
