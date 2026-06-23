"""One-shot soundpack setup: install a pack, derive its world, wire it up.

Ties :class:`PackStore` (install/enable/trust) and :func:`world_from_pack` (the
connection read out of the pack's own MUSHclient world file) together, so a single
call turns a downloaded pack into a ready-to-connect world with sound. UI-agnostic,
so it is testable headless; the caller persists the returned world and connects.
"""

from __future__ import annotations

import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from genericmud.config.worlds import World
from genericmud.packs.manifest import DIALECT_BY_SUFFIX, PackManifest
from genericmud.packs.store import PackError, PackStore
from genericmud.packs.world_import import world_from_pack

# Conventional load-script filenames, best-first.
_ENTRY_PREFERENCE = ("main.set", "main.lua", "main.xml", "start.set", "startup.set", "load.set")
_MIN_NAMED_STEM = 4  # only match a script "named after the pack" if the stem is this long


@dataclass
class SetupResult:
    manifest: PackManifest
    world: World | None  # parsed from the pack (host/port); None -> caller prompts
    enabled_for: str | None  # the world name the pack was enabled for, if any


def _count(path: Path, needle: str) -> int:
    try:
        return path.read_text(encoding="latin-1", errors="ignore").lower().count(needle)
    except OSError:
        return 0


def detect_entry(pack_dir: str | Path) -> str | None:
    """Best-guess load script for a multi-file pack, relative to ``pack_dir``.

    Real packs rarely match a single naming rule, so try, in order: a lone MUSHclient
    ``.MCL`` world when the pack has no VIPMud ``.set`` (a MUSHclient pack — the world
    ``<include>``s its plugins, so it wins over a stray ``main.*`` plugin); a conventional
    ``main.*``/``start.*`` name; a VIPMud ``.set`` that ``#load``s the others (the loader);
    a script named after the pack (e.g. ``toastush.xml`` in a toastush pack); finally a
    lone script. None means ambiguous — the caller explains why.
    Entry paths are POSIX (forward slashes) so they're portable; pathlib accepts
    them on every OS.
    """
    pack_dir = Path(pack_dir)
    scripts = sorted(p for p in pack_dir.rglob("*") if p.suffix.lower() in DIALECT_BY_SUFFIX)
    if not scripts:
        return None

    def rel(script: Path) -> str:
        return script.relative_to(pack_dir).as_posix()

    # A MUSHclient pack (no VIPMud .set entry) loads from its single .MCL world, which
    # <include>s the plugins. Prefer it over a stray main.* plugin; a VIPMud pack that
    # merely bundles a .MCL for connection info still picks its .set entry below.
    worlds = [s for s in scripts if s.suffix.lower() == ".mcl"]
    if len(worlds) == 1 and not any(s.suffix.lower() == ".set" for s in scripts):
        return rel(worlds[0])
    for preferred in _ENTRY_PREFERENCE:
        for script in scripts:
            if script.name.lower() == preferred:
                return rel(script)
    loaders = [(s, _count(s, "#load")) for s in scripts if s.suffix.lower() == ".set"]
    loaders = [(s, n) for s, n in loaders if n]  # .set files that #load others
    if loaders:  # the pack's main loader pulls in the most files
        loaders.sort(key=lambda pair: (-pair[1], rel(pair[0])))
        return rel(loaders[0][0])
    root = pack_dir.name.lower()  # a plugin named after the pack (toastush.xml in toastush/)
    for script in scripts:
        stem = script.stem.lower()
        if len(stem) >= _MIN_NAMED_STEM and stem in root:
            return rel(script)
    if len(scripts) == 1:
        return rel(scripts[0])
    return None


def entry_problem(pack_dir: str | Path) -> str:
    """A human-readable reason why no load script was found, for the UI to show.

    Distinguishes the common dead ends: a Windows installer bundle, a multi-plugin
    MUSHclient pack we can't auto-pick a load file for, or no script at all.
    """
    pack_dir = Path(pack_dir)
    suffixes = {f.suffix.lower() for f in pack_dir.rglob("*")}
    # Check for MUSHclient content BEFORE .exe: these packs bundle git/perl tooling
    # (.exe/.dll) alongside the real .mcl world + plugins, so .exe alone is misleading.
    if {".mcl", ".xml"} & suffixes:
        return "couldn't identify a single MUSHclient world file to load from this pack"
    if {".exe", ".dll"} & suffixes:
        return "this download is a Windows installer, not an importable soundpack"
    return "no soundpack script (.set/.lua/.xml) was found in this download"


def setup_pack(
    store: PackStore,
    source: str | Path,
    *,
    entry: str | None = None,
    sounds: str | None = None,
    trust: bool = True,
    origin: str | None = None,
) -> SetupResult:
    """Install ``source``, derive its world, and enable+trust it for that world.

    Installs the pack (``entry`` picks the load script of a multi-file pack), reads
    the connection from the pack's MUSHclient world file, and — if one is found —
    points it at ``sounds`` and enables the pack for that world. Trusts by default,
    since setting a pack up is an explicit vouch. ``origin`` records where the content
    came from (a URL) so the pack can be updated later. A pack with no world file (a
    bare VIPMud ``.set``) returns ``world=None`` so the caller can prompt for host/port.
    The returned ``world`` is not yet saved; the caller persists it and connects.
    """
    manifest = store.install(source, replace=True, entry=entry, origin=origin)
    world = world_from_pack(store.pack_dir(manifest.id))
    enabled_for = None
    if world is not None:
        if sounds:
            world.sounds = sounds
        store.enable(manifest.id, world.name)
        enabled_for = world.name
    if trust:
        store.trust(manifest.id)
    return SetupResult(manifest=manifest, world=world, enabled_for=enabled_for)


def update_pack(
    store: PackStore, pack_id: str, *, fetch: Callable[[str, Path], object]
) -> SetupResult:
    """Re-fetch a pack from its recorded ``origin`` URL and reinstall it in place.

    ``fetch(url, dest_zip)`` downloads the archive (injected, so this stays testable
    and network-free). Per-world enablement and trust are preserved — install
    ``replace=True`` rewrites only the pack content, not ``worlds.json``/``trust.json``.
    Raises if the pack has no origin (e.g. it was set up from a local folder).
    """
    manifest = store.manifest(pack_id)
    if not manifest.origin:
        raise PackError(f"{pack_id} has no recorded source to update from")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "update.zip"
        fetch(manifest.origin, archive)
        extracted = Path(tmp) / pack_id  # same id -> install replaces in place
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extracted)
        entry = detect_entry(extracted)
        if entry is None:
            raise PackError(f"the updated download for {pack_id} has no load script")
        return setup_pack(store, extracted, entry=entry, origin=manifest.origin)
