"""Transactional in-place upgrades: back up, verify, and roll back a zip overlay.

Ported from Libation 13.5's ``InstallUpgradeManager`` (rmcrackan/Libation, MIT). The
download-and-swap helper (``ZipExtractor.exe``) is not atomic: if a file is locked, the
disk fills mid-extract, or the helper dies after replacing some files, the install is
left half-upgraded -- a new ``genericMud.exe`` running against stale ``_internal``
libraries -- which then crashes or misbehaves in ways that are hard to diagnose. The
download path can verify the *bytes it downloaded*, but nothing verifies the *result of
the extraction*.

This closes that gap. Immediately before the overlay we diff the upgrade zip against the
current install and, for every file whose content will CHANGE, record the SHA-256 it should
have afterwards and back up its current version. At the next startup -- before the UI's
native extensions load -- we re-hash those files on disk; if any was not actually replaced
we restore the backup and surface a recovery message instead of running a broken install.

The verified set is exactly the files the overlay must touch (not a hard-coded handful of
exe/DLL names), so a partial swap of ANY file is caught, while files that don't change are
skipped -- nothing to hash, nothing to back up. Verification only runs when a pending
marker exists (the first launch after an update), so the extra hashing is a one-off cost.
Rollback is best-effort: a file that can't be restored keeps the marker so the next launch
retries, rather than clearing it and booting a half-restored install.

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


def files_to_verify(install_dir: Path, upgrade_zip: Path) -> dict[str, str]:
    """Map each upgrade-zip member that must CHANGE on disk to its expected SHA-256.

    A member is included when its content differs from the current install (or is missing on
    disk) -- i.e. exactly the files the overlay has to replace. That is the precise set a partial
    swap could leave stale, so verifying it catches a broken overlay of ANY file (not just a
    hard-coded handful of exe/DLL names). Files identical in old and new are skipped: no risk,
    and nothing to back up. Directories and our own state folder are ignored.
    """
    install_dir = Path(install_dir)
    changed: dict[str, str] = {}
    with zipfile.ZipFile(upgrade_zip) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            rel = info.filename
            if rel.split("/", 1)[0] == STATE_DIR_NAME:
                continue  # never verify/overwrite our own marker or backup
            with archive.open(info) as stream:
                expected = _sha256_stream(stream)
            on_disk = install_dir / rel
            current = _sha256_file(on_disk) if on_disk.is_file() else None
            if current != expected:
                changed[rel] = expected
    return changed


def prepare_for_upgrade(
    install_dir: Path, upgrade_zip: Path, target_version: str, *, exe_name: str = ""
) -> None:
    """Back up the files this upgrade will change and record their expected post-swap hashes.

    Call immediately before launching the extractor. ``upgrade_zip`` is the *flat* zip the helper
    overlays onto ``install_dir`` (entries at the archive root), so its members line up with the
    install layout. Every member whose content differs from the current install is recorded and
    its current version backed up, so :func:`recover_pending_upgrade` can both detect a partial
    swap and restore the previous version. ``exe_name`` (if given) is always included as an
    anchor, even when this upgrade doesn't change it.
    """
    install_dir = Path(install_dir)
    upgrade_zip = Path(upgrade_zip)
    if not install_dir.is_dir():
        raise NotADirectoryError(f"install directory not found: {install_dir}")
    if not upgrade_zip.is_file():
        raise FileNotFoundError(f"upgrade zip not found: {upgrade_zip}")

    expected = files_to_verify(install_dir, upgrade_zip)
    if exe_name and exe_name not in expected:
        exe_path = install_dir / exe_name
        if exe_path.is_file():  # anchor on the exe even if this build didn't change it
            expected[exe_name] = _sha256_file(exe_path)
    if not expected:
        raise UpgradeIntegrityError("upgrade zip changes nothing verifiable in the install")

    state_dir = _state_dir(install_dir)
    backup_dir = _backup_dir(install_dir)
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)
    backup_dir.mkdir(parents=True)

    backed_up: list[str] = []
    for rel in expected:
        source = install_dir / rel
        if not source.is_file():
            continue  # a brand-new file has no prior version; its absence IS the rollback
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
        "Prepared in-app upgrade to %s: backed up %d file(s), verifying %d.",
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
    restored, restore_failures = _restore_from_backup(install_dir)
    if restore_failures:
        # Couldn't fully restore (a locked file, disk full). KEEP the marker so the next launch
        # retries the rollback, rather than clearing it and booting a half-restored install.
        logger.error(
            "Rollback of upgrade to %s incomplete: restored %d, failed %d.",
            target, len(restored), len(restore_failures),
        )
    else:
        _clear_state(install_dir)
        logger.error("Rolled back %d file(s) after a failed upgrade to %s.", len(restored), target)
    detail = failed + [f"could not restore {item}" for item in restore_failures]
    title = "Update failed — genericMud was restored"
    message = (
        f"genericMud tried to update to version {target}, but one or more program files "
        "were not replaced correctly, so it restored the previous version from a backup.\n\n"
        "Your worlds, soundpacks, credentials, and logs are stored separately and were not "
        "touched.\n\n"
        "To update, quit genericMud, download the latest release zip, and unzip it into a "
        "fresh folder.\n\n"
        "Details:\n" + "\n".join(f"  - {item}" for item in detail)
    )
    return RecoveryResult(rolled_back=True, title=title, message=message, failed_files=detail)


def _restore_from_backup(install_dir: Path) -> tuple[list[str], list[str]]:
    """Copy every backed-up file back over the install. Best-effort: a per-file failure is
    collected, not raised, so recovery runs before the UI/voice load and never blocks startup."""
    backup_dir = _backup_dir(install_dir)
    restored: list[str] = []
    failures: list[str] = []
    if not backup_dir.is_dir():
        return restored, failures
    for source in backup_dir.rglob("*"):
        if not source.is_file():
            continue
        rel = source.relative_to(backup_dir).as_posix()
        destination = install_dir / rel
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            restored.append(rel)
        except OSError as exc:
            failures.append(f"{rel}: {exc}")
    return restored, failures


def _clear_state(install_dir: Path) -> None:
    shutil.rmtree(_state_dir(install_dir), ignore_errors=True)


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
