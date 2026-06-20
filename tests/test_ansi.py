"""Tests for ANSI/escape stripping."""

from __future__ import annotations

from genericmud.render.ansi import strip_ansi


def test_strips_sgr_colour():
    assert strip_ansi("\x1b[1;31mred\x1b[0m text") == "red text"


def test_strips_mxp_line_mode_sequences():
    # The God Wars II leak: \x1b[7z / \x1b[1z MXP mode changes.
    assert strip_ansi("\x1b[7z\x1b[1zYou see a tavern.") == "You see a tavern."


def test_strips_osc():
    assert strip_ansi("\x1b]0;window title\x07hello") == "hello"


def test_plain_text_untouched():
    assert strip_ansi("A long bar stands along the north-west corner.") == (
        "A long bar stands along the north-west corner."
    )
