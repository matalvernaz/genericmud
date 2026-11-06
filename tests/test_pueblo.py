"""Pueblo, stage one: the handshake, and HTML read as the text a player would hear.

Pueblo has no telnet option -- the exchange is ordinary text -- so the two halves that matter
are: announcing the client on a world that asked for it, and reading the world's HTML afterwards
without ever speaking a tag. The announcement is a real line at a login screen, which is why the
world setting is off by default and the tests here hold it off.
"""

from __future__ import annotations

import time

import pytest

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.config.worlds import World, load_worlds, save_worlds
from genericmud.protocol.pueblo import CLIENT_LINE, PuebloParser
from genericmud.protocol.telnet import DataReceived
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

# The real greeting, taken from penultimatemush.com:9500, is plain text until the client
# announces itself, and the world then sends this and writes everything as HTML.
START = "</xch_mudtext><img xch_mode=purehtml><xch_page clear=text>"


def _app(*, pueblo: bool, lines: tuple[str, ...] = ()) -> tuple[EngineApp, list[str]]:
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=sent.append,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        pueblo=pueblo,
    )
    app.on_connect("Test", socket_open=True)
    app.after_connect()  # the window's "the socket opened" call, which is where it announces
    for line in lines:
        app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))
    return app, sent


def _read(parser: PuebloParser, line: str) -> list[str]:
    """The words one line of HTML comes back as, without its links."""
    return [piece.text for piece in parser.feed(line)]


def _spoken(app: EngineApp) -> list[str]:
    return [line.plain_text for line in app.buffer.lines()]


# --- what the client says --------------------------------------------------------------


def test_the_client_announces_the_version_the_spec_asks_for():
    assert CLIENT_LINE == "PUEBLOCLIENT 2.50"


def test_a_world_that_asked_for_pueblo_is_announced_as_soon_as_the_socket_is_up():
    _app_greeting, sent = _app(pueblo=True, lines=())
    assert sent and sent[0] == CLIENT_LINE


def test_a_world_that_did_not_ask_sends_nothing_at_all():
    # The whole reason the setting defaults to off: this line reaches a world that does not
    # speak Pueblo as a character name at its login screen.
    _app_quiet, sent = _app(pueblo=False)
    assert sent == []


def test_the_announcement_comes_before_the_worlds_own_after_connect_commands():
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=sent.append,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        pueblo=True,
        post_connect_commands="look",
    )
    app.on_connect("Test", socket_open=True)
    app.after_connect()
    assert sent == [CLIENT_LINE, "look"]


# --- reading the HTML ------------------------------------------------------------------


def test_plain_text_before_the_switch_is_left_alone():
    parser = PuebloParser()
    assert _read(parser, "This world is Pueblo 1.10 Enhanced.") == [
        "This world is Pueblo 1.10 Enhanced."
    ]
    assert parser.active is False


def test_the_switch_itself_is_never_spoken():
    parser = PuebloParser()
    assert _read(parser, START) == []
    assert parser.active is True


def test_tags_are_dropped_and_the_words_kept():
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, "<SAMP>Welcome to <b>Penultimate</b> Destination!<br>") == [
        "Welcome to Penultimate Destination!"
    ]


def test_a_break_in_the_middle_of_a_line_becomes_its_own_line():
    # Pueblo sends a paragraph as one line with <br> inside it, so one line in is several out.
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, "one<br>two<br><br>four<br>") == ["one", "two", "", "four"]


def test_entities_become_the_characters_they_stand_for():
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, "Use create &lt;name&gt; &lt;password&gt;") == [
        "Use create <name> <password>"
    ]
    assert _read(parser, "  create &quot;First Last&quot; &lt;password&gt;") == [
        '  create "First Last" <password>'
    ]
    assert _read(parser, "Tom &amp; Jerry&nbsp;here &#65;&#x42;") == ["Tom & Jerry here AB"]


def test_an_entity_it_does_not_know_is_left_as_it_stands():
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, "a &fnord; b") == ["a &fnord; b"]


def test_a_script_is_not_read_aloud():
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, "<script>alert('hi')</script>plain") == ["plain"]


def test_the_page_command_is_read_and_not_said():
    # <xch_page clear=text> asks the client to clear its window. Stage one does not clear the
    # scrollback -- losing the history under a screen reader is worse than a repeated greeting
    # -- so it is dropped like any other tag, never spoken.
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, "<xch_page clear=text>after") == ["after"]


