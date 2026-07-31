"""Tests for scao_live.duration.parse_duration.

Covers every supported unit, zero, large values, surrounding whitespace,
uppercase units, and all invalid-input behaviours required by the plan:
empty, missing unit, unknown unit, non-numeric, negative, decimal, and
non-string values.
"""

import pytest

from scao_live.duration import parse_duration

# ---------------------------------------------------------------------------
# Supported units — one example per unit
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
# Large values
# ---------------------------------------------------------------------------


def test_large_milliseconds():
    assert parse_duration("9999999ms") == 9999999


def test_large_seconds():
    assert parse_duration("3600s") == 3_600_000


def test_large_hours():
    assert parse_duration("1000h") == 3_600_000_000


# ---------------------------------------------------------------------------
# Return type is int
# ---------------------------------------------------------------------------


def test_returns_int():
    assert isinstance(parse_duration("1s"), int)


# ---------------------------------------------------------------------------
# Surrounding whitespace is stripped
# ---------------------------------------------------------------------------


def test_leading_whitespace():
    assert parse_duration("   500ms") == 500


def test_trailing_whitespace():
    assert parse_duration("500ms   ") == 500


def test_surrounding_whitespace():
    assert parse_duration("   500ms   ") == 500


def test_tab_whitespace():
    assert parse_duration("\t2s\t") == 2000


def test_newline_whitespace():
    assert parse_duration("\n1m\n") == 60000


# ---------------------------------------------------------------------------
# Uppercase / mixed-case units are accepted (case-insensitive)
# ---------------------------------------------------------------------------


def test_uppercase_ms():
    assert parse_duration("500MS") == 500


def test_uppercase_s():
    assert parse_duration("2S") == 2000


def test_uppercase_m():
    assert parse_duration("1M") == 60000


def test_uppercase_h():
    assert parse_duration("1H") == 3600000


def test_mixed_case_ms():
    assert parse_duration("500Ms") == 500


def test_mixed_case_s():
    assert parse_duration("2s") == 2000


def test_uppercase_with_whitespace():
    assert parse_duration("  1H  ") == 3600000


# ---------------------------------------------------------------------------
# Invalid inputs — each raises ValueError
# ---------------------------------------------------------------------------


def test_empty_string_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_duration("")


def test_whitespace_only_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_duration("   ")


def test_none_raises():
    with pytest.raises(ValueError, match="string"):
        parse_duration(None)  # type: ignore[arg-type]


def test_integer_raises():
    with pytest.raises(ValueError, match="string"):
        parse_duration(500)  # type: ignore[arg-type]


def test_list_raises():
    with pytest.raises(ValueError, match="string"):
        parse_duration(["500ms"])  # type: ignore[arg-type]


def test_missing_unit_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("500")


def test_unknown_unit_d_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("1d")


def test_unknown_unit_ns_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("100ns")


def test_unknown_unit_x_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("5x")


def test_unit_only_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("ms")


def test_non_numeric_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("abc")


def test_negative_value_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("-1s")


def test_decimal_magnitude_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("1.5s")


def test_combined_units_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("1m30s")


def test_space_between_number_and_unit_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("1 s")


def test_trailing_garbage_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("1sextra")


def test_plus_sign_raises():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("+1s")


# ---------------------------------------------------------------------------
# Error messages contain the offending value for clarity
# ---------------------------------------------------------------------------


def test_error_message_contains_value():
    with pytest.raises(ValueError, match="1d"):
        parse_duration("1d")
