"""One-shot soundpack setup: install a pack, derive its world, wire it up.

Ties :class:`PackStore` (install/enable/trust) and :func:`world_from_pack` (the
connection read out of the pack's own MUSHclient world file) together, so a single
call turns a downloaded pack into a ready-to-connect world with sound. UI-agnostic,
so it is testable headless; the caller persists the returned world and connects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from genericmud.config.worlds import World
from genericmud.packs.manifest import DIALECT_BY_SUFFIX, PackManifest
from genericmud.packs.store import PackStore
from genericmud.packs.world_import import world_from_pack

_ENTRY_PREFERENCE = ("main.set", "main.lua", "main.xml")  # the conventional load script


@dataclass
class SetupResult:
    manifest: PackManifest
    world: World | None  # parsed from the pack (host/port); None -> caller prompts
    enabled_for: str | None  # the world name the pack was enabled for, if any


def detect_entry(pack_dir: str | Path) -> str | None:
    """Best-guess load script for a multi-file pack, relative to ``pack_dir``.

    A conventional ``main.*`` wins; otherwise a lone script; otherwise None
    (ambiguous — the caller asks). Lets the setup flow handle real packs that ship
    a ``main.set`` alongside many helper scripts.
    """
    pack_dir = Path(pack_dir)
    scripts = sorted(p for p in pack_dir.rglob("*") if p.suffix.lower() in DIALECT_BY_SUFFIX)
    for preferred in _ENTRY_PREFERENCE:
        for script in scripts:
            if script.name.lower() == preferred:
                return str(script.relative_to(pack_dir))
    if len(scripts) == 1:
        return str(scripts[0].relative_to(pack_dir))
    return None


def setup_pack(
    store: PackStore,
    source: str | Path,
    *,
    entry: str | None = None,
    sounds: str | None = None,
    trust: bool = True,
) -> SetupResult:
    """Install ``source``, derive its world, and enable+trust it for that world.

    Installs the pack (``entry`` picks the load script of a multi-file pack), reads
    the connection from the pack's MUSHclient world file, and — if one is found —
    points it at ``sounds`` and enables the pack for that world. Trusts by default,
    since setting a pack up is an explicit vouch. A pack with no world file (a bare
    VIPMud ``.set``) returns ``world=None`` so the caller can prompt for host/port.
    The returned ``world`` is not yet saved; the caller persists it and connects.
    """
    manifest = store.install(source, replace=True, entry=entry)
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
