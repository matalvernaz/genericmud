"""Moving each saved world's files off the ASCII-only names older builds filed them under.

A world's own data lives under its name: its Automation Manager rules and scripts
(``userpacks/<name>/``), its room map (``maps/<name>.json``) and its saved pack variables
(``state/<name>-vars.json``). Before :func:`~genericmud.safepath.world_component` kept
every alphabet, names were filed ASCII-only, so "Café" went under "Caf" and every
all-Cyrillic or all-CJK name under one shared "session". After an update that data would
sit under the old name, so once per launch, before any session opens, each saved world's
files move to the name it is filed under now.

Ownership is only decided here, where every saved world's name is known. An old name
that another saved world is filed under today is that world's own and is never touched;
names compare case-insensitively, because on NTFS and APFS "Caf" and "CAF" are one folder.
An old name that exactly one saved world used moves to it. An old name that several saved
worlds shared (two all-Cyrillic worlds, or "Café" and "Cafè") is copied to each of them,
and once every one has its copy the shared original is set aside under a name no world
can have: nothing anyone set up disappears, the worlds stop sharing, and a world created
later can't inherit rules it never had.

A copy goes to a hidden temporary name first and takes its real name only when complete,
so an interrupted copy is never mistaken for a finished one and the next launch retries.

This runs before the window can open a session, on the UI thread. It moves folders by
renaming, which is instant; the only copying is of a folder several worlds shared under
an older build, which holds the rules and scripts people made and the sounds they picked
for them, not soundpacks, which live elsewhere.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from genericmud.safepath import sanitize_component, world_component

# (folder under the config dir, suffix after the world's name) for each kind of per-world
# data. Keep in step with EngineApp.user_rules_dir, _map_path and _pack_vars_path.
WORLD_FILE_ROOTS = (("userpacks", ""), ("maps", ".json"), ("state", "-vars.json"))
# Appended to a shared folder once every world that used it has its own copy. world_component
# never keeps "~", so no world can be filed under the retired name and adopt it.
RETIRED_MARK = "~pre-migration"
_MAX_RETIRED_NAMES = 100


def migrate_world_files(config: Path, world_names: Iterable[str]) -> list[str]:
    """Move or copy saved worlds' files to their current names; one line per change.

    Never raises. A move or copy that fails is reported and leaves the files where they
    were, where the session's own fallback (``EngineApp._world_path``) can still find a
    single owner's folder, and the next launch tries again.
    """
    names = list(dict.fromkeys(world_names))
    filed_now = {world_component(name).casefold() for name in names}
    # Where the older build filed each name: sanitize_component with its own fallback,
    # which is exactly where every all-non-Latin name's data went.
    claimants: dict[str, list[str]] = {}
    for name in names:
        old = sanitize_component(name)
        if old.casefold() != world_component(name).casefold():
            claimants.setdefault(old, []).append(name)

    report: list[str] = []
    for folder, suffix in WORLD_FILE_ROOTS:
        root = Path(config) / folder
        for old, owners in claimants.items():
            if old.casefold() in filed_now:
                continue  # another saved world is filed under this name today: its own
            source = root / f"{old}{suffix}"
            if not source.exists():
                continue
            targets = [root / f"{world_component(name)}{suffix}" for name in owners]
            try:
                if len(owners) == 1:
                    if not targets[0].exists():
                        source.rename(targets[0])
                        report.append(f"moved {folder}/{source.name} to {targets[0].name}")
                    continue
                for target in targets:
                    if not target.exists():
                        _copy_atomically(source, target)
                        report.append(f"copied {folder}/{source.name} to {target.name}")
                retired = _retired_name(root, old, suffix)
                source.rename(retired)
                report.append(f"set aside {folder}/{source.name} as {retired.name}")
            except OSError as error:
                report.append(f"left {folder}/{source.name} where it was: {error}")
    return report


def _copy_atomically(source: Path, target: Path) -> None:
    """Copy under a hidden temporary name, and give it the real name only once complete."""
    temporary = target.with_name(f".{target.name}.copying")
    _remove(temporary)  # left over from a launch that died mid-copy
    try:
        if source.is_dir():
            shutil.copytree(source, temporary)
        else:
            shutil.copy2(source, temporary)
        temporary.rename(target)
    except OSError:
        _remove(temporary)
        raise


def _retired_name(root: Path, old: str, suffix: str) -> Path:
    for attempt in range(_MAX_RETIRED_NAMES):
        mark = RETIRED_MARK if attempt == 0 else f"{RETIRED_MARK}-{attempt + 1}"
        candidate = root / f"{old}{mark}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError(f"no free name to set {old}{suffix} aside")


def _remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        pass
