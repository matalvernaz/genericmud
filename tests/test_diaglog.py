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
