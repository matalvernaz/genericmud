"""Visible document transitions, with LF representing each logical CRLF pair."""

import pytest

from genericmud.ui.command_document import (
    down_creates_tail,
    insert_blank_line,
    submit_command,
)


@pytest.mark.parametrize("before,after", [
    ("p\na\nb|", "p\na\nb\n"),
    ("p\na|\nb\n", "p\na\nb\na\n"),
    ("p\na|\ndraft", "p\na\ndraft\na\n"),
    ("p\na2|\nb\n", "p\na2\nb\na2\n"),
    ("p\na|\n", "p\na\n"),
    ("a|\n", "a\na\n"),  # the short-buffer exception is intentional
    ("p\na\na|", "p\na\n"),  # manually typed duplicates use the same rule
    ("a\na|", "a\na\n"),
    ("p\na|\na\nb\n", "p\na\na\nb\na\n"),
    ("p\na|\na", "p\na\na\n"),
    ("al|pha", "alpha\nalpha\n"),
    ("alpha|", "alpha\n"),
    ("p\na\n|", "p\na\n\n"),
    ("p\n\n|", "p\n\n"),
    ("p\na\nA|", "p\na\nA\n"),
    ("p\na\na |", "p\na\na \n"),
    ("|", "\n"),
    ("north\nlook target|\nsouth\neast\n", "north\nlook target\nsouth\neast\nlook target\n"),
    ("p\nзащита 🐉\nзащита 🐉|", "p\nзащита 🐉\n"),
])
def test_enter_preserves_original_lines_and_uses_text_only_cleanup(before, after):
    position = before.index("|")
    assert submit_command(before.replace("|", ""), position) == after


@pytest.mark.parametrize("text,position,expected", [
    ("draft", 5, True), ("draft", 2, True), ("", 0, True),
    ("north\ndraft", 3, False), ("north\ndraft", 8, True),
    ("north\ndraft\n", 8, False), ("north\ndraft\n", 12, False),
    ("north\ndraft\n\n", 13, False),
])
def test_down_only_creates_a_missing_tail(text, position, expected):
    assert down_creates_tail(text, position) is expected


@pytest.mark.parametrize("text,position,result,caret", [
    ("p\nabcd\ntail", 5, "p\n\nabcd\ntail", 2),
    ("abcdef", 2, "ab\ncdef", 1),
    ("abcd", 0, "\nabcd", 0),
    ("p\n", 2, "p\n\n", 2),
    ("", 0, "\n", 0),
])
def test_escape_preserves_the_original_text_and_places_a_blank_line(text, position, result, caret):
    assert insert_blank_line(text, position) == (result, caret)


def test_editing_elsewhere_does_not_change_duplicate_cleanup():
    assert submit_command("changed draft\na\na", 17) == "changed draft\na\n"
