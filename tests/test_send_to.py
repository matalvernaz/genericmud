"""Where a rule's response text goes: MUSHclient's "send to" box.

The text is one field and the destination another, for triggers and aliases alike -- never a
separate field per destination. The targets are the ones this client has a use for, in
MUSHclient's terms: the MUD, the command box (unsent, to finish yourself), the output as a note,
the session log alone, a script, or a variable.
"""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import (
    SEND_TO_CHOICES,
    SEND_TO_LABELS,
    UserAlias,
    UserKey,
    UserRules,
    UserTimer,
    UserTrigger,
    UserWatch,
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
    assert SEND_TO_CHOICES == (
        "world", "command", "output", "print", "log", "execute", "script", "variable",
    )
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


# --- a variable: the text is kept under a name, to be read later ---------------------------


def test_to_a_variable_stores_the_expanded_text_and_does_nothing_else(tmp_path):
    # MUSHclient's variable target: a capture becomes a value, which is how a trigger can
    # remember who paged you without a line of Lua.
    sink, engine = _rule(
        tmp_path,
        UserTrigger(
            pattern="* pages: *", send="${1}", send_to="variable", variable="pager"
        ),
    )
    engine.process_line(Line("Brady pages: hello"))
    assert engine.get_var("pager") == "Brady"
    assert sink.sent == [] and sink.echoed == [] and sink.typed == []


def test_a_stored_value_is_there_for_the_rule_that_reads_it(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(
        api,
        UserRules(
            triggers=[
                UserTrigger(
                    pattern="* pages: *", send="${1}", send_to="variable", variable="pager"
                ),
                UserTrigger(pattern="page back", send="reply ${script:pager}"),
            ]
        ),
    )
    engine.process_line(Line("Brady pages: hello"))
    engine.process_line(Line("page back"))
    assert sink.sent == ["reply Brady"]


def test_a_variable_keeps_semicolons_and_line_breaks_whole(tmp_path):
    # Every other target is a stack of commands; a value is one value, so its text is not split.
    _sink, engine = _rule(
        tmp_path,
        UserTrigger(
            pattern="lookup", send="first; second\nthird", send_to="variable", variable="value"
        ),
    )
    engine.process_line(Line("lookup"))
    assert engine.get_var("value") == "first; second\nthird"


def test_an_alias_can_store_what_you_typed(tmp_path):
    sink, engine = _alias(
        tmp_path,
        UserAlias(
            pattern="mark *", send="${1}", send_to="variable", variable="marked"
        ),
    )
    engine.process_input("mark goblin")
    assert engine.get_var("marked") == "goblin"
    assert sink.sent == [] and sink.typed == []


def test_a_variable_target_without_a_name_is_refused(tmp_path):
    # Refused with a reason rather than quietly doing nothing: a rule that cannot say where its
    # text goes would look like a rule that works.
    with pytest.raises(ValueError, match="no variable name"):
        _rule(tmp_path, UserTrigger(pattern="backup", send="look", send_to="variable"))


def test_a_disabled_rule_may_be_half_finished(tmp_path):
    # A draft left with the target but no name must not stop the rules that do work loading.
    sink, engine = _rule(
        tmp_path,
        UserTrigger(
            pattern="backup", send="look", send_to="variable", variable="", enabled=False
        ),
    )
    engine.process_line(Line("Starting database backup."))
    assert sink.sent == []


def test_the_variable_target_survives_a_saved_rules_file(tmp_path):
    save_rules(
        tmp_path,
        UserRules(
            triggers=[
                UserTrigger(pattern="a", send="x", send_to="variable", variable="where")
            ]
        ),
    )
    loaded = load_rules(tmp_path)
    assert loaded.triggers[0].send_to == "variable"
    assert loaded.triggers[0].variable == "where"


def test_the_manager_says_where_the_text_went():
    # A list line reading "backup: ${1}" would not say the text went into a variable.
    pytest.importorskip("wx")
    from genericmud.ui.automation_manager import _rule_summary

    stored = _rule_summary(
        "trigger",
        UserTrigger(pattern="backup", send="${1}", send_to="variable", variable="pager"),
    )
    assert "store ${1} in variable pager" in stored
    sent = _rule_summary("trigger", UserTrigger(pattern="backup", send="look"))
    assert "send look" in sent


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


def test_to_the_command_box_asks_to_leave_a_typed_command_alone():
    # MUSHclient's SetCommand does nothing when the command line holds text; the box gets the
    # same request, so a rule firing mid-sentence cannot replace what the player is writing.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE)
    user_rules.register_rules(
        api,
        UserRules(triggers=[UserTrigger(pattern="backup", send="drink water", send_to="command")]),
    )
    engine.process_line(Line("backup"))
    assert sink.typed == ["drink water"]
    assert sink.typed_only_if_empty == [True]


def test_typed_refusal_is_the_check_a_link_and_a_rule_share():
    from genericmud.app.links import typed_refusal

    assert typed_refusal("/alias x = quit", ";") == "it would type a genericMud command"
    assert typed_refusal("reply hi;look", ";") == "it would type more than one command"
    assert typed_refusal("reply hello", ";") is None


def test_a_rule_whose_own_text_is_a_command_is_not_refused(tmp_path):
    # Refused only when the capture is what did it: "send to command" is the player's own
    # field, so what they wrote there is theirs to decide.
    from genericmud.app import EngineApp
    from genericmud.packs import PackStore
    from genericmud.packs.user_rules import save_rules
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    posted: list[dict] = []
    backend = RecordingBackend()
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=[].append, post=posted.append, keymap={},
        packs=PackStore(tmp_path / "soundpacks"), name="Test World",
    )
    pack_dir = app.user_rules_dir()
    save_rules(
        pack_dir,
        UserRules(triggers=[UserTrigger(pattern="backup", send="/help", send_to="command")]),
    )
    app.reload_user_rules()
    app.engine.process_line(Line("backup"))
    assert posted, "the player's own client command should still be staged"


def test_a_capture_that_would_type_a_command_is_refused_and_says_why(tmp_path):
    from genericmud.app import EngineApp
    from genericmud.packs import PackStore
    from genericmud.packs.user_rules import save_rules
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    posted: list[dict] = []
    backend = RecordingBackend()
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=[].append, post=posted.append, keymap={},
        packs=PackStore(tmp_path / "soundpacks"), name="Test World",
    )
    pack_dir = app.user_rules_dir()
    save_rules(
        pack_dir,
        UserRules(
            triggers=[
                UserTrigger(pattern="pages: *", send="${1}", send_to="command"),
                UserTrigger(pattern="says: *", send="reply ${1}", send_to="command"),
            ]
        ),
    )
    app.reload_user_rules()

    # The capture is the whole staged text and starts a client command.
    app.engine.process_line(Line("pages: /alias x = quit"))
    assert not posted
    assert any("command box" in said for said in backend.spoken)

    # A second command hidden behind the separator, where the author wrote no separator.
    app.engine.process_line(Line("says: hi;look"))
    assert not posted

    # The same line with nothing dangerous in it does reach the box.
    app.engine.process_line(Line("says: hello"))
    assert posted


