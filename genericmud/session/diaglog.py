"""Diagnostic trace: a durable, append-only record of the sound path.

Soundpack failures are build-blind — the dev host can't run the Windows UI or build
the exe, so the only knowledge of what happened at runtime is what the running app
wrote down. The earlier diagnostics only *spoke* the first failure and only for a
missing/undecodable file, so a chain that breaks anywhere earlier (a pack that loads
inert, a backend that fell back to the no-op poster, a gain computed to zero) left no
trace at all.

This logs one line per stage of the chain — backend selection, pack load + trigger
counts, trigger fire, path resolve, effective gain, backend play — to a file under the
config logs dir. A healthy cue logs ``trigger.fire -> play.entry -> play.resolve ->
sink.gain -> backend.play``; diagnosis is reading back where the chain stops.

Modeled on :class:`~genericmud.session.log.SessionLogger`: append, flush per write so a
crash (e.g. a pygame mixer fault mid-init) keeps everything before it. Always-on and
low-volume — these events fire per cue/connect, not per visible line. A hard byte cap
stops a runaway trigger from filling the disk, and every write is defensive: a logging
fault must never crash the app it is observing.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

_MAX_BYTES = 10 * 1024 * 1024  # ~10 MB: a runaway cue can't fill the disk
_TRUNCATED = "-- diagnostic log truncated (size cap reached)\n"


def _format_value(value: Any) -> str:
    """Render one field value on a single line; empty strings show as ``''``."""
    text = str(value)
    if not text:
        return "''"
    return text.replace("\n", " ").replace("\r", " ")


def _format_fields(fields: dict[str, Any]) -> str:
    return " ".join(f"{key}={_format_value(value)}" for key, value in fields.items())


class DiagnosticLog:
    """Append-only stage trace, one event per line: ``<ts> <stage> k=v k=v``.

    Safe to share across the per-tab engines (they run on one asyncio loop thread; the
    banner is written from the wx thread before any session starts) — a lock serializes
    writes. ``event`` is a no-op before :meth:`start` and after the size cap, so callers
    never need to guard for those.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = datetime.now) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = Lock()
        self._handle = None
        self._written = 0
        self._capped = False

    def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self._path, "a", encoding="utf-8")

    def event(self, stage: str, **fields: Any) -> None:
        """Record one stage of the chain. Fields render as space-separated ``k=v``."""
        if self._handle is None or self._capped:
            return
        stamp = self._clock().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # to the millisecond
        suffix = _format_fields(fields)
        record = f"{stamp} {stage} {suffix}\n" if suffix else f"{stamp} {stage}\n"
        with self._lock:
            if self._capped or self._handle is None:
                return
            try:
                self._handle.write(record)
                if self._written + len(record) >= _MAX_BYTES:
                    self._handle.write(_TRUNCATED)
                    self._capped = True
                self._handle.flush()  # survive a crash; events are infrequent
            except OSError:
                return  # a logging fault must not crash the app it observes
            self._written += len(record)

    def stop(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    @property
    def path(self) -> Path:
        return self._path


def make_diagnostic_log() -> DiagnosticLog | None:
    """Create + start the per-run diagnostic log under the config logs dir, banner written.

    Returns ``None`` if the log can't be opened, so the app runs unobserved rather than
    failing to start. The filename is stamped once per process; tabs share the file for a
    single chronological story.
    """
    from genericmud.config.worlds import config_dir  # local: avoid an import cycle at load

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        diag = DiagnosticLog(config_dir() / "logs" / f"diagnostic-{stamp}.log")
        diag.start()
    except OSError:
        return None
    write_banner(diag)
    return diag


def write_banner(diag: DiagnosticLog) -> None:
    """Record the environment up front — it alone answers 'is audio even possible here?'."""
    diag.event(
        "banner",
        version=_app_version(),
        frozen=bool(getattr(sys, "frozen", False)),
        platform=platform.platform(),
        python=platform.python_version(),
        pygame=_pygame_status(),
    )


def _app_version() -> str:
    try:
        from genericmud import __version__

        return __version__  # single source of truth, always present (even run from source)
    except Exception:  # noqa: BLE001 - version lookup is best-effort diagnostics
        return "unknown"


def _pygame_status() -> str:
    """Whether pygame imports here — a no-pygame frozen build is candidate A for silence."""
    try:
        import pygame
    except Exception as exc:  # noqa: BLE001 - report why audio is unavailable, don't raise
        return f"import-failed: {type(exc).__name__}"
    return getattr(getattr(pygame, "version", None), "ver", "imported")
