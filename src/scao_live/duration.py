"""Duration string parser for the scao_live package.

``parse_duration(s)`` converts a single non-negative integer with a unit
suffix (``ms``, ``s``, ``m``, ``h``) into integer milliseconds. The parser
is intentionally strict: combined-unit expressions, negative values,
fractional values, unsupported units, and non-numeric inputs are all
rejected with ``ValueError``.
"""

from __future__ import annotations

import re

__all__ = ["parse_duration"]

# Multiplier from each supported unit to milliseconds.
_UNIT_TO_MS: dict[str, int] = {
    "ms": 1,
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
}

# One non-negative integer followed by exactly one supported unit.
# ``ms`` is listed before ``m``/``s`` so the two-character unit is tried
# first, avoiding a greedy single-character match that leaves a dangling
# trailing character. The ``$`` anchor rejects combined-unit strings
# such as ``"1m30s"``.
_PATTERN = re.compile(r"^(\d+)(ms|s|m|h)$", re.IGNORECASE)


def parse_duration(s: str) -> int:
    """Parse a duration string into milliseconds.

    The input must be a single non-negative integer immediately followed
    by a case-insensitive unit suffix (``ms``, ``s``, ``m``, ``h``).
    Surrounding whitespace is stripped before matching.

    Args:
        s: Duration string such as ``"100ms"``, ``"5s"``, ``"2m"``, ``"1h"``.

    Returns:
        The equivalent number of milliseconds as an ``int``.

    Raises:
        TypeError: If *s* is not a string.
        ValueError: If *s* is empty, malformed, uses an unsupported unit,
            contains a fractional or negative value, or uses ambiguous forms
            such as bare numbers, signed prefixes, or internal whitespace.
    """
    if not isinstance(s, str):
        raise TypeError(f"duration must be a string, got {type(s).__name__}")
    stripped = s.strip()
    if not stripped:
        raise ValueError("duration string must not be empty")
    match = _PATTERN.match(stripped)
    if match is None:
        raise ValueError(f"invalid duration string: {s!r}")
    value_str, unit = match.group(1), match.group(2).lower()
    return int(value_str) * _UNIT_TO_MS[unit]
