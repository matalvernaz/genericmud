"""The engine half of Find: a FIND message searches the scrollback and reports back."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import DataReceived
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app(*lines: str) -> tuple[EngineApp, RecordingBackend, list[dict]]:
    backend = RecordingBackend()
    posted: list[dict] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=lambda text: None,
        post=posted.append,
        keymap=load_keymap("vipmud"),
    )
    for text in lines:
        app.on_telnet_event(DataReceived(text.encode() + b"\r\n"))
    posted.clear()  # only care about what the search posts
    return app, backend, posted


def _results(posted: list[dict]) -> list[dict]:
    return [m for m in posted if m["type"] == protocol.FIND_RESULT]


def test_find_reports_the_matching_line_and_speaks_it():
    app, backend, posted = _app("a quiet road", "a dragon roars", "a quiet road again")
    app.on_ws_message(protocol.find("dragon"))
    assert _results(posted) == [{"type": protocol.FIND_RESULT, "text": "a dragon roars",
                                 "found": True}]
    assert "a dragon roars" in backend.spoken


def test_a_miss_is_reported_as_not_found_and_said_aloud():
    app, backend, posted = _app("a quiet road")
    app.on_ws_message(protocol.find("griffin"))
    assert _results(posted) == [{"type": protocol.FIND_RESULT, "text": "", "found": False}]
    assert "not found: griffin" in backend.spoken


def test_new_find_includes_the_edge_line_in_the_chosen_direction():
    backward, _backend, backward_posted = _app("old dragon", "middle", "new dragon")
    backward.on_ws_message(protocol.find("dragon"))
    assert _results(backward_posted)[-1]["text"] == "new dragon"

    forward, _backend, forward_posted = _app("old dragon", "middle", "new dragon")
    forward.on_ws_message(protocol.find("dragon", forward=True))
    assert _results(forward_posted)[-1]["text"] == "old dragon"


def test_find_works_when_the_only_line_contains_the_term():
    app, _backend, posted = _app("a dragon roars")
    app.on_ws_message(protocol.find("dragon"))
    assert _results(posted)[-1] == {
        "type": protocol.FIND_RESULT,
        "text": "a dragon roars",
        "found": True,
    }


def test_repeating_a_find_walks_through_the_matches():
    app, _backend, posted = _app("first dragon", "second dragon", "third dragon", "tail")
    for _ in range(3):
        app.on_ws_message(protocol.find("dragon"))
    assert [m["text"] for m in _results(posted)] == [
        "third dragon", "second dragon", "first dragon",
    ]


def test_repeating_a_forward_find_advances_from_the_oldest_match():
    app, _backend, posted = _app("first dragon", "middle", "third dragon")
    app.on_ws_message(protocol.find("dragon", forward=True))
    app.on_ws_message(protocol.find("dragon", forward=True))
    assert [message["text"] for message in _results(posted)] == [
        "first dragon",
        "third dragon",
    ]


def test_reopening_find_restarts_from_the_direction_edge():
    app, _backend, posted = _app("first dragon", "second dragon", "third dragon")
    app.on_ws_message(protocol.find("dragon", restart=True))
    app.on_ws_message(protocol.find("dragon"))
    app.on_ws_message(protocol.find("dragon", restart=True))
    assert [message["text"] for message in _results(posted)] == [
        "third dragon",
        "second dragon",
        "third dragon",
    ]


def test_a_find_from_the_bottom_goes_back_from_the_newest_line_not_the_last_match():
    # Going back to the output puts the reader at the newest line, and F3 then searches
    # from there instead of carrying on from the line it found last.
    app, _backend, posted = _app("first dragon", "second dragon", "third dragon", "tail")
    app.on_ws_message(protocol.find("dragon", restart=True))
    app.on_ws_message(protocol.find("dragon"))
    app.on_ws_message(protocol.find("dragon", from_bottom=True))
    app.on_ws_message(protocol.find("dragon"))
    assert [message["text"] for message in _results(posted)] == [
        "third dragon",
        "second dragon",
        "third dragon",
        "second dragon",
    ]


def test_from_the_bottom_back_includes_the_newest_line_and_forward_finds_nothing():
    app, _backend, posted = _app("old dragon", "middle", "new dragon")
    app.on_ws_message(protocol.find("dragon", forward=True, restart=True))
    app.on_ws_message(protocol.find("dragon", forward=True, from_bottom=True))
    app.on_ws_message(protocol.find("dragon", from_bottom=True))
    assert [(message["text"], message["found"]) for message in _results(posted)] == [
        ("old dragon", True),
        ("", False),
        ("new dragon", True),
    ]


def test_case_sensitive_find_skips_the_wrong_case():
    app, _backend, posted = _app("a dragon roars", "a Dragon sleeps", "tail")
    app.on_ws_message(protocol.find("dragon", case_sensitive=True))
    assert _results(posted)[-1]["text"] == "a dragon roars"


def test_case_insensitive_find_takes_the_nearest_either_way():
    app, _backend, posted = _app("a dragon roars", "a Dragon sleeps", "tail")
    app.on_ws_message(protocol.find("dragon"))
    assert _results(posted)[-1]["text"] == "a Dragon sleeps"


def test_find_on_an_empty_buffer_is_a_miss_not_a_crash():
    app, _backend, posted = _app()
    app.on_ws_message(protocol.find("anything"))
    assert _results(posted) == [{"type": protocol.FIND_RESULT, "text": "", "found": False}]

def _find(app, term, **kwargs):
    """A search that says which line the reader is standing on, as the window does."""
    app.on_ws_message(protocol.find(term, **kwargs))


def test_a_find_starts_from_the_line_the_reader_is_standing_on():
    # Standing on a line in the middle of the output: back means the nearest match at or above
    # that line, not the one the reader has already gone past below it.
    app, _backend, posted = _app("old brady", "newer brady", "standing here", "newest brady")
    _find(app, "brady", text="standing here", from_end=1)
    assert _results(posted)[-1]["text"] == "newer brady"


def test_a_fresh_find_on_the_reader_line_answers_with_that_line_itself():
    # The newest line may be the match the reader is looking at: a search from there includes it
    # rather than skipping to an older one, which is what "it ignored the line below" was.
    app, _backend, posted = _app(
        "brady pages: Well that sounds positive.", "idle message from brady: workin"
    )
    _find(app, "brady", text="idle message from brady: workin", from_end=0, restart=True)
    assert _results(posted)[-1]["text"] == "idle message from brady: workin"


def test_asking_again_goes_on_from_where_the_reader_has_moved_to():
    # The reported bug: after a hit, the reader arrows up to an earlier line and asks again.
    # Carrying on from the last hit answered with the line they were already standing on --
    # "it just jumps back to the same message" -- instead of going on upwards from the caret.
    app, _backend, posted = _app("first brady", "second brady", "third brady", "tail")
    _find(app, "brady", text="tail", from_end=0, restart=True)
    _find(app, "brady", text="second brady", from_end=1)  # the reader has arrowed up to here
    assert [message["text"] for message in _results(posted)] == [
        "third brady",
        "first brady",
    ]


def test_the_reader_line_is_matched_nearest_the_end_when_it_repeats():
    # The same words twice: the reader's distance from the end picks which twin they are on.
    # Standing on the upper one, there is nothing above it to find -- were the lower twin taken
    # for the reader's line, the match between the two would be answered instead.
    app, _backend, posted = _app("same line", "brady here", "same line", "tail")
    _find(app, "brady", text="same line", from_end=2)
    assert _results(posted)[-1]["found"] is False


def test_a_line_the_buffer_no_longer_holds_falls_back_to_the_edge():
    # A caret on a line that has been trimmed out of the deep buffer says nothing about where
    # the reader is, so the search does the old thing rather than failing.
    app, _backend, posted = _app("old dragon", "middle", "new dragon")
    _find(app, "dragon", text="a line that was trimmed away", from_end=0, restart=True)
    assert _results(posted)[-1]["text"] == "new dragon"
