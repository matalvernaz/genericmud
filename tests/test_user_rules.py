"""Field-based user automation plus server-text decoding regressions."""

from __future__ import annotations

import pytest

from genericmud.app import EngineApp
from genericmud.automation.channels import ChannelPolicy
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
    # One field-made trigger drives sound + speech + send + gag-from-speech +
    # channel routing, with %1 captures in the texts -- parity with a scripted rule.
    (tmp_path / "growl.ogg").write_bytes(b"OggS")
    rules = UserRules(
        channels=[UserChannel(name="combat", speak=False, display=True)],
        triggers=[UserTrigger(
            pattern="* growls at you", sound="growl.ogg", volume=60, pan=-50,
            loop=False, speak="%1 attacks", send="consider %1\nflee", gag="speech",
            channel="combat",
        )],
    )
    sink, engine = _register(rules, tmp_path)
    line = engine.process_line(Line("a goblin growls at you"))
    cue = sink.played[-1]
    assert cue["file"].endswith("growl.ogg") and abs(cue["gain"] - 0.6) < 1e-9
    assert abs(cue["pan"] + 0.5) < 1e-9
    assert ("a goblin attacks", "combat", False) in sink.spoken
    assert sink.sent == ["consider a goblin", "flee"]
    assert line.gagged and line.display_when_gagged  # silent but still shown
    assert line.channel == "combat"
    assert engine.channels.policy("combat").speak is False  # user channel policy


def test_alias_with_wildcard_args(tmp_path):
    # The MUDBall-thread question: "sh goblin" -> "shoot goblin".
    rules = UserRules(aliases=[UserAlias(pattern="sh *", send="shoot %1")])
    sink, engine = _register(rules, tmp_path)
    assert engine.process_input("sh goblin") == []  # consumed
    assert sink.sent == ["shoot goblin"]


def test_alias_command_stack_uses_captures_script_vars_and_mud_vars(tmp_path):
    rules = UserRules(aliases=[UserAlias(
        pattern="combo *",
        send=(
            "stand\n"
            "kill ${1}\n"
            "consider %1\n"
            "report ${script:stance} at ${mud:Char.Vitals.hp} health"
        ),
    )])
    sink, engine = _register(rules, tmp_path)
    engine.set_var("stance", "aggressive")
    engine.set_mud_var("Char.Vitals", {"hp": 73})

    assert engine.process_input("combo goblin") == []
    assert sink.sent == [
        "stand", "kill goblin", "consider goblin", "report aggressive at 73 health",
    ]


def test_field_alias_command_uses_named_regex_capture(tmp_path):
    rules = UserRules(aliases=[UserAlias(
        pattern=r"hit (?P<target>\w+)", regex=True, send="kill ${target}",
    )])
    sink, engine = _register(rules, tmp_path)

    assert engine.process_input("hit troll") == []
    assert sink.sent == ["kill troll"]


def test_rule_command_stack_expands_before_sending_any_command(tmp_path):
    rules = UserRules(aliases=[UserAlias(
        pattern="unsafe", send="stand\nkill ${script:missing}", speak="commands sent",
    )])
    sink, engine = _register(rules, tmp_path)

    assert engine.process_input("unsafe") == []
    assert engine.process_input("unsafe") == []
    assert sink.sent == []
    assert sink.spoken == [
        (
            "Automation command not sent: unknown command variable: script:missing",
            "system", False,
        )
    ]


def test_field_rule_rejects_more_than_one_hundred_commands(tmp_path):
    rules = UserRules(aliases=[UserAlias(
        pattern="flood", send="\n".join(f"look {index}" for index in range(101)),
    )])
    with pytest.raises(ValueError, match="too many commands.*100"):
        _register(rules, tmp_path)


def test_legacy_capture_text_cannot_inject_a_variable_template(tmp_path):
    rules = UserRules(aliases=[UserAlias(pattern="say *", send="tell %1")])
    sink, engine = _register(rules, tmp_path)
    engine.set_var("secret", "should-not-expand")

    engine.process_input("say ${script:secret}")
    assert sink.sent == ["tell ${script:secret}"]


def test_key_macro(tmp_path):
    rules = UserRules(keys=[UserKey(key="ctrl+h", send="hp\nscore", speak="health check")])
    sink, engine = _register(rules, tmp_path)
    assert engine.press_key("ctrl+h")
    assert sink.sent == ["hp", "score"]
    assert sink.spoken[-1][0] == "health check"


def test_disabled_automation_is_saved_but_not_registered(tmp_path):
    rules = UserRules(
        triggers=[UserTrigger(pattern="danger", speak="warning", enabled=False)],
        aliases=[UserAlias(pattern="x", send="examine", enabled=False)],
        keys=[UserKey(key="f2", send="score", enabled=False)],
        channels=[UserChannel(name="quiet", speak=False, enabled=False)],
    )
    save_rules(tmp_path, rules)
    loaded = load_rules(tmp_path)
    sink, engine = _register(loaded, tmp_path)

    engine.process_line(Line("danger"))
    assert engine.process_input("x") == ["x"]
    assert not engine.press_key("f2")
    assert "quiet" not in engine.channels.names()
    assert sink.spoken == [] and sink.sent == []


