"""Soundpacks fetched straight from their own git repo (github/gitlab/gitea).

Some vault packs ship only a Windows installer whose real payload is a public git repo the
installer clones (Erion: ``gitlab.com/erion1/soundpack``). genericMud already *follows* the
installer to that repo -- but only after downloading and scanning the ~50 MB installer, and it
lands with the temp-dir pack id ``source``. This hand-curated registry names the repo directly,
so setup can skip the installer, pull the pack from its source in one step, and record a real
pack id + origin.

Deliberately tiny, like :mod:`genericmud.packs.manifest_sources`: one entry per MUD whose vault
download is only an installer wrapping a git repo. An unknown pack just falls back to the
installer-follow path in the vault browser.
"""

from __future__ import annotations

from dataclasses import dataclass

from genericmud.config.worlds import World


@dataclass(frozen=True)
class GitSource:
    """A soundpack whose canonical home is a public git repo."""

    id: str  # pack id and packs_dir/<id> subdir; must be a slug (see PackManifest.validate)
    name: str  # human label shown in the vault browser
    mud: str  # the MUD this pack is for (matched against a vault pack's MUD/name column)
    repo_url: str  # the pack's git repo (github/gitlab/gitea); a clone/web URL, ``.git`` optional
    entry: str  # load script relative to the repo root (the archive's wrapper dir is stripped)
    world: World  # curated connection (the installer bundles more than one world)
    dialect: str = "mushclient"  # one of manifest.KNOWN_DIALECTS


# Erion's installer clones gitlab.com/erion1/soundpack (~1 GB: a portable MUSHclient install).
# The world file is MUSHclient/worlds/Erion MUD.mcl; connection is Erion MUD's published telnet
# endpoint. Fetching the archive directly skips the installer .exe entirely.
_SOURCES: dict[str, GitSource] = {
    "erion": GitSource(
        id="erion",
        name="Erion Mud Soundpack",
        mud="Erion MUD",
        repo_url="https://gitlab.com/erion1/soundpack",
        entry="MUSHclient/worlds/Erion MUD.mcl",
        world=World(name="Erion", host="erionmud.com", port=1234),
    ),
}


def _norm(text: str) -> str:
    """Lowercase, alphanumerics only: 'Erion MUD' == 'erion-mud' == 'ErionMUD'."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def all_sources() -> list[GitSource]:
    return list(_SOURCES.values())


def by_id(source_id: str) -> GitSource | None:
    return _SOURCES.get(source_id)


def for_labels(*labels: str) -> GitSource | None:
    """The git source matching any of ``labels`` (a vault pack's MUD or name), or None.

    Matched case- and punctuation-insensitively against each source's ``mud`` and ``name``, so a
    vault entry ("Erion Mud Soundpack" / MUD "Erion MUD") resolves to its repo.
    """
    targets = {_norm(label) for label in labels if label}
    for source in _SOURCES.values():
        if _norm(source.mud) in targets or _norm(source.name) in targets or source.id in targets:
            return source
    return None
