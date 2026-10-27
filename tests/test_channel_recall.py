"""Ctrl+Alt+0: the tenth message back in a channel.

The channel layer never had a ceiling of nine -- ``ChannelReview.recent`` simply stops when
the channel runs out of lines -- so the only thing capping these keys was the keymap. This
covers the binding, the action it reaches, and what happens when the channel is shorter than
ten lines.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import DataReceived
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app():
    backend = RecordingBackend()
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="gw",
    )
    return app, backend


def _file_lines(app: EngineApp, count: int, channel: str = "say") -> None:
    api = ScriptApi(app.engine, source="pack")
    for number in range(1, count + 1):
        api.file_in_history(channel, f"line {number}")


def _select_channel(app: EngineApp) -> None:
    # Ctrl+Alt+Right lands on the first channel; recent(N) needs one selected.
    app._handle_key("ctrl+alt+right")


def test_ctrl_alt_0_reads_the_tenth_channel_message():
    app, backend = _app()
    _file_lines(app, 10)
    _select_channel(app)
    app._handle_key("ctrl+alt+0")
    assert backend.spoken[-1] == "line 1"  # 10 back from the newest


def test_ctrl_alt_9_still_reads_the_ninth():
    app, backend = _app()
    _file_lines(app, 10)
    _select_channel(app)
    app._handle_key("ctrl+alt+9")
    assert backend.spoken[-1] == "line 2"


def test_a_channel_shorter_than_ten_says_there_is_no_message():
    app, backend = _app()
    _file_lines(app, 5)
    _select_channel(app)
    app._handle_key("ctrl+alt+0")
    assert backend.spoken[-1] == "no message"


def test_the_tenth_message_of_a_channel_that_is_not_the_newest_globally():
    # Channel lines are interleaved with main output; the count is per channel, which is the
    # whole point of the layer.
    app, backend = _app()
    _file_lines(app, 10)
    app.on_telnet_event(DataReceived(b"a main line\r\n"))
    _select_channel(app)
    app._handle_key("ctrl+alt+0")
    assert backend.spoken[-1] == "line 1"


def test_the_binding_is_rebindable_like_the_rest_of_the_row():
    # Nothing about the action says "0": it is an ordinary chan:recall:N, so any key can be
    # pointed at any N, including past ten.
    keymap = load_keymap("vipmud")
    assert keymap["ctrl+alt+0"] == "chan:recall:10"
    assert [keymap[f"ctrl+alt+{n}"] for n in range(1, 10)] == [
        f"chan:recall:{n}" for n in range(1, 10)
    ]
