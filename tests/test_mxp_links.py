"""MXP links, followed from the review cursor instead of the mouse.

A link is text with a command attached (<send href="drink fountain">fountain</send>). A
mouse user clicks the words; here the reader parks the review cursor on the line and
presses one key, and on a line carrying several links the cursor's word picks which.
"""

from __future__ import annotations

from genericmud.app import EngineApp, _link_positions
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Buffer, Line
from genericmud.protocol.mxp import MxpParser
from genericmud.protocol.telnet import DataReceived
from genericmud.review.cursor import ReviewCursor
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"
SECURE = ESC + "[1z"


class FakeWindow:
    """Stands in for the native window's open_url (the pack host's browser call)."""

    def __init__(self, opens: bool = True) -> None:
        self.urls: list[str] = []
        self._opens = opens

    def open_url(self, url: str) -> bool:
        if self._opens:
            self.urls.append(url)
        return self._opens


def _build(*, window=None):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(
        voice,
        send=sent.append,
        post=posted.append,
        keymap=load_keymap("vipmud"),
    )
    # A front end with a window sets this; without it there is nothing to open a URL with.
    app.pack_window = window
    return app, sent, backend, posted


def _app(*, window=None):
    app, sent, backend, _posted = _build(window=window)
    return app, sent, backend


def _press(app: EngineApp, key: str = "alt+enter") -> None:
    app.on_ws_message({"type": "key", "key": key})


def _say(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))


# --- what the parser and the buffer keep ------------------------------------


def test_a_secure_link_is_recorded_with_its_text_and_command():
    outcome = MxpParser().feed(SECURE + 'You see a <send href="drink fountain">fountain</send>.')
    assert outcome.text == "You see a fountain."
    assert outcome.links == [("fountain", "drink fountain")]


def test_a_link_on_an_open_line_is_never_acted_on():
    # Only a secure line may carry markup; an open one is where players' own words arrive.
    # The tag is still known markup, so it goes, but there is nothing to follow -- which is
    # the security boundary: a player can't make another player's client send a command.
    outcome = MxpParser().feed('<send href="quit">quit</send>')
    assert outcome.links == []
    assert "send" not in outcome.text.lower()


def test_a_bare_argument_is_the_command_too():
    # The spec allows <SEND "north"> as well as HREF="north".
    outcome = MxpParser().feed(SECURE + '<send "north">north</send>')
    assert outcome.links == [("north", "north")]


def test_the_support_reply_advertises_links_now_that_they_work():
    replies = MxpParser().feed(SECURE + "<support send a b>").replies
    assert replies and "+SEND" in replies[0] and "+A" in replies[0]
    assert "-B" in replies[0]  # still honest about what it doesn't do


def test_the_link_rides_along_to_the_buffer_line():
    app, _sent, _backend = _app()
    _say(app, SECURE + 'Exits: <send href="north">north</send>')
    line = app.buffer.lines()[-1]
    assert line.plain_text == "Exits: north"
    assert line.links == [("north", "north")]


def test_a_link_on_a_joined_paragraph_is_not_duplicated():
    # A held <P> keeps its tags and the whole text is parsed again on release, so links must
    # come from that final pass alone.
    app, _sent, _backend = _app()
    _say(app, SECURE + '<P>Look: <send href="look">look</send>')
    _say(app, SECURE + "</P>")
    links = app.buffer.lines()[-1].links
    assert links == [("look", "look")]


# --- following one ----------------------------------------------------------


def test_the_follow_key_sends_the_links_command():
    app, sent, _backend = _app()
    _say(app, SECURE + '<send href="drink fountain">fountain</send>')
    _press(app)
    assert sent == ["drink fountain"]


def test_a_line_with_one_link_is_followed_whatever_word_the_cursor_is_on():
    app, sent, _backend = _app()
    _say(app, SECURE + 'A <send href="drink fountain">fountain</send> burbles here.')
    _press(app, "alt+right")  # the word cursor moves off the link's own words
    _press(app)
    assert sent == ["drink fountain"]


