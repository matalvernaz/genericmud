"""Load a keymap profile (combo string -> action string) from TOML."""

from __future__ import annotations

import re
import sys
import tomllib

from genericmud.resources import resource_root

# Option counts as Alt on a Mac, and Option and a number types characters there (# on a UK
# keyboard, brackets and braces on a German one), so the Alt+number tab keys are left free.
# Shift is no help: Option+Shift and a number types a character too.
_OPTION_NUMBER = re.compile(r"alt\+(?:shift\+)?[0-9]")


def load_keymap(name: str = "vipmud") -> dict[str, str]:
    path = resource_root() / "genericmud" / "config" / "keymaps" / f"{name}.toml"
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    keys = dict(data.get("keys", {}))
    if sys.platform == "darwin":
        keys = {
            combo: action
            for combo, action in keys.items()
            if not (_OPTION_NUMBER.fullmatch(combo) and action.startswith("tab:"))
        }
    return keys
