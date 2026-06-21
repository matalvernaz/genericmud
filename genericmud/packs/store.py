"""PackStore: install/update/uninstall soundpacks; track per-world enablement.

Layout under ``root``::

    <root>/packs/<id>/...     copied pack content (script entry + sounds)
    <root>/index.json         {id: manifest-dict} for each installed pack
    <root>/worlds.json        {world: [enabled id, ...]}  -- per-MUD isolation

Pure filesystem + JSON with no engine dependency, so it is fully testable.
Activation (running an enabled pack's script against an engine) lives in the
loader; the store only answers what is installed and enabled for a world.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from genericmud.packs.manifest import PackManifest, load_manifest


class PackError(RuntimeError):
    """Base for pack-store failures."""


class PackExists(PackError):
    """Install would clobber an already-installed pack and ``replace`` was not set."""


class UnknownPack(PackError):
    """Referenced a pack id the store has never installed."""


class PackStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.packs_dir = self.root / "packs"
        self._index_path = self.root / "index.json"
        self._worlds_path = self.root / "worlds.json"
        self._trust_path = self.root / "trust.json"

    # --- install / uninstall ---

    def install(
        self,
        source: str | Path,
        *,
        world: str | None = None,
        replace: bool = False,
        trust: bool = False,
    ) -> PackManifest:
        """Install a pack: a dir with ``pack.toml``, a bare script, or a ``.zip``.

        ``replace=True`` updates an already-installed pack in place. ``world``
        enables the pack for that MUD. Installs are untrusted by default (held
        back from auto-load on connect); pass ``trust=True`` to vouch for it now.
        """
        source = Path(source)
        if not source.exists():
            raise PackError(f"no such pack source: {source}")
        if source.is_file() and source.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    with zipfile.ZipFile(source) as archive:
                        archive.extractall(tmp)  # CPython sanitizes member paths (no zip-slip)
                except zipfile.BadZipFile as exc:
                    raise PackError(f"not a valid zip: {source} ({exc})") from exc
                root = _pack_root(Path(tmp))
                return self._install_from(root, world=world, replace=replace, trust=trust)
        return self._install_from(source, world=world, replace=replace, trust=trust)

    def _install_from(
        self, source: Path, *, world: str | None, replace: bool, trust: bool
    ) -> PackManifest:
        manifest = load_manifest(source)
        index = self._load_index()
        if manifest.id in index and not replace:
            raise PackExists(f"pack {manifest.id!r} already installed; pass replace=True to update")

        dest = self.packs_dir / manifest.id
        if dest.exists():
            shutil.rmtree(dest)
        if source.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / manifest.entry)
        else:
            self.packs_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, dest)

        index[manifest.id] = manifest.to_dict()
        self._save_index(index)
        if world:
            self.enable(manifest.id, world)
        if trust:
            self.trust(manifest.id)
        return manifest

    def uninstall(self, pack_id: str) -> None:
        index = self._load_index()
        if pack_id not in index:
            raise UnknownPack(pack_id)
        dest = self.packs_dir / pack_id
        if dest.exists():
            shutil.rmtree(dest)
        del index[pack_id]
        self._save_index(index)
        worlds = self._load_worlds()
        if any(pack_id in ids for ids in worlds.values()):
            self._save_worlds({w: [i for i in ids if i != pack_id] for w, ids in worlds.items()})
        self.untrust(pack_id)

    # --- enablement (per world) ---

    def enable(self, pack_id: str, world: str) -> None:
        if pack_id not in self._load_index():
            raise UnknownPack(pack_id)
        worlds = self._load_worlds()
        ids = worlds.setdefault(world, [])
        if pack_id not in ids:
            ids.append(pack_id)  # append preserves load order
            self._save_worlds(worlds)

    def disable(self, pack_id: str, world: str) -> None:
        worlds = self._load_worlds()
        if pack_id in worlds.get(world, []):
            worlds[world] = [i for i in worlds[world] if i != pack_id]
            self._save_worlds(worlds)

    def is_enabled(self, pack_id: str, world: str) -> bool:
        return pack_id in self._load_worlds().get(world, [])

    # --- trust (auto-load consent; the sandbox already covers execution safety) ---

    def trust(self, pack_id: str) -> None:
        if pack_id not in self._load_index():
            raise UnknownPack(pack_id)
        trusted = self._load_trust()
        if pack_id not in trusted:
            trusted.add(pack_id)
            self._save_trust(trusted)

    def untrust(self, pack_id: str) -> None:
        trusted = self._load_trust()
        if pack_id in trusted:
            trusted.discard(pack_id)
            self._save_trust(trusted)

    def is_trusted(self, pack_id: str) -> bool:
        return pack_id in self._load_trust()

    # --- queries ---

    def installed(self) -> list[PackManifest]:
        return [PackManifest.from_dict(d) for d in self._load_index().values()]

    def manifest(self, pack_id: str) -> PackManifest:
        index = self._load_index()
        if pack_id not in index:
            raise UnknownPack(pack_id)
        return PackManifest.from_dict(index[pack_id])

    def enabled(self, world: str) -> list[PackManifest]:
        """Manifests enabled for ``world``, in load order; skips dangling ids."""
        index = self._load_index()
        return [
            PackManifest.from_dict(index[pid])
            for pid in self._load_worlds().get(world, [])
            if pid in index
        ]

    def pack_dir(self, pack_id: str) -> Path:
        return self.packs_dir / pack_id

    def entry_path(self, pack_id: str) -> Path:
        return self.pack_dir(pack_id) / self.manifest(pack_id).entry

    # --- json state ---

    def _load_index(self) -> dict:
        return _load_json(self._index_path)

    def _save_index(self, data: dict) -> None:
        _save_json(self._index_path, data)

    def _load_worlds(self) -> dict:
        return _load_json(self._worlds_path)

    def _save_worlds(self, data: dict) -> None:
        _save_json(self._worlds_path, data)

    def _load_trust(self) -> set[str]:
        return set(_load_json(self._trust_path).get("trusted", []))

    def _save_trust(self, trusted: set[str]) -> None:
        _save_json(self._trust_path, {"trusted": sorted(trusted)})


def _pack_root(extracted: Path) -> Path:
    """The real pack root inside an extracted zip: descend lone wrapper dirs.

    Handles the common ``PackName/...`` zip layout as well as a flat zip whose
    files (or single script) sit at the archive root.
    """
    current = extracted
    while not (current / "pack.toml").is_file():
        entries = list(current.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            current = entries[0]
        else:
            break
    return current


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
