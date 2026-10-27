"""Persisted UI/speech toggles, one small TOML beside worlds and update prefs.

Mirrors :mod:`genericmud.config.update_prefs`: ``tomllib`` reads, saving
hand-writes the fixed all-boolean schema. These are the toggles a user sets
once and expects to survive a restart:

* ``background_silence`` -- stay quiet while another window has focus
  (triggers and sounds keep running; speech resumes on return).
* ``numpad_compass`` -- numpad walks (8/2/4/6 + diagonals, 5/0 look, . scan,
  minus up, plus down). Off for NVDA desktop-layout users who keep the numpad
  for object review.
* ``autoretype`` -- Enter on an empty input resends the last command.
* ``follow_mode`` -- speech interrupts on room movement, not on every line.
* ``interrupt_mode`` -- every incoming line interrupts instead of queueing.
* ``title_shows_world`` -- call the window after the world in front, as MUSHclient
  does ("genericMud - [Penworthy]") instead of just "genericMud". Off by default,
  because renaming someone's window is not a change to spring on them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from genericmud.config.atomic import atomic_write_text
from genericmud.config.worlds import config_dir


@dataclass
class UiPrefs:
    background_silence: bool = False
    numpad_compass: bool = True
    autoretype: bool = False
    follow_mode: bool = False
    interrupt_mode: bool = False
    title_shows_world: bool = False


def prefs_path() -> Path:
    return config_dir() / "ui-prefs.toml"


def load_ui_prefs(path: Path | None = None) -> UiPrefs:
    target = path or prefs_path()
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8")).get("ui", {})
    except (OSError, ValueError):
        return UiPrefs()  # missing or corrupt file: defaults, never a crash
    if not isinstance(data, dict):
        return UiPrefs()
    defaults = UiPrefs()
    return UiPrefs(
        **{
            f.name: (
                data[f.name]
                if isinstance(data.get(f.name), bool)
                else getattr(defaults, f.name)
            )
            for f in fields(UiPrefs)
        }
    )


def save_ui_prefs(prefs: UiPrefs, path: Path | None = None) -> None:
    target = path or prefs_path()
    lines = ["[ui]"] + [
        f"{f.name} = {'true' if getattr(prefs, f.name) else 'false'}"
        for f in fields(UiPrefs)
    ]
    atomic_write_text(target, "\n".join(lines) + "\n")
