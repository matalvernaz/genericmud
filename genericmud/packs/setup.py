"""One-shot soundpack setup: install a pack, derive its world, wire it up.

Ties :class:`PackStore` (install/enable/trust) and :func:`world_from_pack` (the
connection read out of the pack's own MUSHclient world file) together, so a single
call turns a downloaded pack into a ready-to-connect world with sound. UI-agnostic,
so it is testable headless; the caller persists the returned world and connects.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from genericmud.config.worlds import World
from genericmud.packs import manifest_sync
from genericmud.packs.manifest import DIALECT_BY_SUFFIX, PackManifest
from genericmud.packs.manifest_sources import ManifestSource
from genericmud.packs.store import PackError, PackStore
from genericmud.packs.world_import import world_from_pack

# Conventional load-script filenames, best-first.
_ENTRY_PREFERENCE = ("main.set", "main.lua", "main.xml", "start.set", "startup.set", "load.set")
_MIN_NAMED_STEM = 4  # only match a script "named after the pack" if the stem is this long

# Dialects where "trusted" grants full-stdlib code execution (os/io) on connect. Setting a pack
# up -- especially a one-click vault download -- is a weak vouch, so these are NOT auto-trusted:
# the user must consciously enable them in the Pack Manager. Sandboxed dialects (native Lua,
# VIPMud .set) are safe to auto-trust; there "trusted" only means auto-run, I/O stays confined.
_CODE_EXEC_DIALECTS = frozenset({"mushclient"})


def _normalize_name(text: str) -> str:
    """Lowercase, strip non-alphanumerics: 'star conquest' == 'Star Conquest' == 'StarConquest'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


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


def detect_entry(pack_dir: str | Path, *, mud_name: str | None = None) -> str | None:
    """Best-guess load script for a multi-file pack, relative to ``pack_dir``.

    Real packs rarely match a single naming rule, so try, in order: the MUSHclient ``.MCL``
    world that ``<include>``s the most plugins, when the pack has no VIPMud ``.set`` (a
    MUSHclient pack — the world wins over a stray ``main.*`` plugin, and over a bundle's
    extra captures/sandbox worlds); a conventional ``main.*``/``start.*`` name; a script
    named after the MUD (``mud_name``: VIPMud loaders are named for the MUD, e.g.
    ``star conquest.set`` for Star Conquest); a VIPMud ``.set`` that ``#load``s the others,
    ranked shallowest-first (the loader sits above the ``Scripts/`` dir it pulls in — a
    ``#ForAll {list} {#load {Scripts\\%I.set}}`` reads as one literal ``#load`` but drives
    many, so a deeper script that ``#load``s from inside a reload alias must not outrank it);
    a script named after the pack dir; finally a lone script. None means ambiguous — the
    caller explains why.
    Entry paths are POSIX (forward slashes) so they're portable; pathlib accepts
    them on every OS.
    """
    pack_dir = Path(pack_dir)
    scripts = sorted(p for p in pack_dir.rglob("*") if p.suffix.lower() in DIALECT_BY_SUFFIX)
    if not scripts:
        return None

    def rel(script: Path) -> str:
        return script.relative_to(pack_dir).as_posix()

    # A MUSHclient pack (no VIPMud .set entry) loads from a .MCL world, which <include>s
    # the plugins. Prefer it over a stray main.* plugin. Among several .MCL (a full
    # MUSHclient-install bundle also ships captures/sandbox worlds), pick the one that
    # <include>s the most plugins -- the soundpack world. A VIPMud pack that merely
    # bundles a .MCL for connection info has a .set, so it picks its .set entry below.
    worlds = [s for s in scripts if s.suffix.lower() == ".mcl"]
    if worlds and not any(s.suffix.lower() == ".set" for s in scripts):
        return rel(max(worlds, key=lambda w: _count(w, "<include")))
    for preferred in _ENTRY_PREFERENCE:
        for script in scripts:
            if script.name.lower() == preferred:
                return rel(script)
    if mud_name:  # a VIPMud loader is named after the MUD ("star conquest.set" -> Star Conquest)
        target = _normalize_name(mud_name)
        for script in scripts:
            if target and _normalize_name(script.stem) == target:
                return rel(script)
    loaders = [(s, _count(s, "#load")) for s in scripts if s.suffix.lower() == ".set"]
    loaders = [(s, n) for s, n in loaders if n]  # .set files that #load others
    if loaders:  # shallowest first: the entry loader sits above the Scripts/ dir it pulls in
        loaders.sort(key=lambda pair: (rel(pair[0]).count("/"), -pair[1], rel(pair[0])))
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
    # Auto-trust the vouch -- but never a code-executing dialect (MUSHclient runs the full Lua
    # stdlib when trusted). A one-click vault download is too weak a vouch to grant os/io on
    # connect, so such packs install enabled-but-untrusted; the user trusts them deliberately.
    if trust and manifest.dialect not in _CODE_EXEC_DIALECTS:
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


def setup_pack_from_manifest(
    store: PackStore, source: ManifestSource, *, progress=None, download=None
) -> SetupResult:
    """Install or update a manifest-style pack (e.g. Mush-Z) by syncing its files in place.

    Syncs the pack's file tree from its remote manifest into ``packs_dir/<id>`` — a fresh
    install downloads everything, a re-run fetches only what changed — then registers and
    enables it for its curated world. Deliberately NOT auto-trusted: like any MUSHclient pack
    it runs its own Lua, so the caller must ``store.trust(id)`` before it auto-loads on connect.
    ``progress(done, total, relpath)`` reports per file; ``download`` is injectable for tests.
    Re-running is the update path (same call, only changed files move).
    """
    kwargs = {"progress": progress}
    if download is not None:
        kwargs["download"] = download
    manifest_sync.sync(source, store.pack_dir(source.id), **kwargs)
    _write_pack_toml(store.pack_dir(source.id), source)
    manifest = store.register(source.id, origin=source.manifest_url)
    world = replace(source.world)  # a copy; the caller persists/edits it, don't mutate the registry
    store.enable(manifest.id, world.name)
    return SetupResult(manifest=manifest, world=world, enabled_for=world.name)


def _write_pack_toml(pack_dir: Path, source: ManifestSource) -> None:
    """Write the pack.toml that lets the store register a synced tree (id/dialect/entry/origin)."""
    lines = [
        f"id = {_toml_str(source.id)}",
        f"name = {_toml_str(source.name)}",
        f"dialect = {_toml_str(source.dialect)}",
        f"entry = {_toml_str(source.entry)}",
        f"origin = {_toml_str(source.manifest_url)}",
    ]
    (Path(pack_dir) / "pack.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _toml_str(value: str) -> str:
    """A double-quoted TOML string literal (escape backslash then quote)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
