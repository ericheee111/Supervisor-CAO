"""Duration string parser for the scao_live package.

``parse_duration(s)`` converts a single non-negative integer with a unit
suffix (``ms``, ``s``, ``m``, ``h``) into integer milliseconds. Surrounding
whitespace is stripped; empty, malformed, unsupported-unit, negative, and
combined-unit inputs raise ``ValueError``.
"""

from __future__ import annotations

import re

__all__ = ["parse_duration"]

# Match a non-negative integer immediately followed by a supported unit.
# The unit alternatives are ordered so that the two-character ``ms`` is
# tried before the single-character units, preventing a greedy match on
# ``m`` from leaving a dangling ``s``.
_UNIT_RE = re.compile(r"^(\d+)(ms|s|m|h)$", re.IGNORECASE)

_MULTIPLIERS = {
    "ms": 1,
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
}


def parse_duration(s: str) -> int:
    """Convert a duration string into integer milliseconds.

    The input must be a single non-negative integer followed by a unit suffix
    (``ms``, ``s``, ``m``, ``h``), case-insensitive. Surrounding whitespace is
    stripped before matching.

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
    text = s.strip()
    if not text:
        raise ValueError("duration string is empty")
    match = _UNIT_RE.match(text)
    if match is None:
        raise ValueError(f"unsupported duration: {s!r}")
    value, unit = match.group(1), match.group(2).lower()
    return int(value) * _MULTIPLIERS[unit]