def test_a_rules_file_from_before_the_new_fields_is_copied_once(tmp_path):
    # Going back a version after this one has written labels and destinations would turn those
    # rules into commands for the MUD; the copy is what lets a player go back.
    old = '{"version": 1, "triggers": [{"pattern": "backup", "send": "look"}]}'
    (tmp_path / "rules.json").write_text(old, encoding="utf-8")
    save_rules(
        tmp_path, UserRules(triggers=[UserTrigger(pattern="backup", send="look", label="b")])
    )
    backup = tmp_path / "rules.json.before-send-to.bak"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == old

    # Once is enough: later saves leave the copy alone.
    save_rules(tmp_path, UserRules(triggers=[UserTrigger(pattern="other", send="score")]))
    assert backup.read_text(encoding="utf-8") == old


def test_a_rules_file_that_already_has_the_new_fields_gets_no_copy(tmp_path):
    save_rules(tmp_path, UserRules(triggers=[UserTrigger(pattern="a", send="x", label="one")]))
    save_rules(tmp_path, UserRules(triggers=[UserTrigger(pattern="b", send="y")]))
    assert not (tmp_path / "rules.json.before-send-to.bak").exists()


def test_a_hotkey_can_write_a_note_store_a_value_or_write_the_log(tmp_path):
    # A hotkey could send to the MUD and speak, and nothing else: "press F2 and write it down"
    # needed a script for no reason.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(
        api,
        UserRules(
            keys=[
                UserKey(key="f2", send="backup started", send_to="output"),
                UserKey(key="f3", send="${script:target}", send_to="variable", variable="copied"),
                UserKey(key="f4", send="say hi", send_to="log"),
            ]
        ),
    )
    engine.set_var("target", "goblin")
    assert engine.press_key("f2") is True
    assert engine.press_key("f3") is True
    assert engine.press_key("f4") is True
    assert sink.echoed == [("backup started", "main")]
    assert engine.get_var("copied") == "goblin"
    assert sink.logged == ["say hi"]
    assert sink.sent == []


