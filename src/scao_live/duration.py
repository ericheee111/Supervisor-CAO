"""Duration string parser for Supervisor-CAO acceptance.

Parses human-readable duration strings such as ``'1h30m'`` or ``'90s'`` into
integer seconds using only the Python standard library.

Supported units:
    s - seconds      (1)
    m - minutes      (60)
    h - hours        (3600)

A duration string is a complete sequence of one or more ``<number><unit>``
segments with no separators, e.g. ``'1h30m45s'``.  Each unit may appear at most
once; duplicate units are rejected.
"""
from __future__ import annotations

import re

_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
}

# A token is one or more digits followed by exactly one supported unit letter.
# Only h, m, s are supported — other letters (including 'd') are rejected as
# unsupported units.
_TOKEN_RE = re.compile(r"(\d+)([smh])")


def parse_duration(s: str) -> int:
    """Parse a duration string into total seconds.

    Supports one or more ``<number><unit>`` segments where ``unit`` is one of
    ``s`` (seconds), ``m`` (minutes), or ``h`` (hours).  Each unit may appear
    at most once.

    Examples:
        >>> parse_duration('90s')
        90
        >>> parse_duration('1h30m')
        5400
        >>> parse_duration('1h30m30s')
        5430

    Args:
        s: Duration string such as ``'1h30m'`` or ``'90s'``.

    Returns:
        Total number of seconds as a non-negative ``int``.

    Raises:
        ValueError: If *s* is not a string, is empty, contains malformed
            tokens, negative values, unsupported units, duplicate units, or
            trailing characters without a unit.
    """
    if not isinstance(s, str):
        raise ValueError(f"expected a string, got {type(s).__name__}")

    stripped = s.strip()
    if not stripped:
        raise ValueError("duration string must not be empty")

    total = 0
    pos = 0
    matched_any = False
    seen: set[str] = set()

    for match in _TOKEN_RE.finditer(stripped):
        # Reject gaps between tokens (whitespace, stray characters, signs).
        if match.start() != pos:
            raise ValueError(
                f"malformed duration near position {pos}: "
                f"{stripped[pos:match.start()]!r}"
            )
        number = int(match.group(1))
        unit = match.group(2)
        if unit in seen:
            raise ValueError(f"duplicate unit {unit!r} in duration {s!r}")
        total += number * _UNIT_SECONDS[unit]
        seen.add(unit)
        pos = match.end()
        matched_any = True

    if not matched_any:
        raise ValueError(f"no valid duration tokens found in {s!r}")

    # Reject trailing characters (e.g. "1h30" -> "30" has no unit).
    if pos != len(stripped):
        raise ValueError(
            f"trailing characters without unit near position {pos}: "
            f"{stripped[pos:]!r}"
        )

    return total
