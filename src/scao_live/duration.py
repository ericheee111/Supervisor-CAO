"""Duration string parser for the scao_live package.

``parse_duration(s)`` converts a numeric value followed by a unit (``ms``,
``s``, ``m``, ``h``) into integer milliseconds. The parser is strict:
``None``, empty strings, non-numeric values, missing units, unknown units,
and combined-unit expressions are all rejected with ``ValueError``.
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

# A non-negative numeric value (integer or decimal) immediately followed by
# exactly one supported unit. ``ms`` is listed before ``m`` so the
# two-character unit is tried first, avoiding a greedy single-character match
# that would leave a dangling trailing character. The ``$`` anchor rejects
# combined-unit strings such as ``"1m30s"`` and any trailing garbage.
_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")


def parse_duration(s: str | None) -> int:
    """Parse a duration string into integer milliseconds.

    Accepts a trimmed numeric value (integer or decimal) followed by one of
    the units ``ms``, ``s``, ``m``, or ``h``.

    Args:
        s: Duration string such as ``"500ms"``, ``"2s"``, ``"1.5s"``,
            ``"1m"``, or ``"1h"``.

    Returns:
        The duration in milliseconds as an ``int``.

    Raises:
        ValueError: If *s* is ``None``, empty, malformed, uses an
            unsupported unit, or contains combined-unit expressions.
    """
    if s is None:
        raise ValueError("duration string must not be None")
    stripped = s.strip()
    if not stripped:
        raise ValueError("duration string must not be empty")
    match = _PATTERN.match(stripped)
    if match is None:
        raise ValueError(f"invalid duration string: {s!r}")
    value_str, unit = match.group(1), match.group(2)
    return int(float(value_str) * _UNIT_TO_MS[unit])