def test_older_rules_default_to_enabled():
    loaded = UserRules.from_json(
        '{"version": 1, "aliases": [{"pattern": "x", "send": "examine"}]}'
    )
    assert loaded.aliases[0].enabled is True


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


def test_remove_source_restores_shadowed_channel_policy(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.channels.set_policy(
        "combat", ChannelPolicy(speak=True, display=False), source="pack"
    )
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    register_rules(
        api,
        UserRules(channels=[UserChannel(name="combat", speak=False, display=True)]),
    )
    assert engine.channels.policy("combat") == ChannelPolicy(speak=False, display=True)

    engine.remove_source(user_rules.SOURCE)
    assert engine.channels.policy("combat") == ChannelPolicy(speak=True, display=False)


def test_remove_source_removes_unshadowed_channel_policy(tmp_path):
    sink, engine = _register(
        UserRules(channels=[UserChannel(name="temporary", speak=False)]), tmp_path
    )
    assert "temporary" in engine.channels.names()

    engine.remove_source(user_rules.SOURCE)
    assert "temporary" not in engine.channels.names()


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


def test_contains_match_is_literal_text_anywhere_in_the_line(tmp_path):
    rules = UserRules(triggers=[UserTrigger(
        pattern="tells you", match="contains", speak="a tell",
    )])
    sink, engine = _register(rules, tmp_path)
    engine.process_line(Line("Bob tells you hi"))
    assert ("a tell", "main", False) in sink.spoken
    # Regex metacharacters in the text stay literal under "contains".
    rules = UserRules(triggers=[UserTrigger(pattern="[HP]", match="contains", speak="hp")])
    sink, engine = _register(rules, tmp_path)
    engine.process_line(Line("[HP] 42/42"))
    assert ("hp", "main", False) in sink.spoken


def test_exact_match_needs_the_whole_line(tmp_path):
    rules = UserRules(triggers=[UserTrigger(
        pattern="You are hungry.", match="exact", speak="eat",
    )])
    sink, engine = _register(rules, tmp_path)
    engine.process_line(Line("You are hungry. You are thirsty."))
    assert sink.spoken == []
    engine.process_line(Line("You are hungry."))
    assert ("eat", "main", False) in sink.spoken


def test_legacy_rules_without_match_field_keep_their_regex_flag_meaning(tmp_path):
    loaded = UserRules.from_json(
        '{"version": 1, "triggers": ['
        '{"pattern": "* growls", "regex": false},'
        '{"pattern": "^\\\\d+ gold$", "regex": true}]}'
    )
    assert loaded.triggers[0].match_kind() == "wildcard"
    assert loaded.triggers[1].match_kind() == "regex"


def test_interrupting_trigger_stops_speech_and_barges_its_own_line_in(tmp_path):
    rules = UserRules(triggers=[UserTrigger(
        pattern="says your name", match="contains", speak="listen!", interrupt=True,
    )])
    sink, engine = _register(rules, tmp_path)
    engine.process_line(Line("Bob says your name"))
    assert sink.speech_stops == 1
    assert ("listen!", "main", True) in sink.spoken  # its own speech barges in too


def test_interrupt_alone_is_a_real_action(tmp_path):
    # MUDBall's "interrupt whatever's speaking" checkbox works with no other action.
    rules = UserRules(triggers=[UserTrigger(pattern="DING", match="contains", interrupt=True)])
    sink, engine = _register(rules, tmp_path)
    engine.process_line(Line("DING you level"))
    assert sink.speech_stops == 1


def test_decode_reads_cp1252_punctuation_not_c1_controls():
    app = _app()
    # Windows MUDs that "send Latin-1" nearly always send CP1252: 0x92 is a curly
    # apostrophe there, but an invisible C1 control in Latin-1 -- which a screen
    # reader garbles and no trigger written with the real character can match.
    app.on_telnet_event(DataReceived(b"It\x92s a caf\xe9\r\n"))
    assert app.buffer.lines()[-1].plain_text == "It’s a café"
    app.on_telnet_event(DataReceived(b"\x93quoted\x94\r\n"))  # stays latched, still CP1252
    assert app.buffer.lines()[-1].plain_text == "“quoted”"


def test_decode_state_resets_on_reconnect():
    # A drop mid-multibyte-char latches the legacy fallback; a fresh socket is a fresh
    # byte stream, so reconnect must clear it or the whole reconnected session mis-decodes.
    app = _app()
    app.on_telnet_event(DataReceived(b"caf\xe9\r\n"))  # invalid UTF-8 -> latch
    assert app.codec.latched is True
    app.on_connection_status("reconnected")
    assert app.codec.latched is False
    app.on_telnet_event(DataReceived(b"caf\xc3\xa9\r\n"))  # clean UTF-8 read again
    assert app.buffer.lines()[-1].plain_text == "café"


def test_malformed_keymap_recall_count_is_ignored_not_fatal():
    app = _app()
    # A user-edited keymap can bind a non-numeric recall; it must no-op, not raise out
    # of key dispatch and kill the key.
    app._handle_key("recall:oops")
    app._handle_key("chan:recall:")
    app._handle_key("chan:recall:notnum")  # no exception = pass
