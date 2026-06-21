"""Cross-session communication: the SessionHub + send_to/broadcast/shared via the app."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.lua_runtime import LuaPackRuntime
from genericmud.session.hub import SessionHub
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def test_hub_register_send_unregister():
    hub = SessionHub()
    got: list[str] = []
    hub.register("a", got.append)
    assert hub.send_to("a", "hi") is True
    assert got == ["hi"]
    assert hub.send_to("ghost", "x") is False
    hub.unregister("a")
    assert hub.send_to("a", "y") is False
    assert hub.sessions() == []


def test_hub_broadcast_excludes_sender():
    hub = SessionHub()
    a: list[str] = []
    b: list[str] = []
    hub.register("a", a.append)
    hub.register("b", b.append)
    assert hub.broadcast("ping", exclude="a") == 1
    assert b == ["ping"] and a == []


def test_hub_shared_vars_stringify():
    hub = SessionHub()
    assert hub.shared_get("x") == ""
    hub.shared_set("x", 5)
    assert hub.shared_get("x") == "5"


def _app(hub, name):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(voice, send=sent.append, post=[].append, name=name, hub=hub, keymap={})
    app.on_connect(name)  # registers the session under its name
    return app, sent


def test_send_to_runs_command_in_the_target_session():
    hub = SessionHub()
    mage, mage_sent = _app(hub, "Mage")
    _healer, healer_sent = _app(hub, "Healer")
    api = ScriptApi(mage.engine, source="t")  # as the Mage's pack would
    assert api.send_to("Healer", "cast heal on Mage") is True
    assert healer_sent == ["cast heal on Mage"]
    assert mage_sent == []  # not echoed back to the sender


def test_broadcast_reaches_others_only():
    hub = SessionHub()
    mage, mage_sent = _app(hub, "Mage")
    _healer, healer_sent = _app(hub, "Healer")
    assert ScriptApi(mage.engine, source="t").broadcast("group up") == 1  # excludes Mage
    assert healer_sent == ["group up"]
    assert mage_sent == []


def test_shared_vars_visible_across_sessions():
    hub = SessionHub()
    mage, _ = _app(hub, "Mage")
    healer, _ = _app(hub, "Healer")
    ScriptApi(mage.engine, source="t").shared_set("target", "goblin")
    assert ScriptApi(healer.engine, source="t").shared_get("target") == "goblin"


def test_lua_cross_session_bindings():
    hub = SessionHub()
    mage, _ = _app(hub, "Mage")
    healer, healer_sent = _app(hub, "Healer")
    runtime = LuaPackRuntime(ScriptApi(mage.engine, source="lua"))
    runtime.run_source('mud.send_to("Healer", "follow Mage"); mud.shared_set("plan", "raid")')
    assert healer_sent == ["follow Mage"]
    assert ScriptApi(healer.engine, source="t").shared_get("plan") == "raid"


def test_shutdown_unregisters_from_hub():
    hub = SessionHub()
    mage, _ = _app(hub, "Mage")
    assert "Mage" in hub.sessions()
    mage.shutdown()
    assert "Mage" not in hub.sessions()
