"""``python -m genericmud.packs`` — manage installed soundpacks from the terminal.

A keyboard- and screen-reader-friendly front end to :class:`PackStore` while the
in-app manager UI is pending. Subcommands: list, install, enable, disable,
uninstall, and conflicts (a dry-run activation that reports load failures and
binding clashes for a world).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from genericmud.automation.engine import AutomationEngine
from genericmud.config.worlds import config_dir
from genericmud.packs import PackError, PackStore, activate_world


def _store(args: argparse.Namespace) -> PackStore:
    root = Path(args.root) if args.root else config_dir() / "soundpacks"
    return PackStore(root)


def _cmd_list(args: argparse.Namespace) -> int:
    store = _store(args)
    packs = store.installed()
    if not packs:
        print("No soundpacks installed.")
        return 0
    for manifest in sorted(packs, key=lambda p: p.id):
        targets = ", ".join(manifest.worlds) if manifest.worlds else "any"
        trust = "trusted" if store.is_trusted(manifest.id) else "UNTRUSTED"
        print(f"{manifest.id}  ({manifest.dialect}, v{manifest.version}, {trust})  "
              f"{manifest.name}  [targets: {targets}]")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    manifest = _store(args).install(
        args.source, world=args.world, replace=args.replace, trust=args.trust
    )
    enabled = f" and enabled for {args.world}" if args.world else ""
    trust = " (trusted)" if args.trust else " (untrusted — run 'trust' to auto-load it)"
    print(f"Installed {manifest.id} ({manifest.dialect}){enabled}{trust}.")
    return 0


def _cmd_trust(args: argparse.Namespace) -> int:
    _store(args).trust(args.id)
    print(f"Trusted {args.id}; it will auto-load on connect.")
    return 0


def _cmd_untrust(args: argparse.Namespace) -> int:
    _store(args).untrust(args.id)
    print(f"Untrusted {args.id}; it stays installed but won't auto-load.")
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    _store(args).enable(args.id, args.world)
    print(f"Enabled {args.id} for {args.world}.")
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    _store(args).disable(args.id, args.world)
    print(f"Disabled {args.id} for {args.world}.")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    _store(args).uninstall(args.id)
    print(f"Uninstalled {args.id}.")
    return 0


def _cmd_conflicts(args: argparse.Namespace) -> int:
    store = _store(args)
    result = activate_world(store, args.world, AutomationEngine())
    enabled = store.enabled(args.world)
    print(f"{len(enabled)} pack(s) enabled for {args.world}; {len(result.loaded)} loaded clean.")
    for pack_id in result.skipped_untrusted:
        print(f"  SKIPPED {pack_id}: not trusted (run 'trust {pack_id}')")
    for pack_id, error in result.failed.items():
        print(f"  FAILED {pack_id}: {error}")
    if not result.conflicts:
        print("No binding conflicts.")
    for conflict in result.conflicts:
        sources = ", ".join(conflict.sources)
        print(f"  CONFLICT {conflict.kind} {conflict.token!r} bound by {sources}")
    return 1 if (result.failed or result.conflicts) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="genericmud.packs", description="Manage soundpacks.")
    parser.add_argument("--root", help="store root (default: ~/.genericmud/soundpacks)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list installed packs").set_defaults(func=_cmd_list)

    install = sub.add_parser("install", help="install a pack dir or a bare script file")
    install.add_argument("source")
    install.add_argument("--world", help="also enable the pack for this MUD")
    install.add_argument("--replace", action="store_true", help="update if already installed")
    install.add_argument("--trust", action="store_true", help="trust now (auto-load on connect)")
    install.set_defaults(func=_cmd_install)

    enable = sub.add_parser("enable", help="enable a pack for a world")
    enable.add_argument("id")
    enable.add_argument("world")
    enable.set_defaults(func=_cmd_enable)

    disable = sub.add_parser("disable", help="disable a pack for a world")
    disable.add_argument("id")
    disable.add_argument("world")
    disable.set_defaults(func=_cmd_disable)

    uninstall = sub.add_parser("uninstall", help="remove a pack entirely")
    uninstall.add_argument("id")
    uninstall.set_defaults(func=_cmd_uninstall)

    trust = sub.add_parser("trust", help="trust a pack so it auto-loads on connect")
    trust.add_argument("id")
    trust.set_defaults(func=_cmd_trust)

    untrust = sub.add_parser("untrust", help="stop a pack auto-loading (keeps it installed)")
    untrust.add_argument("id")
    untrust.set_defaults(func=_cmd_untrust)

    conflicts = sub.add_parser("conflicts", help="dry-run activate a world; report clashes")
    conflicts.add_argument("world")
    conflicts.set_defaults(func=_cmd_conflicts)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (PackError, OSError) as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
