"""DiagnosticLog: deterministic formatting, append, size cap, banner, no-op when unstarted."""

from __future__ import annotations

from datetime import datetime

from genericmud.session.diaglog import DiagnosticLog, write_banner


def _fixed_clock():
    return datetime(2026, 6, 24, 12, 0, 0, 123000)


def test_event_writes_timestamped_key_value_line(tmp_path):
    diag = DiagnosticLog(tmp_path / "logs" / "d.log", clock=_fixed_clock)  # parent made on start
    diag.start()
    diag.event("backend.play", file="/abs/hit.wav", result="OK", gain=0.5)
    diag.stop()
    assert (tmp_path / "logs" / "d.log").read_text(encoding="utf-8") == (
        "2026-06-24 12:00:00.123 backend.play file=/abs/hit.wav result=OK gain=0.5\n"
    )


def test_event_collapses_newlines_and_shows_empty_string(tmp_path):
    diag = DiagnosticLog(tmp_path / "d.log", clock=_fixed_clock)
    diag.start()
    diag.event("trigger.fire", line="two\nlines", sppath="")
    diag.stop()
    line = (tmp_path / "d.log").read_text(encoding="utf-8")
    assert "line=two lines" in line  # newline collapsed so it stays one record
    assert "sppath=''" in line  # empty value is visible, not blank


def test_event_is_a_noop_before_start_and_after_stop(tmp_path):
    path = tmp_path / "d.log"
    diag = DiagnosticLog(path, clock=_fixed_clock)
    diag.event("banner")  # before start: silently ignored, no file created
    assert not path.exists()
    diag.start()
    diag.stop()
    diag.event("backend.play", result="OK")  # after stop: ignored
    assert path.read_text(encoding="utf-8") == ""


def test_size_cap_stops_writing_after_the_limit(tmp_path, monkeypatch):
    import genericmud.session.diaglog as mod

    monkeypatch.setattr(mod, "_MAX_BYTES", 120)  # tiny cap so a few events trip it
    diag = DiagnosticLog(tmp_path / "d.log", clock=_fixed_clock)
    diag.start()
    for index in range(50):
        diag.event("trigger.fire", n=index)
    diag.stop()
    text = (tmp_path / "d.log").read_text(encoding="utf-8")
    assert "truncated" in text.splitlines()[-1]
    assert text.count("trigger.fire") < 50  # writing stopped well before all 50


def test_write_banner_records_the_environment(tmp_path):
    diag = DiagnosticLog(tmp_path / "d.log", clock=_fixed_clock)
    diag.start()
    write_banner(diag)
    diag.stop()
    line = (tmp_path / "d.log").read_text(encoding="utf-8")
    assert line.startswith("2026-06-24 12:00:00.123 banner ")
    for key in ("version=", "frozen=", "platform=", "python=", "pygame="):
        assert key in line


def _app_log(folder, name, *, days_old, first_line=None):
    import os
    import time

    path = folder / name
    path.write_text((first_line or "2026-01-01 00:00:00.000 banner version=1.0 x=y") + "\n",
                    encoding="utf-8")
    then = time.time() - days_old * 24 * 3600
    os.utime(path, (then, then))
    return path


def test_old_app_logs_go_but_the_newest_twenty_of_each_stay(tmp_path):
    from genericmud.session.diaglog import KEEP_NEWEST_LOGS, prune_old_logs

    old = [_app_log(tmp_path, f"diagnostic-202601{day:02d}-120000.log", days_old=100 - day)
           for day in range(1, 26)]
    crash = [_app_log(tmp_path, f"crash-202601{day:02d}-120000.log", days_old=100 - day)
             for day in range(1, 4)]
    recent = _app_log(tmp_path, "diagnostic-20251231-120000.log", days_old=2)  # sorts oldest
    removed = prune_old_logs(tmp_path)
    assert sorted(removed) == sorted(p.name for p in old[: 25 - KEEP_NEWEST_LOGS])
    assert all(p.exists() for p in old[25 - KEEP_NEWEST_LOGS:])
    assert all(p.exists() for p in crash)  # only three: all of them are the newest
    assert recent.exists()  # beyond the newest twenty, but not old enough


def test_world_logs_are_never_pruned_even_under_an_app_logs_name(tmp_path):
    # A world named "crash" files its transcripts as crash-<stamp>.log too.
    from genericmud.session.diaglog import prune_old_logs

    for day in range(1, 26):
        _app_log(tmp_path, f"crash-202601{day:02d}-120000.log", days_old=200,
                 first_line="Session started 2026-01-01 12:00:00")
    transcript = _app_log(tmp_path, "Star_Conquest-20260101-120000.log", days_old=200,
                          first_line="Session started 2026-01-01 12:00:00")
    assert prune_old_logs(tmp_path) == []
    assert transcript.exists()


def test_startup_prunes_the_config_folders_logs(tmp_path, monkeypatch):
    import genericmud.__main__ as entry
    from genericmud.session import diaglog

    looked_in: list = []
    monkeypatch.setattr("genericmud.config.worlds.config_dir", lambda: tmp_path)
    monkeypatch.setattr(diaglog, "prune_old_logs", lambda folder: looked_in.append(folder))
    entry._prune_old_logs()
    # The app's own logs live in diagnostics/, apart from the session transcripts.
    assert looked_in == [tmp_path / "logs" / "diagnostics"]
