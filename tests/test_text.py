"""Tests for scao_live.text.normalize_spaces.

Covers basic space merging, tabs/newlines, leading/trailing whitespace,
empty input, whitespace-only input, and already-normalized text.
"""

from __future__ import annotations

from scao_live.text import normalize_spaces


# ---------------------------------------------------------------------------
# Basic space merging
# ---------------------------------------------------------------------------


class TestBasicSpaceMerging:
    """Consecutive spaces are collapsed to a single space."""

    def test_double_space(self):
        assert normalize_spaces("hello  world") == "hello world"

    def test_triple_space(self):
        assert normalize_spaces("a   b") == "a b"

    def test_many_spaces(self):
        assert normalize_spaces("x    y    z") == "x y z"

    def test_space_at_middle_only(self):
        assert normalize_spaces("a b  c   d") == "a b c d"


# ---------------------------------------------------------------------------
# Tabs and newlines
# ---------------------------------------------------------------------------


class TestTabsAndNewlines:
    """Tabs, newlines, and other whitespace are collapsed to single spaces."""

    def test_tab(self):
        assert normalize_spaces("hello\tworld") == "hello world"

    def test_newline(self):
        assert normalize_spaces("hello\nworld") == "hello world"

    def test_carriage_return(self):
        assert normalize_spaces("hello\rworld") == "hello world"

    def test_form_feed(self):
        assert normalize_spaces("a\x0cb") == "a b"

    def test_vertical_tab(self):
        assert normalize_spaces("a\x0bb") == "a b"

    def test_mixed_whitespace(self):
        assert normalize_spaces("hello \t\n world") == "hello world"

    def test_crlf_sequence(self):
        assert normalize_spaces("line1\r\nline2") == "line1 line2"

    def test_multiple_newlines(self):
        assert normalize_spaces("a\n\n\nb") == "a b"


# ---------------------------------------------------------------------------
# Leading and trailing whitespace
# ---------------------------------------------------------------------------


class TestLeadingTrailingWhitespace:
    """Leading and trailing whitespace is stripped."""

    def test_leading_spaces(self):
        assert normalize_spaces("   hello") == "hello"

    def test_trailing_spaces(self):
        assert normalize_spaces("hello   ") == "hello"

    def test_both_sides(self):
        assert normalize_spaces("  hello  ") == "hello"

    def test_leading_newline(self):
        assert normalize_spaces("\nhello") == "hello"

    def test_trailing_tab(self):
        assert normalize_spaces("hello\t") == "hello"

    def test_surrounding_mixed(self):
        assert normalize_spaces(" \t\n hello \t\n ") == "hello"

    def test_only_leading_no_internal(self):
        assert normalize_spaces("  hello world") == "hello world"

    def test_only_trailing_no_internal(self):
        assert normalize_spaces("hello world  ") == "hello world"


# ---------------------------------------------------------------------------
# Empty and whitespace-only input
# ---------------------------------------------------------------------------


class TestEmptyAndWhitespaceOnly:
    """Empty or whitespace-only input returns an empty string."""

    def test_empty_string(self):
        assert normalize_spaces("") == ""

    def test_single_space(self):
        assert normalize_spaces(" ") == ""

    def test_multiple_spaces(self):
        assert normalize_spaces("   ") == ""

    def test_whitespace_only_mixed(self):
        assert normalize_spaces(" \t\n\r ") == ""

    def test_whitespace_only_tab(self):
        assert normalize_spaces("\t") == ""

    def test_whitespace_only_newline(self):
        assert normalize_spaces("\n") == ""

    def test_whitespace_only_crlf(self):
        assert normalize_spaces("\r\n") == ""

    def test_whitespace_only_many_tabs(self):
        assert normalize_spaces("\t\t\t") == ""


# ---------------------------------------------------------------------------
# Already-normalized text
# ---------------------------------------------------------------------------


class TestAlreadyNormalized:
    """Already-normalized text is returned unchanged."""

    def test_single_word(self):
        assert normalize_spaces("hello") == "hello"

    def test_single_space_separated(self):
        assert normalize_spaces("hello world") == "hello world"

    def test_sentence(self):
        text = "the quick brown fox jumps over the lazy dog"
        assert normalize_spaces(text) == text

    def test_single_character(self):
        assert normalize_spaces("a") == "a"

    def test_two_words(self):
        assert normalize_spaces("foo bar") == "foo bar"

    def test_empty_already_normal(self):
        assert normalize_spaces("") == ""
