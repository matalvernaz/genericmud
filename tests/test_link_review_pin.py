"""Two links in one word are each reachable, and the cursor's hold on one ends when it moves.

A MUD can put two links side by side with nothing between them: N and S making the word "NS".
Review used to work in words, so a jump parked on the word and the next jump skipped the second
link, and Enter on the S followed the N. Now a jump, or the output box's caret, pins the exact
character; any other movement lets go of it.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Buffer, Line, Link
from genericmud.protocol.telnet import OPT_MXP, WILL, DataReceived, Negotiation
from genericmud.review.cursor import ReviewCursor
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

SECURE = "\x1b[1z"
NS_LINE = "Go NS now"
NS_MARKUP = SECURE + 'Go <send href="north">N</send><send href="south">S</send> now'


def _app():
    backend = RecordingBackend()
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=sent.append,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
    )
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    app.on_telnet_event(DataReceived(NS_MARKUP.encode() + b"\r\n"))
    return app, sent, backend


def _press(app: EngineApp, key: str) -> None:
    app.on_ws_message({"type": "key", "key": key})


def _enter_at(app: EngineApp, column: int) -> None:
    app.on_ws_message({"type": protocol.LINK_AT, "text": NS_LINE, "from_end": 0, "column": column})


def test_jumping_visits_both_links_of_one_word_both_ways():
    app, _sent, backend = _app()
    _press(app, "alt+pagedown")
    assert backend.spoken[-1] == "N: Go NS now"
    _press(app, "alt+pagedown")
    assert backend.spoken[-1] == "S: Go NS now"
    _press(app, "alt+pageup")
    assert backend.spoken[-1] == "N: Go NS now"


def test_the_follow_key_after_a_jump_follows_the_link_jumped_to():
    app, sent, _backend = _app()
    _press(app, "alt+pagedown")
    _press(app, "alt+pagedown")
    _press(app, "alt+enter")
    assert sent == ["south"]


def test_enter_with_the_caret_on_each_letter_follows_that_letters_link():
    app, sent, _backend = _app()
    _enter_at(app, NS_LINE.index("N"))
    _enter_at(app, NS_LINE.index("S"))
    assert sent == ["north", "south"]


def test_the_follow_key_on_the_word_names_both_rather_than_guessing():
    app, sent, backend = _app()
    app.review.enter()
    app.review.next_word()  # on "NS", by word
    _press(app, "alt+enter")
    assert sent == []
    assert backend.spoken[-1] == "2 links: 1 N; 2 S"


def test_moving_away_and_back_lets_go_of_the_jump():
    app, sent, backend = _app()
    _press(app, "alt+pagedown")
    _press(app, "alt+pagedown")  # pinned on S
    app.review.next_word()
    app.review.prev_word()  # back on "NS", by word this time
    _press(app, "alt+enter")
    assert sent == []
    assert backend.spoken[-1] == "2 links: 1 N; 2 S"


def test_a_word_that_merely_matches_a_link_elsewhere_on_the_line_is_not_that_link():
    backend = RecordingBackend()
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=sent.append,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
    )
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    line = 'north is where <send href="north">north</send> and <send href="south">south</send> go'
    app.on_telnet_event(DataReceived((SECURE + line).encode() + b"\r\n"))
    app.review.enter()  # on the first word: the "north" that is only prose
    _press(app, "alt+enter")
    assert sent == []
    assert backend.spoken[-1] == "2 links: 1 north; 2 south"


# --- the cursor on its own ---------------------------------------------------------------------


def _ns(text: str = NS_LINE) -> Line:
    first = text.index("NS") if "NS" in text else text.index("XY")
    return Line(
        text,
        links=[
            Link(text[first], "north", start=first, end=first + 1),
            Link(text[first + 1], "south", start=first + 1, end=first + 2),
        ],
    )


def test_a_pin_survives_an_earlier_line_being_evicted():
    buffer = Buffer(capacity=3)
    buffer.append(Line("an early line"))
    buffer.append(_ns())
    cursor = ReviewCursor(buffer)
    cursor.go_to(1)
    cursor.next_link(inclusive=True)
    assert cursor.next_link().command == "south"
    buffer.append(Line("one more"))
    buffer.append(Line("and another"))  # the early line goes; the pinned line moves up
    assert cursor.link().command == "south"


def test_a_pin_never_lands_on_the_line_left_when_its_own_is_evicted():
    buffer = Buffer(capacity=2)
    buffer.append(_ns())
    cursor = ReviewCursor(buffer)
    cursor.go_to(0)
    cursor.next_link(inclusive=True)
    cursor.next_link()  # pinned on S, column 4
    # A line with links at the very same columns takes the evicted line's place under the cursor.
    buffer.append(_ns("Go XY now"))
    buffer.append(Line("newest"))
    assert cursor.current_line() == "Go XY now"
    assert cursor.link() is None  # its word holds two links: named, not the stale pin's pick


def _pinned(buffer_line: Line) -> ReviewCursor:
    buffer = Buffer()
    buffer.append(buffer_line)
    buffer.append(Line("Go further"))
    cursor = ReviewCursor(buffer)
    cursor.go_to(0)
    cursor.next_link(inclusive=True)
    cursor.next_link()
    assert cursor._pinned_column() is not None
    return cursor


MOVES = {
    "enter": (),
    "exit": (),
    "go_to": (0,),
    "next_line": (),
    "prev_line": (),
    "top": (),
    "bottom": (),
    "next_word": (),
    "prev_word": (),
    "next_char": (),
    "prev_char": (),
    "search": ("Go",),
}
PINS = {"go_to_column", "next_link", "prev_link"}
READS = {
    "current_word", "current_char", "recall", "copy_word", "current_line", "spell_line",
    "links", "link",
}


def test_every_cursor_method_is_known_to_move_pin_or_only_read():
    # A new movement has to say what it does to the pin, or a stale one could come back.
    public = {
        name
        for name in dir(ReviewCursor)
        if not name.startswith("_") and callable(getattr(ReviewCursor, name))
    }
    assert public == set(MOVES) | PINS | READS


def test_every_ordinary_movement_lets_go_of_the_pin():
    for name, arguments in MOVES.items():
        cursor = _pinned(_ns())
        getattr(cursor, name)(*arguments)
        assert cursor._pinned_column() is None, name


def test_reading_keeps_the_pin():
    for name in READS:
        cursor = _pinned(_ns())
        method = getattr(cursor, name)
        method(1) if name == "recall" else method()
        assert cursor._pinned_column() == 4, name


def _image_and_address() -> Line:
    # An inline picture called map.png is offered as a link to it, found by its name; the text
    # right after it holds an address, a link the engine placed exactly. One word holds both.
    text = "map.png(https://example.com) here"
    return Line(
        text,
        links=[
            Link("map.png", "http://pictures.example/map.png"),
            Link("https://example.com", "https://example.com", start=8, end=27),
        ],
    )


def test_the_follow_key_acts_on_the_link_a_jump_announced():
    buffer = Buffer()
    buffer.append(_image_and_address())
    cursor = ReviewCursor(buffer)
    cursor.go_to(0)
    picture = cursor.next_link(inclusive=True)
    assert picture.command == "http://pictures.example/map.png"
    assert cursor.link() is picture
    address = cursor.next_link()
    assert address.command == "https://example.com"
    assert cursor.link() is address


def test_the_caret_on_a_link_found_by_its_words_picks_that_link():
    buffer = Buffer()
    buffer.append(_image_and_address())
    cursor = ReviewCursor(buffer)
    cursor.go_to(0)
    cursor.go_to_column(2)  # on "map"
    assert cursor.link().command == "http://pictures.example/map.png"
    cursor.go_to_column(12)  # on the address
    assert cursor.link().command == "https://example.com"


def test_jumping_along_a_line_of_thousands_of_links_is_quick():
    import time

    words = [f"w{number}" for number in range(20_000)]
    text = " ".join(words)
    links, position = [], 0
    for word in words:
        links.append(Link(word, word, start=position, end=position + len(word)))
        position += len(word) + 1
    buffer = Buffer()
    buffer.append(Line(text, links=links))
    cursor = ReviewCursor(buffer)
    cursor.go_to(0)
    started = time.perf_counter()
    assert cursor.next_link(inclusive=True).command == "w0"
    assert cursor.link().command == "w0"
    assert time.perf_counter() - started < 1.0
