"""Where logs are filed, and how logs from an older build find their way there.

The point of the split: a session log is something to read back, a diagnostic file is
something to send to somebody when the client misbehaves. One folder makes both harder.
"""

from __future__ import annotations

import pytest

from genericmud import logpaths
from genericmud.logpaths import (
    diagnostic_log_dir,
    log_count,
    logs_root,
    migrate_legacy_logs,
    world_log_dir,
)

SESSION_HEAD = "Session started 2026-10-27 12:00:00\n"
DIAGNOSTIC_HEAD = "2026-10-27 12:00:00.123 banner version=0.18.1 frozen=False\n"
CRASH_HEAD = "2026-10-27 12:00:00.123 banner version=0.18.1 frozen=False pid=42\n"


@pytest.fixture(autouse=True)
def _config_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr("genericmud.config.worlds.config_dir", lambda: tmp_path)
    return tmp_path


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_two_kinds_get_their_own_folders():
    root = logs_root()
    assert world_log_dir() == root / "worlds"
    assert diagnostic_log_dir() == root / "diagnostics"
    assert world_log_dir() != diagnostic_log_dir()


def test_a_session_log_is_filed_by_what_it_says_not_what_it_is_called():
    # A world called "crash" in session mode produces crash-20260101-120000.log -- exactly a
    # crash log's shape. Its first line is what settles it.
    legacy = _write(logs_root() / "crash-20260101-120000.log", SESSION_HEAD)
    assert migrate_legacy_logs() == 1
    assert not legacy.exists()
    assert (world_log_dir() / legacy.name).exists()


def test_diagnostic_and_crash_logs_go_together():
    diag = _write(logs_root() / "diagnostic-20260101-120000.log", DIAGNOSTIC_HEAD)
    crash = _write(logs_root() / "crash-20260101-120001.log", CRASH_HEAD)
    assert migrate_legacy_logs() == 2
    assert (diagnostic_log_dir() / diag.name).exists()
    assert (diagnostic_log_dir() / crash.name).exists()
    assert log_count(diagnostic_log_dir()) == 2
    assert log_count(world_log_dir()) == 0


def test_a_file_this_app_did_not_write_is_left_alone():
    # Guessing would file somebody else's file somewhere they would never look for it.
    stranger = _write(logs_root() / "notes.log", "my own notes\n")
    empty = _write(logs_root() / "empty.log", "")
    assert migrate_legacy_logs() == 0
    assert stranger.exists() and empty.exists()


def test_only_log_files_at_the_top_are_considered():
    notes = _write(logs_root() / "notes.txt", SESSION_HEAD)  # ours by content, not by name
    _write(world_log_dir() / "already-filed.log", SESSION_HEAD)
    assert migrate_legacy_logs() == 0
    assert notes.exists()
    assert (world_log_dir() / "already-filed.log").exists()


def test_migrating_twice_moves_nothing_the_second_time():
    _write(logs_root() / "gw-20260101-120000.log", SESSION_HEAD)
    assert migrate_legacy_logs() == 1
    assert migrate_legacy_logs() == 0


def test_a_name_clash_never_overwrites_an_existing_log():
    kept = _write(world_log_dir() / "gw.log", SESSION_HEAD + "the one already there\n")
    _write(logs_root() / "gw.log", SESSION_HEAD + "the one being moved\n")
    assert migrate_legacy_logs() == 1
    assert kept.read_text(encoding="utf-8").endswith("the one already there\n")
    assert (world_log_dir() / "gw-1.log").exists()


def test_no_logs_folder_at_all_is_not_an_error():
    assert migrate_legacy_logs() == 0


def test_an_unreadable_file_is_skipped_rather_than_stopping_the_migration(tmp_path):
    # A file locked by another program, or one with no permission: it stays put and is tried
    # again next start, rather than taking the rest of the migration down with it.
    locked = _write(logs_root() / "a-locked.log", SESSION_HEAD)
    good = _write(logs_root() / "b-good.log", SESSION_HEAD)
    real_replace = type(locked).replace

    def replace(self, target):
        if self.name == "a-locked.log":
            raise PermissionError("in use")
        return real_replace(self, target)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(locked), "replace", replace)
        assert migrate_legacy_logs() == 1
    assert locked.exists()
    assert (world_log_dir() / good.name).exists()


def test_the_worlds_folder_holds_a_new_session_log(tmp_path):
    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    app = EngineApp(
        VoiceRouter(RecordingBackend()),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="gw",
    )
    app.start_logging(announce=False)
    try:
        assert app.logger is not None
        assert app.logger.path.parent == world_log_dir()
        assert app.logger.path.name.startswith("gw-")
    finally:
        if app.logger is not None:
            app.logger.stop()


def test_the_log_command_says_where_both_kinds_are():
    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    backend = RecordingBackend()
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="gw",
    )
    _write(world_log_dir() / "one.log", SESSION_HEAD)
    app.on_ws_message({"type": "input", "text": "/logs"})
    said = backend.spoken[-1]
    assert "world logs in" in said and "diagnostics in" in said
    assert "(1 file)" in said  # the session log just written; "1 files" would sound like a bug
    assert "(0 files)" in said  # no diagnostics yet


def test_the_command_is_only_ours_when_it_is_spelled_out():
    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend()),
        send=sent.append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="gw",
    )
    app.on_ws_message({"type": "input", "text": "/logging"})
    assert sent == ["/logging"]  # an unknown /verb still belongs to the MUD


def test_the_module_keeps_the_folder_answer_in_one_place():
    # Both loggers and the session logger get their directory from here, so this is the one
    # place to look when asking where logs go.
    assert logpaths.world_log_dir().name == "worlds"
    assert logpaths.diagnostic_log_dir().name == "diagnostics"


def test_pruning_still_finds_the_apps_logs_once_they_are_filed(monkeypatch):
    # Upstream prunes old diagnostic and crash logs at startup; this branch moves them into
    # their own folder. The two only work together if pruning is pointed there, so this is the
    # test that would have caught them drifting apart. The kept-newest count is shrunk so the
    # policy itself (upstream's own tests) isn't what is being checked here.
    import os
    import time

    from genericmud.session import diaglog

    monkeypatch.setattr(diaglog, "KEEP_NEWEST_LOGS", 1)
    older = _write(logs_root() / "diagnostic-20200101-120000.log", DIAGNOSTIC_HEAD)
    newer = _write(logs_root() / "diagnostic-20200102-120000.log", DIAGNOSTIC_HEAD)
    assert migrate_legacy_logs() == 2
    filed_older = diagnostic_log_dir() / older.name
    filed_newer = diagnostic_log_dir() / newer.name
    long_ago = time.time() - (diaglog.LOG_MAX_AGE_DAYS + 1) * 24 * 3600
    for path in (filed_older, filed_newer):
        os.utime(path, (long_ago, long_ago))

    removed = diaglog.prune_old_logs(diagnostic_log_dir())
    assert removed == [older.name]  # the newest of its kind is kept
    assert not filed_older.exists()
    assert filed_newer.exists()


def test_pruning_leaves_a_world_log_alone_even_inside_the_diagnostics_folder():
    # The names are the same shape; only the first line tells them apart.
    import genericmud.session.diaglog as diaglog

    where = diagnostic_log_dir()
    world_log = _write(where / "crash-20200101-120000.log", SESSION_HEAD)
    assert diaglog.prune_old_logs(where) == []
    assert world_log.exists()
