"""Groups: switching a set of rules together.

"Turn all the combat rules off" is one call in MUSHclient -- EnableGroup -- and it is the reason
to put a name on a set of rules at all. A group is a name the rules share; membership is read
from the rules when it is asked for, so a reload cannot leave a group pointing at rules that no
longer exist.
"""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import (
    UserAlias,
    UserKey,
    UserRules,
    UserTimer,
    UserTrigger,
    UserWatch,
    load_rules,
    save_rules,
)
from genericmud.scripting.api import ScriptApi
from tests.helpers import RecordingSink


def _world(tmp_path, *, triggers=(), aliases=()) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(
        api, UserRules(triggers=list(triggers), aliases=list(aliases))
    )
    return sink, engine


def test_a_group_switches_every_rule_in_it(tmp_path):
    sink, engine = _world(
        tmp_path,
        triggers=[
            UserTrigger(pattern="backup one", send="say one", group="combat"),
            UserTrigger(pattern="backup two", send="say two", group="combat"),
            UserTrigger(pattern="backup three", send="say three", group="other"),
        ],
    )
    assert engine.set_group_enabled("combat", False) == 2
    for line in ("Starting backup one", "Starting backup two", "Starting backup three"):
        engine.process_line(Line(line))
    assert sink.sent == ["say three"]  # only the rule outside the group


def test_a_group_can_be_switched_back_on(tmp_path):
    sink, engine = _world(
        tmp_path,
        triggers=[UserTrigger(pattern="backup", send="look", group="combat")],
    )
    engine.set_group_enabled("combat", False)
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == []
    assert engine.set_group_enabled("combat", True) == 1
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]


def test_a_group_covers_aliases_too(tmp_path):
    sink, engine = _world(
        tmp_path,
        aliases=[
            UserAlias(pattern="sh *", send="shoot ${1}", group="combat"),
            UserAlias(pattern="heal", send="cast heal", group="survival"),
        ],
    )
    assert engine.set_group_enabled("combat", False) == 1
    assert engine.process_input("sh goblin") == ["sh goblin"]  # falls through, untouched
    engine.process_input("heal")
    assert sink.sent == ["cast heal"]


def test_a_group_nothing_belongs_to_says_zero_and_is_not_an_error(tmp_path):
    _sink, engine = _world(tmp_path)
    assert engine.set_group_enabled("nothing-here", False) == 0
    assert engine.set_group_enabled("", False) == 0  # a blank group switches nothing


def test_a_rule_without_a_group_is_never_switched_by_one(tmp_path):
    sink, engine = _world(
        tmp_path,
        triggers=[UserTrigger(pattern="backup", send="look")],
    )
    engine.set_group_enabled("combat", False)
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]


def test_lua_can_switch_a_group(tmp_path):
    pytest.importorskip("lupa")
    from genericmud.scripting.lua_runtime import LuaPackRuntime

    _sink, engine = _world(
        tmp_path, triggers=[UserTrigger(pattern="backup", send="look", group="combat")]
    )
    runtime = LuaPackRuntime(ScriptApi(engine, source="probe"))
    runtime.run_source('mud.set_group_enabled("combat", false)')
    assert engine.set_group_enabled("combat", False) == 1  # still one rule in the group


def test_the_group_survives_a_saved_rules_file(tmp_path):
    save_rules(
        tmp_path,
        UserRules(
            triggers=[UserTrigger(pattern="a", group="combat")],
            aliases=[UserAlias(pattern="b", group="movement")],
        ),
    )
    loaded = load_rules(tmp_path)
    assert loaded.triggers[0].group == "combat"
    assert loaded.aliases[0].group == "movement"


def test_the_list_says_which_group_a_rule_is_in():
    from genericmud.ui.automation_manager import _rule_summary

    summary = _rule_summary("trigger", UserTrigger(pattern="backup", group="combat", send="x"))
    assert "(group combat)" in summary


def test_a_rule_disabled_in_the_file_can_be_switched_on_by_its_group(tmp_path):
    # A rule the file says is off used to be left out of the engine entirely, so nothing could
    # ever switch it back on: no group, no label, no script. It is registered off instead.
    sink, engine = _world(
        tmp_path,
        triggers=[
            UserTrigger(pattern="potion", send="drink potion", enabled=False, group="combat")
        ],
    )
    engine.process_line(Line("potion"))
    assert sink.sent == []  # off is off

    assert engine.set_group_enabled("combat", True) == 1
    engine.process_line(Line("potion"))
    assert sink.sent == ["drink potion"]


def test_a_disabled_rule_with_a_label_can_be_switched_on_by_name(tmp_path):
    sink, engine = _world(
        tmp_path,
        triggers=[UserTrigger(pattern="potion", send="drink potion", enabled=False, label="drink")],
    )
    engine.process_line(Line("potion"))
    assert sink.sent == []
    assert engine.set_enabled("drink", True) is True
    engine.process_line(Line("potion"))
    assert sink.sent == ["drink potion"]


