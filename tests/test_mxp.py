"""MXP line parsing: the text must never carry markup, and open lines must not act."""

from __future__ import annotations

from genericmud.protocol.mxp import MxpParser

ESC = "\x1b"
SECURE = ESC + "[1z"
LOCKED = ESC + "[2z"


def _feed(line: str, **kwargs) -> object:
    return MxpParser(**kwargs).feed(line)


def test_open_formatting_tags_are_stripped_from_the_text():
    # The whole point: a server only earns the right to send MXP once none of it leaks.
    out = _feed("<b>bold</b> and <i>italic</i> and <u>under</u>")
    assert out.text == "bold and italic and under"


def test_an_unknown_tag_is_only_markup_where_markup_was_promised():
    # On a secure line the MUD has said the line is tagged, so unknown tags go.
    assert _feed(SECURE + "<someFutureTag attr=1>text</someFutureTag>").text == "text"
    # On an open line anything unrecognised is more likely a player's own words, so it
    # stays: deleting text out of chat is worse than showing a stray tag.
    assert _feed("<someFutureTag attr=1>text</someFutureTag>").text == (
        "<someFutureTag attr=1>text</someFutureTag>"
    )


def test_ordinary_text_with_angle_brackets_survives():
    # The reported worry: a MUD that had nothing to do with MXP must read exactly as before.
    for line in (
        "HP 45 < 50, you feel weak",
        "You say, 'I <3 this MUD'",
        "Type <build> to construct the wall",
        "a < b > c",
        "5 > 3",
        "<not closed",
    ):
        assert _feed(line).text == line


def test_a_player_quoting_an_open_tag_keeps_the_visible_words():
    # Even a tag this client knows is only formatting, so the words it wrapped are what
    # matters; nothing is deleted from the text either way.
    assert _feed("You say, 'use <b>bold</b> here'").text == "You say, 'use bold here'"


def test_br_breaks_the_line():
    assert _feed("a<BR>b").text == "a\nb"
    assert _feed("a<SBR>b").text == "a\nb"


def test_nobr_asks_the_caller_not_to_break():
    out = _feed("a long line<NOBR>")
    assert out.no_break is True
    assert out.text == "a long line"


def test_a_locked_line_is_verbatim():
    # Mode 2 means "no tags at all", which is how a MUD sends text that looks like markup.
    assert _feed(LOCKED + "<b>verbatim</b>").text == "<b>verbatim</b>"


def test_secure_tags_do_nothing_on_an_open_line():
    # The security rule: players can get text into open lines, so a client must not act on
    # a secure tag there -- otherwise one player could drive another's client.
    out = _feed('<send "buy bread">bread</send>')
    assert out.text == "bread"  # the words still show
    assert out.links == []  # but the link is not ours to act on
    sound = _feed('<sound "oof.wav">')
    assert sound.cues == []


def test_a_secure_line_records_a_send_link():
    out = _feed(SECURE + '<send "buy bread">bread</send>')
    assert out.text == "bread"
    assert out.links == [("bread", "buy bread")]


def test_href_is_the_command_when_given():
    out = _feed(SECURE + '<send href="buy water">water</send>')
    assert out.links == [("water", "buy water")]


def test_sound_and_music_are_read_as_msp_cues():
    out = _feed(SECURE + '<sound "oof.wav" 50 2>x')
    assert out.text == "x"
    assert [(cue.kind, cue.file, cue.volume, cue.repeats) for cue in out.cues] == [
        ("sound", "oof.wav", 50, 2)
    ]
    named = _feed(SECURE + '<music FName="theme.mid" V=30>')
    assert [(cue.kind, cue.file, cue.volume) for cue in named.cues] == [
        ("music", "theme.mid", 30)
    ]


def test_version_and_support_are_answered():
    version = _feed(SECURE + "<version>", client_version="9.9")
    assert version.replies == ["<VERSION MXP=1.0 CLIENT=genericMud VERSION=9.9>"]
    style = _feed(SECURE + "<version 3>")
    assert "STYLE=3" in style.replies[0]
    support = _feed(SECURE + "<support image br>")
    assert support.replies == ["<SUPPORTS -IMAGE +BR>"]


def test_support_with_no_arguments_lists_what_this_client_does():
    out = _feed(SECURE + "<support>")
    assert out.replies[0].startswith("<SUPPORTS ")
    assert "+BR" in out.replies[0] and "+SOUND" in out.replies[0]
    assert "-IMAGE" not in out.replies[0]


def test_entities_expand_but_never_become_tags():
    # &lt; exists to write a literal "<", so expanding before parsing would eat the text.
    out = _feed("&lt;not a tag&gt; and &amp; more &#65;")
    assert out.text == "<not a tag> and & more A"


def test_a_locked_mode_sticks_and_a_reset_ends_it():
    parser = MxpParser()
    assert parser.feed(ESC + "[5z<b>one</b>").text == "one"  # lock open: tags parsed
    assert parser.feed(LOCKED + "<b>two</b>").text == "<b>two</b>"
    # Mode 2 is per line, so the locked default from mode 5 still applies next line.
    parser = MxpParser()
    parser.feed(ESC + "[7z")  # lock locked as the new default
    assert parser.feed("<b>still verbatim</b>").text == "<b>still verbatim</b>"
    parser.feed(ESC + "[3z")  # reset everything
    assert parser.feed("<b>parsed again</b>").text == "parsed again"


def test_secure_line_wrapper_is_the_form_the_spec_asks_for():
    assert MxpParser.secure_line("<VERSION>") == ESC + "[1z<VERSION>\n"
