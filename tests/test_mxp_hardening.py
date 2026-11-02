"""MXP that a player, or a server that never offered MXP, tries to abuse.

Each test here is an attack from the security review of the MXP work, run against the real
parser or the real session: a "<p>" in chat holding everyone's output, escaped text turning
back into live tags when lines are joined, a mode switch partway through a line, numeric
entities that break the log or move pictures, the web UI fetching addresses on its own, and
Enter in the output following a link the reader never heard.
"""

from __future__ import annotations

import sys

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.protocol.mxp import IMAGE_MARK, MxpParser, resolve_image_url
from genericmud.protocol.telnet import OPT_MXP, WILL, DataReceived, Negotiation
from genericmud.safepath import is_allowed_host
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"


def _app(*, offered: bool = True):
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(voice, send=sent.append, post=[].append, keymap=load_keymap("vipmud"))
    if offered:
        app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    return app, sent


def _say(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode("utf-8") + b"\r\n"))


def _texts(app: EngineApp) -> list[str]:
    return [line.plain_text for line in app.buffer.lines()]


# --- a player's <p> -----------------------------------------------------------


def test_a_paragraph_opened_on_an_open_line_ends_with_that_line():
    parser = MxpParser()
    assert parser.feed("Bob chats: <p>hello").join_next is False
    assert parser.feed("The goblin hits you.").join_next is False


def test_a_players_paragraph_does_not_hold_the_next_lines():
    app, _sent = _app()
    _say(app, "Bob chats: <p>hello")
    _say(app, "The goblin hits you.")
    assert _texts(app) == ["Bob chats: hello", "The goblin hits you."]


def test_a_server_that_never_offered_mxp_gets_no_mxp_parsing():
    app, _sent = _app(offered=False)
    _say(app, "Bob chats: <p>hello")
    _say(app, "The goblin hits you.")
    assert _texts(app) == ["Bob chats: <p>hello", "The goblin hits you."]


def test_a_new_connection_starts_mxp_from_nothing():
    app, _sent = _app()
    _say(app, ESC + "[1z<p>The room is")
    app.on_connect("gw")
    _say(app, "quiet.")
    assert _texts(app) == ["quiet."]  # no offer on the new connection, and no held text


def test_a_secure_paragraph_still_joins_its_lines():
    app, _sent = _app()
    _say(app, ESC + "[1z<p>The room is")
    _say(app, " quiet.</p>")
    assert _texts(app) == ["The room is quiet."]


# --- joined lines are parsed once ----------------------------------------------


def test_an_escaped_tag_inside_a_paragraph_stays_text():
    app, _sent = _app()
    _say(app, ESC + "[1z<P>Type &lt;send&gt; to talk.")
    _say(app, "More text.</P>")
    assert _texts(app) == ["Type <send> to talk.More text."]


def test_escaped_player_text_inside_a_server_paragraph_never_becomes_a_picture():
    app, _sent = _app()
    _say(app, ESC + "[6z")
    _say(app, "<P>Bob says '&lt;image url=\"http://192.168.1.1/\" fname=\"x.gif\"&gt;'")
    _say(app, "The room is quiet.</P>")
    line = app.buffer.lines()[-1]
    assert IMAGE_MARK not in line.plain_text
    assert "<image" in line.plain_text


def test_a_joined_open_line_cannot_carry_a_live_link():
    # Locked secure by default, a player's words on open lines: "hi<nobr>" joins the next
    # line onto this one, and that next line's <send> must stay as dead as it was alone.
    app, _sent = _app()
    _say(app, ESC + "[6z")
    _say(app, ESC + "[0zBob chats: hi<nobr>")
    _say(app, ESC + '[0zBob chats: <send href="give all to bob">free gold</send>')
    assert _texts(app)[-1] == "Bob chats: hiBob chats: free gold"
    assert app.buffer.lines()[-1].links == []


def test_dropping_to_an_open_line_partway_through_disarms_what_follows():
    outcome = MxpParser().feed(
        ESC + '[1z<send href="who">who</send> Bob says: ' + ESC
        + '[0z<send href="give all to bob">free gold</send>'
    )
    assert outcome.links == [("who", "who")]
    assert outcome.text == "who Bob says: free gold"


def test_a_secure_escape_partway_through_a_line_is_ignored():
    outcome = MxpParser().feed(
        "Bob says: " + ESC + '[1z<send href="give all to bob">free gold</send>'
    )
    assert outcome.links == []


# --- numeric entities -------------------------------------------------------------


def test_an_entity_naming_half_a_surrogate_pair_is_dropped():
    text = MxpParser().feed("x&#55296;y").text
    assert text == "xy"
    (text + "\n").encode("utf-8")  # what writing the session log needs


def test_an_entity_with_thousands_of_digits_is_left_as_text():
    digits = "0" * (sys.get_int_max_str_digits() + 1)
    text = MxpParser().feed(f"before &#{digits}65; after").text
    assert text.startswith("before &#") and text.endswith("65; after")


def test_an_entity_cannot_make_a_control_character():
    assert MxpParser().feed("&#155;[31mred").text == "[31mred"
    assert MxpParser().feed("a&#7;b&#127;c").text == "abc"


def test_the_picture_marker_cannot_be_injected():
    assert IMAGE_MARK not in MxpParser().feed("a &#63743; b").text
    assert IMAGE_MARK not in MxpParser().feed("a " + IMAGE_MARK + " b").text


# --- addresses a picture may come from ----------------------------------------


def test_addresses_that_only_mean_something_on_the_players_network_are_refused():
    for host in (
        "localhost", "router.lan", "192.168.1.1.nip.io", "127.1", "2130706433",
        "0x7f000001", "0177.0.0.1", "192.168.1.1.", "0", "192.168.1.1", "127.0.0.1",
        "[::ffff:192.168.1.1]", "[::ffff:c0a8:101]",
    ):
        assert is_allowed_host(host) is False, host


def test_public_hosts_and_the_muds_own_address_are_allowed():
    assert is_allowed_host("images.example.com") is True
    assert is_allowed_host("8.8.8.8") is True
    assert is_allowed_host("192.168.1.5", "192.168.1.5") is True


def test_a_picture_url_on_the_players_network_resolves_to_nothing():
    assert resolve_image_url("a.gif", "http://127.1:8080/admin/", "", "") == ""
    assert resolve_image_url("http://localhost:631/jobs?op=x", "", "", "") == ""


# --- Enter in the output box ----------------------------------------------------


def test_enter_follows_the_link_on_the_line_being_read_not_the_newest():
    app, sent = _app()
    _say(app, ESC + '[1z<send href="drink">a fountain</send> burbles.')
    _say(app, ESC + '[1z<send href="buy sword">a sword</send> is for sale.')
    app.on_ws_message({"type": "link_at", "text": "a fountain burbles.", "from_end": 1})
    assert sent == ["drink"]


def test_enter_on_a_line_the_buffer_does_not_have_follows_nothing():
    app, sent = _app()
    _say(app, ESC + '[1z<send href="buy sword">a sword</send> is for sale.')
    app.on_ws_message({"type": "link_at", "text": "something else entirely", "from_end": 0})
    assert sent == []
