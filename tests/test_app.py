"""Integration tests for the EngineApp glue (no socket/webview/NVDA needed)."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import GA, OPT_GMCP, Command, DataReceived, Subnegotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app() -> tuple[EngineApp, RecordingBackend, list[str], list[dict]]:
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"))
    return app, backend, sent, posted


def test_incoming_line_spoken_buffered_and_posted():
    app, backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"You see a dragon\r\n"))
    assert "You see a dragon" in backend.spoken
    assert app.buffer.lines()[-1].plain_text == "You see a dragon"
    assert any(m["type"] == "line" and m["text"] == "You see a dragon" for m in posted)


def test_command_stacking_splits_on_separator():
    app, _backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": "get sword;wield sword;n"})
    assert sent == ["get sword", "wield sword", "n"]


def test_command_stacking_drops_empty_pieces_and_records_walk():
    app, _backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": "n;;s;"})
    assert sent == ["n", "s"]
    assert app.nav.trail == ["n", "s"]


def test_command_stacking_can_be_disabled():
    app, _backend, sent, _posted = _app()
    app.command_separator = ""
    app.on_ws_message({"type": "input", "text": "say hi;bye"})
    assert sent == ["say hi;bye"]


def test_speedwalk_piece_inside_a_stack():
    app, _backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": ".2n;look"})
    assert sent == ["n", "n", "look"]


def test_coloured_line_keeps_spans_alongside_plain_text():
    app, _backend, _sent, _posted = _app()
    app.on_telnet_event(DataReceived(b"a \x1b[31mred\x1b[0m word\r\n"))
    line = app.buffer.lines()[-1]
    assert line.plain_text == "a red word"  # speech/matching text unchanged
    assert any(span.fg == "red" and span.text == "red" for span in line.spans)


def test_colour_aware_trigger_sees_span_colour():
    app, _backend, sent, _posted = _app()

    def on_incoming(ctx):
        if any(span.fg == "red" for span in ctx.line.spans):
            ctx.engine.sink.send("alert")  # only react when the line came in red

    app.engine.add_trigger("incoming", on_incoming)
    app.on_telnet_event(DataReceived(b"\x1b[31mincoming attack\x1b[0m\r\n"))
    assert sent == ["alert"]


def test_gagged_line_not_spoken():
    app, backend, _sent, _posted = _app()
    app.engine.add_trigger("spammy", None, gag=True)
    app.on_telnet_event(DataReceived(b"spammy tick\n"))
    assert "spammy tick" not in backend.spoken


def test_prompt_flushed_on_go_ahead():
    app, backend, _sent, _posted = _app()
    app.on_telnet_event(DataReceived(b"HP:100 MP:50>"))  # no newline yet
    assert not any("HP:100" in s for s in backend.spoken)
    app.on_telnet_event(Command(GA))
    assert any("HP:100 MP:50>" in s for s in backend.spoken)


def test_recall_key_speaks_last_line():
    app, backend, _sent, _posted = _app()
    app.on_telnet_event(DataReceived(b"first\nsecond\n"))
    backend.spoken.clear()
    app.on_ws_message({"type": "key", "key": "ctrl+1"})
    assert backend.spoken[-1] == "second"


def test_review_prev_line_key_enters_and_moves():
    app, backend, _sent, _posted = _app()
    app.on_telnet_event(DataReceived(b"l1\nl2\nl3\n"))
    backend.spoken.clear()
    app.on_ws_message({"type": "key", "key": "alt+up"})
    assert backend.spoken[-1] == "l2"


def test_input_is_sent_to_mud():
    app, _backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": "north"})
    assert sent == ["north"]


def test_gmcp_subnegotiation_posts_status():
    app, _backend, _sent, posted = _app()
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'Char.Vitals {"hp":42}'))
    assert any(m["type"] == "status" for m in posted)


def test_msp_line_emits_sound_and_strips_tag():
    app, backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"A thud !!SOUND(hit.wav V=80)\r\n"))
    sounds = [m for m in posted if m["type"] == "sound"]
    assert sounds and sounds[0]["file"] == "hit.wav"
    assert abs(sounds[0]["gain"] - 0.8) < 1e-9
    # the !!SOUND tag is stripped from what gets spoken/displayed
    assert any("A thud" in s and "!!SOUND" not in s for s in backend.spoken)


def test_msp_blocks_unsafe_sound_paths():
    app, _backend, _sent, posted = _app()
    # A hostile MUD sends absolute / UNC / traversal sound paths; none may be played.
    app.on_telnet_event(DataReceived(rb"a !!SOUND(\\attacker\share\x.wav)" + b"\r\n"))
    app.on_telnet_event(DataReceived(b"b !!SOUND(/etc/evil.wav)\r\n"))
    app.on_telnet_event(DataReceived(rb"c !!SOUND(../../secret.wav)" + b"\r\n"))
    assert [m for m in posted if m["type"] == "sound"] == []
    # a safe relative cue still plays
    app.on_telnet_event(DataReceived(b"d !!SOUND(hit.wav)\r\n"))
    assert any(m["type"] == "sound" and m["file"] == "hit.wav" for m in posted)


def test_ansi_stripped_from_output():
    app, backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"\x1b[1;32mGreen room\x1b[0m\r\n"))
    assert any(m["type"] == "line" and m["text"] == "Green room" for m in posted)
    assert "Green room" in backend.spoken


def test_blank_lines_skipped():
    app, backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"hello\r\n\r\n   \r\nworld\r\n"))
    lines = [m["text"] for m in posted if m["type"] == "line"]
    assert lines == ["hello", "world"]  # blank and whitespace-only lines dropped
    assert "" not in backend.spoken


def test_terminal_disconnect_flushes_looping_sound():
    # Quitting the MUD (or a drop we won't reconnect) must silence the pack's looping
    # music/ambience: nothing else will ever stop those cues once the connection is gone.
    # A transient "reconnecting" status keeps them -- the session is expected to resume.
    class _Backend:
        def __init__(self):
            self.stopped: list[str] = []

        def play(self, file, channel, gain, pan, loop):
            pass

        def stop(self, channel):
            self.stopped.append(channel)

    from genericmud.app import EngineApp as _EngineApp

    backend = _Backend()
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    app = _EngineApp(voice, keymap=load_keymap("vipmud"), sound_backend=backend)
    app.sound.play("area25.ogg", "erion-audio-1", loop=True)
    app.on_connection_status("reconnecting in 2s")
    assert backend.stopped == []  # transient: the loop keeps playing across a reconnect
    app.on_connection_status("disconnected")
    assert backend.stopped == ["erion-audio-1"]


def test_pack_variables_persist_across_sessions(tmp_path):
    # MUSHclient SaveState equivalent: user-adjusted pack settings (volumes, toggles)
    # must survive a restart. Saved at shutdown, seeded before packs load on connect
    # (OnPluginInstall's nil-checks then keep the saved values). sppath is wiring,
    # not a setting, and must not be pinned to an old install location.
    from genericmud.packs import PackStore

    store = PackStore(tmp_path / "soundpacks")

    def session():
        voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
        return EngineApp(voice, keymap=load_keymap("vipmud"), packs=store, name="Erion")

    first = session()
    first.on_connect("Erion")
    first.engine.set_var("volume1", "42")
    first.engine.set_var("sppath", str(tmp_path))
    first.shutdown()

    second = session()
    second.on_connect("Erion")
    assert second.engine.get_var("volume1") == "42"
    assert second.engine.get_var("sppath") == ""  # not persisted


def test_plugin_tick_chain_dispatches_and_pauses_when_disconnected():
    # The tick chain drives soundpack music engines (Erion restarts ambience there).
    # It must dispatch on schedule, skip dispatch while disconnected, and end at close.
    class _FakePack:
        def __init__(self):
            self.ticks = 0

        def has_hook(self, name):
            return name == "OnPluginTick"

        def dispatch(self, name, *args, **kwargs):
            if name == "OnPluginTick":
                self.ticks += 1

    scheduled = []
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    app = EngineApp(voice, keymap=load_keymap("vipmud"),
                    schedule=lambda delay, cb: scheduled.append(cb))
    pack = _FakePack()
    app._mush_packs = [pack]
    app._arm_plugin_ticks()
    assert len(scheduled) == 1
    scheduled.pop()()  # first tick fires and reschedules
    assert pack.ticks == 1 and len(scheduled) == 1
    app.engine.connected = False
    scheduled.pop()()  # disconnected: no dispatch, chain continues
    assert pack.ticks == 1 and len(scheduled) == 1
    app.engine.connected = True
    app.shutdown()
    scheduled.pop()()  # closed: chain ends, nothing rescheduled
    assert pack.ticks == 1 and scheduled == []
