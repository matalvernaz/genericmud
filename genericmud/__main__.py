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


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.web:
        from genericmud.web_launcher import run
    else:
        from genericmud.ui.wx_app import run
    run(args)


if __name__ == "__main__":
    main()
