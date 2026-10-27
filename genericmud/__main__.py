"""Launch genericMud.

Native wxPython UI by default (Windows-first); ``--web`` uses the cross-platform
web/pywebview UI instead. The UI modules are imported lazily so the package
imports without a GUI toolkit present.

    py -m genericmud                      # native UI, no auto-connect
    py -m genericmud host 4000 [--tls]    # native UI, auto-connect a tab
    py -m genericmud host 4000 --encoding cp1251   # a MUD that doesn't speak UTF-8
    py -m genericmud host 4000 --web      # web UI
"""

from __future__ import annotations

import argparse

from genericmud.config.worlds import DEFAULT_PORT, parse_port
from genericmud.protocol.charset import AUTO, normalize_encoding


def _port_arg(value: str) -> int:
    try:
        return parse_port(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _encoding_arg(value: str) -> str:
    encoding = normalize_encoding(value)
    if encoding == AUTO and value.strip().lower() != AUTO:
        raise argparse.ArgumentTypeError(
            f"unsupported encoding {value!r}; try utf-8, cp1252, cp1251, koi8-r, gb18030 or big5"
        )
    return encoding


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="genericmud")
    parser.add_argument("host", nargs="?", default=None)
    parser.add_argument("port", nargs="?", type=_port_arg, default=DEFAULT_PORT)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--sounds", default=None, help="directory of sound files (MSP/soundpacks)")
    parser.add_argument(
        "--encoding", type=_encoding_arg, default=AUTO,
        help="the MUD's character encoding, e.g. utf-8, cp1251, koi8-r (default: auto)",
    )
    parser.add_argument("--web", action="store_true", help="use the web UI instead of native wx")
    return parser.parse_args(argv)


def _recover_pending_upgrade():
    """Verify a pending in-app upgrade and roll it back if the swap only partly landed.

    Runs before the native UI (wxPython/pygame) is imported: a half-overlaid install could
    otherwise crash on a mismatched extension load. Frozen Windows build only, and never
    raises -- a recovery fault must not stop the app from starting.
    """
    import sys

    if not getattr(sys, "frozen", False):
        return None
    try:
        from pathlib import Path

        from genericmud.update.upgrade_manager import recover_pending_upgrade

        return recover_pending_upgrade(Path(sys.executable).resolve().parent)
    except Exception:  # noqa: BLE001 - recovery is best-effort; never block startup
        return None


def _sweep_update_leftovers() -> None:
    """Delete the temp folders earlier in-app updates left behind (a day old or more).

    Each update leaves its download and a copy of the helper there. Frozen build only, and
    never raises: tidying up must not stop the app from starting.
    """
    import sys

    if not getattr(sys, "frozen", False):
        return
    try:
        from genericmud.update.self_update import cleanup_stale_workdirs

        cleanup_stale_workdirs()
    except Exception:  # noqa: BLE001 - best effort, like the recovery above
        pass


def _prune_leftover_files() -> None:
    """Delete the files an earlier version left in the app's contents folder.

    An in-app update lays the new build over the old one, so files a later build stopped
    shipping stay behind. Frozen build only, after a startup that rolled nothing back, and
    never raises: tidying up must not stop the app from starting.
    """
    import sys

    contents = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not contents:
        return
    try:
        from pathlib import Path

        from genericmud.update.upgrade_manager import prune_leftovers

        prune_leftovers(Path(contents))
    except Exception:  # noqa: BLE001 - best effort, like the sweep above
        pass


def _prune_old_logs() -> None:
    """Delete the app's own diagnostic and crash logs from earlier runs that are past keeping
    (diaglog.prune_old_logs). Never raises: tidying up must not stop the app starting."""
    try:
        from genericmud.logpaths import diagnostic_log_dir
        from genericmud.session.diaglog import prune_old_logs

        # The app's own logs moved into their own folder (see logpaths); migration has already
        # filed anything an older build left in the flat folder, so pruning looks there now.
        prune_old_logs(diagnostic_log_dir())
    except Exception:  # noqa: BLE001 - best effort, like the sweeps above
        pass


def main(argv: list[str] | None = None) -> None:
    from genericmud.logpaths import migrate_legacy_logs
    from genericmud.session.crashlog import install_crash_handlers, mark_normal_exit

    # Before either logger opens a file: logs written by an older build into the flat folder
    # are filed into worlds/ or diagnostics/ first, so this run's files land where they belong
    # and no legacy file is left mingling with them. Never raises, so a shared or read-only
    # folder can't stop the launch.
    try:
        migrate_legacy_logs()
    except Exception:  # noqa: BLE001 - housekeeping must never block startup
        pass
    install_crash_handlers()  # earliest chokepoint: covers both UIs and an import-time wx fault
    _prune_old_logs()
    args = _parse_args(argv)
    if args.web:
        from genericmud.web_launcher import run

        run(args)
    else:
        recovery = _recover_pending_upgrade()  # before importing wx: roll back a bad swap first
        _sweep_update_leftovers()
        if recovery is None:  # after a rollback the files on disk aren't this build's
            _prune_leftover_files()  # before wx loads, so an old wx's DLLs can go too
        from genericmud.ui.wx_app import run

        run(args, recovery=recovery)
    # Not reached when the run dies, so a native exception logged above this line was survived.
    mark_normal_exit()


if __name__ == "__main__":
    main()
