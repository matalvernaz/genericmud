"""Integration tests for the EngineApp glue (no socket/webview/NVDA needed)."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import (
    GA,
    OPT_GMCP,
    OPT_MSDP,
    WILL,
    Command,
    DataReceived,
    Negotiation,
    Subnegotiation,
)
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
    app.on_ws_message({"type": "input", "text": ";n;s;"})  # ";;" would be a real semicolon
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


def test_recall_key_speaks_the_tenth_line():
    # Ctrl+0 reaches one past VIPMud's nine, so the tenth has to come back spoken.
    app, backend, _sent, _posted = _app()
    app.on_telnet_event(DataReceived("".join(f"line {i}\n" for i in range(1, 11)).encode()))
    backend.spoken.clear()
    app.on_ws_message({"type": "key", "key": "ctrl+0"})
    assert backend.spoken[-1] == "line 1"


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
    assert app.engine.get_mud_var("Char.Vitals.hp") == 42
    assert app.engine.get_mud_var("gmcp.Char.Vitals.hp") == 42


def test_msdp_subnegotiation_updates_script_visible_mud_variables():
    app, _backend, _sent, _posted = _app()
    app.on_telnet_event(Subnegotiation(OPT_MSDP, b"\x01HEALTH\x0273"))
    assert app.engine.get_mud_var("HEALTH") == "73"
    assert app.engine.get_mud_var("msdp.HEALTH") == "73"


def test_incoming_text_and_gmcp_are_routed_to_mushclient_plugins():
    app, _backend, _sent, _posted = _app()

    class Pack:
        lines: list[str] = []
        gmcp: list[tuple[str, object]] = []

        def dispatch_line(self, line):
            self.lines.append(line)

        def dispatch_gmcp(self, package, value):
            self.gmcp.append((package, value))

    pack = Pack()
    app._mush_packs = [pack]
    app.on_telnet_event(DataReceived(b"incoming\r\n"))
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'Comm.Channel {"msg":"hello"}'))
    assert pack.lines == ["incoming"]
    assert pack.gmcp == [("Comm.Channel", {"msg": "hello"})]


def test_msp_line_emits_sound_and_strips_tag():
    app, backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"A thud !!SOUND(hit.wav V=80)\r\n"))
    sounds = [m for m in posted if m["type"] == "sound"]
    assert sounds and sounds[0]["file"] == "hit.wav"
    assert abs(sounds[0]["gain"] - 0.8) < 1e-9
    # the !!SOUND tag is stripped from what gets spoken/displayed
    assert any("A thud" in s and "!!SOUND" not in s for s in backend.spoken)


def _msdp(pairs: dict[str, str]) -> bytes:
    """An MSDP subnegotiation payload of flat VAR/VAL pairs."""
    out = bytearray()
    for name, value in pairs.items():
        out += bytes([1]) + name.encode() + bytes([2]) + value.encode()
    return bytes(out)


def test_msdp_subscribes_to_the_variables_the_server_offers():
    raw: list[bytes] = []
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    app = EngineApp(voice, post=lambda _m: None, send_raw=raw.append)

    app.on_telnet_event(Negotiation(WILL, OPT_MSDP))
    assert raw == [b"\xff\xfaE\x01LIST\x02REPORTABLE_VARIABLES\xff\xf0"]

    # The server answers with what it actually has; we REPORT the intersection, and only
    # the intersection -- asking for a variable the server never advertised is noise at best.
    raw.clear()
    payload = bytes([1]) + b"REPORTABLE_VARIABLES" + bytes([2, 5])
    for name in ("HEALTH", "ROOM_NAME", "SOMETHING_EXOTIC"):
        payload += bytes([2]) + name.encode()
    payload += bytes([6])
    app.on_telnet_event(Subnegotiation(OPT_MSDP, payload))
    requested = [line.split(b"\x02")[1].rstrip(b"\xff\xf0") for line in raw]
    assert requested == [b"HEALTH", b"ROOM_NAME"]
    assert all(b"REPORT" in line for line in raw)


def test_msdp_room_variables_drive_navigation():
    app, _backend, _sent, _posted = _app()
    app.on_telnet_event(
        Subnegotiation(OPT_MSDP, _msdp({"ROOM_NAME": "The Bridge", "AREA_NAME": "Ship"}))
    )
    # Navigator.where() reads lowercase name/area, so the MSDP names have to be translated
    # or a MUD that speaks MSDP instead of GMCP answers "where am I" with nothing.
    assert app.nav.room.get("name") == "The Bridge"
    assert "The Bridge" in app.nav.where()


def test_msdp_room_update_announces_one_move_per_packet():
    app, _backend, _sent, _posted = _app()
    moves = []
    app._on_player_moved = lambda: moves.append(1)
    app.on_telnet_event(
        Subnegotiation(
            OPT_MSDP,
            _msdp({"ROOM_NAME": "Cargo Hold", "ROOM_VNUM": "42", "AREA_NAME": "Ship"}),
        )
    )
    assert len(moves) == 1  # not one per changed variable


def test_gmcp_goodbye_speaks_the_reason_and_stops_reconnecting():
    suppressed = []
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    app = EngineApp(
        voice, post=lambda _m: None, suppress_reconnect=lambda: suppressed.append(True)
    )
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'Core.Goodbye "Banned: ask the admins."'))
    assert suppressed == [True]  # a deliberate kick must not be retried on backoff
    assert any("Banned" in spoken for spoken in backend.spoken)


def test_msp_off_stops_the_music_it_started():
    app, _backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"You enter the tavern !!MUSIC(tavern.mid V=50)\r\n"))
    music = [m for m in posted if m["type"] == "music"]
    assert music and music[0]["file"] == "tavern.mid"
    assert abs(music[0]["gain"] - 0.5) < 1e-9  # V= applies to music, not just sound
    # Music loops until the server stops it, and !!MUSIC(Off) is the only stop it has.
    app.on_telnet_event(DataReceived(b"You leave !!MUSIC(Off)\r\n"))
    assert any(m["type"] == "stop_sound" and m["channel"] == "music" for m in posted)
    assert [m for m in posted if m["type"] == "music"] == music  # "Off" is not a file


def test_msp_sound_off_stops_effects():
    app, _backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"a !!SOUND(Off)\r\n"))
    assert any(m["type"] == "stop_sound" and m["channel"] == "sound" for m in posted)


def test_msp_infinite_loop_count_reaches_the_backend():
    app, _backend, _sent, posted = _app()
    app.on_telnet_event(DataReceived(b"Rain falls !!SOUND(rain.wav L=-1)\r\n"))
    app.on_telnet_event(DataReceived(b"A thud !!SOUND(hit.wav)\r\n"))
    loops = {m["file"]: m["loop"] for m in posted if m["type"] == "sound"}
    assert loops == {"rain.wav": True, "hit.wav": False}


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


def test_after_reconnect_reattaches_the_session_to_a_live_socket():
    # The UI's manual reconnect calls this directly rather than going through a status
    # string, so it has to restore exactly what the transport's backoff loop restores.
    # A socket that is up while the engine still thinks it is down leaves packs paused and
    # auto-login stranded.
    app, _backend, _sent, _posted = _app()
    app.on_connection_status("disconnected")
    assert app.engine.connected is False
    app._input_masked = True  # set by the dead socket's WILL ECHO
    app.after_reconnect()
    assert app.engine.connected is True
    assert app._input_masked is False  # else every command for the rest logs as "***"


def test_after_reconnect_rearms_autologin(monkeypatch):
    app, _backend, _sent, _posted = _app()
    calls: list[str] = []
    monkeypatch.setattr(app, "begin_login", calls.append)
    app.after_reconnect()
    assert calls == [app.name]  # a reconnect that forgets this lands on the login screen


def test_reconnected_status_shares_the_manual_reattach(monkeypatch):
    # Both reconnect paths must land in the same method, or they drift apart.
    app, _backend, _sent, _posted = _app()
    calls: list[bool] = []
    monkeypatch.setattr(app, "after_reconnect", lambda: calls.append(True))
    app.on_connection_status("reconnected")
    assert calls == [True]


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


def test_a_pack_can_quiet_incoming_lines_without_touching_review_or_the_buffer():
    # A pack's Do Not Disturb (Miriani-Next) silences lines as they arrive. They still land
    # in the buffer and the output, so nothing is lost, and the review keys still speak.
    app, backend, _sent, posted = _app()
    app.engine.line_speech = False
    app.on_telnet_event(DataReceived(b"Someone pages you.\r\n"))
    assert "Someone pages you." not in backend.spoken
    assert app.buffer.lines()[-1].plain_text == "Someone pages you."
    assert any(m["type"] == "line" and m["text"] == "Someone pages you." for m in posted)
    app._handle_key("ctrl+1")  # recall the newest line
    assert backend.spoken[-1] == "Someone pages you."


def test_a_line_filed_under_a_channel_shows_up_in_channel_review():
    # MUSHclient packs file chat with history_add; the line is tagged so the channel review
    # keys (Ctrl+Alt+arrows) find it, and "all" adds nothing because the buffer has it.
    from genericmud.model.buffer import Line
    from genericmud.scripting.api import ScriptApi

    app, _backend, _sent, _posted = _app()
    api = ScriptApi(app.engine, source="pack")
    line = Line("Bob says, hi")
    app.engine.current_line = line
    api.file_in_history("say", "Bob says, hi")
    api.file_in_history("all", "Bob says, hi")
    app.engine.current_line = None
    assert line.channel == "say"
    api.file_in_history("Tell", "a line the pack built")
    assert app.buffer.lines()[-1].plain_text == "a line the pack built"
    assert app.buffer.lines()[-1].channel == "tell"
    assert "tell" in app.chan_review.channels()


def test_a_substituted_line_is_shown_and_spoken_instead_of_the_original():
    # VIPMud's #Sub: the line the trigger fired on is replaced by its text.
    from genericmud.scripting.api import ScriptApi
    from genericmud.scripting.vipmud_dialect import VipMudPack

    app, backend, _sent, _posted = _app()
    VipMudPack(ScriptApi(app.engine, source="vip")).load_source(
        "#trigger {* tells you '*'} {#sub {%1: %2}}"
    )
    app.on_telnet_event(DataReceived(b"james tells you 'hi'\r\n"))
    assert "james: hi" in backend.spoken
    assert "james tells you 'hi'" not in backend.spoken
    assert app.buffer.lines()[-1].plain_text == "james: hi"


def test_the_session_log_keeps_the_line_the_mud_sent_when_a_pack_replaces_it():
    from genericmud.scripting.api import ScriptApi
    from genericmud.scripting.vipmud_dialect import VipMudPack

    class _Logger:
        active = True

        def __init__(self):
            self.lines = []

        def log(self, text):
            self.lines.append(text)
            return True

    app, _backend, _sent, _posted = _app()
    app.logger = _Logger()
    VipMudPack(ScriptApi(app.engine, source="vip")).load_source(
        "#trigger {* tells you '*'} {#sub {%1: %2}}\n#trigger {spam} {#sub {}}\n"
    )
    app.on_telnet_event(DataReceived(b"james tells you 'hi'\r\nspam\r\n"))
    assert app.logger.lines == ["james tells you 'hi'", "spam"]


def test_a_doubled_separator_sends_itself():
    # MUSHclient's rule: "say hi;; bye" is one command with a real semicolon in it.
    app, _backend, sent, _posted = _app()
    app.on_ws_message({"type": "input", "text": "say hi;; bye;look"})
    assert sent == ["say hi; bye", "look"]


def test_a_world_can_use_its_own_separator_or_none():
    from genericmud.config.worlds import World

    app, _backend, sent, _posted = _app()
    app.apply_world_settings(World("W", "w.example", 23, command_separator="|"))
    app.on_ws_message({"type": "input", "text": "n|say a;b||c"})
    assert sent == ["n", "say a;b|c"]
    sent.clear()
    app.apply_world_settings(World("W", "w.example", 23, command_stacking=False))
    app.on_ws_message({"type": "input", "text": "say hi;bye;;"})
    assert sent == ["say hi;bye;;"]


def test_the_worlds_separator_splits_the_players_own_aliases_too():
    from genericmud.config.worlds import World

    app, _backend, sent, _posted = _app()
    app.apply_world_settings(World("W", "w.example", 23, command_separator="|"))
    app.on_ws_message({"type": "input", "text": "/alias gg = get gold|look"})
    sent.clear()
    app.on_ws_message({"type": "input", "text": "gg"})
    assert sent == ["get gold", "look"]


def test_soundpacks_split_their_own_way_whatever_the_world_says():
    from genericmud.config.worlds import World
    from genericmud.scripting.api import ScriptApi
    from genericmud.scripting.vipmud_dialect import VipMudPack

    app, _backend, sent, _posted = _app()
    app.apply_world_settings(World("W", "w.example", 23, command_stacking=False))
    VipMudPack(ScriptApi(app.engine, source="vip")).load_source("#alias go {n;s}")
    app.on_ws_message({"type": "input", "text": "go"})
    assert sent == ["n", "s"]
