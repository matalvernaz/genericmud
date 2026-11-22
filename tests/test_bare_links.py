"""A bare address in the text is followable, the way a MUD's own link is.

Issue #123: a MUD could only make a link by tagging one. "The rules are at
https://example.com/rules" arrived as words, and the only way to use the address was to select
it out of the output and copy it. Now it is a link like any other: **Alt+Enter** follows it, or
plain **Enter** while the output box has focus, and the address is said before the browser takes
the keyboard focus away.

No switch: following a link is already a key the player chooses to press.
"""

from __future__ import annotations

import pytest

from genericmud.app import EngineApp
from genericmud.app.links import bare_link_spans
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import OPT_MXP, WILL, DataReceived, Negotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"
SECURE = ESC + "[1z"


class FakeWindow:
    """Stands in for the native window's open_url, and remembers what had been said by then."""

    def __init__(self, backend: RecordingBackend, opens: bool = True) -> None:
        self.urls: list[str] = []
        self.said_before: list[list[str]] = []
        self._backend = backend
        self._opens = opens

    def open_url(self, url: str, *, announced: bool = False) -> bool:
        if self._opens:
            self.urls.append(url)
            self.said_before.append(list(self._backend.spoken))
        return self._opens


def _build(backend=None, *, window=None):
    backend = backend if backend is not None else RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    posted: list[dict] = []
    app = EngineApp(
        voice,
        send=lambda text: None,
        post=posted.append,
        keymap=load_keymap("vipmud"),
    )
    app.on_telnet_event(Negotiation(WILL, OPT_MXP))
    app.pack_window = window
    return app, backend, posted


def _say(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))


def _line_message(posted: list[dict]) -> dict:
    lines = [message for message in posted if message.get("type") == protocol.LINE]
    assert lines, "no line message was posted"
    return lines[-1]


def _press(app: EngineApp, key: str = "alt+enter") -> None:
    app.on_ws_message({"type": "key", "key": key})


def _spans(text: str, covered=None):
    return bare_link_spans(text, covered or [])


# --- what counts as an address ---------------------------------------------------------------


def test_a_bare_address_is_found_where_it_sits():
    line = "the rules are at https://example.com/rules"
    start = line.index("https://")
    assert _spans(line) == [
        (
            start,
            start + len("https://example.com/rules"),
            "https://example.com/rules",
            False,
            [],
            "",
        )
    ]


def test_a_sentence_full_stop_is_not_part_of_the_address():
    # The commonest thing in running prose: the address ends the sentence.
    assert _spans("see https://example.com.")[0][2] == "https://example.com"
    assert _spans("go to https://example.com, then read")[0][2] == "https://example.com"
    assert _spans("is it https://example.com?")[0][2] == "https://example.com"
    assert _spans("https://example.com/rules!!")[0][2] == "https://example.com/rules"


def test_a_bracket_around_an_address_is_the_writers():
    assert _spans("(see https://example.com)")[0][2] == "https://example.com"
    assert _spans("[https://example.com/rules]")[0][2] == "https://example.com/rules"
    assert _spans('"https://example.com"')[0][2] == "https://example.com"


def test_a_bracket_the_address_closes_itself_stays():
    # A wiki address really does end in a bracket, so an address that opened one keeps it.
    assert _spans("https://en.wikipedia.org/wiki/Mercury_(planet)")[0][2] == (
        "https://en.wikipedia.org/wiki/Mercury_(planet)"
    )


def test_a_www_address_is_opened_over_https():
    # The words are what was written; only the address opened is completed, because the text
    # never chose a scheme and this is the one the client already trusts.
    span = _spans("the wiki is at www.example.com/rules")[0]
    assert span[2] == "https://www.example.com/rules"
    text = "the wiki is at www.example.com/rules"
    assert text[span[0]:span[1]] == "www.example.com/rules"


def test_http_is_kept_as_written():
    assert _spans("http://example.com/old")[0][2] == "http://example.com/old"


