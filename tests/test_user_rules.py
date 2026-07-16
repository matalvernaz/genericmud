"""User rules (the no-code soundpack builder's engine layer) + decode fallback."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.automation.engine import AutomationEngine
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import (
    UserAlias,
    UserChannel,
    UserKey,
    UserRules,
    UserTrigger,
    load_rules,
    register_rules,
    save_rules,
)
from genericmud.protocol.telnet import DataReceived
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend, RecordingSink


def _register(rules: UserRules, tmp_path) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    register_rules(api, rules)
    return sink, engine


def test_trigger_full_power(tmp_path):
    # One dialog-built trigger drives sound + speech + send + gag-from-speech +
    # channel routing, with %1 captures in the texts -- parity with a scripted rule.
    (tmp_path / "growl.ogg").write_bytes(b"OggS")
    rules = UserRules(
        channels=[UserChannel(name="combat", speak=False, display=True)],
        triggers=[UserTrigger(
            pattern="* growls at you", sound="growl.ogg", volume=60, pan=-50,
            loop=False, speak="%1 attacks", send="consider %1", gag="speech",
            channel="combat",
        )],
    )
    sink, engine = _register(rules, tmp_path)
    line = engine.process_line(Line("a goblin growls at you"))
    cue = sink.played[-1]
    assert cue["file"].endswith("growl.ogg") and abs(cue["gain"] - 0.6) < 1e-9
    assert abs(cue["pan"] + 0.5) < 1e-9
    assert ("a goblin attacks", "combat", False) in sink.spoken
    assert sink.sent == ["consider a goblin"]
    assert line.gagged and line.display_when_gagged  # silent but still shown
    assert line.channel == "combat"
    assert engine.channels.policy("combat").speak is False  # user channel policy


def test_alias_with_wildcard_args(tmp_path):
    # The MUDBall-thread question: "sh goblin" -> "shoot goblin".
    rules = UserRules(aliases=[UserAlias(pattern="sh *", send="shoot %1")])
    sink, engine = _register(rules, tmp_path)
    assert engine.process_input("sh goblin") == []  # consumed
    assert sink.sent == ["shoot goblin"]


def test_key_macro(tmp_path):
    rules = UserRules(keys=[UserKey(key="ctrl+h", send="hp", speak="health check")])
    sink, engine = _register(rules, tmp_path)
    assert engine.press_key("ctrl+h")
    assert sink.sent == ["hp"]
    assert sink.spoken[-1][0] == "health check"


def test_roundtrip_and_reload_via_remove_source(tmp_path):
    # Save -> load -> register -> edit -> reload: the old rules must be gone and
    # a pack's own registrations untouched (remove_source is source-scoped).
    save_rules(tmp_path, UserRules(aliases=[UserAlias(pattern="k", send="kill rat")]))
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.add_key("ctrl+h", lambda ctx: sink.send("pack-key"), source="pack")
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    register_rules(api, load_rules(tmp_path))
    engine.process_input("k")
    assert sink.sent == ["kill rat"]

    save_rules(tmp_path, UserRules(aliases=[UserAlias(pattern="k", send="kick rat")]))
    engine.remove_source(user_rules.SOURCE)
    register_rules(api, load_rules(tmp_path))
    engine.process_input("k")
    assert sink.sent == ["kill rat", "kick rat"]  # old rule gone, ONE new fire
    assert engine.press_key("ctrl+h")  # the pack's key survived the reload
    assert sink.sent[-1] == "pack-key"


def test_remove_source_restores_shadowed_key():
    # A user key that shadowed a pack key must fall back to the pack's on removal.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.add_key("f5", lambda ctx: sink.send("pack"), source="pack")
    engine.add_key("f5", lambda ctx: sink.send("user"), source="user")
    engine.press_key("f5")
    engine.remove_source("user")
    engine.press_key("f5")
    assert sink.sent == ["user", "pack"]


def test_corrupt_rules_file_loads_empty(tmp_path):
    (tmp_path / "rules.json").write_text("{not json", encoding="utf-8")
    rules = load_rules(tmp_path)
    assert rules.triggers == [] and rules.aliases == []


def _app():
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    return EngineApp(voice, keymap=load_keymap("vipmud"))


def test_decode_utf8_split_across_chunks():
    app = _app()
    # "café\r\n" as UTF-8 with the é split across two telnet chunks: no mangling.
    app.on_telnet_event(DataReceived(b"caf\xc3"))
    app.on_telnet_event(DataReceived(b"\xa9\r\n"))
    assert app.buffer.lines()[-1].plain_text == "café"


def test_decode_latches_latin1_for_legacy_muds():
    app = _app()
    # A Spanish Latin-1 MUD: 0xE9 is invalid UTF-8 -> latch Latin-1, read "café".
    app.on_telnet_event(DataReceived(b"caf\xe9\r\n"))
    assert app.buffer.lines()[-1].plain_text == "café"
    app.on_telnet_event(DataReceived(b"ma\xf1ana\r\n"))  # stays latched
    assert app.buffer.lines()[-1].plain_text == "mañana"