def test_a_world_that_switches_back_to_text_is_read_as_plain_text_again():
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, "<img xch_mode=text>plain <b>again</b>") == ["plain <b>again</b>"]
    assert parser.active is False


# --- the engine's side of it -----------------------------------------------------------


def test_the_html_greeting_is_heard_as_text():
    app, _sent = _app(
        pueblo=True,
        lines=(
            "Welcome to Penultimate Destination!",
            START,
            "<SAMP>Welcome to Penultimate Destination!<br>",
            "Use create &lt;name&gt; &lt;password&gt; to create a character.<br>",
        ),
    )
    assert _spoken(app) == [
        "Welcome to Penultimate Destination!",
        "Welcome to Penultimate Destination!",
        "Use create <name> <password> to create a character.",
    ]


def test_a_world_with_pueblo_off_reads_its_html_as_plain_text():
    # Nothing is announced, and nothing is stripped: the world was never told to send HTML, so
    # any angle brackets in its text are its own words and are owed to the reader.
    app, _sent = _app(pueblo=False, lines=(START, "<SAMP>hello<br>"))
    assert "<SAMP>hello<br>" in _spoken(app)


def test_a_prompt_with_no_newline_is_read_as_html_too():
    # A prompt is flushed through the same path as a whole line, so the parser has to see it.
    app, _sent = _app(pueblo=True, lines=(START,))
    app.on_telnet_event(DataReceived(b"<b>hp:</b> 100"))
    app._flush_prompt()
    assert _spoken(app)[-1] == "hp: 100"


# --- the setting round trips through the world file ------------------------------------


def test_pueblo_is_off_unless_the_world_file_says_otherwise(tmp_path):
    path = tmp_path / "worlds.toml"
    save_worlds([World("Quiet", "q.example", 4000)], path)
    save_worlds(
        [World("Quiet", "q.example", 4000), World("Loud", "l.example", 4000, pueblo=True)], path
    )
    loaded = {world.name: world.pueblo for world in load_worlds(path)}
    assert loaded == {"Quiet": False, "Loud": True}


def test_only_a_world_that_asked_is_written_with_pueblo(tmp_path):
    path = tmp_path / "worlds.toml"
    save_worlds(
        [World("Quiet", "q.example", 4000), World("Loud", "l.example", 4000, pueblo=True)], path
    )
    text = path.read_text(encoding="utf-8")
    assert text.count("pueblo = true") == 1

def test_the_announcement_and_the_switch_are_logged_for_diagnosis():
    # Both are one-off moments a report like "it says my MUD sends markup" turns on: whether the
    # client announced, and whether the world answered.
    from genericmud.session.diaglog import DiagnosticLog

    events: list[tuple[str, dict]] = []
    log = DiagnosticLog.__new__(DiagnosticLog)
    log.event = lambda name, **fields: events.append((name, fields))
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=lambda _text: None,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        pueblo=True,
        diag=log,
    )
    app.on_connect("Test", socket_open=True)
    app.after_connect()
    app.on_telnet_event(DataReceived(START.encode() + b"\r\n"))
    names = [name for name, _fields in events]
    assert "pueblo.announce" in names
    assert ("pueblo.mode", {"html": True}) in events

# --- the commands the world offers ------------------------------------------------


def _links(parser: PuebloParser, line: str) -> list[tuple[str, str, str]]:
    """Every link on a line, as the words it shows, its command and its caption."""
    return [
        (link.text, link.command, link.hint)
        for piece in parser.feed(line)
        for link in piece.links
    ]


def test_a_command_link_is_read_as_a_link_and_its_words_kept():
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, 'You see a <a xch_cmd="north">north</a> exit.') == [
        "You see a north exit."
    ]
    assert _links(parser, 'You see a <a xch_cmd="north">north</a> exit.') == [
        ("north", "north", "")
    ]


def test_a_link_can_show_one_thing_and_do_another_with_a_caption():
    # What following it is meant to do is said with the command, as MXP's caption is.
    parser = PuebloParser()
    parser.feed(START)
    assert _links(parser, '<a xch_cmd="look sign" xch_hint="Read it">a sign</a> here') == [
        ("a sign", "look sign", "Read it")
    ]


def test_the_older_href_form_is_a_command_too_and_a_web_address_is_not():
    parser = PuebloParser()
    parser.feed(START)
    assert _links(
        parser, '<a href="xch_cmd:south">south</a> and <a href="http://example.com">a page</a>'
    ) == [("south", "south", "")]


