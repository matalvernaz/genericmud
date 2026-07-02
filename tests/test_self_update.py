"""Updater core: version parsing, release selection, asset choice, Zip-Slip-safe extract.

The Windows file-swap itself can't run here; these cover the pure-Python decision logic that
determines whether an update is offered and that a malicious zip is refused.
"""

from __future__ import annotations

import zipfile

import pytest

from genericmud.update import self_update


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v1.2.3", (1, 2, 3)),
        ("1.2.3", (1, 2, 3)),
        ("v0.5.4", (0, 5, 4)),
        ("v1.2.3-beta", None),  # prerelease-shaped -> not offered as stable
        ("v1.2", None),
        ("nightly", None),
        ("", None),
    ],
)
def test_parse_version(tag, expected):
    assert self_update._parse_version(tag) == expected


def test_select_asset_prefers_windows_zip():
    assets = [
        {"name": "source.tar.gz", "browser_download_url": "x"},
        {"name": "genericMud-windows.zip", "browser_download_url": "y"},
    ]
    assert self_update._select_asset(assets)["name"] == "genericMud-windows.zip"


def test_select_asset_none_when_absent():
    assert self_update._select_asset([{"name": "notes.txt"}]) is None


def _release(tag, *, draft=False, with_asset=True):
    assets = (
        [{
            "name": "genericMud-windows.zip",
            "browser_download_url": f"http://x/{tag}.zip",
            "size": 42,
        }]
        if with_asset else []
    )
    return {
        "tag_name": tag, "draft": draft, "html_url": f"http://x/{tag}",
        "body": "notes", "assets": assets,
    }


def test_check_for_update_picks_newest_skipping_drafts(monkeypatch):
    releases = [
        _release("v0.5.1"),
        _release("v0.6.0"),  # newest non-draft, all-prerelease repo -> still selected
        _release("v0.7.0", draft=True),  # draft skipped despite higher version
        _release("nightly"),  # unparseable skipped
    ]
    monkeypatch.setattr(self_update, "_get_json", lambda _url: releases)
    monkeypatch.setattr(self_update, "current_version", lambda: "0.5.4")

    info = self_update.check_for_update()
    assert info is not None
    assert info["tag"] == "v0.6.0"
    assert info["download_url"] == "http://x/v0.6.0.zip"
    assert info["size"] == 42


def test_check_for_update_none_when_up_to_date(monkeypatch):
    monkeypatch.setattr(self_update, "_get_json", lambda _url: [_release("v0.6.0")])
    monkeypatch.setattr(self_update, "current_version", lambda: "0.6.0")
    assert self_update.check_for_update() is None


def test_check_for_update_none_when_newer_has_no_windows_asset(monkeypatch):
    monkeypatch.setattr(
        self_update, "_get_json", lambda _url: [_release("v0.6.0", with_asset=False)]
    )
    monkeypatch.setattr(self_update, "current_version", lambda: "0.5.4")
    assert self_update.check_for_update() is None


def test_check_for_update_none_when_current_version_unknown(monkeypatch):
    # A frozen build missing --copy-metadata resolves to no version: never guess an update.
    monkeypatch.setattr(self_update, "current_version", lambda: None)
    monkeypatch.setattr(self_update, "_get_json", lambda _url: [_release("v9.9.9")])
    assert self_update.check_for_update() is None


def test_safe_extract_normal_zip(tmp_path):
    src = tmp_path / "ok.zip"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("genericMud/genericMud.exe", b"exe")
        archive.writestr("genericMud/_internal/base_library.zip", b"lib")
    out = tmp_path / "out"
    self_update._safe_extract(src, out)
    assert (out / "genericMud" / "genericMud.exe").read_bytes() == b"exe"


@pytest.mark.parametrize("member", ["../escape.txt", "/abs/escape.txt"])
def test_safe_extract_rejects_traversal(tmp_path, member):
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr(member, b"pwned")
    with pytest.raises(RuntimeError):
        self_update._safe_extract(src, tmp_path / "out")


def test_current_version_reads_source_version():
    """current_version() returns genericmud.__version__ -- baked into every frozen build,
    unlike importlib.metadata, which needs --copy-metadata and silently returns nothing."""
    import genericmud

    assert self_update.current_version() == genericmud.__version__


def test_current_version_survives_missing_dist_metadata(monkeypatch):
    """The dead-updater mode: importlib.metadata can't resolve the dist. With __version__ in
    source, current_version must still answer (not None), so check_for_update keeps working."""
    import genericmud

    def _raise(_name):
        raise self_update.PackageNotFoundError("genericmud")

    monkeypatch.setattr(self_update, "_pkg_version", _raise)
    assert self_update.current_version() == genericmud.__version__  # source wins, metadata unused
