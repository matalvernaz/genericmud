"""Where a rule's response text goes: MUSHclient's "send to" box.

The text is one field and the destination another, for triggers and aliases alike -- never a
separate field per destination. The targets are the ones this client has a use for, in
MUSHclient's terms: the MUD, the command box (unsent, to finish yourself), the output as a note,
the session log alone, or a script.
"""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import (
    SEND_TO_CHOICES,
    SEND_TO_LABELS,
    UserAlias,
    UserRules,
    UserTrigger,
    load_rules,
    save_rules,
    send_to_kind,
)
from genericmud.scripting.api import ScriptApi
from tests.helpers import RecordingSink


def _rule(tmp_path, trigger: UserTrigger) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, UserRules(triggers=[trigger]))
    return sink, engine


def _alias(tmp_path, alias: UserAlias) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, UserRules(aliases=[alias]))
    return sink, engine


def test_the_targets_are_the_ones_this_client_can_do():
    assert SEND_TO_CHOICES == ("world", "command", "output", "log", "execute", "script")
    assert len(SEND_TO_LABELS) == len(SEND_TO_CHOICES)


def test_an_unknown_or_missing_target_sends_to_the_mud_as_before():
    # Files written before the field existed have none, and an unknown one must not silently
    # become "nowhere".
    assert send_to_kind("") == "world"
    assert send_to_kind("nonsense") == "world"
    assert send_to_kind(None) == "world"
    assert send_to_kind("output") == "output"


def test_the_default_is_the_mud(tmp_path):
    assert UserTrigger().send_to == "world"
    assert UserAlias().send_to == "world"


def test_to_the_mud_sends_the_commands(tmp_path):
    sink, engine = _rule(tmp_path, UserTrigger(pattern="backup", send="look"))
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]


def test_to_the_command_box_leaves_it_unsent(tmp_path):
    # The point of this target: the text is yours to finish, and nothing leaves.
    sink, engine = _rule(
        tmp_path, UserTrigger(pattern="* pages: *", send="reply ${1} ", send_to="command")
    )
    engine.process_line(Line("Brady pages: hello"))
    assert sink.typed == ["reply Brady "]
    assert sink.sent == []


def test_several_lines_to_the_command_box_arrive_as_one_block(tmp_path):
    sink, engine = _rule(
        tmp_path, UserTrigger(pattern="backup", send="drop sword\ndrop shield", send_to="command")
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.typed == ["drop sword\ndrop shield"]


def test_to_the_output_is_a_note(tmp_path):
    sink, engine = _rule(
        tmp_path, UserTrigger(pattern="backup", send="backup started", send_to="output")
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.echoed == [("backup started", "main")]
    assert sink.sent == []


def test_to_the_log_is_written_without_being_shown(tmp_path):
    sink, engine = _rule(
        tmp_path, UserTrigger(pattern="backup", send="backup started", send_to="log")
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.logged == ["backup started"]
    assert sink.echoed == []  # never in the window
    assert sink.sent == []


def test_execute_hands_the_text_back_to_the_client_not_the_wire(tmp_path):
    # MUSHclient's "execute": the text is processed as if it had been typed, so an alias takes it
    # first and only what falls through reaches the MUD. (Its "send to script" -- calling a named
    # routine in a script -- is not offered: these scripts are hook files with no entry points.)
    sink, engine = _rule(
        tmp_path, UserTrigger(pattern="backup", send="hide backup", send_to="execute")
    )
    sink2: list[str] = []
    engine.add_alias("hide *", lambda ctx: sink2.append(ctx.wildcards[1]))
    engine.process_line(Line("Starting database backup."))
    assert sink2 == ["backup"]
    assert sink.sent == []


def test_an_alias_can_send_to_the_mud_or_somewhere_else(tmp_path):
    sink, engine = _alias(tmp_path, UserAlias(pattern="sh *", send="shoot ${1}"))
    engine.process_input("sh goblin")
    assert sink.sent == ["shoot goblin"]

    sink2, engine2 = _alias(
        tmp_path, UserAlias(pattern="sh *", send="shoot ${1} ", send_to="command")
    )
    engine2.process_input("sh goblin")
    assert sink2.typed == ["shoot goblin "]
    assert sink2.sent == []


def test_carried_words_only_apply_to_the_mud(tmp_path):
    # A plain alias carries the words typed after it onto its command; onto a note they would be
    # words with nothing to join, so an alias sending anywhere else runs only when it matches on
    # its own ("sh", not "sh goblin").
    sink, engine = _alias(tmp_path, UserAlias(pattern="sh", send="shoot", send_to="output"))
    engine.process_input("sh goblin")
    assert sink.echoed == []
    engine.process_input("sh")
    assert sink.echoed == [("shoot", "main")]


# --- the setting survives ----------------------------------------------------------------


def test_the_target_survives_a_saved_rules_file(tmp_path):
    save_rules(
        tmp_path,
        UserRules(
            triggers=[UserTrigger(pattern="a", send="x", send_to="log")],
            aliases=[UserAlias(pattern="b", send="y", send_to="command")],
        ),
    )
    loaded = load_rules(tmp_path)
    assert loaded.triggers[0].send_to == "log"
    assert loaded.aliases[0].send_to == "command"


def test_a_rules_file_from_before_the_target_reads_as_the_mud(tmp_path):
    import json

    from genericmud.packs.user_rules import rules_path

    rules_path(tmp_path).write_text(
        json.dumps({"version": 1, "triggers": [{"pattern": "a", "send": "x"}]}),
        encoding="utf-8",
    )
    assert load_rules(tmp_path).triggers[0].send_to == "world"

# --- labels: the handle a script turns a rule off and on with --------------------------


def test_a_label_is_the_engine_rules_name_and_can_be_switched_off_by_it(tmp_path):
    sink, engine = _rule(
        tmp_path, UserTrigger(pattern="backup", send="look", label="backup-watch")
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]
    # A script's EnableTrigger, by the name the player gave the rule.
    assert engine.set_enabled("backup-watch", False) is True
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]  # nothing more: it is off
    assert engine.set_enabled("backup-watch", True) is True
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look", "look"]


def test_switching_a_name_nothing_has_says_so(tmp_path):
    _sink, engine = _rule(tmp_path, UserTrigger(pattern="backup", send="look", label="named"))
    assert engine.set_enabled("no-such-label", False) is False
    assert engine.set_enabled("", False) is False  # a blank name never matches


def test_an_alias_label_switches_the_alias(tmp_path):
    sink, engine = _alias(tmp_path, UserAlias(pattern="sh *", send="shoot ${1}", label="shoot"))
    engine.process_input("sh goblin")
    assert sink.sent == ["shoot goblin"]
    assert engine.set_enabled("shoot", False) is True
    assert engine.process_input("sh goblin") == ["sh goblin"]  # falls through, unsent by us
    assert sink.sent == ["shoot goblin"]


def test_a_rule_without_a_label_still_works(tmp_path):
    sink, engine = _rule(tmp_path, UserTrigger(pattern="backup", send="look"))
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == ["look"]
    assert engine.set_enabled("anything", False) is False


def test_the_labels_survive_a_saved_rules_file(tmp_path):
    save_rules(
        tmp_path,
        UserRules(
            triggers=[UserTrigger(pattern="a", label="watch-backups")],
            aliases=[UserAlias(pattern="b", label="quick-shoot")],
        ),
    )
    loaded = load_rules(tmp_path)
    assert loaded.triggers[0].label == "watch-backups"
    assert loaded.aliases[0].label == "quick-shoot"
