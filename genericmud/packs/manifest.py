"""Pack manifest: what a soundpack IS, and how to read or infer one.

A genericMud pack is a directory holding a ``pack.toml`` manifest plus its script
entry and sound files. Legacy packs (a bare ``.lua``/``.set``/``.xml`` from a
soundpack site) carry no manifest, so :func:`infer_manifest` synthesizes one from
the file — dialect from the extension, name from the stem. Either way the loader
ends up with a uniform :class:`PackManifest`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "pack.toml"

# Script extension -> dialect front-end key (see scripting.* and the loader).
# .mcl is a MUSHclient world file (same dialect as .xml); it <include>s its plugins.
DIALECT_BY_SUFFIX = {".lua": "lua", ".set": "vipmud", ".xml": "mushclient", ".mcl": "mushclient"}
KNOWN_DIALECTS = frozenset(DIALECT_BY_SUFFIX.values())

# Dialects where "trusted" grants full-stdlib code execution (os/io) when a pack auto-loads on
# connect. Native Lua and VIPMud .set stay sandboxed even when trusted (I/O is confined), so only
# MUSHclient carries this weight. Setup declines to auto-trust these; the store re-arms the vouch
# when remote content is replaced under one (see PackStore.install).
CODE_EXEC_DIALECTS = frozenset({"mushclient"})


class UnknownDialect(ValueError):
    """A pack declares (or a bare file implies) a dialect we can't load."""


@dataclass(frozen=True)
class PackManifest:
    """A pack's identity and load instructions. ``id`` is the store's unique key."""

    id: str
    name: str
    dialect: str  # one of KNOWN_DIALECTS
    entry: str  # script entry, relative to the pack directory
    version: str = "0"
    author: str = ""
    description: str = ""
    worlds: tuple[str, ...] = field(default_factory=tuple)  # advisory targets; () = any
    sound_dir: str = ""  # sounds subdir, relative to the pack dir ("" = pack root)
    origin: str = ""  # where the pack content came from (URL); enables re-fetch/update

    def to_dict(self) -> dict:
        data = asdict(self)
        data["worlds"] = list(self.worlds)  # JSON has no tuples
        return data

    @classmethod
    def from_dict(cls, data: dict) -> PackManifest:
        known = {f for f in cls.__dataclass_fields__}  # tolerate extra/old keys
        clean = {k: v for k, v in data.items() if k in known}
        clean["worlds"] = tuple(clean.get("worlds", ()))
        manifest = cls(**clean)
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.dialect not in KNOWN_DIALECTS:
            raise UnknownDialect(f"{self.dialect!r} (known: {sorted(KNOWN_DIALECTS)})")
        # id keys packs_dir/<id> (install rmtree's then writes it); entry is joined onto the
        # pack dir and read/executed. Both can come from an untrusted pack.toml, so refuse
        # anything that isn't a plain slug / a confined relative path.
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.id):
            raise ValueError(f"unsafe pack id {self.id!r} (must be a slug)")
        entry = PurePosixPath(self.entry.replace("\\", "/"))
        unsafe_entry = (
            not self.entry.strip()
            or entry.is_absolute()
            or ".." in entry.parts
            or ":" in self.entry
        )
        if unsafe_entry:
            raise ValueError(f"unsafe pack entry {self.entry!r}")


def slugify(name: str) -> str:
    """A filesystem- and key-safe id: lowercase, non-alnum runs collapsed to '-'."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "pack"


def infer_manifest(file_path: str | Path) -> PackManifest:
    """Synthesize a manifest for a bare single-file pack from its extension+stem."""
    path = Path(file_path)
    dialect = DIALECT_BY_SUFFIX.get(path.suffix.lower())
    if dialect is None:
        known = sorted(DIALECT_BY_SUFFIX)
        raise UnknownDialect(f"{path.suffix!r} for {path.name} (known: {known})")
    return PackManifest(id=slugify(path.stem), name=path.stem, dialect=dialect, entry=path.name)


def _read_pack_toml(manifest_path: Path) -> PackManifest:
    with open(manifest_path, "rb") as handle:
        data = tomllib.load(handle)
    # Slugify whatever id we land on: an explicit pack.toml id is untrusted and must not be
    # able to become a path (it keys packs_dir/<id>, which install rmtree's then writes to).
    data["id"] = slugify(str(data.get("id") or data.get("name") or manifest_path.parent.name))
    data.setdefault("name", data["id"])
    return PackManifest.from_dict(data)


def load_manifest(path: str | Path, entry: str | None = None) -> PackManifest:
    """Read ``pack.toml`` if present, else infer from a single bare script.

    ``path`` may be a pack directory or a single script file. A directory with no
    ``pack.toml`` normally must contain exactly one recognized script file; pass
    ``entry`` (a script path relative to the directory) to pick the load script of a
    multi-file pack — the case real soundpacks need (e.g. a VIPMud pack's ``main.set``).
    """
    path = Path(path)
    if path.is_file():
        return infer_manifest(path)
    manifest_path = path / MANIFEST_NAME
    if manifest_path.is_file():
        return _read_pack_toml(manifest_path)
    if entry is not None:
        entry_path = path / entry
        dialect = DIALECT_BY_SUFFIX.get(entry_path.suffix.lower())
        if dialect is None or not entry_path.is_file():
            raise UnknownDialect(f"entry {entry!r} is not a known script in {path}")
        return PackManifest(id=slugify(path.name), name=path.name, dialect=dialect, entry=entry)
    scripts = [p for p in sorted(path.iterdir()) if p.suffix.lower() in DIALECT_BY_SUFFIX]
    if len(scripts) == 1:
        return infer_manifest(scripts[0])
    raise UnknownDialect(
        f"{path} has no {MANIFEST_NAME} and {len(scripts)} script files "
        f"(need exactly one, or pass entry= to pick the load script)"
    )
