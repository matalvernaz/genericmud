"""Extract a MUD's connection details from a soundpack's own files.

MUSHclient soundpacks ship a world file (`.MCL`, or an exported world `.xml`) whose
`<world>` element carries `site`/`port`/`name` — so the connection a player needs is
in the pack itself, and the setup flow can create the world without asking. VIPMud
`.set` packs usually carry no world file, so those return None (prefill/ask instead).

Parsing is by regex, not XML: `.MCL` files are large, iso-8859-1, and carry a
`<!DOCTYPE>` that trips ElementTree; we only need three attributes off the opening
`<world>` tag. `\\bport=` matches the world's own port, not `proxy_port`/`mxp_port`.
"""

from __future__ import annotations

import re
from pathlib import Path

from genericmud.config.worlds import World

_WORLD_TAG_RE = re.compile(r"<world\b(.*?)>", re.DOTALL | re.IGNORECASE)
_WORLD_FILE_SUFFIXES = (".mcl", ".xml")


def _attr(attrs: str, name: str) -> str | None:
    match = re.search(rf'\b{name}\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
    return match.group(1) if match else None


def _world_from_text(text: str) -> World | None:
    tag = _WORLD_TAG_RE.search(text)
    if tag is None:
        return None  # a plugin/trigger .xml or a .set pack: no <world> element
    attrs = tag.group(1)
    site, port = _attr(attrs, "site"), _attr(attrs, "port")
    if not site or not port or not port.isdigit():
        return None
    return World(name=_attr(attrs, "name") or site, host=site, port=int(port))


def world_from_pack(path: str | Path) -> World | None:
    """The first world (site/port) found in a pack's `.MCL`/world `.xml`, or None.

    ``path`` may be the world file itself or a pack directory to scan.
    """
    path = Path(path)
    if path.is_file():
        candidates = [path]
    else:
        candidates = sorted(
            p for p in path.rglob("*") if p.suffix.lower() in _WORLD_FILE_SUFFIXES
        )
    for candidate in candidates:
        try:
            text = candidate.read_bytes().decode("latin-1")  # .MCL is iso-8859-1
        except OSError:
            continue
        world = _world_from_text(text)
        if world is not None:
            return world
    return None
