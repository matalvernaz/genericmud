"""Leaving a line out of the session log, without changing how it is read or heard.

MUSHclient separates two questions about a matched line: whether it is shown, and whether it is
written down. A line worth reading now (a database backup starting, a metaprefix marker, a
gateway notice) is not worth keeping for months, and a line worth keeping is not always one worth
hearing. A trigger can now say "show it, but keep it out of the file".
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.automation.engine import AutomationEngine
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import UserRules, UserTrigger, load_rules, save_rules
from genericmud.protocol.telnet import DataReceived
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend, RecordingSink


def _engine(tmp_path) -> AutomationEngine:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    return engine


def test_a_rule_can_leave_its_line_out_of_the_log():
    engine = AutomationEngine(RecordingSink())
    engine.add_trigger("backup", None, omit_from_log=True)
    line = engine.process_line(Line("Starting database backup."))
    assert line.omit_from_log is True


def test_leaving_a_line_out_of_the_log_does_not_hide_it():
    # The two flags are separate: this one only concerns the file.
    engine = AutomationEngine(RecordingSink())
    engine.add_trigger("backup", None, omit_from_log=True)
    line = engine.process_line(Line("Starting database backup."))
    assert line.gagged is False
    assert line.display_when_gagged is False


def test_gagging_a_line_still_logs_it_unless_asked_otherwise():
    engine = AutomationEngine(RecordingSink())
    engine.add_trigger("backup", None, gag=True)
    line = engine.process_line(Line("Starting database backup."))
    assert line.gagged is True
    assert line.omit_from_log is False


def test_a_line_nothing_matched_is_logged_as_usual():
    engine = AutomationEngine(RecordingSink())
    engine.add_trigger("something else", None, omit_from_log=True)
    line = engine.process_line(Line("A quiet road."))
    assert line.omit_from_log is False


def test_a_user_trigger_carries_the_setting(tmp_path):
    rules = UserRules(
        triggers=[UserTrigger(pattern="database backup", omit_log=True)]
    )
    engine = AutomationEngine(RecordingSink())
    api = ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path))
    user_rules.register_rules(api, rules)
    assert engine.process_line(Line("Starting database backup.")).omit_from_log is True


def test_a_soundpacks_own_omit_from_log_now_works_too():
    # MUSHclient's flag was parsed for packs and never acted on. It is the same line field.
    engine = AutomationEngine(RecordingSink())
    engine.add_trigger("backup", None, omit_from_log=True, source="A pack")
    assert engine.process_line(Line("starting backup")).omit_from_log is True


def test_the_setting_survives_a_saved_rules_file(tmp_path):
    save_rules(
        tmp_path,
        UserRules(
            triggers=[
                UserTrigger(pattern="backup", omit_log=True),
                UserTrigger(pattern="other"),
            ]
        ),
    )
    loaded = load_rules(tmp_path)
    assert [trigger.omit_log for trigger in loaded.triggers] == [True, False]


def test_a_rules_file_from_before_the_setting_reads_as_logging(tmp_path):
    import json

    from genericmud.packs.user_rules import rules_path

    rules_path(tmp_path).write_text(
        json.dumps({"version": 1, "triggers": [{"pattern": "backup"}]}), encoding="utf-8"
    )
    assert load_rules(tmp_path).triggers[0].omit_log is False


# --- the whole way through: a session log on disk ------------------------------------


def _logged(tmp_path) -> list[str]:
    return [
        line
        for path in tmp_path.rglob("*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_a_notice_kept_out_of_the_log_is_still_in_the_window(tmp_path):
    # The case this exists for: a world announcing a database backup every few hours, read once
    # and never wanted in the file again.
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=lambda _text: None,
        post=lambda _message: None,
        keymap=load_keymap("vipmud"),
        log_dir=tmp_path,
        name="Test",
    )
    app.engine.add_trigger("Database backup", None, omit_from_log=True)
    app.start_logging(announce=False)
    try:
        for text in ("A quiet road.", "Starting Database backup. Please wait."):
            app.on_telnet_event(DataReceived(text.encode() + b"\r\n"))
        shown = [line.plain_text for line in app.buffer.lines()]
        written = _logged(tmp_path)
        assert "Starting Database backup. Please wait." in shown  # read and reviewable
        assert "A quiet road." in written  # an ordinary line is still logged
        assert not any("Database backup" in line for line in written)
    finally:
        if app.logger is not None:
            app.logger.stop()


def test_a_trigger_matched_line_is_read_normally_even_when_it_is_not_logged():
    engine = AutomationEngine(RecordingSink())
    spoken: list[str] = []
    engine.add_trigger("backup", lambda _ctx: spoken.append("matched"), omit_from_log=True)
    line = engine.process_line(Line("Starting database backup."))
    assert spoken == ["matched"]
    assert line.plain_text == "Starting database backup."
