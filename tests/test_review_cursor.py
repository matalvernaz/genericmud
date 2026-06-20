"""Tests for the review cursor."""

from __future__ import annotations

from genericmud.model.buffer import Buffer, Line
from genericmud.review.cursor import ReviewCursor


def _buffer(*lines: str) -> Buffer:
    buffer = Buffer()
    for text in lines:
        buffer.append(Line(text))
    return buffer


def test_enter_starts_at_last_line():
    cursor = ReviewCursor(_buffer("one", "two", "three"))
    assert cursor.enter() == "three"
    assert cursor.active is True


def test_line_navigation_clamps():
    cursor = ReviewCursor(_buffer("a", "b", "c"))
    cursor.enter()
    assert cursor.prev_line() == "b"
    assert cursor.prev_line() == "a"
    assert cursor.prev_line() == "a"  # clamped at top
    assert cursor.next_line() == "b"
    assert cursor.top() == "a"
    assert cursor.bottom() == "c"


def test_word_navigation_clamps():
    cursor = ReviewCursor(_buffer("hello brave world"))
    cursor.enter()
    assert cursor.current_word() == "hello"
    assert cursor.next_word() == "brave"
    assert cursor.next_word() == "world"
    assert cursor.next_word() == "world"  # clamped
    assert cursor.prev_word() == "brave"


def test_char_navigation_clamps():
    cursor = ReviewCursor(_buffer("hi"))
    cursor.enter()
    assert cursor.current_char() == "h"
    assert cursor.next_char() == "i"
    assert cursor.next_char() == "i"  # clamped
    assert cursor.prev_char() == "h"


def test_recall_last_n():
    cursor = ReviewCursor(_buffer("m1", "m2", "m3"))
    assert cursor.recall(1) == "m3"
    assert cursor.recall(3) == "m1"
    assert cursor.recall(9) == ""  # fewer than 9 lines


def test_search_backward_from_bottom():
    cursor = ReviewCursor(_buffer("alpha", "beta dragon", "gamma"))
    cursor.enter()  # at "gamma"
    assert cursor.search("dragon") == "beta dragon"


def test_copy_word_returns_current():
    cursor = ReviewCursor(_buffer("take the sword"))
    cursor.enter()
    assert cursor.copy_word() == "take"


def test_empty_buffer_is_safe():
    cursor = ReviewCursor(Buffer())
    assert cursor.enter() == ""
    assert cursor.next_line() == ""
    assert cursor.recall(1) == ""
