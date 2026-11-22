"""A MUD's links sit exactly where the markup put them, whatever happens to the text after.

The parser used to report a link by its words, and the engine searched the finished line for
them. Words that appear twice ("a fountain by a fountain"), words the line shows differently
(colour codes inside the link) and words a later pass changed all put the link on the wrong
words, or nowhere. Now the parser leaves a marker where each link's words start and end, the
engine lifts the markers out before MSP, colour codes and password hiding read the line, and
carries their positions through what those passes remove.
"""

from __future__ import annotations

import random

import pytest

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.protocol import marks, msp
from genericmud.protocol.marks import IMAGE_MARK, link_mark, without_marks
from genericmud.protocol.mxp import MxpParser
from genericmud.protocol.pueblo import PuebloParser
from genericmud.protocol.telnet import OPT_MXP, WILL, DataReceived, Negotiation
from genericmud.render.ansi import kept_characters, parse_ansi
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"
SECURE = ESC + "[1z"
GREEN = ESC + "[1;32m"
RESET = ESC + "[0m"


def _build(*, mxp: bool = True):
    backend = RecordingBackend()
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=sent.append,
        post=posted.append,
        keymap=load_keymap("vipmud"),
    )
    if mxp:
        app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    return app, sent, backend, posted


def _say(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))


def _lines(posted: list[dict]) -> list[dict]:
    return [message for message in posted if message.get("type") == protocol.LINE]


def _spans(posted: list[dict]) -> list[tuple[str, str]]:
    """The newest line's links as the window gets them: (the words they cover, the command)."""
    message = _lines(posted)[-1]
    return [(message["text"][start:end], command) for start, end, command, *_ in message["links"]]


def _press(app: EngineApp, key: str) -> None:
    app.on_ws_message({"type": "key", "key": key})


# --- where a link lands --------------------------------------------------------------------


def test_a_repeated_word_is_linked_where_the_markup_put_it():
    app, _sent, backend, posted = _build()
    _say(app, SECURE + 'a fountain by a <send href="drink">fountain</send>')
    message = _lines(posted)[-1]
    start = len("a fountain by a ")
    assert message["links"][0][:3] == [start, start + len("fountain"), "drink"]
    _press(app, "alt+pageup")
    # Parked on the second "fountain": the follow key now acts on the link the MUD meant.
    assert backend.spoken[-1] == "fountain: a fountain by a fountain"
    assert app.review._here() == start


def test_coloured_words_inside_a_link_are_found_and_heard_without_the_codes():
    app, sent, backend, posted = _build()
    _say(app, SECURE + 'Exits: <send href="north">' + GREEN + "north" + RESET + "</send>.")
    assert _spans(posted) == [("north", "north")]
    _press(app, "alt+pageup")
    assert backend.spoken[-1] == "north: Exits: north."
    _press(app, "alt+enter")
    assert sent == ["north"]


def test_coloured_words_go_into_a_command_without_the_codes():
    app, sent, _backend, _posted = _build()
    _say(app, SECURE + '<send href="buy &text;">' + GREEN + "sword" + RESET + "</send>")
    _press(app, "alt+enter")
    assert sent == ["buy sword"]


def test_the_same_address_bare_and_as_a_muds_link_is_two_links_on_their_own_words():
    app, sent, _backend, posted = _build()
    line = 'See https://a.com or <send href="say hi">https://a.com</send>'
    _say(app, SECURE + line)
    message = _lines(posted)[-1]
    first = message["text"].index("https://a.com")
    second = message["text"].rindex("https://a.com")
    assert [tuple(span[:3]) for span in message["links"]] == [
        (first, first + 13, "https://a.com"),
        (second, second + 13, "say hi"),
    ]
    app.on_ws_message(
        {"type": protocol.LINK_AT, "text": message["text"], "from_end": 0, "column": second}
    )
    assert sent == ["say hi"]


def test_an_entity_a_tag_split_reads_as_it_did_and_places_no_link_inside_it():
    app, _sent, _backend, posted = _build()
    _say(app, SECURE + 'Fish &am<send href="x">p;</send> chips')
    message = _lines(posted)[-1]
    assert message["text"] == "Fish & chips"
    assert message["links"] == []


