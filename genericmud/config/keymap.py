"""Load a keymap profile (combo string -> action string) from TOML."""

from __future__ import annotations

import tomllib

from genericmud.resources import resource_root


def load_keymap(name: str = "vipmud") -> dict[str, str]:
    path = resource_root() / "genericmud" / "config" / "keymaps" / f"{name}.toml"
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return dict(data.get("keys", {}))
