"""Transactional upgrade: diff-based verify set, backup, and best-effort rollback.

The overlay itself is ZipExtractor's job on Windows; here we fake it by writing files onto the
"install" directory, so the diff/backup/verify/rollback logic is exercised on any platform.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from genericmud.update import upgrade_manager as um

_OLD = {
    "genericMud.exe": b"old-exe",
    "_internal/base_library.zip": b"old-base",
    "_internal/python313.dll": b"old-dll",
    "_internal/unchanged.pyd": b"same-bytes",  # identical in NEW -> skipped
}
_NEW = {
    "genericMud.exe": b"new-exe",
    "_internal/base_library.zip": b"new-base",
    "_internal/python313.dll": b"new-dll",
    "_internal/unchanged.pyd": b"same-bytes",  # identical -> not verified/backed up
    "_internal/added.pyd": b"brand-new-file",  # a file the upgrade adds
}
_CHANGED = {"genericMud.exe", "_internal/base_library.zip", "_internal/python313.dll"}


def _write_tree(root: Path, contents: dict[str, bytes]) -> None:
    for rel, data in contents.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _make_zip(path: Path, contents: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for rel, data in contents.items():
            archive.writestr(rel, data)
    return path


def _prepared_install(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "app"
    install.mkdir()
    _write_tree(install, _OLD)
    upgrade_zip = _make_zip(tmp_path / "upgrade.zip", _NEW)
    um.prepare_for_upgrade(install, upgrade_zip, "v9.9.9", exe_name="genericMud.exe")
    return install, upgrade_zip


def test_files_to_verify_is_the_changed_set(tmp_path):
    install = tmp_path / "app"
    _write_tree(install, _OLD)
    upgrade_zip = _make_zip(tmp_path / "u.zip", _NEW)
    changed = um.files_to_verify(install, upgrade_zip)
    assert set(changed) == _CHANGED | {"_internal/added.pyd"}
    assert "_internal/unchanged.pyd" not in changed  # identical -> skipped


def test_prepare_backs_up_only_changed_existing_files(tmp_path):
    install, _ = _prepared_install(tmp_path)
    backup = install / um.STATE_DIR_NAME / um.BACKUP_DIR_NAME
    assert (install / um.STATE_DIR_NAME / um.PENDING_FILE_NAME).is_file()
    for rel in _CHANGED:
        assert (backup / rel).is_file()
    assert not (backup / "_internal/unchanged.pyd").exists()  # unchanged -> not backed up
    assert not (backup / "_internal/added.pyd").exists()  # new file -> nothing to back up


def test_prepare_raises_when_nothing_changes(tmp_path):
    install = tmp_path / "app"
    _write_tree(install, _OLD)
    same_zip = _make_zip(tmp_path / "same.zip", _OLD)  # identical content
    with pytest.raises(um.UpgradeIntegrityError):
        um.prepare_for_upgrade(install, same_zip, "v1", exe_name="")  # no exe anchor


def test_recover_clean_when_overlay_succeeds(tmp_path):
    install, _ = _prepared_install(tmp_path)
    _write_tree(install, _NEW)  # a complete overlay: every changed file now matches the zip
    assert um.recover_pending_upgrade(install) is None
    assert not (install / um.STATE_DIR_NAME).exists()  # state cleared on success


def test_recover_rolls_back_partial_overlay(tmp_path):
    install, _ = _prepared_install(tmp_path)
    # Partial overlay: exe + dll replaced, but base_library.zip left stale (e.g. it was locked).
    (install / "genericMud.exe").write_bytes(_NEW["genericMud.exe"])
    (install / "_internal/python313.dll").write_bytes(_NEW["_internal/python313.dll"])

    result = um.recover_pending_upgrade(install)

    assert result is not None and result.rolled_back
    assert any("base_library" in item for item in result.failed_files)
    # Everything restored to the pre-upgrade snapshot, not left half-new.
    for rel in _CHANGED:
        assert (install / rel).read_bytes() == _OLD[rel]
    assert not (install / um.STATE_DIR_NAME).exists()


def test_recover_emergency_rollback_on_corrupt_marker(tmp_path):
    install, _ = _prepared_install(tmp_path)
    (install / "genericMud.exe").write_bytes(b"half-written")
    (install / um.STATE_DIR_NAME / um.PENDING_FILE_NAME).write_text("{ not valid json")

    result = um.recover_pending_upgrade(install)

    assert result is not None and result.rolled_back
    assert (install / "genericMud.exe").read_bytes() == _OLD["genericMud.exe"]
    assert not (install / um.STATE_DIR_NAME).exists()


def test_recover_noop_without_pending(tmp_path):
    install = tmp_path / "app"
    _write_tree(install, _OLD)
    assert um.recover_pending_upgrade(install) is None
