"""Transactional in-place upgrades: back up, verify, and roll back a zip overlay.

Ported from Libation 13.5's ``InstallUpgradeManager`` (rmcrackan/Libation, MIT). The
download-and-swap helper (``ZipExtractor.exe``) is not atomic: if a file is locked, the
disk fills mid-extract, or the helper dies after replacing some files, the install is
left half-upgraded -- a new ``genericMud.exe`` running against stale ``_internal``
libraries -- which then crashes or misbehaves in ways that are hard to diagnose. The
download path can verify the *bytes it downloaded*, but nothing verifies the *result of
the extraction*.

This closes that gap. Immediately before the overlay we snapshot the critical install
files and record the SHA-256 each one *should* have afterwards (read straight out of the
upgrade zip). At the next startup -- before the UI's native extensions load -- we
re-hash those files on disk; if any critical file was not actually replaced we restore
the snapshot and surface a recovery message instead of running a broken install.

"Critical files" is a small named subset (the exe, the helper, the Python runtime DLL,
``base_library.zip``), not the whole ``_internal`` tree. A partial overlay almost always
leaves the running exe or a core runtime file stale -- those are the ones most likely to
be locked -- so verifying the subset catches the failure without hashing hundreds of
files on every launch.

Everything here is pure ``pathlib``/``hashlib``/``zipfile``/``json`` -- no Windows API
and no new dependency -- so it runs and is tested on any platform even though the swap it
guards only happens on the frozen Windows build.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Layout of the per-install upgrade-state folder. It lives beside the exe and is NOT part
# of the release zip, so the overlay never touches it: the marker + backup written by the
# outgoing version survive the swap and are read by the incoming version at startup.
STATE_DIR_NAME = ".genericmud-upgrade"
PENDING_FILE_NAME = "pending.json"
BACKUP_DIR_NAME = "backup"

# The main executable's name is passed in per-call (the caller knows it); these are the
# extra install files whose replacement proves the overlay landed, checked when present.
_HELPER_EXE = "ZipExtractor.exe"
_INTERNAL_DIR = "_internal"
_INTERNAL_CORE = "base_library.zip"  # PyInstaller's stdlib bundle -- always present
_PYTHON_DLL_GLOB = "python3*.dll"  # the interpreter itself, e.g. python313.dll

_HASH_CHUNK = 1 << 20  # 1 MiB: stream large files instead of loading them whole


class UpgradeIntegrityError(Exception):
    """The upgrade package is missing files we must be able to verify against."""


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of a startup recovery check when a pending upgrade was found."""

    rolled_back: bool
    title: str
    message: str
    failed_files: list[str] = field(default_factory=list)


def _state_dir(install_dir: Path) -> Path:
    return install_dir / STATE_DIR_NAME


def _pending_path(install_dir: Path) -> Path:
    return _state_dir(install_dir) / PENDING_FILE_NAME


def _backup_dir(install_dir: Path) -> Path:
    return _state_dir(install_dir) / BACKUP_DIR_NAME


def critical_files(install_dir: Path, exe_name: str) -> list[str]:
    """Relative POSIX paths whose post-overlay content proves the swap succeeded.

    The set is the main exe, the update helper, and the two load-bearing runtime files
    under ``_internal`` -- discovered from what is actually on disk so the check adapts to
    the PyInstaller layout without a hard-coded interpreter version.
    """
    names: set[str] = {exe_name}
    if (install_dir / _HELPER_EXE).is_file():
        names.add(_HELPER_EXE)
    internal = install_dir / _INTERNAL_DIR
    if internal.is_dir():
        if (internal / _INTERNAL_CORE).is_file():
            names.add(f"{_INTERNAL_DIR}/{_INTERNAL_CORE}")
        for dll in internal.glob(_PYTHON_DLL_GLOB):
            names.add(f"{_INTERNAL_DIR}/{dll.name}")
    return sorted(names)


def prepare_for_upgrade(
    install_dir: Path, upgrade_zip: Path, target_version: str, *, exe_name: str
) -> None:
    """Snapshot critical files and record their expected post-upgrade hashes.

    Call immediately before launching the extractor. ``upgrade_zip`` is the *flat* zip the
    helper will overlay onto ``install_dir`` (entries at the archive root), so its critical
    entries are exactly what the on-disk files should become. Writes a ``pending.json``
    marker that :func:`recover_pending_upgrade` reads on the next launch.
    """
    install_dir = Path(install_dir)
    upgrade_zip = Path(upgrade_zip)
    if not install_dir.is_dir():
        raise NotADirectoryError(f"install directory not found: {install_dir}")
    if not upgrade_zip.is_file():
        raise FileNotFoundError(f"upgrade zip not found: {upgrade_zip}")

    names = critical_files(install_dir, exe_name)
    expected = _expected_hashes_from_zip(upgrade_zip, names)

    state_dir = _state_dir(install_dir)
    backup_dir = _backup_dir(install_dir)
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)
    backup_dir.mkdir(parents=True)

    backed_up: list[str] = []
    for rel in names:
        source = install_dir / rel
        if not source.is_file():
            continue
        destination = backup_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        backed_up.append(rel)

    pending = {
        "target_version": target_version,
        "started_utc": datetime.now(UTC).isoformat(),
        "expected_file_hashes_sha256": expected,
        "backed_up_files": backed_up,
    }
    _pending_path(install_dir).write_text(json.dumps(pending, indent=2), encoding="utf-8")
    logger.info(
        "Prepared in-app upgrade to %s: backed up %d file(s), expecting %d to change.",
        target_version, len(backed_up), len(expected),
    )


