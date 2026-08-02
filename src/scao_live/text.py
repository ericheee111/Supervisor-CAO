"""Whitespace normalization utilities for scao_live.

Provides a stdlib-only function to normalize whitespace in strings by
collapsing runs of whitespace into single spaces and stripping edges.
"""

from __future__ import annotations

import re

#: Matches one or more Unicode whitespace characters (spaces, tabs, newlines,
#: carriage returns, form feeds, vertical tabs, and other Unicode separators).
_WHITESPACE_RUN: re.Pattern[str] = re.compile(r"\s+")


def normalize_spaces(text: str) -> str:
    """Normalize whitespace in *text*.

    Collapses runs of whitespace (spaces, tabs, newlines, carriage returns,
    form feeds, etc.) into a single space, strips leading and trailing
    whitespace, and returns an empty string for empty or whitespace-only
    input.

    Args:
        text: The input string to normalize.

    Returns:
        The whitespace-normalized string.  Returns ``""`` when *text* is
        empty or contains only whitespace.

    Raises:
        TypeError: If *text* is not a ``str``.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"normalize_spaces() expected str, got {type(text).__name__}"
        )
    if not text or not text.strip():
        return ""
    return _WHITESPACE_RUN.sub(" ", text).strip()
