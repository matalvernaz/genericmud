"""User preferences for the in-app updater, persisted as TOML beside worlds/soundpacks.

Mirrors :mod:`genericmud.config.worlds`: ``tomllib`` reads, and saving hand-writes the
small fixed schema (tomllib is read-only). Three user choices are remembered so the
background check doesn't nag:

* ``check_enabled`` -- turn the automatic check off entirely.
* ``snoozed_until`` -- "Remind me later" defers all prompts until this time.
* ``skipped_version`` -- "Skip this version" silences one specific release.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genericmud.config.worlds import config_dir

SNOOZE_DURATION = timedelta(days=3)  # how long "Remind me later" defers the next prompt


@dataclass
class UpdatePrefs:
    check_enabled: bool = True
    snoozed_until: str | None = None  # ISO 8601 UTC timestamp
    snoozed_version: str | None = None  # tag the snooze was set on; scopes it to that release
    skipped_version: str | None = None  # a release tag, e.g. "v0.6.0"
    last_check: str | None = None  # ISO 8601 UTC timestamp, for diagnostics


def prefs_path() -> Path:
    return config_dir() / "update-prefs.toml"


def load_prefs(path: Path | None = None) -> UpdatePrefs:
    target = path or prefs_path()
    if not target.exists():
        return UpdatePrefs()
    data = tomllib.loads(target.read_text(encoding="utf-8")).get("update", {})
    return UpdatePrefs(
        check_enabled=bool(data.get("check_enabled", True)),
        snoozed_until=data.get("snoozed_until"),
        snoozed_version=data.get("snoozed_version"),
        skipped_version=data.get("skipped_version"),
        last_check=data.get("last_check"),
    )


def save_prefs(prefs: UpdatePrefs, path: Path | None = None) -> None:
    target = path or prefs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[update]", f"check_enabled = {'true' if prefs.check_enabled else 'false'}"]
    for key in ("snoozed_until", "snoozed_version", "skipped_version", "last_check"):
        value = getattr(prefs, key)
        if value:
            lines.append(f"{key} = {_quote(str(value))}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_snoozed(prefs: UpdatePrefs, now: datetime | None = None) -> bool:
    """True while a "Remind me later" snooze is still in effect.

    A malformed or missing timestamp is treated as not snoozed, so a corrupted prefs file
    can never permanently suppress update prompts.
    """
    if not prefs.snoozed_until:
        return False
    try:
        until = datetime.fromisoformat(prefs.snoozed_until)
    except ValueError:
        return False
    return (now or datetime.now(UTC)) < until


def snooze_timestamp(now: datetime | None = None) -> str:
    """ISO timestamp :data:`SNOOZE_DURATION` from now, to store in ``snoozed_until``."""
    return ((now or datetime.now(UTC)) + SNOOZE_DURATION).isoformat()


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
