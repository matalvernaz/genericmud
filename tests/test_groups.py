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


# --- what a script switched shows where the rule is edited ----------------------------------


def test_the_engine_answers_a_rules_live_state_by_name():
    engine = AutomationEngine(RecordingSink())
    api = ScriptApi(engine, source=user_rules.SOURCE)
    api.add_trigger("backup", lambda _ctx: None, name="backup-watch", group="combat")
    api.add_alias("sh *", lambda _ctx: None, name="shoot", group="combat")
    api.add_named_timer("tick", 5, lambda: None, every=5)
    assert engine.is_rule_enabled("trigger", "backup-watch") is True
    assert engine.is_rule_enabled("timer", "tick") is True
    assert engine.set_enabled("backup-watch", False) is True
    assert engine.is_rule_enabled("trigger", "backup-watch") is False
    assert engine.is_rule_enabled("alias", "shoot") is True  # the name, not the group, was used
    engine.set_group_enabled("combat", False)
    assert engine.is_rule_enabled("alias", "shoot") is False
    assert engine.set_timer_enabled("tick", False) is True
    assert engine.is_rule_enabled("timer", "tick") is False


def test_a_rule_the_engine_never_had_answers_nothing():
    # Disabled in the file, or a name that was never registered: the file copy stays the truth.
    engine = AutomationEngine(RecordingSink())
    assert engine.is_rule_enabled("trigger", "not-registered") is None
    assert engine.is_rule_enabled("trigger", "") is None
    assert engine.is_rule_enabled("nonsense", "x") is None


def test_a_hotkey_and_a_data_rule_answer_by_their_own_binding():
    engine = AutomationEngine(RecordingSink())
    api = ScriptApi(engine, source=user_rules.SOURCE)
    api.add_key("f2", lambda _ctx: None, group="combat")
    api.add_data_watch("Char.Vitals.hp", lambda _n, _v: None, group="combat")
    # A pack can bind the same combo; the user's binding is the one the manager shows.
    engine.add_key("f2", lambda _ctx: None, source="pack")
    assert engine.is_rule_enabled("key", "f2", source=user_rules.SOURCE) is True
    assert engine.is_rule_enabled("data", "gmcp.Char.Vitals.hp", source=user_rules.SOURCE) is True
    engine.set_group_enabled("combat", False)
    assert engine.is_rule_enabled("key", "f2", source=user_rules.SOURCE) is False
    assert engine.is_rule_enabled("data", "char.vitals.hp", source=user_rules.SOURCE) is False


def test_the_model_takes_the_engines_word_for_each_rule(tmp_path):
    engine = AutomationEngine(RecordingSink())
    rules = UserRules(
        triggers=[
            UserTrigger(pattern="a", send="look", label="watched"),
            UserTrigger(pattern="b", send="look"),  # unnamed: the file stays the truth
        ],
        aliases=[UserAlias(pattern="sh *", send="shoot ${1}", label="shoot", group="combat")],
        timers=[UserTimer(label="tick", every=5, send="look")],
        keys=[UserKey(key="f2", speak="health", group="combat")],
        watches=[UserWatch(name="Char.Vitals.hp", speak="${value}", group="combat")],
    )
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, rules)
    assert all(rule.enabled for rule in rules.triggers)
    engine.set_enabled("watched", False)
    engine.set_group_enabled("combat", False)
    engine.set_timer_enabled("tick", False)
    user_rules.apply_engine_state(rules, engine)
    assert [rule.enabled for rule in rules.triggers] == [False, True]
    assert rules.aliases[0].enabled is False
    assert rules.timers[0].enabled is False
    assert rules.keys[0].enabled is False
    assert rules.watches[0].enabled is False


def test_a_timer_the_player_never_named_is_still_answered_for(tmp_path):
    # An unnamed timer is registered under a name made from its schedule, and a script can
    # switch it by that name, so the manager has to be able to ask about the same name.
    engine = AutomationEngine(RecordingSink())
    rules = UserRules(timers=[UserTimer(every=30, send="look")])
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, rules)
    assert engine.set_timer_enabled("timer 30s", False) is True
    user_rules.apply_engine_state(rules, engine)
    assert rules.timers[0].enabled is False


# --- the rules a script made: the manager can list and switch them, not edit them --------------


