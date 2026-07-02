"""Fetching a pack straight from its own git repo, skipping the installer (#git-sources)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from genericmud.packs import git_sources, vault
from genericmud.packs.setup import setup_pack_from_git
from genericmud.packs.store import PackStore

_MCL = '<muclient><world site="erionmud.com" port="1234" name="Erion"/></muclient>'


def _archive(path: Path, *, wrapper: str = "soundpack-master") -> bytes:
    """A gitlab-style archive: everything under one ``<repo>-<branch>/`` wrapper dir, whose repo
    root has more than one entry (like Erion's) so _pack_root stops there, not inside MUSHclient."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(wrapper + "/MUSHclient/worlds/Erion MUD.mcl", _MCL)
        zf.writestr(f"{wrapper}/MUSHclient/sounds/hit.ogg", b"OggS")
        zf.writestr(f"{wrapper}/CHANGELOG", "v1\n")  # a repo-root sibling (not a lone dir)
    return path.read_bytes()


def _downloader(by_url: dict[str, bytes]):
    """A vault.download stand-in serving prebuilt archives; a missing url 404s (branch fallback)."""

    def download(url, dest, **_kwargs):
        if url not in by_url:
            raise FileNotFoundError(url)
        Path(dest).write_bytes(by_url[url])

    return download


def test_registry_matches_erion_by_mud_and_name():
    assert git_sources.for_labels("Erion MUD").id == "erion"
    assert git_sources.for_labels("x", "Erion Mud Soundpack").id == "erion"
    assert git_sources.for_labels("Some Other MUD") is None


def test_setup_from_git_installs_under_curated_id(tmp_path):
    store = PackStore(tmp_path / "store")
    source = git_sources.by_id("erion")
    master = vault.git_archive_urls(source.repo_url)[0]
    result = setup_pack_from_git(
        store, source, download=_downloader({master: _archive(tmp_path / "a.zip")})
    )
    # Installed under the curated id -- NOT the wrapper dir or the temp-dir 'source'.
    assert result.manifest.id == "erion"
    assert result.manifest.origin == source.repo_url  # clean origin for later updates
    assert result.manifest.dialect == "mushclient"
    assert result.enabled_for == "Erion"
    assert store.is_enabled("erion", "Erion")
    assert not store.is_trusted("erion")  # code-exec dialect: the user trusts it deliberately
    # The wrapper dir was stripped and the curated entry resolves inside the installed pack.
    assert store.entry_path("erion").is_file()
    assert store.entry_path("erion").name == "Erion MUD.mcl"


def test_setup_from_git_falls_back_master_to_main(tmp_path):
    store = PackStore(tmp_path / "store")
    source = git_sources.by_id("erion")
    urls = vault.git_archive_urls(source.repo_url)  # [master, main]
    # Only the main archive exists, so the master URL 404s and setup must try the next.
    main_archive = _archive(tmp_path / "a.zip", wrapper="soundpack-main")
    result = setup_pack_from_git(store, source, download=_downloader({urls[1]: main_archive}))
    assert result.manifest.id == "erion"
    assert store.entry_path("erion").name == "Erion MUD.mcl"


def test_setup_from_git_reinstall_replaces_in_place(tmp_path):
    store = PackStore(tmp_path / "store")
    source = git_sources.by_id("erion")
    master = vault.git_archive_urls(source.repo_url)[0]
    dl = _downloader({master: _archive(tmp_path / "a.zip")})
    setup_pack_from_git(store, source, download=dl)
    setup_pack_from_git(store, source, download=dl)  # update path: same id, no PackExists
    assert [m.id for m in store.installed()] == ["erion"]
