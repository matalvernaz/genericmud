"""Pack manifest inference + PackStore install/enable/uninstall lifecycle."""

from __future__ import annotations

import json
import zipfile

import pytest

from genericmud.packs import (
    PackError,
    PackExists,
    PackStore,
    UnknownDialect,
    UnknownPack,
    infer_manifest,
    load_manifest,
    slugify,
)


def _bare_pack(tmp_path, name="hunting.lua", body='mud.send("look")'):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _manifest_pack(tmp_path):
    """A directory pack with an explicit pack.toml + a .set entry."""
    pack = tmp_path / "cosmic_src"
    pack.mkdir()
    (pack / "pack.toml").write_text(
        'name = "Cosmic Rage Immersion"\n'
        'version = "2.0"\n'
        'dialect = "vipmud"\n'
        'entry = "cosmic.set"\n'
        'worlds = ["cosmicrage.com"]\n',
        encoding="utf-8",
    )
    (pack / "cosmic.set").write_text("#alias hi {say hello}", encoding="utf-8")
    return pack


def test_slugify():
    assert slugify("Cosmic Rage Immersion!") == "cosmic-rage-immersion"
    assert slugify("   ") == "pack"


def test_infer_manifest_by_extension(tmp_path):
    m = infer_manifest(_bare_pack(tmp_path, "Hunting Helper.lua"))
    assert m.dialect == "lua"
    assert m.id == "hunting-helper"
    assert m.entry == "Hunting Helper.lua"


def test_infer_manifest_unknown_extension(tmp_path):
    with pytest.raises(UnknownDialect):
        infer_manifest(_bare_pack(tmp_path, "notes.txt", "x"))


def test_load_manifest_reads_pack_toml(tmp_path):
    m = load_manifest(_manifest_pack(tmp_path))
    assert m.dialect == "vipmud"
    assert m.name == "Cosmic Rage Immersion"
    assert m.version == "2.0"
    assert m.worlds == ("cosmicrage.com",)


def test_load_manifest_ambiguous_dir_without_toml(tmp_path):
    pack = tmp_path / "ambiguous"
    pack.mkdir()
    (pack / "a.lua").write_text("", encoding="utf-8")
    (pack / "b.lua").write_text("", encoding="utf-8")
    with pytest.raises(UnknownDialect):
        load_manifest(pack)


def test_install_bare_file_copies_and_indexes(tmp_path):
    store = PackStore(tmp_path / "store")
    manifest = store.install(_bare_pack(tmp_path, "hunting.lua"))
    assert manifest.id == "hunting"
    assert store.entry_path("hunting").read_text(encoding="utf-8") == 'mud.send("look")'
    assert [m.id for m in store.installed()] == ["hunting"]


def test_install_directory_pack_with_manifest(tmp_path):
    store = PackStore(tmp_path / "store")
    manifest = store.install(_manifest_pack(tmp_path))
    assert manifest.id == "cosmic-rage-immersion"
    assert store.entry_path(manifest.id).name == "cosmic.set"
    assert (store.pack_dir(manifest.id) / "pack.toml").is_file()  # content copied verbatim


def test_reinstall_requires_replace(tmp_path):
    store = PackStore(tmp_path / "store")
    src = _bare_pack(tmp_path, "hunting.lua")
    store.install(src)
    with pytest.raises(PackExists):
        store.install(src)
    src.write_text('mud.send("score")', encoding="utf-8")
    store.install(src, replace=True)  # update in place
    assert store.entry_path("hunting").read_text(encoding="utf-8") == 'mud.send("score")'
    assert len(store.installed()) == 1


def test_enable_disable_is_per_world(tmp_path):
    store = PackStore(tmp_path / "store")
    store.install(_bare_pack(tmp_path, "hunting.lua"))
    store.enable("hunting", "aardwolf")
    assert store.is_enabled("hunting", "aardwolf")
    assert not store.is_enabled("hunting", "cosmicrage")  # isolation
    assert [m.id for m in store.enabled("aardwolf")] == ["hunting"]
    assert store.enabled("cosmicrage") == []
    store.disable("hunting", "aardwolf")
    assert not store.is_enabled("hunting", "aardwolf")


def test_enabled_preserves_install_order(tmp_path):
    store = PackStore(tmp_path / "store")
    store.install(_bare_pack(tmp_path, "alpha.lua"), world="mud")
    store.install(_bare_pack(tmp_path, "beta.lua"), world="mud")
    assert [m.id for m in store.enabled("mud")] == ["alpha", "beta"]


def test_uninstall_removes_content_index_and_enablement(tmp_path):
    store = PackStore(tmp_path / "store")
    store.install(_bare_pack(tmp_path, "hunting.lua"), world="aardwolf")
    store.uninstall("hunting")
    assert store.installed() == []
    assert not store.pack_dir("hunting").exists()
    assert store.enabled("aardwolf") == []  # dropped from the world's enable list
    with pytest.raises(UnknownPack):
        store.manifest("hunting")


def test_install_from_zip_with_wrapper_dir(tmp_path):
    src = tmp_path / "Cosmic"
    src.mkdir()
    (src / "pack.toml").write_text(
        'name = "Cosmic"\ndialect = "lua"\nentry = "cosmic.lua"\n', encoding="utf-8"
    )
    (src / "cosmic.lua").write_text('mud.send("hi")', encoding="utf-8")
    zip_path = tmp_path / "cosmic.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:  # laid out as Cosmic/<files>
        archive.write(src / "pack.toml", "Cosmic/pack.toml")
        archive.write(src / "cosmic.lua", "Cosmic/cosmic.lua")

    store = PackStore(tmp_path / "store")
    manifest = store.install(zip_path, world="mud")
    assert manifest.id == "cosmic"
    assert store.entry_path("cosmic").read_text(encoding="utf-8") == 'mud.send("hi")'
    assert store.is_enabled("cosmic", "mud")


def test_install_from_flat_zip_single_script(tmp_path):
    script = tmp_path / "hunt.lua"
    script.write_text('mud.send("look")', encoding="utf-8")
    zip_path = tmp_path / "hunt.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(script, "hunt.lua")  # flat: script at the archive root

    manifest = PackStore(tmp_path / "store").install(zip_path)
    assert manifest.id == "hunt"


def test_install_bad_zip_raises_packerror(tmp_path):
    bad = tmp_path / "broken.zip"
    bad.write_text("not actually a zip", encoding="utf-8")
    with pytest.raises(PackError):
        PackStore(tmp_path / "store").install(bad)


def test_enable_unknown_pack_raises(tmp_path):
    store = PackStore(tmp_path / "store")
    with pytest.raises(UnknownPack):
        store.enable("ghost", "mud")


def test_state_persists_across_store_instances(tmp_path):
    root = tmp_path / "store"
    PackStore(root).install(_bare_pack(tmp_path, "hunting.lua"), world="mud")
    reopened = PackStore(root)
    assert [m.id for m in reopened.enabled("mud")] == ["hunting"]
    # index.json is human-readable JSON, not a pickle
    assert "hunting" in json.loads((root / "index.json").read_text(encoding="utf-8"))
