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


def test_recall_reaches_the_tenth_line():
    # Ctrl+0 is bound to recall:10, so ten has to be inside the cap and eleven outside it.
    cursor = ReviewCursor(_buffer(*[f"m{i}" for i in range(1, 11)]))  # m1..m10
    assert cursor.recall(1) == "m10"
    assert cursor.recall(9) == "m2"
    assert cursor.recall(10) == "m1"
    assert cursor.recall(11) == ""  # still bounded; the cap is what Ctrl+0 needed raised


def test_search_backward_from_bottom():
    cursor = ReviewCursor(_buffer("alpha", "beta dragon", "gamma"))
    cursor.enter()  # at "gamma"
    assert cursor.search("dragon") == "beta dragon"


def test_search_forward_from_the_top():
    cursor = ReviewCursor(_buffer("alpha dragon", "beta", "gamma dragon"))
    cursor.top()
    assert cursor.search("dragon", forward=True) == "gamma dragon"


def test_repeating_a_search_walks_past_the_current_line():
    cursor = ReviewCursor(_buffer("one dragon", "two dragon", "three dragon"))
    cursor.enter()  # at "three dragon"
    assert cursor.search("dragon") == "two dragon"
    assert cursor.search("dragon") == "one dragon"
    assert cursor.search("dragon") == ""  # no wrap: running off the end is a miss


def test_first_search_can_include_the_current_edge_line():
    cursor = ReviewCursor(_buffer("one dragon", "two"))
    cursor.top()
    assert cursor.search("dragon", forward=True, include_current=True) == "one dragon"
    cursor.bottom()
    assert cursor.search("two", include_current=True) == "two"


def test_search_is_case_insensitive_by_default():
    cursor = ReviewCursor(_buffer("alpha", "beta Dragon", "gamma"))
    cursor.enter()
    assert cursor.search("dragon") == "beta Dragon"


def test_case_sensitive_search_skips_the_wrong_case():
    cursor = ReviewCursor(_buffer("alpha dragon", "beta Dragon", "gamma"))
    cursor.enter()
    assert cursor.search("dragon", case_sensitive=True) == "alpha dragon"


def test_case_sensitive_search_can_miss_entirely():
    cursor = ReviewCursor(_buffer("alpha", "beta Dragon", "gamma"))
    cursor.enter()
    assert cursor.search("dragon", case_sensitive=True) == ""


def test_copy_word_returns_current():
    cursor = ReviewCursor(_buffer("take the sword"))
    cursor.enter()
    assert cursor.copy_word() == "take"


def test_empty_buffer_is_safe():
    cursor = ReviewCursor(Buffer())
    assert cursor.enter() == ""
    assert cursor.next_line() == ""
    assert cursor.recall(1) == ""


def test_spell_line_names_spaces_and_separates_characters():
    cursor = ReviewCursor(_buffer("ok go"))
    cursor.enter()
    assert cursor.spell_line() == "o, k, space, g, o"


def test_spell_line_on_an_empty_buffer_is_safe():
    assert ReviewCursor(Buffer()).spell_line() == ""


def test_position_survives_ring_eviction():
    # A parked review position must not silently slide onto a newer line once the bounded
    # buffer fills and starts dropping the oldest lines (the seq anchor, not a raw index).
    buffer = Buffer(capacity=5)
    for i in range(5):
        buffer.append(Line(f"line{i}"))
    cursor = ReviewCursor(buffer)
    cursor.enter()
    assert cursor.top() == "line0"  # parked on the oldest
    for i in range(5, 8):
        buffer.append(Line(f"line{i}"))  # evicts line0..line2; buffer is line3..line7
    # line0 is gone; the cursor clamps to the oldest surviving line, not a slid-to newer one.
    assert cursor.current_word() == "line3"


def test_parked_line_stays_put_as_new_lines_arrive():
    buffer = Buffer(capacity=100)
    for i in range(5):
        buffer.append(Line(f"line{i}"))
    cursor = ReviewCursor(buffer)
    cursor.enter()
    cursor.top()
    cursor.next_line()  # on line1
    for i in range(5, 10):
        buffer.append(Line(f"line{i}"))  # appends, no eviction
    assert cursor.current_word() == "line1"  # unmoved

def test_go_to_column_parks_on_the_word_holding_it():
    # The output box has a caret of its own, and a line can carry several links: the word the
    # caret is in is the only way to know which one the reader means.
    from genericmud.model.buffer import Buffer, Line

    buffer = Buffer()
    buffer.append(Line("Exits: north and down."))
    cursor = ReviewCursor(buffer)
    assert cursor.go_to_column(4) == "Exits:"  # inside the first word
    assert cursor.go_to_column(7) == "north"  # a column in the gap takes the next word
    assert cursor.go_to_column(8) == "north"
    assert cursor.go_to_column(20) == "down."  # the word is the one the world wrote
    assert cursor.go_to_column(999) == "down."  # past the end is the last word, not an error


def test_go_to_column_on_an_empty_line_is_harmless():
    from genericmud.model.buffer import Buffer, Line

    buffer = Buffer()
    buffer.append(Line("   "))
    cursor = ReviewCursor(buffer)
    assert cursor.go_to_column(2) == ""