def test_the_engine_lists_what_a_script_registered():
    engine = AutomationEngine(RecordingSink())
    from genericmud.scripting import rule_scripts, user_scripts

    source = f"{user_scripts.SOURCE_PREFIX}1"
    api = ScriptApi(engine, source=source)
    api.add_trigger("You see *", lambda _ctx: None)
    api.add_alias("sh *", lambda _ctx: None, name="shoot")
    api.add_key("f2", lambda _ctx: None)
    api.add_named_timer("tick", 5, lambda: None, every=5)
    api.add_data_watch("Char.Vitals.hp", lambda _name, _value: None)
    engine.add_trigger("a pack's line", lambda _ctx: None, source="erion")

    listed = engine.live_rules((user_scripts.SOURCE_PREFIX, rule_scripts.RULE_SOURCE))
    assert [(rule.kind, rule.name) for rule in listed] == [
        ("trigger", ""), ("alias", "shoot"), ("key", ""), ("timer", "tick"), ("data", ""),
    ]
    # The pattern as the script wrote it, not the wildcard translation the engine compiled.
    assert listed[0].summary == "You see *"
    assert listed[1].summary == "sh *"
    assert listed[3].summary == "every 5 seconds"
    assert listed[4].summary == "Char.Vitals.hp"
    assert all(rule.enabled for rule in listed)


def test_a_rule_from_a_rule_s_own_code_is_listed_too():
    engine = AutomationEngine(RecordingSink())
    from genericmud.scripting import rule_scripts, user_scripts

    api = ScriptApi(engine, source=rule_scripts.RULE_SOURCE)
    api.add_trigger("backup", lambda _ctx: None)
    listed = engine.live_rules((user_scripts.SOURCE_PREFIX, rule_scripts.RULE_SOURCE))
    assert [rule.summary for rule in listed] == ["backup"]


def test_a_script_s_rule_can_be_switched_off_and_on_by_its_handle():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    from genericmud.scripting import user_scripts

    api = ScriptApi(engine, source=f"{user_scripts.SOURCE_PREFIX}1", base_dir=".")
    api.add_trigger("You feel better", lambda _ctx: sink.send("fired"))
    (rule,) = engine.live_rules((user_scripts.SOURCE_PREFIX,))
    assert engine.set_live_enabled(rule.handle, False) is True
    engine.process_line(Line("You feel better"))
    assert sink.sent == []
    assert engine.set_live_enabled(rule.handle, True) is True
    engine.process_line(Line("You feel better"))
    assert sink.sent == ["fired"]


def test_every_kind_a_script_can_register_can_be_switched():
    engine = AutomationEngine(RecordingSink())
    from genericmud.scripting import user_scripts

    api = ScriptApi(engine, source=f"{user_scripts.SOURCE_PREFIX}1")
    api.add_alias("sh *", lambda _ctx: None)
    api.add_key("f2", lambda _ctx: None)
    api.add_named_timer("tick", 5, lambda: None, every=5)
    api.add_data_watch("Char.Vitals.hp", lambda _name, _value: None)
    for rule in engine.live_rules((user_scripts.SOURCE_PREFIX,)):
        assert engine.set_live_enabled(rule.handle, False) is True, rule.kind
        assert engine.live_rules((user_scripts.SOURCE_PREFIX,))[
            [r.kind for r in engine.live_rules((user_scripts.SOURCE_PREFIX,))].index(rule.kind)
        ].enabled is False, rule.kind


def test_switching_a_rule_the_engine_no_longer_has_says_so():
    engine = AutomationEngine(RecordingSink())
    assert engine.set_live_enabled(object(), False) is False


def test_a_script_can_name_and_group_the_rules_it_makes(tmp_path):
    # What the manager shows as "from your scripts" only reads well if a script can say what its
    # rules are called and which set they belong to, as MUSHclient's AddTriggerEx can.
    pytest.importorskip("lupa")
    from genericmud.scripting import user_scripts
    from genericmud.scripting.lua_runtime import LuaPackRuntime

    engine = AutomationEngine(RecordingSink())
    runtime = LuaPackRuntime(
        ScriptApi(engine, source=f"{user_scripts.SOURCE_PREFIX}1", base_dir=str(tmp_path))
    )
    runtime.run_source(
        '''
        mud.trigger("You feel better", function() end, {label="heal-watch", group="combat"})
        mud.alias("sh *", function() end, {label="shoot", group="combat"})
        mud.key("f2", function() end, {group="combat"})
        '''
    )
    listed = engine.live_rules((user_scripts.SOURCE_PREFIX,))
    assert [(rule.kind, rule.name, rule.group) for rule in listed] == [
        ("trigger", "heal-watch", "combat"),
        ("alias", "shoot", "combat"),
        ("key", "", "combat"),
    ]
    # And they are switchable the ways a rule's name and group are meant to be.
    assert engine.set_enabled("heal-watch", False) is True
    assert engine.is_rule_enabled("trigger", "heal-watch") is False
    assert engine.set_group_enabled("combat", False) == 3
