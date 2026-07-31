"""Duration string parsing for scao_live.

Parses compact duration strings like ``"100ms"`` or ``"2s"`` into integer
milliseconds using explicit per-unit factors.
"""

from __future__ import annotations

import re

# Explicit millisecond factors per supported unit.
_MS_PER_MS: int = 1
_MS_PER_S: int = 1_000
_MS_PER_M: int = 60 * _MS_PER_S
_MS_PER_H: int = 60 * _MS_PER_M

_FACTORS: dict[str, int] = {
    "ms": _MS_PER_MS,
    "s": _MS_PER_S,
    "m": _MS_PER_M,
    "h": _MS_PER_H,
}

# Match a nonnegative integer magnitude directly followed by a known unit,
# with no gap.  ``ms`` is listed before ``s``/``m`` so the regex engine commits
# to the two-character unit first.  Only integer magnitudes are accepted;
# decimal points are rejected.  Units are matched case-insensitively.
_PATTERN = re.compile(r"^(\d+)(ms|s|m|h)$", re.IGNORECASE)


def parse_duration(s: str) -> int:
    """Parse ``s`` into integer milliseconds.

    Accepted forms: a nonnegative integer magnitude (``"100"``, ``"2"``)
    immediately followed by one of the units ``ms``, ``s``, ``m``, ``h``
    (case-insensitively).  Surrounding whitespace is stripped before matching.

    Args:
        s: Duration string, e.g. ``"100ms"``, ``"2s"``, ``"1m"``, ``"1h"``.

    Returns:
        Integer number of milliseconds.

    Raises:
        ValueError: If ``s`` is not a string, empty, malformed, negative,
            decimal, missing a unit, or uses an unknown unit.
    """
    if not isinstance(s, str):
        # Plan spec requires ValueError (not TypeError) for all invalid input
        # categories, including non-string.  Suppress the TRY004 convention.
        raise ValueError(f"duration must be a string, got {type(s).__name__}")  # noqa: TRY004
    text = s.strip()
    if not text:
        raise ValueError("duration string is empty")
    match = _PATTERN.match(text)
    if match is None:
        raise ValueError(f"malformed duration: {s!r}")
    # int() on the digit string avoids float precision loss; Python ints are
    # arbitrary-precision so multiplication cannot overflow.
    value = int(match.group(1))
    unit = match.group(2).lower()
    return value * _FACTORS[unit]