def test_an_unknown_entity_a_tag_split_keeps_the_link_on_its_own_letters():
    app, _sent, _backend, posted = _build()
    _say(app, SECURE + 'Odd &bog<send href="x">us;</send> here')
    message = _lines(posted)[-1]
    assert message["text"] == "Odd &bogus; here"
    assert _spans(posted) == [("us;", "x")]


def test_an_echoed_password_beside_a_link_is_still_hidden_and_the_link_still_placed():
    app, sent, _backend, posted = _build()
    app._password_guard = "hunter2"
    _say(app, SECURE + 'connect hero hunter2 then <send href="look">look</send> around')
    message = _lines(posted)[-1]
    assert "hunter2" not in message["text"]
    assert message["text"] == "connect hero *** then look around"
    assert _spans(posted) == [("look", "look")]


def test_a_sound_trigger_a_tag_split_still_plays_and_leaves_nothing_behind():
    app, _sent, _backend, posted = _build()
    played: list[str] = []
    app._start_media = lambda file, *_args, **_kwargs: played.append(file)
    _say(app, SECURE + '!!<send href="x">SOUND</send>(bell.wav) The bell rings.')
    assert played == ["bell.wav"]
    line = app.buffer.lines()[-1]
    assert line.plain_text == " The bell rings."
    # Its words went with the trigger, so it has no place in the line: none is drawn.
    assert all(not link.placed for link in line.links)
    assert _lines(posted)[-1]["links"] == []


def test_a_link_whose_end_tag_sits_in_a_sound_trigger_never_takes_another_links_command():
    app, _sent, _backend, posted = _build()
    _say(
        app,
        SECURE
        + '<send href="a">one !!SOUND(x</send>.wav) and <send href="b">two</send> done',
    )
    spans = _spans(posted)
    assert ("two", "b") in spans
    assert all(command != "a" or words == "one" for words, command in spans)


def test_a_link_cut_by_a_pause_leaves_no_markers_and_no_link(monkeypatch):
    scheduled: list = []
    backend = RecordingBackend()
    posted: list[dict] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=lambda _text: None,
        post=posted.append,
        keymap=load_keymap("vipmud"),
        schedule=lambda _delay, callback: scheduled.append(callback),
    )
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    app.on_telnet_event(DataReceived((SECURE + 'Go <send href="north">no').encode()))
    for callback in list(scheduled):
        callback()  # the pause: what has come so far is shown as a prompt
    app.on_telnet_event(DataReceived(b"rth</send> now\r\n"))
    texts = [message["text"] for message in _lines(posted)]
    assert texts and all(text == without_marks(text) for text in texts)
    assert all(message["links"] == [] for message in _lines(posted))


# --- markers the MUD did not ask for -----------------------------------------------------------


@pytest.mark.parametrize("mxp", [False, True])
def test_marker_characters_from_the_mud_are_never_shown_spoken_or_logged(mxp):
    app, _sent, backend, posted = _build(mxp=mxp)
    logged: list[str] = []
    app._log = logged.append
    spoofed = "Look " + link_mark(0) + "here" + link_mark(1) + " now" + IMAGE_MARK + "."
    _say(app, (SECURE if mxp else "") + spoofed)
    assert _lines(posted)[-1]["text"] == "Look here now."
    assert _lines(posted)[-1]["links"] == []
    assert backend.spoken[-1] == "Look here now."
    assert logged[-1] == "Look here now."


def test_spoofed_markers_cannot_move_a_real_link():
    app, _sent, _backend, posted = _build()
    _say(
        app,
        SECURE
        + "x"
        + link_mark(0)
        + '&#1048576; y <send href="north">north</send> z'
        + link_mark(0),
    )
    assert _spans(posted) == [("north", "north")]
    assert without_marks(_lines(posted)[-1]["text"]) == _lines(posted)[-1]["text"]


def test_a_mud_defined_entity_cannot_carry_a_marker():
    parser = MxpParser(mark_links=True)
    parser.feed(SECURE + '<!ENTITY sneaky "&#1048576;">')
    outcome = parser.feed(SECURE + "a &sneaky; b")
    assert without_marks(outcome.text) == outcome.text


