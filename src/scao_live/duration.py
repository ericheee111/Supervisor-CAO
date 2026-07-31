"""Duration string parser for the scao_live package.

parse_duration(s) converts a single numeric value followed by a unit
(ms, s, m, h) into milliseconds. The parser is intentionally strict:
combined-unit expressions, negative values, unsupported units, and
non-numeric inputs are rejected with ValueError.
"""

from __future__ import annotations

import re

__all__ = ["parse_duration"]

# Multiplier from each unit to milliseconds.
_UNIT_TO_MS: dict[str, int] = {
    "ms": 1,
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
}

# One non-negative integer followed by exactly one supported unit.
# Anchors prevent combined-unit strings like "1s2m" from matching.
_PATTERN = re.compile(r"^(\d+)(ms|s|m|h)$", re.IGNORECASE)


def parse_duration(s: str) -> int:
    """Parse a duration string into milliseconds.

    Accepts a single non-negative integer followed by a case-insensitive
    unit (``ms``, ``s``, ``m``, ``h``). Surrounding whitespace is stripped.

    Args:
        s: Duration string such as ``"500ms"``, ``"2s"``, ``"1m"``, ``"1h"``.

    Returns:
        The duration in milliseconds.

    Raises:
        ValueError: If the input is empty, malformed, uses an unsupported
            unit, is negative, or contains combined-unit expressions.
    """
    stripped = s.strip()
    if not stripped:
        raise ValueError("duration string must not be empty")
    match = _PATTERN.match(stripped)
    if match is None:
        raise ValueError(f"invalid duration string: {s!r}")
    value_str, unit = match.groups()
    return int(value_str) * _UNIT_TO_MS[unit.lower()]
