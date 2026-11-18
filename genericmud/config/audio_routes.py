"""Machine-local audio destinations. Rules carry only a route name, never a device index."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from genericmud.config.atomic import atomic_write_text
from genericmud.config.worlds import config_dir

FALLBACK_CHOICES = ("default", "silent")


@dataclass(frozen=True)
class AudioOutput:
    platform: str
    identifier: str  # BASS endpoint ID / UID / ALSA name, not its enumeration index
    name: str  # last known display name; not used to choose hardware
    fallback: str = "default"  # this route's policy, independent of other routes to the device


def routes_path() -> Path:
    return config_dir() / "audio-routes.json"


def load_audio_routes(path: Path | None = None) -> dict[str, AudioOutput]:
    try:
        data = json.loads((path or routes_path()).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("unrecognized audio routes file")
    routes = data.get("routes")
    if not isinstance(routes, dict):
        raise ValueError("audio routes must be an object")
    result = {}
    for name, output in routes.items():
        if (not isinstance(name, str) or not name.strip() or not isinstance(output, dict)
                or any(not isinstance(output.get(key), str) or not output[key]
                       for key in ("platform", "identifier", "name"))):
            raise ValueError("invalid audio route")
        fallback = output.get("fallback", "default")
        if fallback not in FALLBACK_CHOICES:
            raise ValueError(f"invalid fallback for audio route {name}")
        result[name] = AudioOutput(
            **{key: output[key] for key in ("platform", "identifier", "name")}, fallback=fallback,
        )
    return result


def save_audio_routes(routes: dict[str, AudioOutput], path: Path | None = None) -> None:
    atomic_write_text(path or routes_path(), json.dumps(
        {"version": 1, "routes": {name: asdict(output) for name, output in routes.items()}},
        indent=2, ensure_ascii=False, sort_keys=True,
    ) + "\n")
