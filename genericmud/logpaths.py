"""Where each kind of log file lives, and how logs written before the split are moved.

Two audiences were sharing one folder. A player looking for a session log had no business
scrolling past diagnostic traces and crash dumps, and whoever is asked for a diagnostic
file wants that one kind without the session logs around it. So the folder now has two:

``logs/worlds/``
    what a MUD said, and -- if the world asks for it -- what the player typed.

``logs/diagnostics/``
    the per-run diagnostic trace and any crash dumps; for working out why something went
    wrong, rather than for reading back what happened in the game.

Everything is decided here rather than in the two loggers, so "where do logs go" has one
answer to read and one place to change.
"""

from __future__ import annotations

from pathlib import Path

LOG_SUFFIX = ".log"
# How the two kinds are told apart: by what the file says about itself, not by its name. Both
# diagnostic loggers write a "banner" line first; a session log starts by saying so. Names
# can't decide it -- a world called "crash" produces crash-20260101-120000.log in session
# mode, which is exactly a crash log's shape -- and the first line can.
_DIAGNOSTIC_MARKER = " banner "
_SESSION_MARKER = "Session started"
_PROBE_CHARS = 4096
_MAX_COLLISION_ATTEMPTS = 1000


def logs_root() -> Path:
    """The folder both kinds of log live under."""
    from genericmud.config.worlds import config_dir  # local: keeps this module dependency-free

    return config_dir() / "logs"


def world_log_dir() -> Path:
    """Session logs: one file per session, day or world, depending on the world's setting."""
    return logs_root() / "worlds"


def diagnostic_log_dir() -> Path:
    """Diagnostic traces and crash dumps."""
    return logs_root() / "diagnostics"


def migrate_legacy_logs() -> int:
    """Move logs sitting loose in the ``logs`` folder into the right one of the two.

    Runs on every start and is harmless once there is nothing to do: only ``.log`` files
    directly in ``logs`` are considered, and the common case afterwards is an empty scan.
    Because it is a scan rather than a one-off flag, a log copied back into the folder later
    is filed correctly too.

    A file this app didn't write -- or one whose first line doesn't identify it -- is left
    exactly where it is. Guessing would risk filing a stranger's file, or a log the player
    keeps there deliberately, into a folder they'd never look in. A file that can't be moved
    (open in another program, no permission) is left for the next start rather than failing
    the launch. Returns how many moved.
    """
    root = logs_root()
    if not root.is_dir():
        return 0
    moved = 0
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() != LOG_SUFFIX:
            continue
        kind = _kind_of(path)
        if kind is None:
            continue
        destination = diagnostic_log_dir() if kind == "diagnostic" else world_log_dir()
        target = _unused_path(destination, path.name)
        if target is None:
            continue
        try:
            destination.mkdir(parents=True, exist_ok=True)
            path.replace(target)  # a rename: a moved log is never half-written
        except OSError:
            continue
        moved += 1
    return moved


def log_count(directory: Path) -> int:
    """How many ``.log`` files ``directory`` holds directly, or 0 if it can't be read."""
    try:
        return sum(
            1 for entry in directory.iterdir() if entry.is_file() and entry.suffix == LOG_SUFFIX
        )
    except OSError:
        return 0


def _kind_of(path: Path) -> str | None:
    """``"diagnostic"``, ``"session"``, or None for a file that says nothing about itself."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(_PROBE_CHARS)
    except OSError:
        return None
    if not head:
        return None
    first = head.splitlines()[0]
    if _DIAGNOSTIC_MARKER in first:
        return "diagnostic"
    if _SESSION_MARKER in first:
        return "session"
    return None


def _unused_path(directory: Path, name: str) -> Path | None:
    """``directory/name``, or a numbered variant if that name is taken; None if there are too
    many. Never a path that already exists: moving onto one would overwrite a log the player
    might still want."""
    target = directory / name
    if not target.exists():
        return target
    stem = name[: -len(LOG_SUFFIX)] if name.lower().endswith(LOG_SUFFIX) else name
    for number in range(1, _MAX_COLLISION_ATTEMPTS):
        candidate = directory / f"{stem}-{number}{LOG_SUFFIX}"
        if not candidate.exists():
            return candidate
    return None
