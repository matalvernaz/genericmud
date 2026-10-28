"""Per-world login credentials, behind a swappable store interface.

Two implementations of the same :class:`CredentialStore` protocol:

* :class:`KeyringCredentialStore` keeps them in the operating system's credential store
  (Windows Credential Manager, the macOS Keychain, or the Secret Service on Linux) through
  ``keyring``. The OS encrypts them at rest and unlocks with the user's own login, so there
  is no master password to type and nothing readable among the app's other files.
* :class:`PlaintextCredentialStore` keeps username/password in a JSON file
  (``~/.genericmud/credentials.json``) -- simple and zero-dependency, but readable on disk,
  so it is only the fallback for a machine with no working credential store.

:func:`open_credential_store` picks between them once, at startup, and moves any logins
already in the file into the OS store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from genericmud.config.atomic import atomic_write_text

CREDENTIAL_SERVICE = "genericmud"  # what the OS credential store files these under


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
        if not isinstance(entry, dict):
            return None
        username = entry.get("username")
        password = entry.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        return username, password

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
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            # A corrupt/partial file (e.g. a crash mid-write before this was atomic) must not
            # crash session startup; treat it as no stored credentials.
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        # The atomic helper's mkstemp file is owner-only on POSIX, so plaintext
        # credentials stay private as well as protected from partial writes.
        atomic_write_text(self._path, json.dumps(data, indent=2, sort_keys=True))


class KeyringCredentialStore:
    """Username/password in the operating system's own credential store.

    One entry per world, keyed by the world's name. Every backend stores a single opaque
    string, so the pair travels as JSON inside it -- which also means a world rename has to
    move its entry, or the login silently stops being found.
    """

    def __init__(self, service: str = CREDENTIAL_SERVICE) -> None:
        import keyring  # imported here so this module still imports without the dependency

        self._keyring = keyring
        self._service = service

    def get(self, world: str) -> tuple[str, str] | None:
        try:
            raw = self._keyring.get_password(self._service, world)
        except Exception:  # noqa: BLE001 - a locked or vanished store must not stop a session
            return None
        if not isinstance(raw, str):
            return None
        try:
            entry = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(entry, dict):
            return None
        username, password = entry.get("username"), entry.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        return username, password

    def set(self, world: str, username: str, password: str) -> None:
        payload = json.dumps({"username": username, "password": password})
        self._keyring.set_password(self._service, world, payload)

    def delete(self, world: str) -> None:
        try:
            self._keyring.delete_password(self._service, world)
        except Exception:  # noqa: BLE001 - already absent, or the store went away
            pass


def migrate_plaintext_to_keyring(path: Path, store: KeyringCredentialStore) -> int:
    """Move logins from the plaintext file into ``store``, then delete the file.

    Returns how many moved. The file is removed only after every row has been written: an
    interrupted migration must leave the logins where they still are, never nowhere.
    """
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    moved = 0
    for world, entry in data.items():
        if not isinstance(entry, dict):
            continue
        username, password = entry.get("username"), entry.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            continue
        try:
            store.set(world, username, password)
        except Exception:  # noqa: BLE001 - keep the plaintext rather than lose the login
            return moved
        moved += 1
    try:
        path.unlink()
    except OSError:
        pass
    return moved


def _usable_keyring() -> tuple[object | None, str | None]:
    """The keyring module with a working backend, or None and why it can't be used."""
    try:
        import keyring
    except Exception as error:  # noqa: BLE001 - a missing dependency is just a fallback
        return None, f"the keyring library is unavailable ({error})"
    try:
        backend = keyring.get_keyring()
    except Exception as error:  # noqa: BLE001
        return None, f"no credential store is available ({error})"
    # keyring answers with its "fail" backend when the platform offers nothing usable, and
    # only raising on write. Check for it up front, so the fallback is decided at startup.
    if type(backend).__module__.endswith(".fail"):
        return None, "no credential store is available on this system"
    return keyring, None


def open_credential_store(path: Path) -> tuple[CredentialStore, str | None]:
    """The best credential store this machine offers, and why if it is the plaintext one.

    The reason is returned rather than logged so the app can say it out loud: a silent
    downgrade to a readable file is exactly what a user must not discover months later.
    """
    _keyring, reason = _usable_keyring()
    if _keyring is None:
        return PlaintextCredentialStore(path), reason
    store = KeyringCredentialStore()
    try:
        migrate_plaintext_to_keyring(path, store)
    except Exception as error:  # noqa: BLE001 - fall back rather than fail to start
        return PlaintextCredentialStore(path), f"logins stay in the plaintext file ({error})"
    return store, None