def test_a_link_that_is_never_closed_runs_to_the_end_of_its_line():
    # What a browser would do, and better than losing the words and the command with them.
    parser = PuebloParser()
    parser.feed(START)
    assert _links(parser, '<a xch_cmd="west">an unclosed link') == [
        ("an unclosed link", "west", "")
    ]


def test_a_link_with_nothing_to_run_or_nothing_to_read_is_not_a_link():
    parser = PuebloParser()
    parser.feed(START)
    assert _links(parser, '<a xch_cmd="">   </a>nothing to follow') == []
    # The spaces inside it are the world's own text, so they stay; there is just nothing to
    # follow, and nothing of the tag left behind.
    assert _read(parser, '<a xch_cmd="">   </a>nothing to follow') == ["   nothing to follow"]


def test_several_links_on_a_line_are_kept_in_the_order_they_appear():
    parser = PuebloParser()
    parser.feed(START)
    assert _links(
        parser,
        '<a xch_cmd="north">north</a>, <a xch_cmd="south">south</a> and '
        '<a xch_cmd="down">down</a>',
    ) == [("north", "north", ""), ("south", "south", ""), ("down", "down", "")]


def test_a_link_on_a_second_break_is_on_its_own_line():
    parser = PuebloParser()
    parser.feed(START)
    pieces = parser.feed('a road<br><a xch_cmd="north">north</a>')
    assert [(piece.text, [link.command for link in piece.links]) for piece in pieces] == [
        ("a road", []),
        ("north", ["north"]),
    ]


def test_following_a_pueblo_link_sends_its_command():
    # The engine's own follow path, unchanged: a Pueblo link is the same Link an MXP one is.
    from genericmud.model.buffer import Line

    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=sent.append,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        pueblo=True,
    )
    app.on_connect("Test", socket_open=True)
    app.after_connect()
    app.on_telnet_event(DataReceived(START.encode() + b"\r\n"))
    app.on_telnet_event(DataReceived(b'<a xch_cmd="north">north</a>\r\n'))
    line = app.buffer.lines()[-1]
    assert isinstance(line, Line)
    assert [(link.text, link.command) for link in line.links] == [("north", "north")]

def test_the_first_offered_link_is_noted_once_for_diagnosis():
    # "My exits are not followable" needs to tell a world that writes plain words from a world
    # whose links this client dropped, without a session of guessing.
    from genericmud.session.diaglog import DiagnosticLog

    events: list[tuple[str, dict]] = []
    log = DiagnosticLog.__new__(DiagnosticLog)
    log.event = lambda name, **fields: events.append((name, fields))
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=lambda _text: None,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        pueblo=True,
        diag=log,
    )
    app.on_connect("Test", socket_open=True)
    app.after_connect()
    app.on_telnet_event(DataReceived(START.encode() + b"\r\n"))
    events.clear()
    app.on_telnet_event(DataReceived(b'<a xch_cmd="north">north</a>\r\n'))
    app.on_telnet_event(DataReceived(b'<a xch_cmd="south">south</a>\r\n'))
    noted = [fields for name, fields in events if name == "pueblo.links"]
    assert noted == [{"first": "north", "count": 1}]  # the second line adds nothing

def test_following_a_pueblo_link_is_logged_as_a_link_not_as_mxp():
    # The link logs stopped saying "mxp." when Pueblo links began reaching the same path: a
    # report of "a link sent the wrong command" is not an MXP question any more.
    from genericmud.session.diaglog import DiagnosticLog

    events: list[tuple[str, dict]] = []
    log = DiagnosticLog.__new__(DiagnosticLog)
    log.event = lambda name, **fields: events.append((name, fields))
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=sent.append,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        pueblo=True,
        diag=log,
    )
    app.on_connect("Test", socket_open=True)
    app.after_connect()
    app.on_telnet_event(DataReceived(START.encode() + b"\r\n"))
    app.on_telnet_event(DataReceived(b'<a xch_cmd="north">north</a>\r\n'))
    app.review.go_to(len(app.buffer.lines()) - 1)
    app.review.next_link(inclusive=True)
    app.on_ws_message({"type": "key", "key": "alt+enter"})
    assert sent[-1] == "north"
    followed = {"text": "north", "command": "north", "insert": False, "hint": ""}
    assert ("link.follow", followed) in events

