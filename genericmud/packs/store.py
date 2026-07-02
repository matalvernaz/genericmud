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
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace  # 'replace' the kwarg shadows it here
from pathlib import Path

from genericmud.packs.manifest import CODE_EXEC_DIALECTS, PackManifest, load_manifest


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
        entry: str | None = None,
        origin: str | None = None,
    ) -> PackManifest:
        """Install a pack: a dir with ``pack.toml``, a bare script, or a ``.zip``.

        ``replace=True`` updates an already-installed pack in place. ``world``
        enables the pack for that MUD. Installs are untrusted by default (held
        back from auto-load on connect); pass ``trust=True`` to vouch for it now.
        ``entry`` picks the load script of a multi-file pack (relative to its root).
        ``origin`` records where the content came from (a URL) so the pack can be
        re-fetched/updated later.
        """
        source = Path(source)
        if not source.exists():
            raise PackError(f"no such pack source: {source}")
        opts = {
            "world": world, "replace": replace, "trust": trust, "entry": entry, "origin": origin,
        }
        if source.is_file() and source.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    extract_pack(source, tmp)
                except zipfile.BadZipFile as exc:
                    raise PackError(f"not a valid zip: {source} ({exc})") from exc
                return self._install_from(_pack_root(Path(tmp)), **opts)
        return self._install_from(source, **opts)

    def _install_from(
        self,
        source: Path,
        *,
        world: str | None,
        replace: bool,
        trust: bool,
        entry: str | None,
        origin: str | None,
    ) -> PackManifest:
        manifest = load_manifest(source, entry=entry)
        if origin:
            manifest = dataclass_replace(manifest, origin=origin)
        index = self._load_index()
        if manifest.id in index and not replace:
            raise PackExists(f"pack {manifest.id!r} already installed; pass replace=True to update")
        # Replacing a trusted code-executor's content over cleartext http invalidates the vouch:
        # the user trusted the bytes they saw, not whatever an on-path attacker now serves. Drop
        # trust so it must be re-granted before the new code auto-runs (see _revoke_stale_trust).
        revoke_trust = self._replace_invalidates_trust(manifest, replace=replace)

        dest = self.packs_dir / manifest.id
        src, dst = source.resolve(), dest.resolve()
        if src == dst or dst in src.parents or src in dst.parents:
            raise PackError(f"refusing to install {manifest.id!r}: source and destination overlap")
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
        if revoke_trust:
            self.untrust(manifest.id)
        if world:
            self.enable(manifest.id, world)
        if trust:  # an explicit re-vouch (e.g. CLI --trust) still wins over the revoke above
            self.trust(manifest.id)
        return manifest

    def _replace_invalidates_trust(self, manifest: PackManifest, *, replace: bool) -> bool:
        """True if writing ``manifest``'s content now voids an existing trust grant.

        Only when we are replacing an already-installed, currently-trusted, code-executing pack
        whose content is being pulled from a cleartext-http origin -- the case where an on-path
        attacker could swap the auto-running code out from under a stale vouch.
        """
        return (
            replace
            and manifest.dialect in CODE_EXEC_DIALECTS
            and _is_cleartext_origin(manifest.origin)
            and self.is_trusted(manifest.id)
        )

    def register(self, pack_id: str, *, origin: str | None = None) -> PackManifest:
        """Record an already-populated ``packs_dir/<id>`` in the index without copying.

        For content synced into place (see :mod:`genericmud.packs.manifest_sync`) rather than
        extracted from an archive — install's copy step would only duplicate a large tree, and
        its overlap guard forbids source==dest anyway. Reads the dir's ``pack.toml`` for
        identity; ``enable``/``trust`` stay separate, as with :meth:`install`.
        """
        source = self.pack_dir(pack_id)
        if not source.is_dir():
            raise PackError(f"no pack directory to register: {source}")
        manifest = load_manifest(source)
        if manifest.id != pack_id:  # the on-disk dir name is authoritative for the key
            manifest = dataclass_replace(manifest, id=pack_id)
        if origin:
            manifest = dataclass_replace(manifest, origin=origin)
        # Re-registering synced content is a replace; drop a now-stale trust the same way install
        # does (a no-op on first register / an https origin, so routine updates keep working).
        revoke_trust = self._replace_invalidates_trust(manifest, replace=True)
        index = self._load_index()
        index[manifest.id] = manifest.to_dict()
        self._save_index(index)
        if revoke_trust:
            self.untrust(manifest.id)
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


