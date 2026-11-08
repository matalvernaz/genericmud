"""Soundpack lifecycle: manifest model + install/enable store (the P2 moat).

A pack is a directory with a ``pack.toml`` manifest, a script entry in one of the
three supported dialects, and its sound files. Legacy single-file packs get a
manifest inferred from the extension. :class:`PackStore` owns install/update/
uninstall and per-world enablement; the loader (separate module) activates an
enabled pack against an engine.
"""

from genericmud.packs.loader import (
    ActivationResult,
    Conflict,
    activate_world,
    detect_conflicts,
)
from genericmud.packs.manifest import (
    DIALECT_BY_SUFFIX,
    MANIFEST_NAME,
    PackManifest,
    UnknownDialect,
    infer_manifest,
    load_manifest,
    slugify,
)
from genericmud.packs.setup import (
    GitUpdateResult,
    PendingUpdate,
    SetupResult,
    UpdateUnchanged,
    check_pack_update,
    describe_update,
    detect_entry,
    entry_problem,
    setup_pack,
    setup_pack_from_git,
    setup_pack_from_manifest,
    update_pack,
    update_pack_from_git,
)
from genericmud.packs.store import PackError, PackExists, PackStore, UnknownPack
from genericmud.packs.world_import import world_from_pack

__all__ = [
    "DIALECT_BY_SUFFIX",
    "MANIFEST_NAME",
    "ActivationResult",
    "Conflict",
    "GitUpdateResult",
    "PackError",
    "PackExists",
    "PackManifest",
    "PackStore",
    "PendingUpdate",
    "SetupResult",
    "UnknownDialect",
    "UnknownPack",
    "UpdateUnchanged",
    "activate_world",
    "check_pack_update",
    "describe_update",
    "detect_conflicts",
    "detect_entry",
    "entry_problem",
    "infer_manifest",
    "load_manifest",
    "setup_pack",
    "setup_pack_from_git",
    "setup_pack_from_manifest",
    "slugify",
    "update_pack",
    "update_pack_from_git",
    "world_from_pack",
]