# --- numbers reused --------------------------------------------------------------------------


def test_reused_marker_numbers_never_put_a_link_on_another_links_words(monkeypatch):
    # Two numbers to go round: a paragraph joining several lines' links reuses them.
    import genericmud.protocol.mxp as mxp_module

    monkeypatch.setattr(mxp_module, "LINK_MARK_COUNT", 2)
    app, _sent, _backend, posted = _build()
    _say(app, SECURE + '<P><send href="one">alpha</send> <send href="two">beta</send>')
    _say(app, SECURE + ' <send href="three">gamma</send></P>')
    # alpha and gamma share a number: neither is placed. beta's is its own.
    assert _spans(posted) == [("beta", "two")]
    review_links = app.buffer.lines()[-1].links
    assert {link.command for link in review_links} == {"one", "two", "three"}


# --- a paragraph held for its next line ----------------------------------------------------------


def test_a_paragraph_just_under_its_cap_stays_held_with_a_link_in_it():
    app, _sent, _backend, posted = _build()
    # 3,998 characters of text: under the 4,000 cap, and over it only if the two markers round
    # the link were counted.
    _say(app, SECURE + "<P>" + "a" * 3993 + ' <send href="x">link</send>')
    assert _lines(posted) == []
    _say(app, SECURE + "end</P>")
    assert len(_lines(posted)) == 1


def test_a_held_paragraph_of_only_a_wordless_link_waits_as_an_empty_one_does():
    scheduled: list = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=lambda _text: None,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        schedule=lambda _delay, callback: scheduled.append(callback),
    )
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    _say(app, SECURE + '<P><send href="x"></send>')
    held_before = app._mxp_held_lines
    for callback in list(scheduled):
        callback()  # the deadline
    assert app._mxp_held_lines == held_before  # not released: nothing to say yet


# --- the parser on its own ------------------------------------------------------------------


def test_the_parser_leaves_markers_round_a_kept_link_only():
    parser = MxpParser(mark_links=True)
    outcome = parser.feed(SECURE + 'go <send href="north">north</send> now')
    number = outcome.links[0].mark
    assert outcome.text == "go " + link_mark(number) + "north" + link_mark(number) + " now"
    dropped = parser.feed(SECURE + '<send href="x">never closed')
    assert dropped.links == []
    assert sum(1 for char in dropped.text if marks.is_link_mark(char)) == 1


_TRICKY = [
    'a &am<send href="x">p;</send> b',
    'a <send href="x">&am</send>p; b',
    '&bog<send href="x">us;</send>',
    '<send href="x">&lt;b&gt;</send> &#65; &#1; &#55296; &#1048576;',
    '<!ENTITY hero "Bob"><send href="tell &hero;">&hero;</send>',
    'plain <send>north</send> <send prompt>tell bob</send> <send href="">gone</send>',
    '<send "look &text;|get &text;">' + GREEN + "lamp" + RESET + "</send>",
    "<send href=x>" + IMAGE_MARK + "<image fname=a.png url=http://h/></send> tail",
    ESC + "[2z" + 'locked <send href="x">&amp;</send>',
    ESC + "[4z<send href=x>one</send> " + ESC + "[0z<send href=y>two</send>",
    "<P>joined <send href=x>part</send>",
    "<NOBR>no <send href=y>break</send>",
]


def _outcome(parser: MxpParser, line: str) -> tuple:
    outcome = parser.feed(line)
    return (
        without_marks(outcome.text),
        [(link.text, link.command, link.hint, link.choices, link.insert) for link in outcome.links],
        outcome.cues,
        outcome.replies,
        outcome.login,
        outcome.images,
        outcome.join_next,
    )


