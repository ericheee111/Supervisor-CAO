"""Duration string parser for the scao_live package.

``parse_duration(s)`` converts an integer magnitude followed by exactly one
supported unit (``ms``, ``s``, ``m``, ``h``) into integer milliseconds.

The parser is strict — the following all raise ``ValueError``:

- ``None`` or empty strings.
- Whitespace-padded strings (e.g. ``" 500ms"``, ``"500ms "``).
- Uppercase units (e.g. ``"500MS"``, ``"2S"``).
- Unsupported units (e.g. ``"1d"``, ``"100ns"``).
- Negative values (e.g. ``"-1s"``).
- Non-numeric values, missing units, or combined-unit expressions.
- Decimal magnitudes (e.g. ``"1.5s"``) — only integer magnitudes are accepted.
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
# leave a dangling trailing ``s``.  The ``^`` and ``$`` anchors reject any
# leading/trailing whitespace, combined-unit strings such as ``"1m30s"``, and
# any trailing garbage.  Only lowercase units match, so uppercase variants like
# ``"500MS"`` are rejected.  ``\d+`` accepts only integer magnitudes, so
# decimal values like ``"1.5s"`` are rejected.
_PATTERN = re.compile(r"^(\d+)(ms|s|m|h)$")


def parse_duration(s: str) -> int:
    """Parse a duration string into integer milliseconds.

    Accepts an integer magnitude followed by exactly one supported unit:
    ``ms``, ``s``, ``m``, or ``h``.

    Args:
        s: Duration string such as ``"500ms"``, ``"2s"``, ``"1m"``, or
            ``"1h"``.  Must not be ``None``, empty, or surrounded by
            whitespace.

    Returns:
        The duration in milliseconds as an ``int``.

    Raises:
        ValueError: If *s* is falsy (``None`` or empty), contains
            leading/trailing whitespace, uses an uppercase or unsupported
            unit, is negative, non-numeric, contains a decimal magnitude,
            or contains combined-unit expressions.
    """
    if not s:
        raise ValueError("duration string must not be empty or None")
    match = _PATTERN.match(s)
    if match is None:
        raise ValueError(f"invalid duration string: {s!r}")
    value_str, unit = match.group(1), match.group(2)
    return int(value_str) * _UNIT_TO_MS[unit]
