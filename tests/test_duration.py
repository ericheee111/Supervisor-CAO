"""Tests for :func:`scao_live.duration.parse_duration`.

Covers single-unit and compound durations, day conversion, zero values, and
all invalid-input cases that must raise :class:`ValueError`.
"""
from __future__ import annotations

import pytest

from scao_live.duration import parse_duration


# ---------------------------------------------------------------------------
# Happy paths: single-unit durations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("90s", 90),
        ("0s", 0),
        ("5m", 300),
        ("0m", 0),
        ("2h", 7200),
        ("0h", 0),
        ("1d", 86400),
        ("0d", 0),
    ],
)
def test_single_unit(value: str, expected: int) -> None:
    """Single-unit duration strings return the correct number of seconds."""
    assert parse_duration(value) == expected


# ---------------------------------------------------------------------------
# Happy paths: compound durations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1h30m", 5400),
        ("1h30m30s", 5430),
        ("2h30m", 9000),
        ("1d2h", 93600),
        ("1d2h30m45s", 95445),
        ("1d1h1m1s", 90061),
        ("10s", 10),
        ("100s", 100),
    ],
)
def test_compound_durations(value: str, expected: int) -> None:
    """Compound duration strings sum correctly across units."""
    assert parse_duration(value) == expected


# ---------------------------------------------------------------------------
# Day conversion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1d", 86400),
        ("2d", 172800),
        ("7d", 604800),
        ("1d1s", 86401),
    ],
)
def test_day_conversion(value: str, expected: int) -> None:
    """Day unit converts to 86 400 seconds and combines with other units."""
    assert parse_duration(value) == expected


# ---------------------------------------------------------------------------
# Zero values
# ---------------------------------------------------------------------------

def test_zero_single_units() -> None:
    """A zero coefficient yields zero seconds for every supported unit."""
    assert parse_duration("0s") == 0
    assert parse_duration("0m") == 0
    assert parse_duration("0h") == 0
    assert parse_duration("0d") == 0


def test_zero_compound() -> None:
    """All-zero compound durations yield zero."""
    assert parse_duration("0h0m0s") == 0
    assert parse_duration("0d0h0m0s") == 0


# ---------------------------------------------------------------------------
# Invalid inputs that must raise ValueError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "",                   # empty string
        "   ",                # whitespace only
        "abc",                # non-numeric
        "1x",                 # unknown unit
        "1",                  # unitless (no unit at all)
        "1h30",               # trailing unitless number
        "-5s",                # negative value
        "1h-30m",             # negative inside compound
        "1.5h",               # non-integer / float
        "h",                  # unit without number
        "s",                  # unit without number
        "1h 30m",             # internal whitespace
        "1H30M",              # wrong case
        "1s2",                # trailing digits after unit
        "++5s",               # stray characters
    ],
)
def test_invalid_inputs_raise_value_error(value: str) -> None:
    """Malformed, empty, negative, unknown-unit, and unitless inputs raise."""
    with pytest.raises(ValueError):
        parse_duration(value)


def test_non_string_raises_value_error() -> None:
    """Non-string input raises ValueError."""
    with pytest.raises(ValueError):
        parse_duration(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_duration(90)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

def test_returns_int() -> None:
    """The return value is an instance of int."""
    result = parse_duration("90s")
    assert isinstance(result, int)
    assert result == 90
