"""Tests for scao_live.duration.parse_duration.

Covers all supported units, zero values, surrounding whitespace, decimal
values, and invalid inputs including None, empty strings, non-numeric values,
missing units, and unknown units.
"""

from __future__ import annotations

import pytest

from scao_live.duration import parse_duration

# ---------------------------------------------------------------------------
# Supported units — one test per unit with the specified factors
# ---------------------------------------------------------------------------


def test_milliseconds():
    assert parse_duration("500ms") == 500


def test_seconds():
    assert parse_duration("2s") == 2000


def test_minutes():
    assert parse_duration("1m") == 60000


def test_hours():
    assert parse_duration("1h") == 3600000


# ---------------------------------------------------------------------------
# Zero values — every unit
# ---------------------------------------------------------------------------


def test_zero_milliseconds():
    assert parse_duration("0ms") == 0


def test_zero_seconds():
    assert parse_duration("0s") == 0


def test_zero_minutes():
    assert parse_duration("0m") == 0


def test_zero_hours():
    assert parse_duration("0h") == 0


# ---------------------------------------------------------------------------
# Numeric values — integers and decimals
# ---------------------------------------------------------------------------


def test_large_integer_value():
    assert parse_duration("86400s") == 86400000


def test_decimal_seconds():
    assert parse_duration("1.5s") == 1500


def test_decimal_milliseconds_truncated():
    # 0.5 ms truncated to integer milliseconds
    assert parse_duration("0.5ms") == 0


def test_decimal_hours():
    assert parse_duration("0.5h") == 1800000


def test_decimal_minutes():
    assert parse_duration("2.5m") == 150000


def test_leading_zeros_accepted():
    assert parse_duration("007ms") == 7


# ---------------------------------------------------------------------------
# Surrounding whitespace — trimmed before matching
# ---------------------------------------------------------------------------


def test_leading_whitespace_stripped():
    assert parse_duration("  500ms") == 500


def test_trailing_whitespace_stripped():
    assert parse_duration("500ms  ") == 500


def test_surrounding_whitespace_stripped():
    assert parse_duration("  500ms  ") == 500


def test_tab_whitespace_stripped():
    assert parse_duration("\t1s\t") == 1000


def test_newline_whitespace_stripped():
    assert parse_duration("\n2m\n") == 120000


# ---------------------------------------------------------------------------
# Invalid inputs — must raise ValueError
# ---------------------------------------------------------------------------


def test_none_raises():
    with pytest.raises(ValueError):
        parse_duration(None)


def test_empty_string_raises():
    with pytest.raises(ValueError):
        parse_duration("")


def test_whitespace_only_raises():
    with pytest.raises(ValueError):
        parse_duration("   ")


def test_non_numeric_raises():
    with pytest.raises(ValueError):
        parse_duration("abc")


def test_unit_only_raises():
    with pytest.raises(ValueError):
        parse_duration("ms")


def test_missing_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("100")


def test_unknown_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("100x")


def test_unknown_unit_days_raises():
    with pytest.raises(ValueError):
        parse_duration("1d")


def test_unknown_unit_nanoseconds_raises():
    with pytest.raises(ValueError):
        parse_duration("100ns")


def test_negative_raises():
    with pytest.raises(ValueError):
        parse_duration("-1s")


def test_combined_units_raises():
    with pytest.raises(ValueError):
        parse_duration("1m30s")


def test_space_between_number_and_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("100 ms")


def test_unit_before_number_raises():
    with pytest.raises(ValueError):
        parse_duration("ms100")


def test_random_text_raises():
    with pytest.raises(ValueError):
        parse_duration("hello")


def test_multiple_values_raises():
    with pytest.raises(ValueError):
        parse_duration("100ms100ms")
