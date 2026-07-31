"""Tests for scao_live.duration.parse_duration."""

import pytest

from scao_live.duration import parse_duration

# --- Supported units ---------------------------------------------------------


class TestSupportedUnits:
    """Each declared unit converts via its explicit millisecond factor."""

    def test_milliseconds(self):
        assert parse_duration("100ms") == 100

    def test_seconds(self):
        assert parse_duration("2s") == 2000

    def test_minutes(self):
        assert parse_duration("1m") == 60000

    def test_hours(self):
        assert parse_duration("1h") == 3600000

    def test_large_hours(self):
        assert parse_duration("3h") == 10800000

    def test_each_unit_uses_explicit_factor(self):
        """Verify exact factor per unit: ms=1, s=1000, m=60000, h=3600000."""
        assert parse_duration("1ms") == 1
        assert parse_duration("1s") == 1000
        assert parse_duration("1m") == 60000
        assert parse_duration("1h") == 3600000


# --- Zero --------------------------------------------------------------------


class TestZero:
    def test_zero_milliseconds(self):
        assert parse_duration("0ms") == 0

    def test_zero_seconds(self):
        assert parse_duration("0s") == 0

    def test_zero_minutes(self):
        assert parse_duration("0m") == 0

    def test_zero_hours(self):
        assert parse_duration("0h") == 0


# --- Surrounding whitespace --------------------------------------------------


class TestWhitespace:
    def test_leading_whitespace(self):
        assert parse_duration("  100ms") == 100

    def test_trailing_whitespace(self):
        assert parse_duration("100ms  ") == 100

    def test_surrounding_whitespace(self):
        assert parse_duration("   2s  ") == 2000

    def test_tab_whitespace(self):
        assert parse_duration("\t5s\t") == 5000

    def test_newline_whitespace(self):
        assert parse_duration("\n1m\n") == 60000


# --- Fractional values -------------------------------------------------------


class TestFractional:
    def test_fractional_seconds(self):
        assert parse_duration("1.5s") == 1500

    def test_fractional_milliseconds(self):
        assert parse_duration("0.5ms") == 0

    def test_fractional_minutes(self):
        assert parse_duration("0.5m") == 30000

    def test_fractional_hours(self):
        assert parse_duration("0.25h") == 900000


# --- Invalid inputs ----------------------------------------------------------


class TestInvalidInputs:
    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("")

    def test_whitespace_only_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("   ")

    def test_missing_unit_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("100")

    def test_invalid_number_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("abcms")

    def test_negative_value_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("-1s")

    def test_unknown_unit_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("100x")

    def test_unknown_unit_days_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("1d")

    def test_unit_without_number_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("s")

    def test_number_and_unit_with_space_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("100 ms")

    def test_uppercase_unit_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("100MS")

    def test_double_unit_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("1mss")

    def test_zero_with_missing_unit_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_duration("0")
