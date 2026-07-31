"""Focused tests for scao_live.duration.parse_duration.

Covers: 500ms, seconds, minutes, hours, zero, surrounding whitespace,
uppercase units, and invalid inputs (empty, non-numeric, unsupported-unit,
negative, combined-unit strings).
"""

import pytest

from scao_live.duration import parse_duration

# --- Valid inputs ---


def test_milliseconds():
    assert parse_duration("500ms") == 500


def test_seconds():
    assert parse_duration("1s") == 1000
    assert parse_duration("2s") == 2000


def test_minutes():
    assert parse_duration("1m") == 60000
    assert parse_duration("5m") == 300000


def test_hours():
    assert parse_duration("1h") == 3600000
    assert parse_duration("2h") == 7200000


def test_zero():
    assert parse_duration("0s") == 0
    assert parse_duration("0ms") == 0
    assert parse_duration("0m") == 0
    assert parse_duration("0h") == 0


# --- Whitespace handling ---


def test_surrounding_whitespace_stripped():
    assert parse_duration("  500ms  ") == 500
    assert parse_duration("\t1s\n") == 1000
    assert parse_duration("  2m ") == 120000


# --- Case-insensitive units ---


def test_uppercase_units():
    assert parse_duration("500MS") == 500
    assert parse_duration("1S") == 1000
    assert parse_duration("1M") == 60000
    assert parse_duration("1H") == 3600000


def test_mixed_case_units():
    assert parse_duration("500Ms") == 500
    assert parse_duration("1mS") == 1
    assert parse_duration("2H") == 7200000


# --- Invalid inputs ---


def test_empty_raises():
    with pytest.raises(ValueError):
        parse_duration("")


def test_whitespace_only_raises():
    with pytest.raises(ValueError):
        parse_duration("   ")
    with pytest.raises(ValueError):
        parse_duration("\t\n")


def test_non_numeric_raises():
    with pytest.raises(ValueError):
        parse_duration("abc")
    with pytest.raises(ValueError):
        parse_duration("ms")


def test_no_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("500")


def test_unsupported_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("500x")
    with pytest.raises(ValueError):
        parse_duration("1d")
    with pytest.raises(ValueError):
        parse_duration("10sec")


def test_negative_raises():
    with pytest.raises(ValueError):
        parse_duration("-1s")
    with pytest.raises(ValueError):
        parse_duration("-500ms")


def test_fractions_raise():
    with pytest.raises(ValueError):
        parse_duration("1.5s")
    with pytest.raises(ValueError):
        parse_duration("0.5h")
    with pytest.raises(ValueError):
        parse_duration("2.5m")
    with pytest.raises(ValueError):
        parse_duration("0.5ms")


def test_combined_units_raises():
    with pytest.raises(ValueError):
        parse_duration("1s2m")
    with pytest.raises(ValueError):
        parse_duration("1m30s")
    with pytest.raises(ValueError):
        parse_duration("500ms1s")
