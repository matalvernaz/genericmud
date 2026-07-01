"""Launch genericMud.

Native wxPython UI by default (Windows-first); ``--web`` uses the cross-platform
web/pywebview UI instead. The UI modules are imported lazily so the package
imports without a GUI toolkit present.

    py -m genericmud                      # native UI, no auto-connect
    py -m genericmud host 4000 [--tls]    # native UI, auto-connect a tab
    py -m genericmud host 4000 --web      # web UI
"""

from __future__ import annotations

import argparse


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="genericmud")
    parser.add_argument("host", nargs="?", default=None)
    parser.add_argument("port", nargs="?", type=int, default=4000)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--sounds", default=None, help="directory of sound files (MSP/soundpacks)")
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


def main(argv: list[str] | None = None) -> None:
    from genericmud.session.crashlog import install_crash_handlers

    install_crash_handlers()  # earliest chokepoint: covers both UIs and an import-time wx fault
    args = _parse_args(argv)
    if args.web:
        from genericmud.web_launcher import run

        run(args)
    else:
        recovery = _recover_pending_upgrade()  # before importing wx: roll back a bad swap first
        from genericmud.ui.wx_app import run

        run(args, recovery=recovery)


if __name__ == "__main__":
    main()
