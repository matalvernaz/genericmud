"""Sequencing: which rule is tried first, and whether the rest are still tried.

MUSHclient has a sequence number (lower is sooner) and "keep evaluating". Both matter as soon as
two rules match the same line -- one gag fighting another gag, two rules both speaking, an alias
that should not also send what was typed.
"""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import UserAlias, UserRules, UserTrigger, load_rules, save_rules
from genericmud.scripting.api import ScriptApi
from tests.helpers import RecordingSink


def _world(tmp_path, *, triggers=(), aliases=()) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, UserRules(triggers=list(triggers), aliases=list(aliases)))
    return sink, engine


def test_a_lower_order_is_tried_first(tmp_path):
    sink, engine = _world(
        tmp_path,
        triggers=[
            UserTrigger(pattern="backup", send="say second", order=200),
            UserTrigger(pattern="backup", send="say first", order=50),
        ],
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["say first", "say second"]


def test_equal_orders_fall_back_to_the_pattern_text(tmp_path):
    # MUSHclient sorts equal sequences by match text; doing otherwise would make the order depend
    # on the order the rules happen to be listed in, which the player cannot see.
    sink, engine = _world(
        tmp_path,
        triggers=[
            UserTrigger(pattern="backup", send="say b", order=100),
            UserTrigger(pattern="backup", send="say a", order=100),
        ],
    )
    engine.process_line(Line("Starting database backup."))
    assert len(sink.sent) == 2  # both ran; the tie is broken by pattern, not by position


def test_clearing_keep_checking_makes_a_rule_the_last_one_heard(tmp_path):
    sink, engine = _world(
        tmp_path,
        triggers=[
            UserTrigger(pattern="backup", send="say first", order=10, keep_evaluating=False),
            UserTrigger(pattern="backup", send="say never", order=20),
        ],
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["say first"]


def test_a_player_rule_cannot_silence_a_packs_rules(tmp_path):
    # MUSHclient stops evaluation inside one plugin. A player rule stopping the search must not
    # take a soundpack's trigger with it, which is what stop_scope is for.
    sink, engine = _world(
        tmp_path,
        triggers=[UserTrigger(pattern="backup", send="say first", order=10, keep_evaluating=False)],
    )
    # A pack rule registers as -sequence, so it sorts after an unordered player rule. At priority
    # 0 it would run first and the test would pass even with no stop_scope at all.
    engine.add_trigger(
        "backup", lambda _ctx: sink.sent.append("pack"), source="A pack", priority=-100
    )
    engine.process_line(Line("Starting database backup."))
    assert "pack" in sink.sent


def test_an_alias_still_takes_the_command_when_it_keeps_looking(tmp_path):
    sink, engine = _world(
        tmp_path,
        aliases=[
            UserAlias(pattern="sh *", send="shoot ${1}", order=10, keep_evaluating=True),
            UserAlias(pattern="sh *", send="also ${1}", order=20),
        ],
    )
    # Both aliases run, and what was typed does not also reach the MUD.
    assert engine.process_input("sh goblin") == []
    assert sink.sent == ["shoot goblin", "also goblin"]


def test_an_alias_that_stops_keeps_the_later_ones_out(tmp_path):
    sink, engine = _world(
        tmp_path,
        aliases=[
            UserAlias(pattern="sh *", send="shoot ${1}", order=10, keep_evaluating=False),
            UserAlias(pattern="sh *", send="never ${1}", order=20),
        ],
    )
    assert engine.process_input("sh goblin") == []
    assert sink.sent == ["shoot goblin"]


def test_the_default_still_lets_every_rule_run(tmp_path):
    # A rules file written before this existed has neither field; it must behave as it did.
    sink, engine = _world(
        tmp_path,
        triggers=[
            UserTrigger(pattern="backup", send="say one"),
            UserTrigger(pattern="backup", send="say two"),
        ],
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["say one", "say two"]


def test_the_sequencing_survives_a_saved_rules_file(tmp_path):
    save_rules(
        tmp_path,
        UserRules(
            triggers=[UserTrigger(pattern="a", order=20, keep_evaluating=False)],
            aliases=[UserAlias(pattern="b", order=30, keep_evaluating=False)],
        ),
    )
    loaded = load_rules(tmp_path)
    assert (loaded.triggers[0].order, loaded.triggers[0].keep_evaluating) == (20, False)
    assert (loaded.aliases[0].order, loaded.aliases[0].keep_evaluating) == (30, False)


def test_an_unordered_player_rule_comes_before_a_packs_rule(tmp_path):
    # 0.25.0 put player rules at priority 0 and a pack's trigger at -sequence. -order would have
    # put an unordered player rule at -100, in with the packs, which reordered every old world.
    sink, engine = _world(
        tmp_path, triggers=[UserTrigger(pattern="backup", send="say mine")]
    )
    engine.add_trigger(
        "backup", lambda _ctx: sink.sent.append("pack"), source="A pack", priority=-100
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["say mine", "pack"]


def test_your_first_matching_alias_takes_the_command_on_its_own(tmp_path):
    # 0.25.0 stopped at the first alias of yours that matched; two aliases both matching used to
    # send one command, and after the order change they both sent.
    sink, engine = _world(
        tmp_path,
        aliases=[
            UserAlias(pattern="k *", send="kill ${1}"),
            UserAlias(pattern="k *", send="kick ${1}"),
        ],
    )
    engine.process_input("k orc")
    assert sink.sent == ["kill orc"]


def test_an_alias_that_is_told_to_keep_looking_does(tmp_path):
    sink, engine = _world(
        tmp_path,
        aliases=[
            UserAlias(pattern="k *", send="kill ${1}", keep_evaluating=True),
            UserAlias(pattern="k *", send="kick ${1}"),
        ],
    )
    engine.process_input("k orc")
    assert sink.sent == ["kill orc", "kick orc"]


def test_your_alias_beats_a_packs_alias_of_the_same_name(tmp_path):
    # A pack's aliases register at -sequence, so an unordered player's alias sorts first, which is
    # what 0.25.0 did. When it stops the search, the pack's alias does not also send.
    sink, engine = _world(tmp_path, aliases=[UserAlias(pattern="k", send="kill orc")])
    engine.add_alias(
        "k", lambda _ctx: sink.sent.append("pack"), source="A pack", priority=-100
    )
    engine.process_input("k")
    assert sink.sent == ["kill orc"]


def test_two_rules_with_one_pattern_keep_their_file_order_across_a_save(tmp_path):
    # Equal order and equal pattern used to fall through to whatever order they were registered
    # in; the rule's place in the file is written into the tiebreak so a reload keeps it.
    rules = UserRules(
        triggers=[
            UserTrigger(pattern="backup", send="say first"),
            UserTrigger(pattern="backup", send="say second"),
        ]
    )
    save_rules(tmp_path, rules)
    loaded = load_rules(tmp_path)
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, loaded)
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["say first", "say second"]
def test_the_list_says_a_rules_order_and_says_nothing_at_the_default():
    # The order decides which of two matching rules is heard first, and the list is the only
    # place to compare them without opening each one in turn.
    from genericmud.ui.automation_manager import _rule_summary

    ordered = _rule_summary("trigger", UserTrigger(pattern="backup", order=20, send="x"))
    assert "Trigger, order 20" in ordered
    default = _rule_summary("trigger", UserTrigger(pattern="backup", send="x"))
    assert "order" not in default
    alias = _rule_summary("alias", UserAlias(pattern="sh", order=5, send="shoot"))
    assert "Alias, order 5" in alias


def test_your_rules_go_before_a_vipmud_or_lua_packs_at_the_same_order(tmp_path):
    # VIPMud and Lua packs register at priority 0, the same place as an unordered rule of yours,
    # and the tie went to the pack: its alias took the command and its trigger ran first.
    # Yours win the tie now, whichever was registered first.
    from genericmud.scripting.lua_runtime import LuaPackRuntime
    from genericmud.scripting.vipmud_dialect import VipMudPack

    for packs_first in (False, True):
        sink = RecordingSink()
        engine = AutomationEngine(sink)

        def yours(engine=engine) -> None:
            api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
            user_rules.register_rules(
                api,
                UserRules(
                    aliases=[UserAlias(pattern="k", send="kick")],
                    triggers=[UserTrigger(pattern="tells you", send="reply mine")],
                ),
            )

        def packs(engine=engine) -> None:
            VipMudPack(ScriptApi(engine, source="vip")).load_source("#alias k {kill kobold}")
            LuaPackRuntime(ScriptApi(engine, source="lua", base_dir=str(tmp_path))).run_source(
                'mud.trigger("tells you", function() mud.send("reply pack") end)'
            )

        for step in (packs, yours) if packs_first else (yours, packs):
            step()
        assert engine.process_input("k") == []
        engine.process_line(Line("Bob tells you hi"))
        assert sink.sent == ["kick", "reply mine", "reply pack"], packs_first
