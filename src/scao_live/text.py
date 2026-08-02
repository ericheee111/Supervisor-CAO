"""Whitespace normalization utilities for scao_live.

Provides a stdlib-only function to normalize whitespace in strings by
collapsing runs of whitespace into single spaces and stripping edges,
using ``str.split()`` — no regex, no external dependencies.
"""

from __future__ import annotations


def normalize_spaces(text: str) -> str:
    """Normalize whitespace in *text*.

    Collapses runs of whitespace (spaces, tabs, newlines, carriage returns,
    form feeds, vertical tabs, and other Unicode whitespace) into a single
    space, strips leading and trailing whitespace, and returns an empty
    string for empty or whitespace-only input.

    Uses ``str.split()`` with no arguments, which splits on any Unicode
    whitespace and discards empty segments.  Joining the resulting tokens
    with a single space produces the normalized result.  This naturally
    handles:

    - Empty input (``""``) → returns ``""``.
    - Whitespace-only input → returns ``""``.
    - Leading/trailing whitespace → stripped (edge tokens are empty and
      discarded by ``split``).
    - Consecutive internal whitespace → collapsed to a single space.
    - Already-normalized text → returned unchanged.

    Args:
        text: The input string to normalize.

    Returns:
        The whitespace-normalized string.  Returns ``""`` when *text* is
        empty or contains only whitespace.
    """
    return " ".join(text.split())
