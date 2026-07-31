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

# Match a nonnegative numeric value (int or decimal) directly followed by a
# known unit, with no gap.  ``ms`` is listed before ``s``/``m`` so the regex
# engine commits to the two-character unit first.
_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")


def parse_duration(s: str) -> int:
    """Parse ``s`` into integer milliseconds.

    Accepted forms: a nonnegative numeric value (``"100"``, ``"1.5"``)
    immediately followed by one of the units ``ms``, ``s``, ``m``, ``h``.
    Surrounding whitespace is stripped before matching.

    Args:
        s: Duration string, e.g. ``"100ms"``, ``"2s"``, ``"1m"``, ``"1h"``.

    Returns:
        Integer number of milliseconds.

    Raises:
        ValueError: If ``s`` is empty, malformed, negative, missing a unit,
            or uses an unknown unit.
    """
    text = s.strip()
    if not text:
        raise ValueError("duration string is empty")
    match = _PATTERN.match(text)
    if match is None:
        raise ValueError(f"malformed duration: {s!r}")
    value = float(match.group(1))
    return int(value * _FACTORS[match.group(2)])
