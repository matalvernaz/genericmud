"""Echo input: showing what you type in the output as well.

Off by default and per world, because most people find a doubled command line noise. It has
nothing to do with logging the same thing -- either can be wanted without the other -- and it
must never put a password into the scrollback.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.config.worlds import World, load_worlds, save_worlds
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app(**kwargs):
    backend = RecordingBackend()
    posted: list[dict] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=[].append,
        post=posted.append,
        keymap=load_keymap("vipmud"),
        name="gw",
        **kwargs,
    )
    return app, backend, posted


def _type(app: EngineApp, text: str) -> None:
    app.on_ws_message({"type": "input", "text": text})


def _echoed(posted: list[dict]) -> list[str]:
    return [
        message["text"]
        for message in posted
        if message.get("type") == protocol.LINE and message["text"].startswith("> ")
    ]


def test_off_by_default_so_nothing_changes_unasked():
    app, _backend, posted = _app()
    _type(app, "look")
    assert _echoed(posted) == []
    assert app.buffer.lines() == []


def test_on_puts_the_command_in_the_output():
    app, _backend, posted = _app()
    app.echo_input = True
    _type(app, "look")
    assert _echoed(posted) == ["> look"]


def test_the_echo_is_buffered_so_the_review_keys_can_read_it_back():
    # Shown on screen alone would not be much use to someone listening; the buffer is what the
    # review keys walk.
    app, _backend, _posted = _app()
    app.echo_input = True
    _type(app, "score")
    assert app.buffer.lines()[-1].plain_text == "> score"
    assert app.review.recall(1) == "> score"


def test_the_echo_is_never_spoken():
    app, backend, _posted = _app()
    app.echo_input = True
    _type(app, "look")
    assert backend.spoken == []  # typed by hand: hearing it again is noise


def test_a_hidden_password_is_not_echoed():
    app, _backend, posted = _app()
    app.echo_input = True
    app._input_masked = True
    _type(app, "hunter2")
    assert _echoed(posted) == []
    assert app.buffer.lines() == []


def test_client_commands_are_echoed_too_since_they_are_typed():
    app, _backend, posted = _app()
    app.echo_input = True
    _type(app, "/aliases")
    assert _echoed(posted) == ["> /aliases"]


def test_echoing_does_not_depend_on_logging():
    # The two settings are deliberately separate.
    app, _backend, posted = _app()
    app.echo_input = True
    app.log_commands = False
    _type(app, "look")
    assert _echoed(posted) == ["> look"]


def test_a_stacked_line_echoes_as_the_parts_that_were_sent():
    # "n;look" is two commands, so it reads back as two lines: those are the commands the MUD
    # received, in order, rather than the one line that was typed.
    app, _backend, posted = _app()
    app.echo_input = True
    _type(app, "n;look")
    assert _echoed(posted) == ["> n", "> look"]


def test_editing_a_world_while_open_takes_effect_at_once():
    app, _backend, _posted = _app()
    assert app.echo_input is False
    app.apply_world_settings(World("gw", "h.example", 4000, echo_input=True))
    _type(app, "look")
    assert app.buffer.lines()[-1].plain_text == "> look"


def test_the_world_setting_round_trips(tmp_path):
    path = tmp_path / "worlds.toml"
    world = World("Echoing", "h.example", 4000, echo_input=True)
    save_worlds([world], path)
    assert load_worlds(path) == [world]
    assert load_worlds(path)[0].echo_input is True


def test_a_world_without_it_writes_none(tmp_path):
    path = tmp_path / "worlds.toml"
    save_worlds([World("Plain", "h.example", 4000)], path)
    assert "echo_input" not in path.read_text(encoding="utf-8")
    assert World("Plain", "h.example", 4000).echo_input is False