def test_a_hotkey_can_run_a_rule_script(tmp_path):
    class _Runner:
        def __init__(self) -> None:
            self.ran: list[str] = []

        def run(self, text: str, **_kwargs: object) -> None:
            self.ran.append(text)

    runner = _Runner()
    engine = AutomationEngine(RecordingSink())
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    api.scripts = runner  # what the app attaches; a rule's script target calls it
    user_rules.register_rules(
        api, UserRules(keys=[UserKey(key="f2", send="on_key()", send_to="script")])
    )
    engine.press_key("f2")
    assert runner.ran == ["on_key()"]


def test_a_mud_data_rule_can_write_a_note_or_store_the_value(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(
        api,
        UserRules(
            watches=[
                UserWatch(name="Char.Vitals.hp", send="health ${value}", send_to="output"),
                UserWatch(
                    name="Char.Vitals.mp", send="${value}", send_to="variable", variable="mana"
                ),
            ]
        ),
    )
    engine.data_arrived("gmcp", "Char.Vitals.hp", 90)
    engine.data_arrived("gmcp", "Char.Vitals.mp", 30)
    assert sink.echoed == [("health 90", "main")]
    assert engine.get_var("mana") == "30"
    assert sink.sent == []


# --- the two switches that were missing ------------------------------------------------------


def test_an_alias_and_a_timer_can_cut_current_speech(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    user_rules.register_rules(
        ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path)),
        UserRules(
            aliases=[UserAlias(pattern="heal", send="cast heal", interrupt=True)],
            timers=[UserTimer(label="alert", every=5, send="look", interrupt=True)],
        ),
    )
    engine.process_input("heal")
    sink.scheduled[0][1]()  # the timer fires
    assert sink.speech_stops == 2


def test_a_note_is_always_logged_and_a_print_never_is(tmp_path):
    # The destination decides, not a world-wide setting: MUSHclient's Note goes in the record and
    # its Print does not, and that is the whole difference between them.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    user_rules.register_rules(
        ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path)),
        UserRules(
            timers=[
                UserTimer(label="quiet", every=5, send="still down", send_to="print"),
                UserTimer(label="loud", every=5, send="starting", send_to="output"),
            ]
        ),
    )
    for _delay, callback in list(sink.scheduled):
        callback()
    assert ("still down", "main") in sink.echoed
    assert ("starting", "main") in sink.echoed
    # Both are shown; only the note is part of the record.
    assert sink.echo_flags == [False, True]


def test_every_kind_of_rule_can_print_instead_of_noting(tmp_path):
    # A trigger, a hotkey and a MUD data rule reach the same two destinations as a timer.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(
        api,
        UserRules(
            triggers=[UserTrigger(pattern="backup", send="running", send_to="print")],
            keys=[UserKey(key="f2", send="pressed", send_to="print")],
            watches=[UserWatch(name="Char.Vitals.hp", send="health ${value}", send_to="print")],
        ),
    )
    engine.process_line(Line("Starting database backup."))
    engine.press_key("f2")
    engine.data_arrived("gmcp", "Char.Vitals.hp", 90)
    assert sink.echoed == [
        ("running", "main"), ("pressed", "main"), ("health 90", "main"),
    ]
    assert sink.echo_flags == [False, False, False]  # a print never reaches the log


# --- pack and script notes follow the same rule ---------------------------------------------


def test_a_packs_note_is_logged_and_its_print_is_not():
    # MUSHclient's Note goes in the record; its print does not. The pack layer used to treat
    # both as prints, so a pack that logged by noting silently did not log.
    pytest.importorskip("lupa")
    from genericmud.scripting.mushclient_compat import MushclientPack

    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="p"))
    pack.run_source('Note("a note")')
    pack.run_source('print("a print")')
    assert [text for text, _channel in sink.echoed] == ["a note", "a print"]
    assert sink.echo_flags == [True, False]  # the note is part of the record, the print is not
