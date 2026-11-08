"""Tests for the output control's reading position: keeping a place, and finding one."""

from __future__ import annotations

from genericmud.ui.scrollback import (
    FindState,
    anchor_after_append,
    find_line,
    line_at_position,
    position_of_line,
)


def test_plain_append_leaves_the_anchor_alone():
    # Nothing trimmed off the front, so the reader's offset still points at the same text.
    assert anchor_after_append(anchor=500, removed=0, last_position=4000) == 500


def test_trim_shifts_the_anchor_down_by_the_removed_count():
    # The oldest 200 characters went; the reader's place moved down with the text.
    assert anchor_after_append(anchor=500, removed=200, last_position=3800) == 300


def test_anchor_inside_the_trimmed_region_lands_at_the_top():
    # The reader was parked in text that has since been dropped: clamp, don't go negative.
    assert anchor_after_append(anchor=50, removed=200, last_position=3800) == 0


def test_anchor_past_the_end_is_clamped_into_the_control():
    assert anchor_after_append(anchor=9000, removed=0, last_position=4000) == 4000


def test_everything_trimmed_leaves_the_anchor_at_zero():
    assert anchor_after_append(anchor=500, removed=4000, last_position=0) == 0


# --- find ---

# The control's GetValue() split on "\n": duplicate hit lines at 0 and 3, a longer
# line at 1 that merely CONTAINS the hit text, and the trailing empty line the
# control's closing newline produces.
LINES = ["you say hi", "and then you say hi again", "quiet", "you say hi", ""]
HIT = "you say hi"


def test_new_find_takes_the_direction_edge():
    assert find_line(LINES, HIT, 2, forward=True, from_edge=True) == 0
    assert find_line(LINES, HIT, 2, forward=False, from_edge=True) == 3


def test_repeating_a_find_walks_strictly_past_the_caret_line():
    # Sitting on a match and searching again must move on, not sit still.
    assert find_line(LINES, HIT, 0, forward=True) == 3
    assert find_line(LINES, HIT, 3, forward=False) == 0


def test_a_longer_line_containing_the_text_is_not_a_match():
    # The engine matched a whole line; a substring occurrence must not take the caret.
    assert find_line(LINES, HIT, 0, forward=True) == 3  # line 1 contains HIT, skipped
    assert find_line(["and then you say hi again", ""], HIT, 1, forward=False) is None


def test_matching_is_exact_on_case():
    assert find_line(LINES, "You say hi", 2, forward=True, from_edge=True) is None


def test_no_match_returns_none_rather_than_wrapping():
    assert find_line(LINES, HIT, 3, forward=True) is None
    assert find_line(LINES, HIT, 0, forward=False) is None
    assert find_line(LINES, "griffin", 0, forward=True, from_edge=True) is None


def test_empty_needle_never_matches():
    # The trailing empty line is real text in the control; an empty search term
    # must not park the caret there.
    assert find_line(LINES, "", 0, forward=True) is None


def test_caret_line_outside_the_control_is_tolerated():
    assert find_line(LINES, HIT, -50, forward=True) == 0
    assert find_line(LINES, HIT, 900, forward=False) == 3


def test_find_state_defaults_to_a_backwards_case_insensitive_search():
    state = FindState()
    assert state.term == ""
    assert state.forward is False
    assert state.case_sensitive is False

# --- where a line begins in the control ---


def test_a_lines_start_is_its_text_plus_two_characters_per_break():
    # Windows stores a line break as two characters and GetValue() renders one, so a line's
    # start is every preceding character of text plus two per break.
    assert position_of_line(["abc", "de", "f"], 0) == 0
    assert position_of_line(["abc", "de", "f"], 1) == 5
    assert position_of_line(["abc", "de", "f"], 2) == 9
    assert position_of_line(["abc", "de", "f"], 3) == 12  # past the end: the text's own end


def test_a_position_and_a_line_number_agree_each_way():
    lines = ["", "short", "x" * 3000, "tail"]
    for index in (0, 1, 2, 3):
        start = position_of_line(lines, index)
        assert line_at_position(lines, start) == index
        if lines[index]:
            assert line_at_position(lines, start + len(lines[index]) - 1) == index


def test_a_line_longer_than_the_controls_own_limit_still_round_trips():
    # The whole point: Windows splits a 3,000-character line into three when asked for its line
    # numbers, and this arithmetic does not.
    long_line = "x" * 3000
    lines = [long_line, "after it"]
    assert line_at_position(lines, position_of_line(lines, 1)) == 1
    assert position_of_line(lines, 1) == 3002


def test_a_position_past_the_end_belongs_to_the_last_line():
    assert line_at_position(["a", "b"], 10_000) == 1


def test_find_ignores_trailing_spaces_at_either_end():
    # A reader cannot see or aim at the end of a line, so a control that kept a stray space
    # must not hide a line that is right there. The match is still whole-line: "you say hi" must
    # not capture "and then you say hi again".
    lines = ["you say hi   ", "and then you say hi again", "you say hi"]
    assert find_line(lines, "you say hi", 3, forward=False) == 2
    assert find_line(lines, "you say hi  ", 3, forward=False) == 2


def test_the_break_width_is_what_the_platform_stores():
    # Windows keeps a break as CRLF, GTK as one character; a caret counted with the wrong one
    # shifts by a line for every line above it, which is how a Linux run caught this.
    lines = ["abc", "de"]
    assert position_of_line(lines, 1, width=2) == 5
    assert position_of_line(lines, 1, width=1) == 4
    assert line_at_position(lines, 4, width=2) == 0
    assert line_at_position(lines, 4, width=1) == 1
