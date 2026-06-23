"""One-shot pack setup: install (multi-file entry) + derive world + enable/trust."""

from __future__ import annotations

import shutil
import zipfile

from genericmud.packs.setup import detect_entry, entry_problem, setup_pack, update_pack
from genericmud.packs.store import PackStore

MCL = """<?xml version="1.0" encoding="iso-8859-1"?>
<!DOCTYPE muclient>
<muclient>
<world
   chat_port="4050"
   name="Cosmic Rage"
   site="cosmicrage.earth"
   port="7777"
   proxy_port="1080"
>
</world>
</muclient>"""


def _multi_file_pack(root):
    """A pack shaped like a real one: several .set scripts + a MUSHclient world file."""
    pack = root / "VIPMudCosmicRageScripts"
    (pack / "scripts").mkdir(parents=True)
    (pack / "scripts" / "main.set").write_text("#trigger {x} {#say {hi}}", encoding="utf-8")
    (pack / "scripts" / "keys.set").write_text("#key f1 {look}", encoding="utf-8")
    (pack / "worlds").mkdir()
    (pack / "worlds" / "cr.MCL").write_text(MCL, encoding="latin-1")
    return pack


def test_setup_multi_file_pack_creates_world_enables_and_trusts(tmp_path):
    store = PackStore(tmp_path / "store")
    pack = _multi_file_pack(tmp_path)
    result = setup_pack(store, pack, entry="scripts/main.set", sounds="/snd")
    assert result.world is not None
    assert (result.world.host, result.world.port) == ("cosmicrage.earth", 7777)
    assert result.world.sounds == "/snd"
    assert result.enabled_for == "Cosmic Rage"
    assert store.is_trusted(result.manifest.id)
    assert store.is_enabled(result.manifest.id, "Cosmic Rage")


def test_detect_entry_prefers_main_set(tmp_path):
    assert detect_entry(_multi_file_pack(tmp_path)) == "scripts/main.set"


def test_detect_entry_none_when_ambiguous(tmp_path):
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "a.xml").write_text("<muclient/>", encoding="utf-8")
    (pack / "b.xml").write_text("<muclient/>", encoding="utf-8")
    assert detect_entry(pack) is None


def test_detect_entry_finds_vipmud_loader(tmp_path):
    # No main.set, but one .set #loads the others -> that's the loader/entry.
    pack = tmp_path / "vippack" / "scripts"
    pack.mkdir(parents=True)
    (pack / "boot.set").write_text("#load {scripts/extra.set}\n#say {hi}", encoding="utf-8")
    (pack / "extra.set").write_text("#trigger {x} {look}", encoding="utf-8")
    assert detect_entry(tmp_path / "vippack") == "scripts/boot.set"


def test_detect_entry_prefers_the_main_loader(tmp_path):
    # Among several .set loaders, the one that #loads the most files is the main one.
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "buffers.set").write_text("#load {a.set}", encoding="utf-8")  # sorts first, 1 load
    (pack / "boot.set").write_text("#load {a.set}\n#load {b.set}\n#load {c.set}", encoding="utf-8")
    (pack / "a.set").write_text("#say {a}", encoding="utf-8")
    assert detect_entry(pack) == "boot.set"


def test_detect_entry_finds_plugin_named_after_pack(tmp_path):
    # A MUSHclient pack: pick the plugin named after the pack (toastush.xml in toastush/).
    plugins = tmp_path / "toastush" / "worlds" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "timer.xml").write_text("<muclient/>", encoding="utf-8")
    (plugins / "toastush.xml").write_text("<muclient/>", encoding="utf-8")
    assert detect_entry(tmp_path / "toastush") == "worlds/plugins/toastush.xml"


def test_detect_entry_prefers_mcl_world(tmp_path):
    # A MUSHclient pack: the lone .MCL world is the entry (it <include>s the plugins),
    # even though the plugin .xml files alone would be ambiguous.
    pack = tmp_path / "erionish"
    (pack / "worlds" / "plugins").mkdir(parents=True)
    (pack / "worlds" / "erion.MCL").write_text(MCL, encoding="latin-1")
    (pack / "worlds" / "plugins" / "gather.xml").write_text("<muclient/>", encoding="utf-8")
    (pack / "worlds" / "plugins" / "combat.xml").write_text("<muclient/>", encoding="utf-8")
    assert detect_entry(pack) == "worlds/erion.MCL"


