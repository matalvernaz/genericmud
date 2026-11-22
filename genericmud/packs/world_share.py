"""Share one world's whole setup as a single zip (the MUDBall pack-sharing flow).

Export bundles the world's connection details, field-based automation, copied sounds,
and sandboxed automation scripts into one file a friend can be sent; import
unpacks that file into the local userpacks tree and hands back a :class:`World`
ready to save and connect. The zip layout is flat and boring on purpose::

    world.json      {"name", "host", "port", "tls", "encoding", "connect_method",
                     "command_stacking", "command_separator"}
    rules.json      the soundpack builder's rules (optional)
    sounds/...      cue files referenced by the rules (optional)
    scripts/*.lua   sandboxed per-world automation scripts (optional)

Extraction goes through :func:`~genericmud.packs.store.extract_pack`, which
already enforces the decompression quotas; on top of that only the known file
groups above are kept, so a crafted zip can't plant files anywhere else.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

from genericmud.config.worlds import DEFAULT_CONNECT_METHOD, World, parse_port
from genericmud.packs.store import extract_pack
from genericmud.packs.user_rules import RULES_FILENAME, SOUNDS_DIRNAME
from genericmud.protocol.charset import normalize_encoding
from genericmud.safepath import legacy_world_component, world_component
from genericmud.scripting.user_scripts import (
    SCRIPTS_DIRNAME,
    list_scripts,
    normalize_script_name,
)

WORLD_META_FILENAME = "world.json"
_MAX_IMPORT_NAME_TRIES = 100  # "name-2".."name-100" before giving up on a unique dir


def export_world(world: World, pack_dir: Path | None, dest: Path) -> int:
    """Write the world's shareable zip to ``dest``; returns the file count.

    ``pack_dir`` is the world's userpack directory (rules.json + sounds + scripts); None
    or a missing directory exports connection details alone, which is still a
    useful "here's how to connect" share.
    """
    # The encoding travels with the world: a friend connecting to the same KOI8-R MUD needs
    # it as much as the port. So do the way it logs in and its command stacking (issue #128):
    # a MUD sharing a world for its players knows which login method answers its prompts, and
    # neither is a secret -- the username and password stay in the credential store. The
    # sounds folder does not travel; it's a path on this machine.
    meta = {
        "name": world.name, "host": world.host, "port": world.port, "tls": world.tls,
        "encoding": world.encoding, "connect_method": world.connect_method,
        "command_stacking": world.command_stacking,
        "command_separator": world.command_separator,
    }
    count = 1
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(WORLD_META_FILENAME, json.dumps(meta, indent=2, ensure_ascii=False))
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
        scripts = Path(pack_dir) / SCRIPTS_DIRNAME
        if scripts.is_dir():
            for name in list_scripts(pack_dir):
                archive.write(scripts / name, f"{SCRIPTS_DIRNAME}/{name}")
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
        scripts = staging / SCRIPTS_DIRNAME
        if scripts.is_dir():
            for path in sorted(scripts.iterdir()):
                if path.is_file() and path.suffix.lower() == ".lua":
                    try:
                        script_name = normalize_script_name(path.name)
                    except ValueError:
                        continue
                    dest = target / SCRIPTS_DIRNAME / script_name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)

    try:
        port = parse_port(meta.get("port") or 23)
    except ValueError:
        port = 23
    # Anything missing or unusable becomes what a new world starts with, as World's own
    # checks decide: an older zip, or one written by hand, still imports.
    return World(
        name=name, host=host, port=port, tls=meta.get("tls") is True,
        encoding=normalize_encoding(meta.get("encoding")),
        connect_method=meta.get("connect_method"),
        command_stacking=meta.get("command_stacking") is not False,
        command_separator=meta.get("command_separator"),
    )


def login_method_refusal(
    world: World, saved_worlds: Iterable[World], credentials: object
) -> str | None:
    """Why an imported world may not keep the login method its zip came with, or None.

    Logins are kept by world name, and a zip names its own host. If a login is already kept
    under the name the import lands on, the zip's method would send that login to the zip's
    host on the next connect, without the player having chosen either. So the method is only
    kept when the import adds a new world AND the credential store can say there is no login
    under its name. Replacing a saved world never keeps it, whatever the store says (its login
    stays with the name, and a store can match names in its own way); a store that can't
    answer, or isn't there, counts as one that might hold a login.
    """
    if world.connect_method == DEFAULT_CONNECT_METHOD:
        return None
    name = world.name.casefold()
    if any(saved.name.casefold() == name for saved in saved_worlds):
        return "it replaces a saved world, whose login stays with that name"
    has_login = getattr(credentials, "has_login", None)
    stored = has_login(world.name) if has_login is not None else None
    if stored is False:
        return None
    if stored is True:
        return "a login is already saved under that name"
    return "the saved logins couldn't be checked"


def _unique_pack_dir(root: Path, name: str) -> tuple[Path, str]:
    """A not-yet-existing userpack dir for ``name``, suffixing the name until unique.

    The dir MUST be ``world_component(world_name)`` — that's how
    ``EngineApp.user_rules_dir`` finds a world's rules — so the suffix goes on
    the world name and the dir is re-derived from it each try. A name whose data
    an older build filed under its ASCII-only form counts as taken too: user_rules_dir
    still finds that folder, and an import must not take it over.
    """
    candidate_name = name
    for suffix in range(2, _MAX_IMPORT_NAME_TRIES + 2):
        candidate = root / world_component(candidate_name)
        legacy_name = legacy_world_component(candidate_name)
        legacy_taken = bool(legacy_name) and (root / legacy_name).exists()
        if not candidate.exists() and not legacy_taken:
            return candidate, candidate_name
        candidate_name = f"{name} {suffix}"
    raise ValueError(f"too many worlds named {name}")
