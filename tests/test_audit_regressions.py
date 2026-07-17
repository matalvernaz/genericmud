"""Regression tests for the 2026-07 roundtable audit fixes.

Each test pins one confirmed bug so it can't silently return. Grouped by subsystem; the
finding id (H1, M6, ...) from the audit is in each test name/comment.
"""

from __future__ import annotations

import asyncio

import pytest

from genericmud.app import INTERACTIVE_SOURCE, EngineApp
from genericmud.automation.engine import AutomationEngine, EngineSink
from genericmud.model.buffer import Line
from genericmud.navigation import _MAX_SPEEDWALK_STEPS, expand_speedwalk
from genericmud.packs import PackStore, user_rules
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.guard import ScriptGuard
from genericmud.scripting.lua_runtime import LuaPackRuntime
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.vipmud_dialect import VipMudPack
from genericmud.session.credentials import PlaintextCredentialStore
from genericmud.sound.pygame_backend import PygameSoundBackend
from genericmud.transport.connection import MudConnection
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend, RecordingSink


def _app(tmp_path, *, name="mud", credentials=None):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(
        voice, send=sent.append, post=[].append, keymap={},
        packs=PackStore(tmp_path / "store"), name=name, credentials=credentials,
    )
    return app, sent, backend


# --- transport (H1, L5) ---


class _FakeWriter:
    def is_closing(self):
        return False

    def close(self):
        pass


async def test_connect_resets_negotiation_state(monkeypatch):
    """H1: a reconnect must start from clean telnet/MCCP state, or stale zlib + option sets
    corrupt the new link and suppress GMCP/MSDP re-negotiation."""
    conn = MudConnection()
    conn._remote_enabled.add(999)
    conn._local_enabled.add(888)
    stale_parser = conn._parser

    class _Reader:
        async def read(self, _n):
            return b""  # immediate EOF

    async def fake_open(host, port, ssl=None):
        return _Reader(), _FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    await conn.connect("host", 23)
    assert conn._remote_enabled == set()
    assert conn._local_enabled == set()
    assert conn._parser is not stale_parser
    if conn._read_task is not None:
        await conn._read_task


async def test_disconnect_during_pending_connect_stays_closed(monkeypatch):
    """L5: close() during the handshake can't cancel a not-yet-created read task, so connect()
    must notice _closing and drop the socket instead of going live behind the user."""
    conn = MudConnection()

    async def fake_open(host, port, ssl=None):
        conn._closing = True  # user hit Disconnect mid-handshake
        return object(), _FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    await conn.connect("host", 23)
    assert conn._writer is None
    assert conn._read_task is None


# --- automation engine / scripting timers (H4) ---


def test_cancel_timers_cancels_pending():
    """H4: pending pack timers must be cancellable at session close so none fire (starting
    audio) after the tab is gone."""

    class _Handle:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    handles: list[_Handle] = []

    class _HandleSink(EngineSink):
        def schedule(self, delay, callback):
            handle = _Handle()
            handles.append(handle)
            return handle

    engine = AutomationEngine(_HandleSink())
    api = ScriptApi(engine)
    api.add_timer(1.0, lambda: None)
    api.add_timer(1.0, lambda: None)
    assert not any(h.cancelled for h in handles)
    engine.cancel_timers()
    assert all(h.cancelled for h in handles)


# --- interactive rules vs the soundpack builder (H5, M1) ---


def test_builder_source_is_distinct_from_interactive():
    """H5 (engine level): remove_source(user) is what a builder save does; it must not take
    the interactive /alias registered under a different source."""
    engine = AutomationEngine()
    engine.add_alias("^k$", lambda ctx: None, regex=True, name="k", source=INTERACTIVE_SOURCE)
    engine.add_trigger("hi", lambda ctx: None, regex=True, name="hi", source=user_rules.SOURCE)
    engine.remove_source(user_rules.SOURCE)
    aliases = engine.registrations_by_source().get(INTERACTIVE_SOURCE, {}).get("alias", [])
    assert aliases, "the interactive alias must survive a builder reload"


