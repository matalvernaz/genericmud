"""Reacting when the MUD sends data: Mudlet's GMCP events, done genericMud's way.

The engine's watches, the app calling them once per GMCP/MSDP/MSSP message, Lua's
mud.on_data, and the Automation Manager's no-code MUD data rules.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.automation.engine import AutomationEngine
from genericmud.config.keymap import load_keymap
from genericmud.packs import user_rules
from genericmud.packs.user_rules import UserRules, UserWatch, register_rules
from genericmud.protocol.telnet import OPT_GMCP, OPT_MSDP, OPT_MSSP, Subnegotiation
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.lua_runtime import LuaPackRuntime
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend, RecordingSink


def _engine_hearing(path: str, **opts) -> tuple[AutomationEngine, list[tuple[str, object]]]:
    engine = AutomationEngine(RecordingSink())
    heard: list[tuple[str, object]] = []
    engine.add_data_watch(path, lambda name, value: heard.append((name, value)), **opts)
    return engine, heard


# --- what a watch hears ------------------------------------------------------------


def test_a_watch_hears_its_own_package():
    engine, heard = _engine_hearing("Char.Vitals")
    engine.data_arrived("gmcp", "Char.Vitals", {"hp": 90})
    engine.data_arrived("gmcp", "Room.Info", {"num": 1})
    assert heard == [("Char.Vitals", {"hp": 90})]


def test_a_parent_package_hears_every_package_below_it():
    # As Mudlet raises gmcp.Char for Char.Vitals.
    engine, heard = _engine_hearing("Char")
    engine.data_arrived("gmcp", "Char.Vitals", {"hp": 90})
    engine.data_arrived("gmcp", "Char.Status", {"level": 5})
    engine.data_arrived("gmcp", "Charm.Spell", {})  # a different word, not a package below
    assert [name for name, _value in heard] == ["Char.Vitals", "Char.Status"]


def test_a_path_inside_a_message_hears_the_value_there_and_only_when_it_is_sent():
    engine, heard = _engine_hearing("Char.Vitals.hp")
    engine.data_arrived("gmcp", "Char.Vitals", {"hp": 90, "mp": 40})
    engine.data_arrived("gmcp", "Char.Vitals", {"mp": 35})  # no hp this time
    assert heard == [("Char.Vitals.hp", 90)]


def test_names_match_in_any_case():
    engine, heard = _engine_hearing("char.vitals.HP")
    engine.data_arrived("gmcp", "Char.Vitals", {"hp": 90})
    assert heard == [("char.vitals.HP", 90)]


def test_a_protocol_prefix_hears_only_that_protocol():
    engine, heard = _engine_hearing("msdp.HEALTH")
    engine.data_arrived("gmcp", "HEALTH", "1")
    engine.data_arrived("msdp", "HEALTH", "73")
    assert heard == [("HEALTH", "73")]


def test_only_changes_skips_a_value_that_has_not_moved_but_hears_the_first():
    engine, heard = _engine_hearing("Char.Vitals.hp", only_changes=True)
    for hp in (90, 90, 85, 85, 90):
        engine.data_arrived("gmcp", "Char.Vitals", {"hp": hp, "mp": 40})
    assert [value for _name, value in heard] == [90, 85, 90]


def test_one_watch_failing_does_not_stop_the_others():
    engine, heard = _engine_hearing("Char.Vitals")

    def broken(_name, _value):
        raise RuntimeError("a pack's mistake")

    engine.add_data_watch("Char.Vitals", broken)
    engine.add_data_watch("Char.Vitals", lambda name, value: heard.append(("second", value)))
    engine.data_arrived("gmcp", "Char.Vitals", {"hp": 1})
    assert [name for name, _value in heard] == ["Char.Vitals", "second"]


def test_reloading_a_source_drops_its_watches():
    engine = AutomationEngine(RecordingSink())
    heard: list[str] = []
    engine.add_data_watch("Char", lambda name, _v: heard.append(name), source="pack")
    engine.remove_source("pack")
    engine.data_arrived("gmcp", "Char.Vitals", {})
    assert heard == []


# --- the app calls them once per message ---------------------------------------------


def _app() -> tuple[EngineApp, RecordingBackend, list[str]]:
    backend = RecordingBackend()
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0), send=sent.append, post=lambda _m: None,
        keymap=load_keymap("vipmud"),
    )
    return app, backend, sent


def test_a_gmcp_message_is_heard_once_with_its_value_already_stored():
    app, _backend, _sent = _app()
    seen: list[tuple[str, object, object]] = []
    app.engine.add_data_watch(
        "Char.Vitals",
        lambda name, value: seen.append((name, value, app.engine.get_mud_var("Char.Vitals.hp"))),
    )
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'Char.Vitals {"hp":42}'))
    assert seen == [("Char.Vitals", {"hp": 42}, 42)]  # once, though it's stored twice


def test_msdp_and_mssp_values_are_heard_too():
    app, _backend, _sent = _app()
    seen: list[str] = []
    app.engine.add_data_watch("msdp.HEALTH", lambda name, value: seen.append(f"{name}={value}"))
    app.engine.add_data_watch("mssp.PLAYERS", lambda name, value: seen.append(f"{name}={value}"))
    app.on_telnet_event(Subnegotiation(OPT_MSDP, b"\x01HEALTH\x0273"))
    app.on_telnet_event(Subnegotiation(OPT_MSSP, b"\x01PLAYERS\x0212"))
    assert seen == ["HEALTH=73", "PLAYERS=12"]


# --- Lua -----------------------------------------------------------------------------


def test_lua_on_data_runs_with_the_name_and_a_lua_value():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    runtime = LuaPackRuntime(ScriptApi(engine, source="lua"))
    runtime.run_source(
        'mud.on_data("Char.Vitals", function(name, value) mud.send(name .. " " .. value.hp) end)\n'
        'mud.on_data("Char.Vitals.hp", function(name, value) mud.send("hp " .. value) end,'
        " {changes = true})"
    )
    engine.data_arrived("gmcp", "Char.Vitals", {"hp": 90})
    engine.data_arrived("gmcp", "Char.Vitals", {"hp": 90})
    assert sink.sent == ["Char.Vitals 90", "hp 90", "Char.Vitals 90"]


# --- the no-code MUD data rule -------------------------------------------------------


def _rules_engine(rules: UserRules, tmp_path) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    register_rules(ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path)), rules)
    return sink, engine


def test_a_data_rule_speaks_the_value_when_it_changes(tmp_path):
    rules = UserRules(watches=[UserWatch(name="Char.Vitals.hp", speak="${value} health")])
    sink, engine = _rules_engine(rules, tmp_path)
    for hp in (90, 90, 70):
        engine.data_arrived("gmcp", "Char.Vitals", {"hp": hp})
    assert [text for text, *_rest in sink.spoken] == ["90 health", "70 health"]


def test_a_data_rule_sends_commands_with_the_value_and_name(tmp_path):
    rules = UserRules(watches=[UserWatch(
        name="Comm.Channel.Text", only_changes=False, send="say heard ${name}: ${value}",
    )])
    sink, engine = _rules_engine(rules, tmp_path)
    # Colour codes and line breaks stay out of what's sent and said.
    engine.data_arrived("gmcp", "Comm.Channel.Text", "\x1b[36mBo says\nhi\x1b[0m")
    assert sink.sent == ["say heard Comm.Channel.Text: Bo says hi"]


def test_data_rules_are_saved_with_the_others_and_old_files_still_load():
    rules = UserRules(watches=[UserWatch(name="Char.Vitals.hp", speak="${value}")])
    again = UserRules.from_json(rules.to_json())
    assert again.watches == rules.watches
    assert UserRules.from_json('{"version": 1, "triggers": []}').watches == []


# --- the MUD variables list: how long ago, most recent first --------------------------


def test_each_value_takes_the_age_of_the_message_it_came_in():
    from genericmud.automation.variables import list_variables, recent_first, spoken_age

    entries, _cut = list_variables(
        {"Char.Vitals": {"hp": 90}, "gmcp.Char.Vitals": {"hp": 90}, "HEALTH": "5",
         "msdp.HEALTH": "5"},
        {"target": "orc"},
        arrivals={"char.vitals": 99.0, "health": 30.0},
        now=100.0,
    )
    ages = {entry.name: entry.age for entry in entries}
    assert ages == {"Char.Vitals.hp": 1.0, "HEALTH": 70.0, "target": None}
    assert [entry.name for entry in recent_first(entries)] == ["Char.Vitals.hp", "HEALTH", "target"]
    assert [spoken_age(s) for s in (0.4, 5, 61, 7300)] == [
        "just now", "5 seconds ago", "1 minute ago", "2 hours ago",
    ]


def test_the_app_lists_arrival_ages(tmp_path):
    app, _backend, _sent = _app()
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'Char.Vitals {"hp":42}'))
    entries, _cut = app.variable_listing()
    hp = next(entry for entry in entries if entry.name == "Char.Vitals.hp")
    assert hp.age is not None and hp.age < 5