def test_detect_entry_mcl_world_beats_a_stray_main_xml(tmp_path):
    # A MUSHclient pack whose plugin happens to be named main.xml: the .MCL world still
    # wins (no .set present), so we don't load a plugin as if it were the whole pack.
    pack = tmp_path / "p"
    (pack / "worlds").mkdir(parents=True)
    (pack / "worlds" / "world.MCL").write_text(MCL, encoding="latin-1")
    (pack / "main.xml").write_text("<muclient/>", encoding="utf-8")
    assert detect_entry(pack) == "worlds/world.MCL"


def test_detect_entry_vipmud_loader_wins_over_bundled_mcl(tmp_path):
    # A VIPMud pack may bundle a .MCL for connection info; its .set loader is the entry,
    # not the world (the .mcl rule is gated on the pack having no .set).
    pack = tmp_path / "vip"
    pack.mkdir()
    (pack / "boot.set").write_text("#load {extra.set}\n#say {hi}", encoding="utf-8")
    (pack / "extra.set").write_text("#say {x}", encoding="utf-8")
    (pack / "conn.MCL").write_text(MCL, encoding="latin-1")
    assert detect_entry(pack) == "boot.set"


def test_detect_entry_picks_the_richest_mcl_world(tmp_path):
    # A full MUSHclient-install bundle ships extra worlds (captures/sandbox); pick the one
    # that <include>s the plugin suite, not a bare capture world.
    pack = tmp_path / "bundle"
    (pack / "worlds").mkdir(parents=True)
    (pack / "worlds" / "captures.MCL").write_text(MCL, encoding="latin-1")  # 0 includes
    (pack / "worlds" / "Main World.MCL").write_text(
        MCL.replace("</muclient>", '<include name="a.xml"/><include name="b.xml"/></muclient>'),
        encoding="latin-1",
    )
    assert detect_entry(pack) == "worlds/Main World.MCL"


def test_entry_problem_distinguishes_dead_ends(tmp_path):
    installer = tmp_path / "inst"
    installer.mkdir()
    (installer / "setup.exe").write_bytes(b"MZ")
    assert "installer" in entry_problem(installer)

    mush = tmp_path / "mush"
    mush.mkdir()
    (mush / "a.xml").write_text("<muclient/>", encoding="utf-8")
    assert "MUSHclient" in entry_problem(mush)

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "readme.txt").write_text("hi", encoding="utf-8")
    assert "no soundpack script" in entry_problem(empty)

    # A MUSHclient pack bundling git tooling (.exe) is MUSHclient, not an "installer".
    both = tmp_path / "both"
    both.mkdir()
    (both / "git.exe").write_bytes(b"MZ")
    (both / "LuaAudio.xml").write_text("<muclient/>", encoding="utf-8")
    assert "MUSHclient" in entry_problem(both)


def test_install_records_origin(tmp_path):
    store = PackStore(tmp_path / "store")
    bare = tmp_path / "x.set"
    bare.write_text("#trigger {a} {look}", encoding="utf-8")
    manifest = store.install(bare, origin="https://example.test/x.zip")
    assert store.manifest(manifest.id).origin == "https://example.test/x.zip"


def test_update_pack_refetches_from_origin_and_preserves_state(tmp_path):
    store = PackStore(tmp_path / "store")
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "main.set").write_text("#trigger {old} {look}", encoding="utf-8")
    setup_pack(store, v1, entry="main.set", origin="https://example.test/pack.zip")
    store.enable("v1", "myworld")

    # A "new version" archive the fake fetch returns.
    new = tmp_path / "new"
    new.mkdir()
    (new / "main.set").write_text("#trigger {new} {look}", encoding="utf-8")
    new_zip = tmp_path / "new.zip"
    with zipfile.ZipFile(new_zip, "w") as bundle:
        bundle.write(new / "main.set", "main.set")

    update_pack(store, "v1", fetch=lambda _url, dest: shutil.copy(new_zip, dest))

    assert "new" in store.entry_path("v1").read_text(encoding="utf-8")  # content refreshed
    assert store.manifest("v1").origin == "https://example.test/pack.zip"  # origin kept
    assert store.is_enabled("v1", "myworld")  # enablement preserved
    assert store.is_trusted("v1")  # trust preserved


def test_setup_bare_set_pack_has_no_world(tmp_path):
    store = PackStore(tmp_path / "store")
    bare = tmp_path / "sounds.set"
    bare.write_text("#trigger {x} {#play {x.wav}}", encoding="utf-8")
    result = setup_pack(store, bare)
    assert result.world is None  # no world file -> caller prompts for host/port
    assert result.enabled_for is None
    assert store.is_trusted(result.manifest.id)  # still installed + trusted
