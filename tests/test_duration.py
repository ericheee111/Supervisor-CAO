"""Tests for :func:`scao_live.duration.parse_duration`.

Covers the plan-required examples (1h30m=5400, 90s=90), individual and
combined units, zero values, and all invalid-input cases that must raise
:class:`ValueError`.  Includes a type assertion that the result is ``int``.
"""
from __future__ import annotations

import pytest

from scao_live.duration import parse_duration


# ---------------------------------------------------------------------------
# Plan examples + return-type assertion
# ---------------------------------------------------------------------------


def test_parse_duration_examples() -> None:
    """Plan-mandated examples produce exact integer totals.

    ``1h30m`` must equal 5400 seconds and ``90s`` must equal 90 seconds.
    The return value must be a genuine ``int`` (not ``bool`` or ``float``).
    """
    assert parse_duration("1h30m") == 5400
    assert parse_duration("90s") == 90

    # --- Type assertion: result must be int (and not a bool subclass) ---
    result = parse_duration("1h30m")
    assert isinstance(result, int)
    assert not isinstance(result, bool)  # bool is a subclass of int; exclude it
    assert type(result) is int


# ---------------------------------------------------------------------------
# Individual units, combined units, and zero values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # --- individual units ---
        ("1s", 1),
        ("1m", 60),
        ("1h", 3600),
        ("90s", 90),
        ("5m", 300),
        ("2h", 7200),
        # --- combined units ---
        ("1h30m", 5400),
        ("1h30m30s", 5430),
        ("2h30m", 9000),
        ("1h1s", 3601),
        ("1m1s", 61),
        ("1h1m1s", 3661),
        # --- zero values ---
        ("0s", 0),
        ("0m", 0),
        ("0h", 0),
        ("0h0m0s", 0),
    ],
)
def test_parse_duration_individual_and_combined_units(
    value: str, expected: int
) -> None:
    """Individual units, combined units, and zero values return correct seconds."""
    assert parse_duration(value) == expected
    assert isinstance(parse_duration(value), int)


# ---------------------------------------------------------------------------
# Invalid inputs that must raise ValueError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # --- empty ---
        "",                   # empty string
        "   ",                # whitespace only
        # --- malformed / non-numeric ---
        "abc",                # non-numeric
        "h",                  # unit without number
        "s",                  # unit without number
        "m",                  # unit without number
        "1.5h",               # non-integer / float
        "1h 30m",             # internal whitespace
        "++5s",               # stray characters
        # --- unsupported units ---
        "1x",                 # unknown unit
        "1d",                 # day unit not supported (only h/m/s)
        "1d2h",               # unsupported unit in compound
        "1H30M",              # wrong case
        # --- unitless / trailing input ---
        "1",                  # unitless (no unit at all)
        "1h30",               # trailing unitless number
        "1s2",                # trailing digits after unit
        # --- negative components ---
        "-5s",                # negative value
        "1h-30m",             # negative inside compound
        # --- duplicate units ---
        "1h2h",               # duplicate hour
        "1m2m",               # duplicate minute
        "1s2s",               # duplicate second
        "1h1h1m",             # duplicate hour in compound
    ],
)
def test_parse_duration_invalid_inputs(value: str) -> None:
    """Empty, malformed, unsupported, negative, duplicate, and trailing inputs raise ValueError."""
    with pytest.raises(ValueError):
        parse_duration(value)


# ---------------------------------------------------------------------------
# Non-string input
# ---------------------------------------------------------------------------


def test_parse_duration_non_string_raises() -> None:
    """Non-string input raises ValueError."""
    with pytest.raises(ValueError):
        parse_duration(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_duration(90)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_duration(5400.0)  # type: ignore[arg-type]
