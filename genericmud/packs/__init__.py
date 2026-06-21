"""Soundpack lifecycle: manifest model + install/enable store (the P2 moat).

A pack is a directory with a ``pack.toml`` manifest, a script entry in one of the
three supported dialects, and its sound files. Legacy single-file packs get a
manifest inferred from the extension. :class:`PackStore` owns install/update/
uninstall and per-world enablement; the loader (separate module) activates an
enabled pack against an engine.
"""

from genericmud.packs.manifest import (
    DIALECT_BY_SUFFIX,
    MANIFEST_NAME,
    PackManifest,
    UnknownDialect,
    infer_manifest,
    load_manifest,
    slugify,
)
from genericmud.packs.store import PackError, PackExists, PackStore, UnknownPack

__all__ = [
    "DIALECT_BY_SUFFIX",
    "MANIFEST_NAME",
    "PackError",
    "PackExists",
    "PackManifest",
    "PackStore",
    "UnknownDialect",
    "UnknownPack",
    "infer_manifest",
    "load_manifest",
    "slugify",
]
