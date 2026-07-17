"""Share one world's whole setup as a single zip (the MUDBall pack-sharing flow).

Export bundles the world's connection details plus its builder rules and every
copied sound into one file a friend can be sent; import unpacks that file into
the local userpacks tree and hands back a :class:`World` ready to save and
connect. The zip layout is flat and boring on purpose::

    world.json      {"name", "host", "port", "tls"}
    rules.json      the soundpack builder's rules (optional)
    sounds/...      cue files referenced by the rules (optional)

Extraction goes through :func:`~genericmud.packs.store.extract_pack`, which
already enforces the decompression quotas; on top of that only the three known
shapes above are kept, so a crafted zip can't plant files anywhere else.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from genericmud.config.worlds import World
from genericmud.packs.store import extract_pack
from genericmud.packs.user_rules import RULES_FILENAME, SOUNDS_DIRNAME
from genericmud.safepath import sanitize_component

WORLD_META_FILENAME = "world.json"
_MAX_IMPORT_NAME_TRIES = 100  # "name-2".."name-100" before giving up on a unique dir


def export_world(world: World, pack_dir: Path | None, dest: Path) -> int:
    """Write the world's shareable zip to ``dest``; returns the file count.

    ``pack_dir`` is the world's userpack directory (rules.json + sounds); None
    or a missing directory exports connection details alone, which is still a
    useful "here's how to connect" share.
    """
    meta = {"name": world.name, "host": world.host, "port": world.port, "tls": world.tls}
    count = 1
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(WORLD_META_FILENAME, json.dumps(meta, indent=2))
        if pack_dir is None:
            return count
        rules = Path(pack_dir) / RULES_FILENAME
        if rules.is_file():
            archive.write(rules, RULES_FILENAME)
            count += 1
        sounds = Path(pack_dir) / SOUNDS_DIRNAME
        if sounds.is_dir():
            for path in sorted(sounds.rglob("*")):
                if path.is_file():
                    archive.write(path, f"{SOUNDS_DIRNAME}/{path.relative_to(sounds).as_posix()}")
                    count += 1
    return count


def import_world(zip_path: Path, userpacks_root: Path) -> World:
    """Unpack a shared world zip into ``userpacks_root``; returns its World.

    The world's name (sanitized) names the userpack directory; a taken name gets
    a numeric suffix so an import can never overwrite an existing world's rules.
    Raises ValueError when the zip has no usable world.json.
    """
    with tempfile.TemporaryDirectory(prefix="genericmud-import-") as scratch:
        staging = Path(scratch) / "unpacked"
        extract_pack(zip_path, staging)
        meta_path = staging / WORLD_META_FILENAME
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"not a shared world zip: {error}") from error
        name = str(meta.get("name") or "").strip()
        host = str(meta.get("host") or "").strip()
        if not name or not host:
            raise ValueError("not a shared world zip: missing name or host")

        target, name = _unique_pack_dir(Path(userpacks_root), name)
        target.mkdir(parents=True)
        rules = staging / RULES_FILENAME
        if rules.is_file():
            shutil.copy2(rules, target / RULES_FILENAME)
        sounds = staging / SOUNDS_DIRNAME
        if sounds.is_dir():
            # Only regular files under sounds/ survive; anything else in the zip is dropped.
            for path in sorted(sounds.rglob("*")):
                if path.is_file():
                    dest = target / SOUNDS_DIRNAME / path.relative_to(sounds)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)

    try:
        port = int(meta.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return World(name=name, host=host, port=port or 23, tls=bool(meta.get("tls")))


def _unique_pack_dir(root: Path, name: str) -> tuple[Path, str]:
    """A not-yet-existing userpack dir for ``name``, suffixing the name until unique.

    The dir MUST be ``sanitize_component(world_name)`` — that's how
    ``EngineApp.user_rules_dir`` finds a world's rules — so the suffix goes on
    the world name and the dir is re-derived from it each try.
    """
    candidate_name = name
    for suffix in range(2, _MAX_IMPORT_NAME_TRIES + 2):
        candidate = root / sanitize_component(candidate_name)
        if not candidate.exists():
            return candidate, candidate_name
        candidate_name = f"{name} {suffix}"
    raise ValueError(f"too many worlds named {name}")
