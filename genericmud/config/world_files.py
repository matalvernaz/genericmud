"""Moving each saved world's files off the ASCII-only names older builds filed them under.

A world's own data lives under its name: its Automation Manager rules and scripts
(``userpacks/<name>/``), its room map (``maps/<name>.json``) and its saved pack variables
(``state/<name>-vars.json``). Before :func:`~genericmud.safepath.world_component` kept
every alphabet, names were filed ASCII-only, so "Café" went under "Caf" and every
all-Cyrillic or all-CJK name under one shared "session". After an update that data would
sit under the old name, so once per launch, before any session opens, each saved world's
files move to the name it is filed under now.

Ownership is only decided here, where every saved world's name is known. An old name
that another saved world is filed under today is that world's own and is never touched.
An old name that exactly one saved world used moves to it. An old name that several saved
worlds shared (two all-Cyrillic worlds, or "Café" and "Cafè") is copied to each of them:
nothing anyone set up disappears, and from then on they no longer share.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from genericmud.safepath import sanitize_component, world_component

# (folder under the config dir, suffix after the world's name) for each kind of per-world
# data. Keep in step with EngineApp.user_rules_dir, _map_path and _pack_vars_path.
WORLD_FILE_ROOTS = (("userpacks", ""), ("maps", ".json"), ("state", "-vars.json"))


def migrate_world_files(config: Path, world_names: Iterable[str]) -> list[str]:
    """Move or copy saved worlds' files to their current names; one line per change.

    Never raises. A move or copy that fails is reported and leaves the files where they
    were, where the session's own fallback (``EngineApp._world_path``) can still find a
    single owner's folder, and the next launch tries again.
    """
    names = list(dict.fromkeys(world_names))
    filed_now = {world_component(name) for name in names}
    # Where the older build filed each name: sanitize_component with its own fallback,
    # which is exactly where every all-non-Latin name's data went.
    claimants: dict[str, list[str]] = {}
    for name in names:
        old = sanitize_component(name)
        if old != world_component(name):
            claimants.setdefault(old, []).append(name)

    report: list[str] = []
    for folder, suffix in WORLD_FILE_ROOTS:
        root = Path(config) / folder
        for old, owners in claimants.items():
            if old in filed_now:
                continue  # another saved world is filed under this name today: its own
            source = root / f"{old}{suffix}"
            if not source.exists():
                continue
            targets = [root / f"{world_component(name)}{suffix}" for name in owners]
            pending = [target for target in targets if not target.exists()]
            try:
                if len(owners) == 1 and pending:
                    source.rename(pending[0])
                    report.append(f"moved {folder}/{source.name} to {pending[0].name}")
                else:
                    for target in pending:
                        _copy(source, target)
                        report.append(f"copied {folder}/{source.name} to {target.name}")
            except OSError as error:
                report.append(f"left {folder}/{source.name} where it was: {error}")
    return report


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