def _random_lines(count: int) -> list[str]:
    pieces = [
        "a", " ", "&amp;", "&am", "p;", "&lt;", "&#65;", "&#1;", "&bogus;", "<send href=x>",
        '<send href="y &text;">', "<send>", "</send>", "<a href=z>", "</a>", "<b>", "</b>",
        "<p>", "</p>", "<br>", ESC + "[4z", ESC + "[0z", ESC + "[1z", GREEN, RESET,
        "!!SOUND(", "x.wav)", "<!ENTITY e 'v'>", "&e;", "north",
    ]
    generator = random.Random(20261122)
    return [
        SECURE + "".join(generator.choice(pieces) for _ in range(generator.randint(1, 14)))
        for _ in range(count)
    ]


def test_marked_and_unmarked_parsing_read_every_line_the_same():
    lines = [SECURE + line for line in _TRICKY] + _random_lines(3000)
    plain = MxpParser()
    marked = MxpParser(mark_links=True)
    for line in lines:
        assert _outcome(marked, line) == _outcome(plain, line), repr(line)


def test_marked_and_unmarked_pueblo_read_every_line_the_same():
    switch = "</xch_mudtext><img xch_mode=purehtml><xch_page clear=text>"
    lines = [
        '&nb<a xch_cmd="x">sp;</a>',
        '<a xch_cmd="a">x</a> x <a xch_cmd="b">x</a><br>',
        'one<br><a href="xch_cmd:north">north</a><br><br>',
        '&am<a xch_cmd="y">p;</a> and &bog<a xch_cmd="z">us;</a>',
        '<a xch_cmd="w">' + link_mark(3) + "spoof</a>",
    ]
    plain, marked = PuebloParser(), PuebloParser(mark_links=True)
    plain.feed(switch)
    marked.feed(switch)
    for line in lines:
        expected = [(piece.text, [(link.text, link.command) for link in piece.links])
                    for piece in plain.feed(line)]
        got = [(without_marks(piece.text), [(link.text, link.command) for link in piece.links])
               for piece in marked.feed(line)]
        assert got == expected, repr(line)


def test_a_pueblo_link_with_a_repeated_caption_is_placed_on_its_own_copy():
    app, sent, _backend, posted = _build(mxp=False)
    app.pueblo_enabled = True
    _say(app, "</xch_mudtext><img xch_mode=purehtml><xch_page clear=text>")
    _say(app, 'x and x then <a xch_cmd="pick">x</a>')
    message = _lines(posted)[-1]
    assert message["links"][0][:3] == [len("x and x then "), len("x and x then x"), "pick"]


# --- the passes that read the text after the parser ------------------------------------------


def _reference_msp(text: str) -> tuple[str, list]:
    """parse_msp_line as it was written before it could report what it kept."""
    cues: list = []

    def take(kind):
        def repl(match):
            cue = msp._parse_cue(kind, match.group("body"))
            if cue is not None:
                cues.append(cue)
            return ""

        return repl

    clean = msp._SOUND_RE.sub(take("sound"), text)
    clean = msp._MUSIC_RE.sub(take("music"), clean)
    return clean, cues


def test_what_msp_keeps_rebuilds_exactly_what_it_returns():
    generator = random.Random(7)
    alphabet = ["!!SOUND(", "!!MUSIC(", "a.wav", ")", "(", " ", "x", "!!MU", "SIC(m)", "L=-1"]
    for _ in range(5000):
        text = "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 12)))
        kept: list[bool] = []
        clean, cues = msp.parse_msp_line(text, kept=kept)
        assert (clean, cues) == _reference_msp(text), repr(text)
        assert "".join(char for char, keep in zip(text, kept, strict=True) if keep) == clean
        leading_kept: list[bool] = []
        rest, _cues = msp.parse_msp_line(text, allow_midline=False, kept=leading_kept)
        assert "".join(c for c, keep in zip(text, leading_kept, strict=True) if keep) == rest


def test_what_the_colour_pass_keeps_rebuilds_exactly_what_it_returns():
    generator = random.Random(11)
    alphabet = [ESC, "[", "1", ";", "32", "m", "]", "x", "\x07", "\\", " ", "z", "A"]
    for _ in range(5000):
        text = "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 14)))
        kept = kept_characters(text)
        joined = "".join(span.text for span in parse_ansi(text))
        assert "".join(c for c, keep in zip(text, kept, strict=True) if keep) == joined
