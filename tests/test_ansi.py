"""Tests for ANSI/escape stripping and SGR colour-span parsing."""

from __future__ import annotations

from genericmud.render.ansi import Span, parse_ansi, strip_ansi


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


def test_parse_ansi_splits_coloured_runs():
    spans = parse_ansi("plain \x1b[1;31mred bold\x1b[0m again")
    assert spans == [
        Span("plain ", None, None, False),
        Span("red bold", "red", None, True),
        Span(" again", None, None, False),
    ]


def test_parse_ansi_background_and_bright():
    spans = parse_ansi("\x1b[92;44mhi\x1b[0m")
    assert spans == [Span("hi", "bright_green", "blue", False)]


def test_parse_ansi_256_and_truecolor():
    assert parse_ansi("\x1b[38;5;208mx").pop().fg == "256:208"
    assert parse_ansi("\x1b[38;2;10;20;30my").pop().fg == "rgb:10,20,30"


def test_parse_ansi_ignores_non_sgr_escapes():
    # MXP mode + OSC carry no colour; they must not create style or text.
    spans = parse_ansi("\x1b[7z\x1b[1zYou see a tavern.")
    assert spans == [Span("You see a tavern.", None, None, False)]


def test_parse_ansi_plaintext_matches_strip_ansi():
    sample = "\x1b[33mgold\x1b[0m \x1b]0;t\x07and \x1b[1mbold\x1b[22m normal"
    assert "".join(span.text for span in parse_ansi(sample)) == strip_ansi(sample)
