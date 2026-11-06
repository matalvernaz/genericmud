"""Persisted UI/speech toggles, one small TOML beside worlds and update prefs.

Mirrors :mod:`genericmud.config.update_prefs`: ``tomllib`` reads, saving
hand-writes the small boolean/integer/string schema. These are the settings a user sets
once and expects to survive a restart:

* ``background_silence`` -- stay quiet while another window has focus
  (triggers and sounds keep running; speech resumes on return).
* ``silence_other_worlds`` -- only the world you're looking at speaks. Off lets
  every open world speak, so nine worlds sound like nine worlds rather than one
  and eight silences.
* ``numpad_compass`` -- numpad walks (8/2/4/6 + diagonals, 5/0 look, . scan,
  minus up, plus down). Off for NVDA desktop-layout users who keep the numpad
  for object review.
* ``command_input_mode`` -- standard or editable command input for new native sessions.
* ``autoretype`` -- Enter on an empty input resends the last command.
* ``follow_mode`` -- speech interrupts on room movement, not on every line.
* ``interrupt_mode`` -- every incoming line interrupts instead of queueing.
* ``title_shows_world`` -- call the window after the world in front, as MUSHclient
  does ("genericMud - [Penworthy]") instead of just "genericMud". Off by default,
  because renaming someone's window is not a change to spring on them.
* ``auto_reconnect`` / ``reconnect_interval`` -- recover unavailable connections,
  waiting 1–300 seconds between attempts (on, every five seconds by default).
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from genericmud.config.atomic import atomic_write_text
from genericmud.config.worlds import config_dir


@dataclass
class UiPrefs:
    background_silence: bool = False
    silence_other_worlds: bool = True
    numpad_compass: bool = True
    command_input_mode: str = "standard"
    autoretype: bool = False
    follow_mode: bool = False
    interrupt_mode: bool = False
    title_shows_world: bool = False
    auto_reconnect: bool = True
    reconnect_interval: int = 5


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
    # Migrate the unpublished prototype preference without adding another setting.
    if "command_input_mode" not in data and data.get("editable_command_buffer") is True:
        data["command_input_mode"] = "editable"
    defaults = UiPrefs()
    return UiPrefs(
        **{
            f.name: (
                data[f.name]
                if type(data.get(f.name)) is type(getattr(defaults, f.name))
                and (f.name != "reconnect_interval" or 1 <= data[f.name] <= 300)
                and (f.name != "command_input_mode" or data[f.name] in ("standard", "editable"))
                else getattr(defaults, f.name)
            )
            for f in fields(UiPrefs)
        }
    )


def save_ui_prefs(prefs: UiPrefs, path: Path | None = None) -> None:
    target = path or prefs_path()
    lines = ["[ui]"] + [
        f"{f.name} = " + (json.dumps(getattr(prefs, f.name))
                         if isinstance(getattr(prefs, f.name), str)
                         else str(getattr(prefs, f.name)).lower())
        for f in fields(UiPrefs)
    ]
    atomic_write_text(target, "\n".join(lines) + "\n")