def test_interactive_alias_survives_builder_reload(tmp_path):
    """H5 (end to end): defining /alias then reloading user rules (a builder save) must not
    silently delete the alias."""
    app, sent, _ = _app(tmp_path)
    app.on_ws_message({"type": "input", "text": "/alias k = kill"})
    app.reload_user_rules()  # a soundpack-builder save reloads the user rules
    sent.clear()
    app.on_ws_message({"type": "input", "text": "k"})
    assert sent == ["kill"]


def test_bad_user_rule_does_not_wipe_the_working_set(tmp_path):
    """M1: a rule with a bad advanced-regex must be validated before the live rules are
    removed, so it can't leave the world with zero user rules."""
    app, _sent, _ = _app(tmp_path)
    pack_dir = app.user_rules_dir()
    user_rules.save_rules(
        pack_dir,
        user_rules.UserRules(triggers=[user_rules.UserTrigger(pattern="hello", speak="hi")]),
    )
    app.reload_user_rules()
    good = app.engine.registrations_by_source()[user_rules.SOURCE]["trigger"]
    assert good, "the good trigger should be registered"

    user_rules.save_rules(
        pack_dir,
        user_rules.UserRules(triggers=[user_rules.UserTrigger(pattern="(", regex=True, speak="x")]),
    )
    app.reload_user_rules()  # bad regex: must keep the working set, not wipe it
    after = app.engine.registrations_by_source().get(user_rules.SOURCE, {}).get("trigger", [])
    assert after == good


# --- navigation (M13) ---


def test_speedwalk_rejects_absurd_repeat_count():
    """M13: a mistyped/pasted count must not allocate a giant list (client freeze)."""
    assert expand_speedwalk("999999999n") == []
    assert expand_speedwalk(f"{_MAX_SPEEDWALK_STEPS + 1}n") == []
    assert expand_speedwalk("500n") == ["n"] * 500  # a large-but-sane route still expands


def test_new_safe_walk_cancels_the_previous(tmp_path):
    """H7: starting a second walk must cancel the first, or two sets of step timers interleave."""
    app, _sent, _ = _app(tmp_path)
    app.on_ws_message({"type": "input", "text": "..3n"})
    walk1 = app._walk
    assert walk1 is not None and walk1.active
    app.on_ws_message({"type": "input", "text": "..2e"})
    assert app._walk is not walk1
    assert not walk1.active  # the superseded walk was cancelled
    assert app._walk.active


# --- VIPMud #math (M6) ---


def _vip(source):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vipmud")).load_source(source)
    return sink, engine


def test_math_rejects_exponentiation_bomb():
    """M6: ** is regex-clean (the class allows *) but 9**9**9 is a big-int CPU/memory bomb."""
    _sink, engine = _vip("#VAR pan {7}\n#MATH pan {9**9**9}")
    assert engine.get_var("pan") == "7"  # refused, prior value untouched


def test_math_still_computes_plain_arithmetic():
    _sink, engine = _vip("#MATH pan {3 * 50}")
    assert engine.get_var("pan") == "150"


# --- MUSHclient sound (M3, M4) ---


def _mush(tmp_path, script, *, full_stdlib=True):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(
        ScriptApi(engine, source="p", base_dir=str(tmp_path)), full_stdlib=full_stdlib
    )
    pack.load_source(f"<muclient><script><![CDATA[\n{script}\n]]></script></muclient>")
    return sink, engine


def test_playsound_forwards_volume_and_pan(tmp_path):
    """M4: PlaySound(buffer, file, loop, volume, pan) dropped volume+pan before -- every cue
    played full-volume and centered (pan is directional info for a blind user)."""
    sink, _engine = _mush(tmp_path, 'PlaySound(0, "hit.wav", false, 50, -100)')
    assert sink.played, "PlaySound must reach the sound sink"
    cue = sink.played[-1]
    assert cue["gain"] == pytest.approx(0.5)  # 50 / 100
    assert cue["pan"] == pytest.approx(-1.0)  # -100 / 100 (hard left)