def test_other_schemes_are_not_links():
    # The allow-list is the security boundary: nothing else may reach the shell or an app.
    for line in (
        "javascript:alert(1)",
        "file:///etc/passwd",
        "file:///C:/Windows/System32/calc.exe",
        "mailto:someone@example.com",
        "data:text/html,<b>x</b>",
    ):
        assert _spans(line) == [], line


def test_an_email_address_is_not_a_link():
    # Following one would open a mail client, which is not what a web address is for.
    assert _spans("write to Ariadne@example.com about it") == []


def test_a_word_that_only_starts_like_an_address_is_left_alone():
    assert _spans("www. is not an address") == []
    assert _spans("https:// is not an address") == []
    assert _spans("https:///no-host") == []


# --- the bounds ------------------------------------------------------------------------------


def test_at_most_eight_addresses_a_line_become_links():
    line = " ".join(f"https://example.com/{number}" for number in range(12))
    assert len(_spans(line)) == 8


def test_an_address_over_the_length_cap_stays_plain_text():
    assert _spans("https://example.com/" + "a" * 2000) == []  # over the cap
    assert len(_spans("https://example.com/" + "a" * 1000)) == 1  # well under it


# --- through the app -------------------------------------------------------------------------


def test_the_line_a_player_hears_keeps_its_words_and_gains_a_link():
    line = "The rules are at https://example.com/rules, and they are short."
    app, _backend, posted = _build()
    _say(app, line)
    message = _line_message(posted)
    start = line.index("https://")
    assert message["text"] == line
    assert message["links"] == [
        [
            start,
            start + len("https://example.com/rules"),
            "https://example.com/rules",
            False,
            [],
            "",
        ]
    ]


def test_alt_enter_opens_a_bare_address():
    backend = RecordingBackend()
    window = FakeWindow(backend)
    app, _backend, _posted = _build(backend, window=window)
    _say(app, "the rules are at https://example.com/rules")
    _press(app)
    assert window.urls == ["https://example.com/rules"]


def test_the_address_is_said_before_the_browser_takes_the_focus():
    # The browser takes the keyboard focus, and with it the screen reader, so an announcement
    # afterwards is one the player may never hear.
    backend = RecordingBackend()
    window = FakeWindow(backend)
    app, _backend, _posted = _build(backend, window=window)
    _say(app, "the rules are at https://example.com/rules")
    _press(app)
    assert "opening https://example.com/rules" in window.said_before[0]


def test_a_tagged_link_to_a_page_is_said_first_too():
    # The same move helps the links genericMud already followed.
    backend = RecordingBackend()
    window = FakeWindow(backend)
    app, _backend, _posted = _build(backend, window=window)
    _say(app, SECURE + '<a href="https://example.com">the rules</a>')
    _press(app)
    assert window.urls == ["https://example.com"]
    assert "opening https://example.com" in window.said_before[0]


def test_a_muds_own_link_wins_over_the_address_in_its_words():
    # Its link may do something other than open a page, and two links over one word would leave
    # the reader choosing between them on that word.
    app, _backend, posted = _build()
    _say(app, SECURE + '<send href="say fetched">https://example.com</send>')
    message = _line_message(posted)
    assert message["links"] == [[0, 19, "say fetched", False, [], ""]]


def test_an_address_is_found_without_mxp_at_all():
    # A world that never negotiates MXP still gets this: it is not markup, it is text.
    backend = RecordingBackend()
    window = FakeWindow(backend)
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    posted: list[dict] = []
    app = EngineApp(
        voice, send=lambda text: None, post=posted.append, keymap=load_keymap("vipmud")
    )
    app.pack_window = window
    line = "the wiki is at https://example.com/wiki until Friday"
    _say(app, line)
    start = line.index("https://")
    assert _line_message(posted)["links"] == [
        [start, start + len("https://example.com/wiki"), "https://example.com/wiki", False, [], ""]
    ]
    _press(app)
    assert window.urls == ["https://example.com/wiki"]