def test_a_disabled_alias_is_registered_off_and_asks_for_nothing(tmp_path):
    sink, engine = _world(
        tmp_path,
        aliases=[UserAlias(pattern="kk", send="kill orc", enabled=False, group="combat")],
    )
    assert engine.process_input("kk") == ["kk"]  # off: it falls through to the MUD untouched
    assert engine.set_group_enabled("combat", True) == 1
    assert engine.process_input("kk") == []  # the alias takes it now
    assert sink.sent == ["kill orc"]
# --- hotkeys and MUD data rules are rules too, so they join a group as well ------------------


def test_a_group_switches_a_hotkey_off_and_on():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    fired: list[str] = []
    api = ScriptApi(engine, source=user_rules.SOURCE)
    api.add_key("f2", lambda _ctx: fired.append("f2"), group="combat")
    assert engine.press_key("f2") is True
    assert engine.set_group_enabled("combat", False) == 1
    assert engine.has_key("f2") is False  # the key belongs to the platform again
    assert engine.press_key("f2") is False
    assert engine.set_group_enabled("combat", True) == 1
    assert engine.press_key("f2") is True
    assert fired == ["f2", "f2"]


def test_a_switched_off_hotkey_falls_back_to_an_earlier_binding():
    # Two sources on one combo: switching the later one's group off must leave the combo to the
    # earlier owner, exactly as if the second had never bound it.
    engine = AutomationEngine(RecordingSink())
    fired: list[str] = []
    engine.add_key("f2", lambda _ctx: fired.append("first"), source="pack")
    engine.add_key("f2", lambda _ctx: fired.append("second"), source="user", group="combat")
    engine.press_key("f2")
    assert fired == ["second"]
    engine.set_group_enabled("combat", False)
    engine.press_key("f2")
    assert fired == ["second", "first"]
    engine.set_group_enabled("combat", True)
    engine.press_key("f2")
    assert fired == ["second", "first", "second"]


def test_a_group_switches_a_mud_data_rule_off_and_on():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    heard: list[tuple[str, object]] = []
    api = ScriptApi(engine, source=user_rules.SOURCE)
    api.add_data_watch(
        "Char.Vitals.hp", lambda name, value: heard.append((name, value)), group="combat"
    )
    engine.data_arrived("gmcp", "Char.Vitals.hp", 90)
    assert heard == [("Char.Vitals.hp", 90)]
    assert engine.set_group_enabled("combat", False) == 1
    engine.data_arrived("gmcp", "Char.Vitals.hp", 80)
    assert heard == [("Char.Vitals.hp", 90)]  # switched off for now
    assert engine.set_group_enabled("combat", True) == 1
    engine.data_arrived("gmcp", "Char.Vitals.hp", 70)
    assert heard == [("Char.Vitals.hp", 90), ("Char.Vitals.hp", 70)]


def test_switching_a_name_does_not_fall_over_a_bound_hotkey():
    # Hotkeys live in their own map and have no name to switch by; the sweep used to iterate
    # that map's keys and ask a plain string for its name.
    engine = AutomationEngine(RecordingSink())
    engine.add_key("f2", lambda _ctx: None, source="pack")
    assert engine.set_enabled("nothing-by-that-name", False) is False


def test_a_hotkey_and_a_data_rule_carry_their_group_from_the_rules_file(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(
        api,
        UserRules(
            keys=[UserKey(key="f2", speak="health", group="combat")],
            watches=[UserWatch(name="Char.Vitals.hp", speak="${value}", group="combat")],
        ),
    )
    assert engine.set_group_enabled("combat", False) == 2
    assert engine.has_key("f2") is False


def test_the_group_on_a_hotkey_and_a_data_rule_survives_a_saved_file(tmp_path):
    save_rules(
        tmp_path,
        UserRules(
            keys=[UserKey(key="f2", group="combat")],
            watches=[UserWatch(name="Char.Vitals.hp", group="combat")],
        ),
    )
    loaded = load_rules(tmp_path)
    assert loaded.keys[0].group == "combat"
    assert loaded.watches[0].group == "combat"


def test_the_list_says_which_group_a_hotkey_or_data_rule_is_in():
    from genericmud.ui.automation_manager import _rule_summary

    hotkey = _rule_summary("key", UserKey(key="f2", speak="health", group="combat"))
    assert "(group combat)" in hotkey
    data = _rule_summary(
        "data", UserWatch(name="Char.Vitals.hp", speak="${value}", group="combat")
    )
    assert "(group combat)" in data
    timer = _rule_summary("timer", UserTimer(every=5, send="look", group="combat"))
    assert "(group combat)" in timer
