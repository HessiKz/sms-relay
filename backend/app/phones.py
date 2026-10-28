"""Phone-number and sender normalisation.

The same number reaches the relay spelled in many ways: as the network
displays it ("+98 912 123 4567"), without the country code
("09121234567"), with Persian or Arabic digits, or with a shortcode.
This module maps every spelling to one canonical *thread key* so that a
conversation is a single thread regardless of how the sender was
written.

The digit map mirrors android-app/.../util/PhoneNumbers.kt:35-52 and the
rules follow PhoneNumbers.kt:63-90 plus SenderNormalizer.kt:22-38.

Keys are dependency-free strings:

  ``989121234567``   an international number (digits only, no ``+``)
  ``short:110``      a 3-6 digit shortcode
  ``raw:ad-bank``    an alphanumeric sender, lowercased and sanitised

``display_number`` turns a key back into something a human can read.
"""

import re
import unicodedata

_PERSIAN_FIRST = 0x06F0
_ARABIC_FIRST = 0x0660
_FULLWIDTH_FIRST = 0xFF10

# Space, dash, brackets and the like between the digits. Mirrors the
# SEPARATORS regex in PhoneNumbers.kt.
_SEPARATORS = re.compile(r"[\s\-()./\u200c\u2013\u2014]")
_RAW_PUNCTUATION = frozenset("._-")

MIN_SHORTCODE_DIGITS = 3
MAX_SHORTCODE_DIGITS = 6
MIN_NUMBER_DIGITS = 5
MAX_NUMBER_DIGITS = 15
MAX_RAW_LEN = 64


def to_ascii_digits(text: str) -> str:
    """Replace Persian, Arabic-Indic and fullwidth digits with ASCII."""
    builder = []
    for character in text:
        code = ord(character)
        if _PERSIAN_FIRST <= code <= _PERSIAN_FIRST + 9:
            digit = code - _PERSIAN_FIRST
        elif _ARABIC_FIRST <= code <= _ARABIC_FIRST + 9:
            digit = code - _ARABIC_FIRST
        elif _FULLWIDTH_FIRST <= code <= _FULLWIDTH_FIRST + 9:
            digit = code - _FULLWIDTH_FIRST
        else:
            digit = -1
        builder.append(chr(ord("0") + digit) if digit >= 0 else character)
    return "".join(builder)


def raw_key(value: str) -> str | None:
    """The key for an alphanumeric sender, or None when nothing is left.

    Letters and digits of any script are kept: Persian sender names such
    as ``مترو تهران`` must form a thread like ``AD-BANK`` does. NFC keeps
    canonically equivalent spellings on one key.
    """
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", to_ascii_digits(value).lower())
    sanitized = "".join(
        character for character in text
        if character.isalnum() or character in _RAW_PUNCTUATION
    )[:MAX_RAW_LEN]
    return f"raw:{sanitized}" if sanitized else None


def thread_key(value: str) -> str | None:
    """The canonical conversation key for a sender or a destination.

    Returns ``None`` when the value carries no usable number or sender
    name. Numeric strings that cannot be a number or a shortcode are
    rejected instead of being treated as raw senders.
    """
    if not isinstance(value, str):
        return None
    text = to_ascii_digits(value).strip()
    if not text:
        return None

    cleaned = _SEPARATORS.sub("", text)
    if not cleaned:
        return None

    international = False
    digits = cleaned
    if cleaned.startswith("+"):
        international = True
        digits = cleaned[1:]
    elif cleaned.startswith("00"):
        international = True
        digits = cleaned[2:]

    if not digits.isdigit():
        return raw_key(text)

    if international:
        if not MIN_NUMBER_DIGITS <= len(digits) <= MAX_NUMBER_DIGITS:
            return None
        return digits

    length = len(digits)
    if MIN_SHORTCODE_DIGITS <= length <= MAX_SHORTCODE_DIGITS:
        return f"short:{digits}"
    if length == 11 and digits.startswith("09"):
        return "98" + digits[1:]
    if length == 10 and digits.startswith("9"):
        return "98" + digits
    if length in (11, 12) and digits.startswith("98"):
        return digits
    if MIN_NUMBER_DIGITS <= length <= MAX_NUMBER_DIGITS:
        return digits
    return None


def normalize_target(value: str) -> str | None:
    """A sendable destination: E.164 digits or ``short:<digits>``.

    Alphanumeric senders have no address the radio can dial, so they are
    rejected here even though ``thread_key`` can group them.
    """
    key = thread_key(value)
    if key is None or key.startswith("raw:"):
        return None
    return key


def display_number(key: str) -> str:
    """Turn a thread key back into a readable number."""
    if not isinstance(key, str) or not key:
        return ""
    if key.startswith(("short:", "raw:", "+")):
        return key
    if key.isdigit():
        return "+" + key
    return key