def recover_pending_upgrade(install_dir: Path) -> RecoveryResult | None:
    """Verify a pending upgrade at startup; roll back from the snapshot on failure.

    Returns ``None`` when there is nothing to do (no pending marker) or the upgrade
    verified cleanly -- in the clean case the state folder is deleted. Returns a
    :class:`RecoveryResult` when a rollback happened, so the UI can tell the user. Never
    raises: a recovery fault must not stop the app from starting.
    """
    install_dir = Path(install_dir)
    pending_path = _pending_path(install_dir)
    if not pending_path.is_file():
        return None

    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        expected = dict(pending["expected_file_hashes_sha256"])
        target = str(pending.get("target_version", "unknown"))
    except (OSError, ValueError, KeyError) as exc:
        logger.error("Unreadable pending upgrade marker; attempting emergency rollback: %s", exc)
        return _rollback(install_dir, "unknown", ["Upgrade marker was unreadable."])

    failed = verify_install(install_dir, expected)
    if not failed:
        _clear_state(install_dir)
        logger.info("In-app upgrade to %s verified at startup.", target)
        return None

    logger.error("Incomplete in-app upgrade to %s detected: %s", target, "; ".join(failed))
    return _rollback(install_dir, target, failed)


def verify_install(install_dir: Path, expected: dict[str, str]) -> list[str]:
    """Return a list of human-readable failures; empty means every critical file matches."""
    install_dir = Path(install_dir)
    failed: list[str] = []
    for rel, expected_hash in expected.items():
        path = install_dir / rel
        if not path.is_file():
            failed.append(f"{rel}: missing from the install folder")
            continue
        if _sha256_file(path).lower() != expected_hash.lower():
            failed.append(f"{rel}: on-disk content was not replaced by the upgrade")
    return failed


def _rollback(install_dir: Path, target: str, failed: list[str]) -> RecoveryResult:
    restored = _restore_from_backup(install_dir)
    _clear_state(install_dir)
    logger.error("Rolled back %d file(s) after a failed upgrade to %s.", len(restored), target)
    title = "Update failed — genericMud was restored"
    message = (
        f"genericMud tried to update to version {target}, but one or more program files "
        "were not replaced correctly, so it restored the previous version from a backup.\n\n"
        "Your worlds, soundpacks, credentials, and logs are stored separately and were not "
        "touched.\n\n"
        "To update, quit genericMud, download the latest release zip, and unzip it into a "
        "fresh folder.\n\n"
        "Details:\n" + "\n".join(f"  - {item}" for item in failed)
    )
    return RecoveryResult(rolled_back=True, title=title, message=message, failed_files=failed)


def _restore_from_backup(install_dir: Path) -> list[str]:
    backup_dir = _backup_dir(install_dir)
    restored: list[str] = []
    if not backup_dir.is_dir():
        return restored
    for source in backup_dir.rglob("*"):
        if not source.is_file():
            continue
        rel = source.relative_to(backup_dir)
        destination = install_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        restored.append(rel.as_posix())
    return restored


def _clear_state(install_dir: Path) -> None:
    shutil.rmtree(_state_dir(install_dir), ignore_errors=True)


def _expected_hashes_from_zip(upgrade_zip: Path, names: list[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    with zipfile.ZipFile(upgrade_zip) as archive:
        for rel in names:
            info = _find_entry(archive, rel)
            if info is None:
                logger.warning("Upgrade zip is missing an expected file: %s", rel)
                continue
            with archive.open(info) as stream:
                expected[rel] = _sha256_stream(stream)
    if not expected:
        raise UpgradeIntegrityError("Upgrade zip contains none of the verifiable install files.")
    return expected


def _find_entry(archive: zipfile.ZipFile, rel: str) -> zipfile.ZipInfo | None:
    """Match a critical file in the zip by exact path, falling back to basename.

    The flat zip mirrors the install layout so the exact path normally hits; the basename
    fallback covers a release packaged with a stray top-level folder.
    """
    try:
        return archive.getinfo(rel)
    except KeyError:
        base = rel.rsplit("/", 1)[-1]
        for info in archive.infolist():
            if not info.is_dir() and info.filename.rsplit("/", 1)[-1] == base:
                return info
        return None


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_stream(stream) -> str:
    hasher = hashlib.sha256()
    for chunk in iter(lambda: stream.read(_HASH_CHUNK), b""):
        hasher.update(chunk)
    return hasher.hexdigest()
