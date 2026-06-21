"""Per-world login credentials, behind a swappable store interface.

:class:`PlaintextCredentialStore` keeps username/password in a JSON file
(``~/.genericmud/credentials.json``) — simple and zero-dependency, but readable
on disk, so fine only on a trusted single-user machine. The app depends only on
the :class:`CredentialStore` protocol, so a keyring-backed store (Windows
Credential Manager / macOS Keychain / libsecret) can replace it later with no
caller changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class CredentialStore(Protocol):
    def get(self, world: str) -> tuple[str, str] | None:
        """(username, password) for ``world``, or None if none stored."""
        ...

    def set(self, world: str, username: str, password: str) -> None: ...

    def delete(self, world: str) -> None: ...


class PlaintextCredentialStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def get(self, world: str) -> tuple[str, str] | None:
        entry = self._load().get(world)
        if not entry:
            return None
        return entry.get("username", ""), entry.get("password", "")

    def set(self, world: str, username: str, password: str) -> None:
        data = self._load()
        data[world] = {"username": username, "password": password}
        self._save(data)

    def delete(self, world: str) -> None:
        data = self._load()
        if data.pop(world, None) is not None:
            self._save(data)

    def _load(self) -> dict:
        if not self._path.is_file():
            return {}
        with open(self._path, encoding="utf-8") as handle:
            return json.load(handle)

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
