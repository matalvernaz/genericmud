"""CrashLog: banner, traceback block, defensive write, install no-op, asyncio loop handler."""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import datetime

import genericmud.session.crashlog as crashlog
from genericmud.session.crashlog import (
    CrashLog,
    install_crash_handlers,
    install_loop_exception_handler,
)


def _fixed_clock():
    return datetime(2026, 6, 24, 12, 0, 0, 123000)


def _captured(exc: BaseException):
    """Give an exception a real traceback so format_exception has something to render."""
    try:
        raise exc
    except BaseException:  # noqa: BLE001 - we want the live exc_info, type intentionally broad
        return sys.exc_info()


def test_start_writes_a_banner(tmp_path):
    log = CrashLog(tmp_path / "logs" / "crash.log", clock=_fixed_clock)
    log.start()
    log.stop()
    line = (tmp_path / "logs" / "crash.log").read_text(encoding="utf-8")
    assert line.startswith("2026-06-24 12:00:00.123 banner ")
    for key in ("version=", "frozen=", "platform=", "python=", "pid="):
        assert key in line


def test_record_appends_a_timestamped_traceback_block(tmp_path):
    log = CrashLog(tmp_path / "crash.log", clock=_fixed_clock)
    log.start()
    log.record(*_captured(ValueError("boom")), source="main")
    log.stop()
    text = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "2026-06-24 12:00:00.123 crash source=main thread=" in text
    assert "Traceback (most recent call last):" in text
    assert "ValueError: boom" in text


def test_record_is_a_noop_before_start(tmp_path):
    path = tmp_path / "crash.log"
    log = CrashLog(path, clock=_fixed_clock)
    log.record(*_captured(RuntimeError("x")), source="main")  # no handle yet: ignored
    assert not path.exists()


def test_record_swallows_a_write_fault(tmp_path):
    class _BoomFile:
        def write(self, _text):
            raise OSError("disk full")

        def flush(self):
            pass

    log = CrashLog(tmp_path / "crash.log", clock=_fixed_clock)
    log._handle = _BoomFile()  # simulate a write that fails mid-crash
    log.record(*_captured(RuntimeError("x")), source="main")  # must return, not raise


def test_install_returns_none_and_leaves_hooks_when_logs_dir_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / "cfg"
    # config_dir/"logs" mkdir -> NotADirectoryError
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(crashlog, "_ACTIVE", None)
    monkeypatch.setattr("genericmud.config.worlds.config_dir", lambda: blocker)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # auto-restored by monkeypatch
    original = sys.excepthook

    assert install_crash_handlers() is None
    assert sys.excepthook is original  # hooks untouched on failure
    assert crashlog._ACTIVE is None


def test_install_opens_log_and_sets_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(crashlog, "_ACTIVE", None)
    monkeypatch.setattr("genericmud.config.worlds.config_dir", lambda: tmp_path)
    monkeypatch.setattr(crashlog.faulthandler, "enable", lambda **_: None)  # don't touch real fd
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    original_sys = sys.excepthook

    log = install_crash_handlers()
    try:
        assert log is not None
        assert sys.excepthook is not original_sys  # main-thread hook installed
        # Crash dumps live with the diagnostic traces, apart from the session logs.
        assert log.path.exists() and log.path.parent == tmp_path / "logs" / "diagnostics"
    finally:
        if log is not None:
            log.stop()


def test_loop_handler_records_and_chains(tmp_path, monkeypatch):
    log = CrashLog(tmp_path / "crash.log", clock=_fixed_clock)
    log.start()
    monkeypatch.setattr(crashlog, "_ACTIVE", log)

    loop = asyncio.new_event_loop()
    chained = []
    try:
        loop.set_exception_handler(lambda _l, ctx: chained.append(ctx))  # prior handler
        install_loop_exception_handler(loop)

        def raises():
            raise ValueError("loop-boom")

        loop.call_soon(raises)
        loop.call_later(0.02, loop.stop)
        loop.run_forever()
    finally:
        loop.close()
        log.stop()

    text = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "crash source=asyncio" in text
    assert "ValueError: loop-boom" in text
    assert chained  # the handler we installed over is still invoked


def test_mark_exit_records_a_normal_close(tmp_path):
    log = CrashLog(tmp_path / "crash.log", clock=_fixed_clock)
    log.start()
    log.mark_exit()
    log.stop()
    lines = (tmp_path / "crash.log").read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "2026-06-24 12:00:00.123 exit normal"


def test_mark_exit_is_a_noop_before_start(tmp_path):
    path = tmp_path / "crash.log"
    CrashLog(path, clock=_fixed_clock).mark_exit()
    assert not path.exists()


def _run_main_with(ui_run, tmp_path, monkeypatch):
    """Run the app's entry point on the web path, with ``ui_run`` standing in for the UI."""
    import genericmud.__main__ as entry
    import genericmud.web_launcher as web_launcher

    log = CrashLog(tmp_path / "crash.log", clock=_fixed_clock)
    log.start()
    monkeypatch.setattr(crashlog, "install_crash_handlers", lambda: log)
    monkeypatch.setattr(crashlog, "_ACTIVE", log)
    monkeypatch.setattr(web_launcher, "run", ui_run)
    monkeypatch.setattr("genericmud.config.worlds.config_dir", lambda: tmp_path)  # its logs
    try:
        entry.main(["--web"])
    finally:
        log.stop()
    return (tmp_path / "crash.log").read_text(encoding="utf-8")


def test_main_marks_the_exit_when_the_ui_closes(tmp_path, monkeypatch):
    text = _run_main_with(lambda _args: None, tmp_path, monkeypatch)
    assert text.splitlines()[-1] == "2026-06-24 12:00:00.123 exit normal"


def test_main_leaves_no_exit_mark_when_the_ui_dies(tmp_path, monkeypatch):
    def dies(_args):
        raise RuntimeError("the window fell over")

    try:
        _run_main_with(dies, tmp_path, monkeypatch)
    except RuntimeError:
        pass
    text = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "exit normal" not in text
