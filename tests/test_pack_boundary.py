"""Pack capability boundary: cross-session isolation, #file confinement, trust gating."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.automation.engine import AutomationEngine
from genericmud.packs.setup import setup_pack
from genericmud.packs.store import PackStore
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.vipmud_dialect import VipMudPack
from genericmud.session.hub import SessionHub
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app(hub: SessionHub, name: str, sent: list[str]) -> EngineApp:
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    return EngineApp(voice, send=sent.append, post=[].append, hub=hub, name=name, keymap={})


# --- C: cross-session command injection ---


def test_cross_session_cannot_run_client_commands():
    hub = SessionHub()
    sent: list[str] = []
    app = _app(hub, "B", sent)
    app.on_connect("B")  # registers the hub deliver callback (packs=None)

    hub.send_to("B", "/alias hi = wave")  # another session tries to reprogram B

    assert app._user_aliases == {}  # no alias was installed in B
    assert "/alias hi = wave" in sent  # the text just went to B's MUD literally


def test_local_client_commands_still_work():
    hub = SessionHub()
    sent: list[str] = []
    app = _app(hub, "B", sent)
    app.on_connect("B")

    app.on_ws_message({"type": "input", "text": "/alias hi = wave"})  # locally typed

    assert app._user_aliases.get("hi") == "wave"  # local /alias still installs


# --- #file confinement ---


def test_vipmud_file_create_is_confined_to_pack(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    vp = VipMudPack(ScriptApi(AutomationEngine(), base_dir=str(pack)))
    assert vp._resolve_in_pack("../../evil.dat", must_exist=False) is None
    assert vp._resolve_in_pack("/etc/evil.dat", must_exist=False) is None
    got = vp._resolve_in_pack("settings.set", must_exist=False)
    assert got is not None
    assert str(got).startswith(str(pack.resolve()))


# --- B: trust gating ---


def test_setup_does_not_autotrust_mushclient(tmp_path):
    store = PackStore(tmp_path / "store")
    pack = tmp_path / "mush"
    pack.mkdir()
    (pack / "main.xml").write_text("<muclient></muclient>", encoding="utf-8")
    result = setup_pack(store, pack, entry="main.xml")
    assert not store.is_trusted(result.manifest.id)


def test_setup_autotrusts_sandboxed_vipmud(tmp_path):
    store = PackStore(tmp_path / "store")
    pack = tmp_path / "vip"
    pack.mkdir()
    (pack / "main.set").write_text("#TRIGGER hi {#say hello}", encoding="utf-8")
    result = setup_pack(store, pack, entry="main.set")
    assert store.is_trusted(result.manifest.id)
