"""Tests for scao_live.duration.parse_duration.

Covers the four required unit examples and all invalid-input behaviours
required by the plan:

- Empty strings and ``None``.
- Unsupported units (``d``, ``ns``, ``x``).
- Non-numeric values.
- Negative values.
- Uppercase units (``MS``, ``S``, ``M``, ``H``).
- Whitespace-padded strings (leading, trailing, surrounding, tab, newline).
- Combined-unit expressions and space between number and unit.
- Decimal values are accepted and truncated to integer milliseconds.
"""

from __future__ import annotations

import pytest

from scao_live.duration import parse_duration


# ---------------------------------------------------------------------------
# Four required examples — one per supported unit
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
# Decimal values — accepted and truncated toward zero to int milliseconds
# ---------------------------------------------------------------------------


def test_decimal_seconds():
    assert parse_duration("1.5s") == 1500


def test_decimal_hours():
    assert parse_duration("0.5h") == 1800000


def test_decimal_minutes():
    assert parse_duration("2.5m") == 150000


def test_decimal_ms_truncated_to_zero():
    # 0.5 ms → 0 (truncated toward zero)
    assert parse_duration("0.5ms") == 0


def test_large_integer_value():
    assert parse_duration("86400s") == 86400000


# ---------------------------------------------------------------------------
# Invalid: None and empty strings
# ---------------------------------------------------------------------------


def test_none_raises():
    with pytest.raises(ValueError):
        parse_duration(None)  # type: ignore[arg-type]


def test_empty_string_raises():
    with pytest.raises(ValueError):
        parse_duration("")


def test_whitespace_only_raises():
    with pytest.raises(ValueError):
        parse_duration("   ")


# ---------------------------------------------------------------------------
# Invalid: whitespace-padded strings (must NOT be trimmed)
# ---------------------------------------------------------------------------


def test_leading_whitespace_raises():
    with pytest.raises(ValueError):
        parse_duration("  500ms")


def test_trailing_whitespace_raises():
    with pytest.raises(ValueError):
        parse_duration("500ms  ")


def test_surrounding_whitespace_raises():
    with pytest.raises(ValueError):
        parse_duration("  500ms  ")


def test_tab_padding_raises():
    with pytest.raises(ValueError):
        parse_duration("\t1s\t")


def test_newline_padding_raises():
    with pytest.raises(ValueError):
        parse_duration("\n2m\n")


# ---------------------------------------------------------------------------
# Invalid: uppercase units
# ---------------------------------------------------------------------------


def test_uppercase_ms_raises():
    with pytest.raises(ValueError):
        parse_duration("500MS")


def test_uppercase_s_raises():
    with pytest.raises(ValueError):
        parse_duration("2S")


def test_uppercase_m_raises():
    with pytest.raises(ValueError):
        parse_duration("1M")


def test_uppercase_h_raises():
    with pytest.raises(ValueError):
        parse_duration("1H")


def test_mixed_case_ms_raises():
    with pytest.raises(ValueError):
        parse_duration("500Ms")


# ---------------------------------------------------------------------------
# Invalid: unsupported units
# ---------------------------------------------------------------------------


def test_unsupported_unit_days_raises():
    with pytest.raises(ValueError):
        parse_duration("1d")


def test_unsupported_unit_nanoseconds_raises():
    with pytest.raises(ValueError):
        parse_duration("100ns")


def test_unsupported_unit_x_raises():
    with pytest.raises(ValueError):
        parse_duration("100x")


# ---------------------------------------------------------------------------
# Invalid: non-numeric values and unit-only strings
# ---------------------------------------------------------------------------


def test_non_numeric_raises():
    with pytest.raises(ValueError):
        parse_duration("abc")


def test_unit_only_raises():
    with pytest.raises(ValueError):
        parse_duration("ms")


def test_unit_only_s_raises():
    with pytest.raises(ValueError):
        parse_duration("s")


def test_random_text_raises():
    with pytest.raises(ValueError):
        parse_duration("hello")


# ---------------------------------------------------------------------------
# Invalid: negative values
# ---------------------------------------------------------------------------


def test_negative_seconds_raises():
    with pytest.raises(ValueError):
        parse_duration("-1s")


def test_negative_milliseconds_raises():
    with pytest.raises(ValueError):
        parse_duration("-500ms")


def test_negative_minutes_raises():
    with pytest.raises(ValueError):
        parse_duration("-1m")


def test_negative_hours_raises():
    with pytest.raises(ValueError):
        parse_duration("-1h")


# ---------------------------------------------------------------------------
# Invalid: missing unit, combined units, space between number and unit
# ---------------------------------------------------------------------------


def test_missing_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("100")


def test_combined_units_raises():
    with pytest.raises(ValueError):
        parse_duration("1m30s")


def test_space_between_number_and_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("100 ms")


def test_unit_before_number_raises():
    with pytest.raises(ValueError):
        parse_duration("ms100")


def test_multiple_values_raises():
    with pytest.raises(ValueError):
        parse_duration("100ms100ms")
