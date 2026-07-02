"""Manifest-sync pack install/update: parse, diff, confine, verify, register."""

from __future__ import annotations

import gzip
import hashlib
from urllib.parse import unquote

from genericmud.config.worlds import World
from genericmud.packs import manifest_sync
from genericmud.packs.manifest_sources import ManifestSource
from genericmud.packs.setup import setup_pack_from_manifest
from genericmud.packs.store import PackStore


def _digest(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()[:16]  # stand-in for the manifest's opaque 16-hex hash


def _manifest_bytes(files: dict[str, bytes], *, gzipped=True, sizes=None) -> bytes:
    lines = []
    for rel, content in files.items():
        size = sizes[rel] if sizes and rel in sizes else len(content)
        lines.append(f"{_digest(content)} {size} {rel}")
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    return gzip.compress(raw) if gzipped else raw


def _make(files: dict[str, bytes], *, include=(), sizes=None):
    """A ManifestSource plus an offline downloader serving ``files`` and their manifest."""
    source = ManifestSource(
        id="testpack",
        name="Test Pack",
        mud="Test MUD",
        dialect="mushclient",
        base_url="http://packs.example.test/tree/",
        manifest_name="all.lst.gz",
        entry="worlds/w.mcl",
        world=World(name="Test MUD", host="mud.example.test", port=1234),
        include=include,
    )
    manifest = _manifest_bytes(files, sizes=sizes)

    def download(url, dest, **_kwargs):
        if url == source.manifest_url:
            payload = manifest
        else:
            rel = unquote(url[len(source.base_url):])
            payload = files[rel]  # KeyError => the sync asked for a file not in the tree
        dest.write_bytes(payload)
        return dest

    return source, download


def test_parse_manifest_gzip_plain_and_spaced_paths():
    files = {"a.ogg": b"aaa", "combat/you miss.ogg": b"miss", "worlds/w.mcl": b"<world/>"}
    parsed = manifest_sync.parse_manifest(_manifest_bytes(files))
    assert set(parsed) == set(files)
    assert parsed["combat/you miss.ogg"][1] == 4  # size, path-with-space preserved
    plain = manifest_sync.parse_manifest(_manifest_bytes(files, gzipped=False))
    assert plain.keys() == parsed.keys()  # plain (non-gzip) parses identically


def test_parse_manifest_skips_comments_and_blank_lines():
    raw = b"# a comment\n\nabc123 5 a.ogg\n   \nbad line without size fields\n"
    parsed = manifest_sync.parse_manifest(raw)
    assert parsed == {"a.ogg": ("abc123", 5)}


def test_sync_fresh_install_downloads_everything(tmp_path):
    files = {"worlds/w.mcl": b"<world/>", "sounds/hit.ogg": b"\x00\x01\x02", "a.txt": b"hello"}
    source, download = _make(files)
    result = manifest_sync.sync(source, tmp_path, download=download)
    assert result.ok and result.downloaded == 3 and result.skipped_unchanged == 0
    for rel, content in files.items():
        assert (tmp_path / rel).read_bytes() == content
    assert (tmp_path / manifest_sync._BASELINE_NAME).is_file()  # baseline committed


def test_sync_incremental_fetches_only_changed(tmp_path):
    files = {"worlds/w.mcl": b"<world/>", "sounds/hit.ogg": b"old"}
    source, download = _make(files)
    manifest_sync.sync(source, tmp_path, download=download)

    files2 = {"worlds/w.mcl": b"<world/>", "sounds/hit.ogg": b"NEWER"}  # only hit.ogg changed
    source2, download2 = _make(files2)
    result = manifest_sync.sync(source2, tmp_path, download=download2)
    assert result.downloaded == 1 and result.skipped_unchanged == 1
    assert (tmp_path / "sounds/hit.ogg").read_bytes() == b"NEWER"


def test_sync_deletes_files_removed_upstream(tmp_path):
    source, download = _make({"worlds/w.mcl": b"<world/>", "old.ogg": b"gone-soon"})
    manifest_sync.sync(source, tmp_path, download=download)
    assert (tmp_path / "old.ogg").is_file()

    source2, download2 = _make({"worlds/w.mcl": b"<world/>"})  # old.ogg dropped
    result = manifest_sync.sync(source2, tmp_path, download=download2)
    assert result.deleted == 1 and not (tmp_path / "old.ogg").exists()


def test_sync_rejects_path_traversal(tmp_path):
    source, download = _make({"worlds/w.mcl": b"ok", "../escape.txt": b"evil"})
    result = manifest_sync.sync(source, tmp_path, download=download)
    assert "../escape.txt" in result.rejected
    assert not (tmp_path.parent / "escape.txt").exists()  # never written outside the pack dir


def test_sync_size_mismatch_fails_and_withholds_baseline(tmp_path):
    files = {"worlds/w.mcl": b"<world/>", "sounds/hit.ogg": b"1234567890"}
    # manifest lies about hit.ogg's size -> the download's real size won't match
    source, download = _make(files, sizes={"sounds/hit.ogg": 999})
    result = manifest_sync.sync(source, tmp_path, download=download)
    assert not result.ok and any("hit.ogg" in f for f in result.failed)
    assert not (tmp_path / "sounds/hit.ogg").exists()  # the mismatched download was discarded
    assert not (tmp_path / manifest_sync._BASELINE_NAME).exists()  # baseline withheld on failure


def test_sync_include_filter_limits_the_subtree(tmp_path):
    files = {"worlds/w.mcl": b"world", "sounds/a.ogg": b"snd", "docs/readme.txt": b"skip me"}
    source, download = _make(files, include=("worlds/", "sounds/"))
    result = manifest_sync.sync(source, tmp_path, download=download)
    assert result.downloaded == 2
    assert (tmp_path / "worlds/w.mcl").is_file() and (tmp_path / "sounds/a.ogg").is_file()
    assert not (tmp_path / "docs/readme.txt").exists()


def test_setup_pack_from_manifest_registers_enabled_but_untrusted(tmp_path):
    files = {"worlds/w.mcl": b"<world/>", "sounds/hit.ogg": b"snd", "lua/x.lua": b"-- lib"}
    source, download = _make(files)
    store = PackStore(tmp_path / "store")
    result = setup_pack_from_manifest(store, source, download=download)

    assert result.manifest.id == "testpack"
    assert result.world.name == "Test MUD" and result.world.host == "mud.example.test"
    assert store.manifest("testpack").entry == "worlds/w.mcl"
    assert store.is_enabled("testpack", "Test MUD")
    # MUSHclient runs its own Lua -> installs enabled-but-untrusted; the user trusts deliberately.
    assert not store.is_trusted("testpack")
    assert (store.pack_dir("testpack") / "sounds/hit.ogg").read_bytes() == b"snd"


def test_mush_z_source_uses_https():
    """The bundled Mush-Z source must fetch over TLS (size-only integrity needs a safe channel)."""
    from genericmud.packs import manifest_sources

    src = manifest_sources.by_id("mush-z")
    assert src is not None
    assert src.base_url.startswith("https://")
    assert src.manifest_url.startswith("https://")
