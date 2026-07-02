"""Manifest-style pack sources: soundpacks served as an HTTP file tree + a manifest.

Some soundpacks ship only a Windows installer (no vault ``.zip`` mirror) yet expose
their whole content as individual files under one base URL, described by a manifest
their own updater fetches. Mush-Z (the Alter Aeon MUSHclient soundpack) is the first:
``https://www.mush-z.com/mush/`` serves every file, and ``update_everything.lst.gz``
lists them as ``<hash> <size> <relative/path>``. Pointing genericMud at that lets one
routine (:mod:`genericmud.packs.manifest_sync`) both install the pack (sync from empty)
and update it (fetch only what changed) — no installer, no ``innoextract``.

This registry is deliberately tiny and hand-curated: each entry names a MUD whose vault
download is only an installer, and supplies the connection (the installer bundles more
than one world, so we can't rely on reading it out of the pack).
"""

from __future__ import annotations

from dataclasses import dataclass

from genericmud.config.worlds import World


@dataclass(frozen=True)
class ManifestSource:
    """A soundpack distributed as an HTTP file tree described by a remote manifest."""

    id: str  # pack id and packs_dir/<id> subdir; must be a slug (see PackManifest.validate)
    name: str  # human label shown in the vault browser
    mud: str  # the MUD this pack is for (matched against a vault pack's MUD/name column)
    dialect: str  # one of manifest.KNOWN_DIALECTS
    base_url: str  # tree root; every file is ``base_url + relpath``. Must end with "/".
    manifest_name: str  # manifest file under base_url (``.gz`` is transparently inflated)
    entry: str  # load script (the world file) relative to the pack root
    world: World  # curated connection (the installer bundles several worlds)
    include: tuple[str, ...] = ()  # only sync paths under these prefixes; () = the whole tree

    @property
    def manifest_url(self) -> str:
        return self.base_url + self.manifest_name


# Alter Aeon's connection is www.alteraeon.com's telnet endpoint (from the pack's own world
# file); the tree also bundles a Stellar Aeon world, so the connection is curated here rather
# than read out of the pack. include=() = fetch the whole tree (the pack's plugins `require`
# libs across the install; trimming to a subtree is a later optimisation, not correctness).
_SOURCES: dict[str, ManifestSource] = {
    "mush-z": ManifestSource(
        id="mush-z",
        name="Mush-Z (Alter Aeon)",
        mud="Alter Aeon",
        dialect="mushclient",
        base_url="https://www.mush-z.com/mush/",
        manifest_name="update_everything.lst.gz",
        entry="worlds/alteraeon/alter_aeon.mcl",
        world=World(name="Alter Aeon", host="alteraeon.com", port=3010),
    ),
}


def _norm(text: str) -> str:
    """Lowercase, alphanumerics only: 'Alter Aeon' == 'alter-aeon' == 'AlterAeon'."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def all_sources() -> list[ManifestSource]:
    return list(_SOURCES.values())


def by_id(source_id: str) -> ManifestSource | None:
    return _SOURCES.get(source_id)


def for_labels(*labels: str) -> ManifestSource | None:
    """The manifest source matching any of ``labels`` (a vault pack's MUD or name), or None.

    Matched case- and punctuation-insensitively against each source's ``mud`` and ``name`` so
    a vault entry ("Mush-Z" / MUD "Alter Aeon") resolves to its streamed source.
    """
    targets = {_norm(label) for label in labels if label}
    for source in _SOURCES.values():
        if _norm(source.mud) in targets or _norm(source.name) in targets or source.id in targets:
            return source
    return None
