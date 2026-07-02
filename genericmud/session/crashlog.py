"""Crash capture: a durable record of the traceback when the app dies.

The diagnostic log (:mod:`genericmud.session.diaglog`) traces the sound path and
survives a crash, but it only knows the sound chain. An unhandled exception in a wx
handler, a worker thread, an asyncio coroutine, or a native pygame fault leaves no
traceback anywhere. The dev host can't run the Windows UI or build the exe, so a
traceback the app didn't write down is gone.

This routes the four escape routes an error can take into one ``crash-<ts>.log``
beside the diagnostic log:

* ``sys.excepthook``             -- uncaught on the main thread (wx forwards uncaught
                                    event-handler exceptions here too)
* ``threading.excepthook``       -- uncaught on a worker thread (the asyncio engine
                                    runs on a daemon thread)
* ``loop.set_exception_handler`` -- raised inside a coroutine / loop callback
* ``faulthandler``               -- a native fault, e.g. a pygame mixer segfault

A clean run leaves a banner-only file; a real break appends a traceback below it, so a
recent crash log that holds a traceback is itself the "it broke" signal. Every write is
defensive (as in diaglog): a logging fault must never crash the app it observes, and
each Python hook chains to the one it replaced so a dev running from a terminal still
sees the traceback on stderr.
"""

from __future__ import annotations

import faulthandler
import os
import platform
import sys
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, TextIO


def _app_version() -> str:
    try:
        from genericmud import __version__

        return __version__  # single source of truth, always present (even run from source)
    except Exception:  # noqa: BLE001 - version lookup is best-effort diagnostics
        return "unknown"


class CrashLog:
    """Append-only crash record: a banner, then one traceback block per uncaught error.

    A lock serializes writes, which can arrive on any thread a crash reaches (main, the
    asyncio daemon thread, a worker). ``record`` is a no-op before :meth:`start`, and a
    write fault is swallowed -- crash logging must never raise on the way down.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = datetime.now) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = Lock()
        self._handle: TextIO | None = None

    def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self._path, "a", encoding="utf-8", buffering=1)  # line-buffered
        self._write(
            f"{self._stamp()} banner version={_app_version()} "
            f"frozen={bool(getattr(sys, 'frozen', False))} "
            f"platform={platform.platform()} python={platform.python_version()} "
            f"pid={os.getpid()}\n"
        )

    def record(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_tb: Any,
        *,
        source: str,
    ) -> None:
        """Append a timestamped traceback block. No-op before start; never raises."""
        if self._handle is None:
            return
        header = f"{self._stamp()} crash source={source} thread={threading.current_thread().name}\n"
        body = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        with self._lock:
            self._write(header + body + "\n")

    def handle(self) -> TextIO | None:
        """The open file, for handing to ``faulthandler.enable``; ``None`` before start."""
        return self._handle

    def stop(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    @property
    def path(self) -> Path:
        return self._path

    def _stamp(self) -> str:
        return self._clock().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # to the millisecond

    def _write(self, text: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(text)
            self._handle.flush()  # survive the crash that triggered us
        except OSError:
            return  # a logging fault must not crash the app it observes


_ACTIVE: CrashLog | None = None


def install_crash_handlers() -> CrashLog | None:
    """Open the per-run crash log and route every uncaught-error path into it.

    Returns the live :class:`CrashLog` (also held in a module global so the faulthandler
    file descriptor outlives this call), or ``None`` if the log can't be opened -- the
    app then runs without crash capture rather than failing to start, mirroring
    :func:`genericmud.session.diaglog.make_diagnostic_log`.
    """
    global _ACTIVE
    from genericmud.config.worlds import config_dir  # local: avoid an import cycle at load

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        log = CrashLog(config_dir() / "logs" / f"crash-{stamp}.log")
        log.start()
    except OSError:
        return None

    _ACTIVE = log
    _install_python_hooks(log)
    _install_faulthandler(log)
    return log


def install_loop_exception_handler(loop) -> None:
    """Route asyncio coroutine/callback exceptions on *loop* into the active crash log.

    No-op if crash capture failed to install. Delegates to the handler already on the
    loop (or asyncio's default) after recording, so the loop's normal logging is kept.
    """
    log = _ACTIVE
    if log is None:
        return

    previous = loop.get_exception_handler()

    def _handler(loop_, context: dict) -> None:
        exc = context.get("exception")
        if exc is not None:
            log.record(type(exc), exc, exc.__traceback__, source="asyncio")
        if previous is None:
            loop_.default_exception_handler(context)
        else:
            previous(loop_, context)

    loop.set_exception_handler(_handler)


def _install_faulthandler(log: CrashLog) -> None:
    handle = log.handle()
    if handle is None:
        return
    try:
        faulthandler.enable(file=handle)
    except (OSError, ValueError):
        return  # no usable fd (e.g. an odd frozen build); the Python hooks still cover us


def _install_python_hooks(log: CrashLog) -> None:
    previous_sys = sys.excepthook

    def _sys_hook(exc_type, exc_value, exc_tb) -> None:
        log.record(exc_type, exc_value, exc_tb, source="main")
        _chain(previous_sys, exc_type, exc_value, exc_tb)

    sys.excepthook = _sys_hook

    previous_thread = threading.excepthook

    def _thread_hook(args) -> None:
        log.record(args.exc_type, args.exc_value, args.exc_traceback, source="thread")
        _chain(previous_thread, args)

    threading.excepthook = _thread_hook


def _chain(previous: Callable, *call_args: Any) -> None:
    """Call the replaced hook, swallowing its failure -- stderr may be absent (windowed build)."""
    try:
        previous(*call_args)
    except Exception:  # noqa: BLE001 - we already recorded; the chained hook is best-effort
        return