def test_the_word_picks_between_links_on_one_line():
    app, sent, backend = _app()
    _say(
        app,
        SECURE + 'Exits: <send href="north">north</send>, <send href="south">south</send>',
    )
    _press(app)  # cursor is on "Exits:", which belongs to no link
    assert sent == []
    # Rather than guess, the client names them so one more Alt+Right lands on a real word.
    assert "2 links" in backend.spoken[-1] and "north" in backend.spoken[-1]
    _press(app, "alt+right")  # onto "north,"
    _press(app)
    assert sent == ["north"]


def test_punctuation_does_not_stop_a_word_matching_its_link():
    app, sent, _backend = _app()
    _say(app, SECURE + 'Doors: <send href="north">north</send>, south')
    _press(app, "alt+right")  # the word here is "north," with its comma
    _press(app)
    assert sent == ["north"]


def test_a_line_with_no_link_says_so_instead_of_doing_nothing():
    app, sent, backend = _app()
    _say(app, "An ordinary line.")
    _press(app)
    assert sent == []
    assert backend.spoken[-1] == "no link on this line"


def test_following_a_link_before_any_review_acts_on_the_newest_line():
    app, sent, _backend = _app()
    _say(app, SECURE + '<send href="look">look</send>')
    assert app.review.active is False
    _press(app)
    assert sent == ["look"]


def test_an_old_line_keeps_its_link_when_read_back_to():
    app, sent, _backend = _app()
    _say(app, SECURE + '<send href="north">north</send>')
    _say(app, "later output")
    _press(app, "alt+up")  # back onto the link's line
    _press(app)
    assert sent == ["north"]


def test_a_link_may_not_run_a_client_command():
    # A MUD could tag any word with <SEND HREF="/quit">; the command goes to the MUD as
    # words, never to the client's own command dispatcher.
    app, sent, _backend = _app()
    _say(app, SECURE + '<send href="/quit">everything is fine</send>')
    _press(app)
    assert sent == ["/quit"]
    assert app._closed is False  # type: ignore[attr-defined]


def test_a_url_link_is_opened_and_never_typed_at_the_mud():
    window = FakeWindow()
    app, sent, backend = _app(window=window)
    _say(app, SECURE + '<a href="http://example.com/page">the map</a>')
    _press(app)
    assert window.urls == ["http://example.com/page"]
    assert sent == []
    assert "opening http://example.com/page" in backend.spoken[-1]


def test_only_web_schemes_are_opened():
    window = FakeWindow()
    app, sent, _backend = _app(window=window)
    _say(app, SECURE + '<a href="file:///C:/Windows/System32/calc.exe">payload</a>')
    _press(app)
    assert window.urls == []
    assert sent == ["file:///C:/Windows/System32/calc.exe"]


def test_a_url_link_opens_through_the_renderer_when_there_is_no_window():
    # The web UI runs inside a browser, so it has no native window to ask: the page opens the
    # tab instead, and the command is still never typed at the MUD.
    app, sent, backend, posted = _build()
    _say(app, SECURE + '<a href="https://example.com">here</a>')
    _press(app)
    assert sent == []
    assert [m for m in posted if m.get("type") == protocol.OPEN_URL] == [
        {"type": protocol.OPEN_URL, "url": "https://example.com"}
    ]
    assert backend.spoken[-1] == "opening https://example.com"


def test_a_window_that_refuses_the_url_says_so_instead():
    window = FakeWindow(opens=False)
    app, sent, backend, posted = _build(window=window)
    _say(app, SECURE + '<a href="https://example.com">here</a>')
    _press(app)
    assert sent == []
    assert [m for m in posted if m.get("type") == protocol.OPEN_URL] == []
    assert backend.spoken[-1] == "no browser to open that link"


