"""One-shot pack setup: install (multi-file entry) + derive world + enable/trust."""

from __future__ import annotations

from genericmud.packs.setup import detect_entry, setup_pack
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


def test_setup_bare_set_pack_has_no_world(tmp_path):
    store = PackStore(tmp_path / "store")
    bare = tmp_path / "sounds.set"
    bare.write_text("#trigger {x} {#play {x.wav}}", encoding="utf-8")
    result = setup_pack(store, bare)
    assert result.world is None  # no world file -> caller prompts for host/port
    assert result.enabled_for is None
    assert store.is_trusted(result.manifest.id)  # still installed + trusted