def test_a_refused_browser_says_so_and_nothing_is_sent_to_the_mud():
    backend = RecordingBackend()
    window = FakeWindow(backend, opens=False)
    app, _backend, _posted = _build(backend, window=window)
    _say(app, "https://example.com")
    _press(app)
    assert window.urls == []
    assert backend.spoken[-1] == "no browser to open that link"


class AnnouncingWindow(FakeWindow):
    """Records whether it was told the session had already announced the address."""

    def __init__(self, backend: RecordingBackend) -> None:
        super().__init__(backend)
        self.announced: list[bool] = []

    def open_url(self, url: str, *, announced: bool = False) -> bool:
        self.announced.append(announced)
        return super().open_url(url, announced=announced)


def test_opening_a_link_says_it_once_and_not_twice():
    # The window says "Opening <address>" in its own right, for a pack that opens a browser
    # without a word. A link is already announced by the session, so the window has to be told:
    # otherwise the same line lands in the output twice, once from each side, differing only in
    # a capital letter. That is what a player found when the feature was first tried.
    backend = RecordingBackend()
    window = AnnouncingWindow(backend)
    app, _backend, posted = _build(backend, window=window)
    _say(app, "the rules are at https://example.com/rules")
    _press(app)
    assert window.announced == [True]
    said = [
        message["text"]
        for message in posted
        if message.get("type") == protocol.ECHO
        and "opening https://example.com/rules" in message["text"].lower()
    ]
    assert said == ["* opening https://example.com/rules"]


@pytest.mark.parametrize(
    "line",
    [
        "no addresses here at all",
        "an address split across two words: https:// isn't one",
    ],
)
def test_a_line_without_an_address_gets_no_links(line):
    app, _backend, posted = _build()
    _say(app, line)
    assert _line_message(posted)["links"] == []


# --- found in order, cheaply, and only in the MUD's own words -------------------------------------


def test_link_jumping_meets_an_address_before_a_muds_link_on_the_same_line():
    app, backend, _posted = _build()
    _say(app, SECURE + 'Rules at https://example.com/rules or go <send href="north">north</send>.')
    line = "Rules at https://example.com/rules or go north."
    _press(app, "alt+pageup")
    assert backend.spoken[-1] == f"north: {line}"
    _press(app, "alt+pageup")
    assert backend.spoken[-1] == f"https://example.com/rules: {line}"
    _press(app, "alt+pageup")
    assert backend.spoken[-1] == "no more links"


def test_a_long_run_of_closing_brackets_after_an_address_is_read_in_moments():
    import time

    started = time.perf_counter()
    spans = _spans("https://x" + ")" * 100_000)
    assert time.perf_counter() - started < 1.0
    assert [span[2] for span in spans] == ["https://x"]


def test_a_line_of_muds_links_over_addresses_is_read_in_moments():
    import time

    words = [f"https://e{number}.com" for number in range(20_000)]
    text = " ".join(words)
    covered, position = [], 0
    for word in words:
        covered.append((position, position + len(word), "say x", False, [], ""))
        position += len(word) + 1
    started = time.perf_counter()
    assert _spans(text, covered) == []
    assert time.perf_counter() - started < 1.0


def test_an_address_a_pack_replaced_is_no_link():
    # The address was in words the player no longer gets, so following the line must not open it.
    from genericmud.scripting.api import ScriptApi
    from genericmud.scripting.vipmud_dialect import VipMudPack

    backend = RecordingBackend()
    window = FakeWindow(backend)
    app, backend, posted = _build(backend, window=window)
    VipMudPack(ScriptApi(app.engine, source="vip")).load_source(
        "#trigger {The rules are at *} {#sub {Rules posted.}}"
    )
    _say(app, "The rules are at https://example.com/rules")
    assert _line_message(posted)["links"] == []
    _press(app)
    assert window.urls == []
    assert backend.spoken[-1] == "no link on this line"
