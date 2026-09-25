"""Saved MUD worlds (name/host/port/tls/sounds/encoding), persisted as TOML.

tomllib is read-only, so saving hand-writes the small, well-defined schema.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from genericmud.config.atomic import atomic_write_text
from genericmud.protocol.charset import AUTO, normalize_encoding

MIN_PORT = 1
MAX_PORT = 65535
DEFAULT_PORT = 4000


@dataclass
class World:
    name: str
    host: str
    port: int
    tls: bool = False
    sounds: str | None = None
    # Character encoding for this MUD's text, both directions (protocol/charset.py). Always
    # a supported setting: anything else becomes "auto", so a typo can't break a world.
    encoding: str = AUTO

    def __post_init__(self) -> None:
        self.encoding = normalize_encoding(self.encoding)


def parse_port(value: object) -> int:
    """Return a valid TCP port, or raise ``ValueError`` with a user-facing message."""
    if isinstance(value, bool):
        raise ValueError("Port must be a whole number from 1 to 65535.")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be a whole number from 1 to 65535.") from exc
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError("Port must be a whole number from 1 to 65535.")
    return port


def config_dir() -> Path:
    """Where genericMud keeps soundpacks, worlds, logs, and credentials.

    In the shipped (frozen) build this is a folder beside the executable, so the
    whole app is portable: unzip it and everything — including downloaded packs —
    lives in that one directory. From source it's ``~/.genericmud``.

    Exception: a macOS ``.app`` bundle is read-only (and code-signed), so writing beside
    the executable would land inside ``genericMud.app/Contents/MacOS`` and fail. There we
    use the platform's per-user data location instead.
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "genericMud"
        return Path(sys.executable).resolve().parent / "genericmud-data"
    return Path.home() / ".genericmud"


def worlds_path() -> Path:
    return config_dir() / "worlds.toml"


def load_worlds(path: Path | None = None) -> list[World]:
    target = path or worlds_path()
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("world", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return []
    worlds: list[World] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host = str(entry.get("host", "")).strip()
        if not host:
            continue
        sounds = entry.get("sounds")
        if sounds is not None and not isinstance(sounds, str):
            sounds = None
        try:
            port = parse_port(entry.get("port", DEFAULT_PORT))
        except ValueError:
            continue
        worlds.append(
            World(
                name=str(entry.get("name", "")).strip() or host,
                host=host,
                port=port,
                tls=entry.get("tls") is True,
                sounds=sounds,
                encoding=normalize_encoding(entry.get("encoding")),
            )
        )
    return worlds


def save_worlds(worlds: list[World], path: Path | None = None) -> None:
    target = path or worlds_path()
    blocks: list[str] = []
    for world in worlds:
        port = parse_port(world.port)
        lines = [
            "[[world]]",
            f"name = {_quote(world.name)}",
            f"host = {_quote(world.host)}",
            f"port = {port}",
            f"tls = {'true' if world.tls else 'false'}",
        ]
        if world.sounds:
            lines.append(f"sounds = {_quote(world.sounds)}")
        encoding = normalize_encoding(world.encoding)
        if encoding != AUTO:  # absent means auto, which keeps files older builds wrote as-is
            lines.append(f"encoding = {_quote(encoding)}")
        blocks.append("\n".join(lines))
    atomic_write_text(target, "\n\n".join(blocks) + "\n")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
