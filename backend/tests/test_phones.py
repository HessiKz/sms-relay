"""Tests for thread-key and destination normalisation."""

import pytest

from app.phones import (display_number, normalize_target, raw_key,
                        thread_key, to_ascii_digits)


@pytest.mark.parametrize("value,expected", [
    ("+98 912 123 4567", "989121234567"),
    ("00989121234567", "989121234567"),
    ("09121234567", "989121234567"),
    ("9121234567", "989121234567"),
    ("989121234567", "989121234567"),
    ("+989121234567", "989121234567"),
    ("0912-123-4567", "989121234567"),
    ("(0912) 123 4567", "989121234567"),
    ("\u200c0912\u200c123\u20134567", "989121234567"),
])
def test_thread_key_international_and_local_forms(value, expected):
    assert thread_key(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("110", "short:110"),
    ("1000", "short:1000"),
    ("12345", "short:12345"),
    ("123456", "short:123456"),
])
def test_thread_key_shortcodes(value, expected):
    assert thread_key(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("AD-BANK", "raw:ad-bank"),
    ("ad-bank", "raw:ad-bank"),
    ("Bank Alert", "raw:bankalert"),
    ("BANK*Alert!", "raw:bankalert"),
])
def test_thread_key_alphanumeric_senders(value, expected):
    assert thread_key(value) == expected


def test_thread_key_persian_arabic_and_fullwidth_digits():
    assert thread_key("\u06f0\u06f9\u06f1\u06f2\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7") \
        == "989121234567"
    assert thread_key("\u0660\u0669\u0661\u0662\u0661\u0662\u0663\u0664\u0665\u0666\u0667") \
        == "989121234567"
    assert thread_key("\uff10\uff19\uff11\uff12\uff11\uff12\uff13\uff14\uff15\uff16\uff17") \
        == "989121234567"
    assert to_ascii_digits("\u06f1\u0662\uff13") == "123"


def test_thread_key_keeps_numbers_outside_iran_as_digits():
    assert thread_key("+1 555 123 4567") == "15551234567"
    assert thread_key("0015551234567") == "15551234567"
    assert thread_key("+44 20 7946 0958") == "442079460958"


@pytest.mark.parametrize("value", ["", "   ", "+++", "\u260e", "12", "1" * 16])
def test_thread_key_unusable_values_are_none(value):
    assert thread_key(value) is None


def test_thread_key_non_string_is_none():
    assert thread_key(None) is None  # type: ignore[arg-type]


def test_raw_key_is_sanitised_and_bounded():
    assert raw_key("AD-BANK") == "raw:ad-bank"
    assert raw_key("***") is None
    long = raw_key("a" * 100)
    assert long is not None
    assert len(long) == len("raw:") + 64
    assert long == "raw:" + "a" * 64


@pytest.mark.parametrize("value,expected", [
    ("مترو تهران و حومه", "raw:متروتهرانوحومه"),
    ("ترب\u200cپی", "raw:تربپی"),
    ("بانک ملت ۲", "raw:بانکملت2"),
    ("  ", None),
])
def test_raw_key_keeps_letters_of_any_script(value, expected):
    assert raw_key(value) == expected


def test_thread_key_falls_back_to_raw_key_for_non_latin_senders():
    assert thread_key("مترو تهران و حومه") == "raw:متروتهرانوحومه"
    assert thread_key("ترب\u200cپی") == "raw:تربپی"


def test_normalize_target_accepts_numbers_and_shortcodes_only():
    assert normalize_target("09121234567") == "989121234567"
    assert normalize_target("+98 912 123 4567") == "989121234567"
    assert normalize_target("110") == "short:110"
    assert normalize_target("AD-BANK") is None
    assert normalize_target("") is None
    assert normalize_target("1234567890123456") is None


def test_display_number():
    assert display_number("989121234567") == "+989121234567"
    assert display_number("short:110") == "short:110"
    assert display_number("raw:ad-bank") == "raw:ad-bank"
    assert display_number("") == ""
