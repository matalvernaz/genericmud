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
    engine.add_trigger("backup", lambda _ctx: sink.sent.append("pack"), source="A pack")
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
