"""Saved MUD worlds (name/host/port/tls/sounds), persisted as TOML.

tomllib is read-only, so saving hand-writes the small, well-defined schema.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class World:
    name: str
    host: str
    port: int
    tls: bool = False
    sounds: str | None = None


def config_dir() -> Path:
    return Path.home() / ".genericmud"


def worlds_path() -> Path:
    return config_dir() / "worlds.toml"


def load_worlds(path: Path | None = None) -> list[World]:
    target = path or worlds_path()
    if not target.exists():
        return []
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    worlds: list[World] = []
    for entry in data.get("world", []):
        worlds.append(
            World(
                name=str(entry.get("name", "")),
                host=str(entry.get("host", "")),
                port=int(entry.get("port", 0)),
                tls=bool(entry.get("tls", False)),
                sounds=entry.get("sounds"),
            )
        )
    return worlds


def save_worlds(worlds: list[World], path: Path | None = None) -> None:
    target = path or worlds_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for world in worlds:
        lines = [
            "[[world]]",
            f"name = {_quote(world.name)}",
            f"host = {_quote(world.host)}",
            f"port = {world.port}",
            f"tls = {'true' if world.tls else 'false'}",
        ]
        if world.sounds:
            lines.append(f"sounds = {_quote(world.sounds)}")
        blocks.append("\n".join(lines))
    target.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