_MAX_NEST_DEPTH = 2  # a pack nests at most one level: sounds.zip + scripts.zip inside a wrapper

# Quotas so a malicious/broken pack zip can't fill the disk or OOM during install (this host
# hard-reboots on OOM). Soundpacks can be large (Miriani bundles ~870 MB of audio), so the caps
# are generous; a zip bomb blows past them by orders of magnitude. The total and member caps are
# enforced across the WHOLE nested tree, not per archive -- a wrapper of N inner zips, each just
# under a per-archive cap, must not sum to N times the limit on disk.
_MAX_PACK_MEMBERS = 50_000
_MAX_PACK_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB uncompressed, across the whole (nested) tree
_MAX_PACK_FILE_BYTES = 1024 * 1024 * 1024  # 1 GiB per member
_MAX_COMPRESSION_RATIO = 200  # refuse a member that inflates >200x (audio never does; a bomb does)
_RATIO_MIN_SIZE = 1_000_000  # only ratio-check members big enough to matter


@dataclass
class _ExtractBudget:
    """Remaining disk/inode headroom, drawn down across every archive in a nested tree.

    A single ``_ExtractBudget`` threads through :func:`extract_pack`'s recursion, so the byte
    and member caps bound the whole expanded tree rather than resetting per archive (the gap a
    wrapper-of-zip-bombs would otherwise walk through).
    """

    bytes_left: int = _MAX_PACK_TOTAL_BYTES
    members_left: int = _MAX_PACK_MEMBERS


def _check_zip_quota(archive: zipfile.ZipFile, budget: _ExtractBudget) -> None:
    """Draw one archive's members against ``budget``; raise before extracting if it overruns."""
    members = archive.infolist()
    budget.members_left -= len(members)
    if budget.members_left < 0:
        raise PackError(f"pack has too many files (over {_MAX_PACK_MEMBERS} across the tree)")
    for info in members:
        if info.file_size > _MAX_PACK_FILE_BYTES:
            raise PackError(f"pack member too large: {info.filename} ({info.file_size} bytes)")
        if (
            info.compress_size > 0
            and info.file_size > _RATIO_MIN_SIZE
            and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
        ):
            raise PackError(f"pack member has a bomb-like compression ratio: {info.filename}")
        budget.bytes_left -= info.file_size
        if budget.bytes_left < 0:
            raise PackError("pack uncompressed size exceeds the limit (across the nested tree)")


def extract_pack(
    zip_path: str | Path,
    dest: str | Path,
    *,
    _depth: int = 0,
    _budget: _ExtractBudget | None = None,
) -> None:
    """Extract a pack zip into ``dest``, descending into any nested zips.

    Some packs (e.g. Miriani) ship a wrapper zip holding a separate sounds zip and scripts
    zip rather than the files directly; without descending, no script is found. Each nested
    zip is expanded into a sibling folder named after it and then removed, so the tree holds
    files, not archives. CPython sanitises member paths on extract (no zip-slip); a quota check
    (:func:`_check_zip_quota`) refuses a decompression bomb before any bytes are written, and a
    single :class:`_ExtractBudget` is carried through the recursion so the caps bound the whole
    tree -- not each archive independently.
    """
    dest = Path(dest)
    # Read the caps at call time (not the dataclass field defaults, frozen at class creation) so
    # they stay tunable/monkeypatchable; the same budget object then threads through the recursion.
    budget = _budget or _ExtractBudget(
        bytes_left=_MAX_PACK_TOTAL_BYTES, members_left=_MAX_PACK_MEMBERS
    )
    with zipfile.ZipFile(zip_path) as archive:
        _check_zip_quota(archive, budget)
        archive.extractall(dest)
    if _depth >= _MAX_NEST_DEPTH:
        return
    for nested in sorted(dest.rglob("*.zip")):
        try:
            extract_pack(nested, nested.with_suffix(""), _depth=_depth + 1, _budget=budget)
        except zipfile.BadZipFile:
            continue  # a stray non-zip named .zip: leave it, it just won't be a pack source
        nested.unlink(missing_ok=True)


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


def _is_cleartext_origin(origin: str | None) -> bool:
    """True for an ``http://`` origin URL -- content pulled over an unauthenticated channel."""
    return bool(origin) and origin.lower().startswith("http://")


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file and atomically replace, so a crash mid-write can't leave a torn or
    # empty index/worlds/trust file (which would lose every installed pack's state).
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
