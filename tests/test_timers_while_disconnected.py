"""A timer can keep its clock through a disconnect (MUSHclient's ActiveWhenClosed).

A world's rules are registered once per session, so a dropped link used to cost every timer rule
until the rules were reloaded: the disconnect cancelled the timers and nothing started them
again. Now a terminal disconnect pauses the timers that did not ask to keep going and resumes
them when the world comes back, and a timer that did ask carries on through the gap.

What such a timer can do while disconnected is the local half of a rule -- write a note, write
the log, store a value, run a script (which can speak or play a sound). A command has nowhere to
go, so it is dropped, which is why the flag belongs on an alert and not on a fight.
"""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine, EngineSink
from genericmud.packs import user_rules
from genericmud.packs.user_rules import UserRules, UserTimer
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from tests.helpers import RecordingSink


class _Handle:
    """A scheduled callback with a cancel(), as the event loop's handle has."""

    def __init__(self, delay: float, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback()


class _HandleSink(EngineSink):
    """Keeps every handle it hands out, so a test can see what was cancelled."""

    def __init__(self) -> None:
        self.handles: list[_Handle] = []

    def schedule(self, delay: float, callback):
        handle = _Handle(delay, callback)
        self.handles.append(handle)
        return handle


def _engine():
    sink = _HandleSink()
    engine = AutomationEngine(sink)
    return engine, sink


# --- the engine's two halves: pause, and start again ----------------------------------------


def test_a_disconnect_pauses_the_timers_that_did_not_ask_to_keep_going():
    engine, sink = _engine()
    api = ScriptApi(engine, source="user")
    api.add_named_timer("paused", 5, lambda: None, every=5)
    api.add_named_timer("kept", 5, lambda: None, every=5, while_disconnected=True)
    api.add_timer(5, lambda: None)  # anonymous work: nothing to resume into
    engine.suspend_timers()
    assert [(h.delay, h.cancelled) for h in sink.handles] == [
        (5, True), (5, False), (5, True),
    ]


def test_a_reconnect_starts_the_paused_timers_again():
    engine, sink = _engine()
    fired: list[str] = []
    api = ScriptApi(engine, source="user")
    api.add_named_timer("paused", 5, lambda: fired.append("paused"), every=5)
    engine.suspend_timers()
    assert fired == []
    engine.resume_timers()
    assert len(sink.handles) == 2  # armed again, from now
    assert sink.handles[1].delay == 5
    sink.handles[1].fire()
    assert fired == ["paused"]


def test_a_timer_asked_to_keep_going_is_left_alone_by_both():
    engine, sink = _engine()
    fired: list[str] = []
    api = ScriptApi(engine, source="user")
    api.add_named_timer(
        "kept", 5, lambda: fired.append("kept"), every=5, while_disconnected=True
    )
    engine.suspend_timers()
    engine.resume_timers()
    assert len(sink.handles) == 1  # not cancelled, not re-armed: it never stopped
    sink.handles[0].fire()
    assert fired == ["kept"]


def test_a_second_disconnect_does_not_lose_the_paused_timers():
    # A world can report the drop more than once; the first pause must survive the second.
    engine, sink = _engine()
    fired: list[str] = []
    api = ScriptApi(engine, source="user")
    api.add_named_timer("paused", 5, lambda: fired.append("paused"), every=5)
    engine.suspend_timers()
    engine.suspend_timers()
    engine.resume_timers()
    last = sink.handles[-1]
    assert last.delay == 5 and last.cancelled is False
    last.fire()
    assert fired == ["paused"]


def test_closing_the_session_still_cancels_everything():
    # The flag is about a world that dropped, not about a tab being closed.
    engine, sink = _engine()
    api = ScriptApi(engine, source="user")
    api.add_named_timer("kept", 5, lambda: None, every=5, while_disconnected=True)
    engine.cancel_timers()
    assert sink.handles[0].cancelled is True


# --- a world's own timer rule --------------------------------------------------------------


def _timer_rules(tmp_path, *timers: UserTimer):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.connected = False  # the world is down
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, UserRules(timers=list(timers)))
    return sink, engine


