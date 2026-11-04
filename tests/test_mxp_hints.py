"""The caption a link carries: MXP's HINT=.

A link saying "fountain" tells a player what the words are, not what following them does. The
MUD can caption it -- <send "drink fountain" hint="Click to drink">fountain</send> -- and the
caption is said with the command, because the reader is about to send something on the
strength of it.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Link
from genericmud.protocol.mxp import MxpParser
from genericmud.protocol.telnet import OPT_MXP, WILL, DataReceived, Negotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"
SECURE = ESC + "[1z"


def _app():
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"))
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    return app, sent, posted, backend


def _say(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))


def test_the_parser_keeps_the_caption():
    outcome = MxpParser().feed(
        SECURE + '<send "drink fountain" hint="Click to drink">fountain</send>'
    )
    assert outcome.links == [Link("fountain", "drink fountain", hint="Click to drink")]


def test_a_link_without_a_caption_keeps_none():
    outcome = MxpParser().feed(SECURE + '<send "look">look</send>')
    assert outcome.links[0].hint == ""


def test_following_a_link_says_what_it_does():
    app, sent, _posted, backend = _app()
    _say(app, SECURE + '<send "drink fountain" hint="Click to drink">fountain</send>')
    app._activate_link()
    assert sent == ["drink fountain"]
    assert backend.spoken[-1] == "fountain: drink fountain. Click to drink"


def test_a_link_with_no_caption_says_only_the_command():
    # Nothing changes for the links that have always worked: no dangling separator, no "None".
    app, _sent, _posted, backend = _app()
    _say(app, SECURE + '<send "look">look</send>')
    app._activate_link()
    assert backend.spoken[-1] == "look: look"


def test_a_typed_link_says_its_caption_too():
    app, sent, _posted, backend = _app()
    _say(app, SECURE + '<send "tell Ariadne " PROMPT hint="Click to write">Zugg</send>')
    app._activate_link()
    assert sent == []
    assert backend.spoken[-1] == "in the command box: tell Ariadne. Click to write"


def test_the_line_message_carries_the_caption():
    # The web renderer draws the anchor, so it needs the caption to hand back on a click.
    app, _sent, posted, _backend = _app()
    _say(app, SECURE + '<send "look" hint="Click to look">look</send>')
    message = next(m for m in posted if m.get("type") == protocol.LINE)
    assert message["links"] == [[0, 4, "look", False, [], "Click to look"]]


def test_a_click_in_the_browser_says_the_caption():
    app, sent, _posted, backend = _app()
    app.on_ws_message(
        {
            "type": protocol.LINK,
            "text": "fountain",
            "command": "drink fountain",
            "hint": "Click to drink",
        }
    )
    assert sent == ["drink fountain"]
    assert backend.spoken[-1] == "fountain: drink fountain. Click to drink"


# --- hovering: the caption as the cursor arrives, before the key that sends -------------


def _review_line(app: EngineApp, line: str) -> None:
    _say(app, line)


def _right(app: EngineApp, times: int = 1) -> None:
    for _ in range(times):
        app.on_ws_message({"type": protocol.KEY, "key": "alt+right"})


def _hovered(backend) -> list[str]:
    return [said for said in backend.spoken if said.startswith("link: ")]


def test_the_caption_is_said_as_the_cursor_arrives_on_the_link():
    app, _sent, _posted, backend = _app()
    _say(
        app,
        SECURE + 'You see the <send "drink fountain" hint="Click to drink">fountain</send>.',
    )
    _right(app, 3)  # "You", "see", "the", then "fountain"
    assert _hovered(backend) == ["link: Click to drink"]


def test_it_is_said_once_per_link_not_once_per_word():
    app, _sent, _posted, backend = _app()
    _say(
        app,
        SECURE + 'the <send "look" hint="Click to look">old stone fountain</send> stands',
    )
    _right(app, 6)  # through all three words of the link and out the far side
    assert _hovered(backend) == ["link: Click to look"]


def test_the_caption_does_not_cut_off_the_word_it_explains():
    # The word arrives first and the caption queues behind it: barging in left the reader
    # hearing "link: ..." and never the word the cursor had just reached.
    app, _sent, _posted, backend = _app()
    _say(app, SECURE + 'a <send "look" hint="Click to look">fountain</send> here')
    stops_before = backend.stops
    _right(app, 1)  # entering lands on "a"; one step reaches the link's word
    assert backend.spoken[-2:] == ["fountain", "link: Click to look"]
    assert backend.stops == stops_before + 1  # the word's own barge-in, and not a second


def test_leaving_the_link_and_coming_back_says_it_again():
    app, _sent, _posted, backend = _app()
    _say(
        app,
        SECURE
        + 'a <send "look" hint="Click to look">fountain</send> and a '
        + '<send "north">gate</send>',
    )
    _right(app, 2)  # "fountain", then "and" between the two links
    app.on_ws_message({"type": protocol.KEY, "key": "alt+left"})  # back onto the fountain
    assert _hovered(backend) == ["link: Click to look", "link: Click to look"]


def test_a_link_with_no_caption_stays_quiet_while_reading():
    app, _sent, _posted, backend = _app()
    _say(app, SECURE + 'a <send "look">fountain</send> here')
    _right(app, 3)
    assert _hovered(backend) == []


def test_a_line_with_one_link_announces_it_wherever_the_follow_key_would_act():
    # Alt+Enter follows a line's only link from any word on it, so the caption is the reader's
    # from anywhere on the line: the key must never act on a link they have not heard described.
    app, _sent, _posted, backend = _app()
    _say(app, SECURE + 'far away a <send "look" hint="Click to look">fountain</send>')
    _right(app, 1)  # "away" -- nowhere near the link, but Alt+Enter would follow it
    assert _hovered(backend) == ["link: Click to look"]


def test_a_line_with_two_links_announces_only_the_one_being_stood_on():
    # With a choice to make, the caption belongs to the link under the cursor: the follow key
    # refuses to guess here, and nothing describes a link the reader has not reached.
    app, _sent, _posted, backend = _app()
    _say(
        app,
        SECURE
        + 'a <send "look" hint="Click to look">fountain</send> and a '
        + '<send "north">gate</send>',
    )
    _right(app, 1)  # "fountain"
    _right(app, 1)  # "and", between the two
    assert _hovered(backend) == ["link: Click to look"]


def test_the_caption_is_said_before_the_command_is_sent():
    # The order is the point: an explanation that arrives after the command is not a preview.
    order: list[tuple[str, str]] = []

    class OrderedBackend(RecordingBackend):
        def speak(self, text: str) -> None:
            order.append(("say", text))
            super().speak(text)

    voice = VoiceRouter(OrderedBackend(), clock=lambda: 0.0)
    app = EngineApp(
        voice,
        send=lambda text: order.append(("send", text)),
        post=[].append,
        keymap=load_keymap("vipmud"),
    )
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    _say(app, SECURE + '<send "drink fountain" hint="Click to drink">fountain</send>')
    app._activate_link()
    kinds = [kind for kind, _text in order]
    sent_at = kinds.index("send")
    said_before_sending = [text for kind, text in order[:sent_at] if kind == "say"]
    # The arriving line is spoken too, so the caption has to be among what came first, not
    # merely present: the point is which of the two the player hears first.
    assert "fountain: drink fountain. Click to drink" in said_before_sending
