"""Tests for scao_live.duration.parse_duration.

parse_duration(s) converts a single non-negative integer with a unit suffix
(ms, s, m, h) into integer milliseconds. Malformed, ambiguous, or unsupported
inputs must raise ValueError.
"""

import pytest

from scao_live.duration import parse_duration

# ---------------------------------------------------------------------------
# Valid conversions — all four required units
# ---------------------------------------------------------------------------


def test_parse_milliseconds():
    assert parse_duration("100ms") == 100


def test_parse_seconds():
    assert parse_duration("5s") == 5000


def test_parse_minutes():
    assert parse_duration("2m") == 120000


def test_parse_hours():
    assert parse_duration("1h") == 3600000


# ---------------------------------------------------------------------------
# Zero values
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
    assert parse_duration("999999ms") == 999999


def test_large_seconds_one_day():
    # 86400 seconds = 1 day in milliseconds
    assert parse_duration("86400s") == 86400000


def test_large_hours_one_year():
    # 8760 hours ≈ one year in milliseconds
    assert parse_duration("8760h") == 31536000000


# ---------------------------------------------------------------------------
# Whitespace and case handling
# ---------------------------------------------------------------------------


def test_surrounding_whitespace_stripped():
    assert parse_duration("  100ms  ") == 100


def test_tab_surrounding_whitespace_stripped():
    assert parse_duration("\t100ms\t") == 100


def test_uppercase_units_accepted():
    assert parse_duration("100MS") == 100
    assert parse_duration("5S") == 5000
    assert parse_duration("2M") == 120000
    assert parse_duration("1H") == 3600000


def test_mixed_case_units_accepted():
    assert parse_duration("100Ms") == 100
    assert parse_duration("5s") == 5000
    assert parse_duration("2m") == 120000
    assert parse_duration("1h") == 3600000


# ---------------------------------------------------------------------------
# Invalid inputs — must raise ValueError
# ---------------------------------------------------------------------------


def test_empty_string_raises():
    with pytest.raises(ValueError):
        parse_duration("")


def test_whitespace_only_raises():
    with pytest.raises(ValueError):
        parse_duration("   ")


def test_no_unit_raises():
    # Ambiguous bare numeric form — reject
    with pytest.raises(ValueError):
        parse_duration("100")


def test_no_number_raises():
    with pytest.raises(ValueError):
        parse_duration("ms")


def test_negative_raises():
    with pytest.raises(ValueError):
        parse_duration("-100ms")


def test_fractional_raises():
    with pytest.raises(ValueError):
        parse_duration("1.5s")


def test_unsupported_unit_days_raises():
    with pytest.raises(ValueError):
        parse_duration("100d")


def test_unsupported_unit_nanoseconds_raises():
    with pytest.raises(ValueError):
        parse_duration("100ns")


def test_unsupported_unit_weeks_raises():
    with pytest.raises(ValueError):
        parse_duration("1w")


def test_combined_units_raises():
    with pytest.raises(ValueError):
        parse_duration("1m30s")


def test_space_between_number_and_unit_raises():
    # Ambiguous internal whitespace — reject
    with pytest.raises(ValueError):
        parse_duration("100 ms")


def test_plus_prefix_raises():
    # Ambiguous signed form — reject
    with pytest.raises(ValueError):
        parse_duration("+100ms")


def test_unit_before_number_raises():
    with pytest.raises(ValueError):
        parse_duration("ms100")


def test_random_text_raises():
    with pytest.raises(ValueError):
        parse_duration("hello")


def test_multiple_values_raises():
    with pytest.raises(ValueError):
        parse_duration("100ms100ms")


def test_leading_zeros_accepted():
    # Leading zeros are still a valid non-negative integer representation
    assert parse_duration("007ms") == 7