def test_a_link_with_no_command_is_not_recorded_at_all():
    # Nothing to follow, so it never becomes a link in the first place.
    assert MxpParser().feed(SECURE + '<send href="">nothing</send>').links == []


# --- the cursor on its own --------------------------------------------------


def _cursor(*links: tuple[str, str]) -> ReviewCursor:
    buffer = Buffer()
    buffer.append(Line("Exits: north, south", links=list(links)))
    cursor = ReviewCursor(buffer)
    cursor.enter()
    return cursor


def test_the_cursor_reports_a_lines_links():
    assert _cursor(("north", "north")).links() == [("north", "north")]
    assert _cursor().links() == []


def test_the_cursor_has_no_link_when_several_and_the_word_matches_none():
    cursor = _cursor(("north", "north"), ("south", "south"))
    assert cursor.link() is None


def test_the_cursor_takes_the_only_link():
    assert _cursor(("north", "north")).link() == ("north", "north")


def test_the_cursor_matches_the_word_inside_a_multi_word_link():
    cursor = _cursor(("wooden door", "open door"))
    cursor.next_word()  # "Exits:"
    assert cursor.link() == ("wooden door", "open door")


def test_the_keymap_binds_the_follow_key():
    assert load_keymap("vipmud").get("alt+enter") == "review:link"


# --- the web renderer's real links -------------------------------------------


def _line_message(posted: list[dict]) -> dict:
    lines = [message for message in posted if message.get("type") == protocol.LINE]
    assert lines, "no line message was posted"
    return lines[-1]


def test_the_line_message_carries_link_positions():
    # The renderer draws anchors from plain offsets, so it never sees markup and never has to
    # decide for itself what is a link.
    app, _sent, _backend, posted = _build()
    _say(app, SECURE + 'You see a <send href="drink fountain">fountain</send> here.')
    message = _line_message(posted)
    assert message["text"] == "You see a fountain here."
    assert message["links"] == [[10, 18, "drink fountain"]]


def test_two_links_keep_the_order_the_mud_sent_them():
    app, _sent, _backend, posted = _build()
    _say(app, SECURE + '<send href="first">a</send> <send href="second">a</send> again')
    assert _line_message(posted)["links"] == [[0, 1, "first"], [2, 3, "second"]]


def test_a_line_without_links_sends_none():
    app, _sent, _backend, posted = _build()
    _say(app, "An ordinary line.")
    assert _line_message(posted)["links"] == []


def test_a_link_whose_words_are_gone_gets_no_position():
    # What a pack substitution leaves behind: guessing a position would make the wrong words
    # clickable, so the link is dropped from the rendering rather than misplaced.
    assert _link_positions("a pack's own text", [("fountain", "drink fountain")]) == []


def test_a_clicked_link_sends_its_command():
    app, sent, _backend, _posted = _build()
    app.on_ws_message(
        {"type": protocol.LINK, "text": "fountain", "command": "drink fountain"}
    )
    assert sent == ["drink fountain"]


def test_a_clicked_link_may_not_run_a_client_command():
    # The renderer only reports the click; the decision stays here, where it also covers the
    # review-cursor path.
    app, sent, _backend, _posted = _build()
    app.on_ws_message({"type": protocol.LINK, "text": "fine", "command": "/quit"})
    assert sent == ["/quit"]
    assert app._closed is False  # type: ignore[attr-defined]


def test_a_clicked_url_link_opens_the_browser():
    window = FakeWindow()
    app, sent, _backend, _posted = _build(window=window)
    app.on_ws_message(
        {"type": protocol.LINK, "text": "map", "command": "https://example.com/page"}
    )
    assert window.urls == ["https://example.com/page"]
    assert sent == []


def test_a_clicked_link_with_no_command_does_nothing():
    app, sent, _backend, _posted = _build()
    app.on_ws_message({"type": protocol.LINK, "text": "x", "command": "   "})
    assert sent == []