def test_sound_volume_directive_leaves_category_master_untouched(tmp_path):
    """M3: Sound("volume=50") must re-level the live cue (adjust), not permanently drop the
    whole sound category (set_volume), which would halve every future cue."""
    _sink, engine = _mush(tmp_path, 'Sound("volume=50")')
    assert engine.sound.policy("sound").gain == 1.0


# --- Lua sandbox (H6) ---


def test_untrusted_sandbox_removes_coroutine():
    """H6: coroutine child threads don't inherit the instruction-count guard, so an untrusted
    pack could loop forever inside one; it must be stripped from the locked-down sandbox."""
    engine = AutomationEngine(RecordingSink())
    runtime = LuaPackRuntime(ScriptApi(engine, source="lua"))
    assert runtime.run_source("return coroutine == nil") is True


# --- ScriptGuard error reporting (M12) ---


def test_guard_reports_contained_error():
    """M12: a contained fire-time fault was swallowed silently (missing cue, no word); the
    guard must now hand it to a reporter."""
    reported: list[Exception] = []
    guard = ScriptGuard(None, report=reported.append)

    def boom():
        raise ValueError("nope")

    assert guard.run(boom) is None
    assert len(reported) == 1
    assert isinstance(reported[0], ValueError)
    assert guard.last_error is reported[0]


# --- credentials at rest (M11) ---


def test_credentials_file_is_owner_only_and_survives_corruption(tmp_path):
    """M11: the plaintext passwords file must not be world-readable, and a corrupt file must
    not crash the next load (which would break session startup)."""
    import os
    import stat

    path = tmp_path / "credentials.json"
    store = PlaintextCredentialStore(path)
    store.set("mud", "user", "secret")
    assert store.get("mud") == ("user", "secret")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if os.name == "posix":  # Windows uses ACLs, not mode bits
        assert mode & 0o077 == 0, f"credentials.json is group/other-readable: {oct(mode)}"

    path.write_text("{ this is not valid json", encoding="utf-8")
    assert PlaintextCredentialStore(path).get("mud") is None  # corrupt -> empty, no crash


# --- pack load rollback (L3) ---


def test_failed_pack_leaves_no_partial_registrations(tmp_path):
    """L3: a pack that registers a trigger then errors must be rolled back, not left half-live."""
    from genericmud.packs import activate_world

    store = PackStore(tmp_path / "store")
    src = tmp_path / "halfbad.lua"
    src.write_text(
        'mud.trigger("ping", function() mud.send("pong") end)\nerror("boom")', encoding="utf-8"
    )
    store.install(src, world="mud", trust=True)
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    result = activate_world(store, "mud", engine)
    assert "halfbad" in result.failed
    engine.process_line(Line("ping"))
    assert sink.sent == []  # the partially-registered trigger was rolled back


# --- pygame music channel (L1) ---


class _FakeMusic:
    def __init__(self):
        self.played: list[int] = []
        self.stopped = 0

    def load(self, file):
        pass

    def set_volume(self, volume):
        pass

    def play(self, loops):
        self.played.append(loops)

    def stop(self):
        self.stopped += 1


class _FakeMixer:
    def __init__(self):
        self.music = _FakeMusic()

    def get_num_channels(self):
        return 8

    def Channel(self, index):  # noqa: N802
        raise AssertionError("music must not allocate a mixer channel")

    def Sound(self, path):  # noqa: N802
        return ("sound", path)


def test_music_on_custom_channel_is_stoppable():
    """L1: music always drives the single global stream but may be filed under a channel other
    than 'music'; stop()/flush() on that channel must still stop it."""
    mixer = _FakeMixer()
    backend = PygameSoundBackend(mixer)
    backend.music("song.ogg", channel="area", gain=1.0)
    assert mixer.music.played  # started
    backend.stop("area")
    assert mixer.music.stopped == 1  # the global stream was actually stopped