def test_the_mode_switch_only_counts_inside_an_img_tag():
    # Pueblo has no secure line, so the world's markup and a player's words arrive together. Read
    # anywhere on a line, someone typing "xch_mode=html" at the MUD would switch every reader's
    # parser on (or off, which is worse: their markup would then be spoken as text).
    parser = PuebloParser()
    assert _read(parser, 'You say, "xch_mode=html"') == ['You say, "xch_mode=html"']
    assert parser.active is False
    assert _read(parser, START) == []
    assert parser.active is True
    assert _read(parser, "someone says: xch_mode=text") == ["someone says: xch_mode=text"]
    assert parser.active is True

def test_a_sound_tag_is_not_a_page_mode():
    # Pueblo puts xch_mode="sound" on an <img> to mean "play this", and reading that as a page
    # mode turned a sound into a switch back to plain text -- after which the world's markup
    # would be spoken as words.
    parser = PuebloParser()
    parser.feed(START)
    assert _read(parser, '<img src="wind.wav" xch_mode=sound>') == []
    assert parser.active is True
    assert _read(parser, "<b>still</b> markup") == ["still markup"]


def test_the_first_picture_is_noted_and_never_spoken():
    parser = PuebloParser()
    parser.feed(START)
    assert parser.first_image is None
    assert _read(parser, 'A map hangs here.<img src="maps/city.gif"><br>') == ["A map hangs here."]
    assert parser.first_image == "maps/city.gif"
    _read(parser, '<img src="other.gif">')
    assert parser.first_image == "maps/city.gif"  # the first one, kept


def test_the_mode_switch_tag_is_not_counted_as_a_picture():
    parser = PuebloParser()
    parser.feed(START)
    assert parser.first_image is None


def test_the_first_picture_or_sound_a_world_sends_is_noted_once():
    # Nothing plays one yet; the line is what tells "my MUD's pictures are missing" from "my MUD
    # sends no pictures".
    from genericmud.session.diaglog import DiagnosticLog

    events: list[tuple[str, dict]] = []
    log = DiagnosticLog.__new__(DiagnosticLog)
    log.event = lambda name, **fields: events.append((name, fields))
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=lambda _text: None,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        pueblo=True,
        diag=log,
    )
    app.on_connect("Test", socket_open=True)
    app.after_connect()
    app.on_telnet_event(DataReceived(START.encode() + b"\r\n"))
    app.on_telnet_event(DataReceived(b'<img src="maps/city.gif">\r\n'))
    app.on_telnet_event(DataReceived(b'<img src="other.gif">\r\n'))
    noted = [fields for name, fields in events if name == "pueblo.image"]
    assert noted == [{"src": "maps/city.gif"}]


# --- what a hostile line can't do ------------------------------------------------------------



@pytest.mark.parametrize("unit", ["<img", "<!--", "<script>", "<b", "<p", "<a x>"])
def test_a_long_line_of_openings_is_read_in_moments(unit):
    # Each of these once made a pattern rescan to the end of the line for every opening: one
    # 64 KB line of "<img" kept the session busy for half a minute, reading nothing else.
    parser = PuebloParser()
    parser.feed("<img xch_mode=html>")
    line = unit * (65536 // len(unit))
    started = time.perf_counter()
    parser.feed(line)
    assert time.perf_counter() - started < 1.0


def test_an_unclosed_script_still_lets_a_later_closed_style_go():
    parser = PuebloParser()
    parser.feed("<img xch_mode=html>")
    [line] = parser.feed("one <script>two <style>hidden</style> three")
    assert line.text == "one two  three"


def test_a_closed_comment_goes_and_an_unclosed_one_is_left_to_the_tags():
    parser = PuebloParser()
    parser.feed("<img xch_mode=html>")
    [line] = parser.feed("a<!-- gone -->b<!-- open")
    assert line.text == "ab<!-- open"


@pytest.mark.parametrize(
    "entity", ["&#xD800;", "&#0;", "&#27;", "&#x9B;", "&#127;", "&#63743;", "&#99999999;"]
)
def test_a_numbered_entity_that_is_no_printable_character_is_dropped(entity):
    # As MXP does: half a surrogate pair stops the session log and is dropped by speech, an
    # escape starts ANSI, and U+F8FF is the client's own picture marker.
    parser = PuebloParser()
    parser.feed("<img xch_mode=html>")
    [line] = parser.feed(f"a{entity}b")
    assert line.text == "ab"


def test_printable_numbered_entities_still_read():
    parser = PuebloParser()
    parser.feed("<img xch_mode=html>")
    [line] = parser.feed("&#65;&#x263A;&#233;")
    assert line.text == "A☺é"