def test_a_keeping_timer_does_the_local_half_and_drops_the_commands(tmp_path):
    sink, _engine = _timer_rules(
        tmp_path,
        UserTimer(label="alert", every=5, send="look", run_disconnected=True),
        UserTimer(
            label="note", every=5, send="still down", send_to="output", run_disconnected=True
        ),
    )
    for _delay, callback in list(sink.scheduled):
        callback()
    assert sink.sent == []  # nowhere to send it
    assert sink.echoed == [("still down", "main")]  # but a note still lands


def test_a_keeping_timer_can_store_a_value(tmp_path):
    # The local half of a rule: a value is as useful while the world is down as a note.
    sink, engine = _timer_rules(
        tmp_path,
        UserTimer(
            label="stamp", every=5, send="down", send_to="variable",
            variable="state", run_disconnected=True,
        ),
    )
    sink.scheduled[0][1]()
    assert engine.get_var("state") == "down"


def test_a_timer_rule_reaches_the_engine_as_keeping_or_not():
    sink = _HandleSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE)
    user_rules.register_rules(
        api,
        UserRules(
            timers=[
                UserTimer(label="kept", every=5, send="look", run_disconnected=True),
                UserTimer(label="not", every=5, send="look"),
            ]
        ),
    )
    assert engine.named_timer("kept").while_disconnected is True
    assert engine.named_timer("not").while_disconnected is False


# --- and a soundpack's own timer, which has MUSHclient's name for the same flag --------------


def test_a_pack_timer_flagged_active_when_closed_keeps_running():
    # MUSHclient's flag by its MUSHclient name: a pack's heartbeat survives the drop.
    sink = _HandleSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="p"))
    pack.run_source(
        'AddTimer("heartbeat", 0, 0, 1, "look", '
        'timer_flag.Enabled + timer_flag.ActiveWhenClosed, "")'
    )
    pack.run_source('AddTimer("quiet", 0, 0, 1, "look", timer_flag.Enabled, "")')
    engine.suspend_timers()
    # The flagged one survives; the plain one is dropped like any other pending timer.
    assert [h.cancelled for h in sink.handles] == [False, True]


# --- the window: a dropped link pauses, the next connection starts them again -----------------


def test_a_dropped_link_pauses_the_timers_and_the_next_connection_starts_them(tmp_path):
    from genericmud.app import EngineApp
    from genericmud.packs import PackStore
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    handles: list[_Handle] = []

    def schedule(delay: float, callback):
        handle = _Handle(delay, callback)
        handles.append(handle)
        return handle

    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        schedule=schedule,
        keymap={},
        packs=PackStore(tmp_path / "soundpacks"),
        name="Test World",
    )
    timer = app.engine.add_named_timer(
        "tick", 30, lambda: None, every=30, source=user_rules.SOURCE
    )
    assert timer.armed is True

    app.on_connection_status("disconnected")
    assert timer.armed is False  # paused, not forgotten

    app.on_connection_status("reconnected")
    assert timer.armed is True  # and the schedule survives the drop, as it never used to


def test_lua_can_ask_for_it_by_name(tmp_path):
    pytest.importorskip("lupa")
    from genericmud.scripting.lua_runtime import LuaPackRuntime

    sink = _HandleSink()
    engine = AutomationEngine(sink)
    runtime = LuaPackRuntime(ScriptApi(engine, source="probe"))
    runtime.run_source(
        'mud.add_timer("kept", 5, function() end, {every=5, while_disconnected=true})'
    )
    assert engine.named_timer("kept").while_disconnected is True
    engine.suspend_timers()
    assert sink.handles[0].cancelled is False


def test_the_editor_keeps_the_flag(tmp_path):
    pytest.importorskip("wx")
    import wx

    from genericmud.ui.rule_editors import TimerEditorDialog

    app = wx.GetApp() or wx.App(False)
    assert app is not None
    frame = wx.Frame(None)
    timer = UserTimer(label="alert", every=60, send="still down", run_disconnected=True)
    dialog = TimerEditorDialog(frame, timer)
    try:
        assert dialog._run_disconnected.GetValue() is True
        assert dialog.result().run_disconnected is True
        dialog._run_disconnected.SetValue(False)
        assert dialog.result().run_disconnected is False
    finally:
        dialog.Destroy()
        frame.Destroy()

    from genericmud.ui.automation_manager import _rule_summary

    listed = _rule_summary("timer", timer)
    assert "runs while disconnected" in listed
