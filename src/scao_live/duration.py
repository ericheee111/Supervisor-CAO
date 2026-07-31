"""Duration string parser for the scao_live package.

``parse_duration(s)`` converts a non-negative integer magnitude followed by
exactly one supported unit (``ms``, ``s``, ``m``, ``h`` — case-insensitive)
into integer milliseconds.  Surrounding whitespace is stripped before parsing.

The parser is strict — the following all raise ``ValueError`` with clear
messages:

- Non-string values (e.g. ``None``, integers).
- Empty strings (after whitespace stripping).
- Missing unit (e.g. ``"500"``).
- Unknown / unsupported units (e.g. ``"1d"``, ``"100ns"``).
- Negative values (e.g. ``"-1s"``).
- Non-integer magnitudes / malformed input (e.g. ``"1.5s"``, ``"abc"``,
  ``"1m30s"``).
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

# An integer magnitude (one or more digits) immediately followed by exactly
# one supported unit.  ``ms`` is listed before ``m`` so the two-character unit
# is matched first, preventing a greedy single-character match that would
# leave a dangling trailing ``s``.  The ``^`` and ``$`` anchors (applied after
# whitespace stripping) reject combined-unit strings such as ``"1m30s"`` and
# any trailing garbage.  The units are lowercased before matching, so
# case-insensitive variants like ``"500MS"`` are accepted.  ``\d+`` accepts
# only integer magnitudes, so decimal values like ``"1.5s"`` are rejected.
_PATTERN = re.compile(r"^(\d+)(ms|s|m|h)$", re.IGNORECASE)


def parse_duration(s: str) -> int:
    """Parse a duration string into integer milliseconds.

    Surrounding whitespace is stripped, and the unit suffix is matched
    case-insensitively.  Supports ``ms``, ``s``, ``m``, and ``h``.

    Args:
        s: Duration string such as ``"500ms"``, ``"2s"``, ``"1m"``, or
            ``"1h"``.  May be surrounded by whitespace; the unit may be
            upper or lower case.

    Returns:
        The duration in milliseconds as a non-negative ``int``.

    Raises:
        ValueError: If *s* is not a string, is empty after stripping, is
            missing a unit, uses an unknown unit, is negative, or contains
            a non-integer / malformed magnitude.
    """
    if not isinstance(s, str):
        # Plan spec mandates ValueError (not TypeError) for non-string input
        # so all invalid-input cases share a single exception type.
        raise ValueError(  # noqa: TRY004
            f"duration must be a string, got {type(s).__name__}: {s!r}"
        )
    stripped = s.strip()
    if not stripped:
        raise ValueError("duration string must not be empty")
    match = _PATTERN.match(stripped)
    if match is None:
        raise ValueError(f"invalid duration string: {s!r}")
    value_str, unit = match.group(1), match.group(2).lower()
    return int(value_str) * _UNIT_TO_MS[unit]
