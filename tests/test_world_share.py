"""World export/import: the one-zip sharing flow."""

from __future__ import annotations

import json
import zipfile

import pytest

from genericmud.config.worlds import World
from genericmud.packs.world_share import export_world, import_world
from genericmud.safepath import sanitize_component


def _pack_dir(tmp_path, rules: str = '{"version": 1, "triggers": []}'):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "rules.json").write_text(rules, encoding="utf-8")
    sounds = pack / "sounds"
    sounds.mkdir()
    (sounds / "growl.ogg").write_bytes(b"OggS")
    return pack


def test_export_then_import_round_trips_rules_sounds_and_connection(tmp_path):
    world = World(name="Aardwolf", host="aardmud.org", port=4000, tls=False)
    dest = tmp_path / "share.zip"
    count = export_world(world, _pack_dir(tmp_path), dest)
    assert count == 3  # world.json + rules.json + one sound

    userpacks = tmp_path / "userpacks"
    imported = import_world(dest, userpacks)
    assert (imported.name, imported.host, imported.port) == ("Aardwolf", "aardmud.org", 4000)
    target = userpacks / sanitize_component(imported.name)
    assert (target / "rules.json").is_file()
    assert (target / "sounds" / "growl.ogg").read_bytes() == b"OggS"


def test_export_without_a_pack_dir_still_shares_the_connection(tmp_path):
    world = World(name="Bare", host="mud.example", port=23)
    dest = tmp_path / "bare.zip"
    assert export_world(world, None, dest) == 1
    with zipfile.ZipFile(dest) as archive:
        meta = json.loads(archive.read("world.json"))
    assert meta["host"] == "mud.example"


def test_import_suffixes_the_name_instead_of_overwriting(tmp_path):
    world = World(name="Aardwolf", host="aardmud.org", port=4000)
    dest = tmp_path / "share.zip"
    export_world(world, _pack_dir(tmp_path), dest)
    userpacks = tmp_path / "userpacks"

    first = import_world(dest, userpacks)
    second = import_world(dest, userpacks)
    assert first.name == "Aardwolf"
    assert second.name == "Aardwolf 2"
    # The suffixed world's rules dir pairs with its name the way user_rules_dir derives it.
    assert (userpacks / sanitize_component(second.name) / "rules.json").is_file()


def test_import_rejects_a_zip_without_world_metadata(tmp_path):
    bogus = tmp_path / "bogus.zip"
    with zipfile.ZipFile(bogus, "w") as archive:
        archive.writestr("readme.txt", "not a world")
    with pytest.raises(ValueError):
        import_world(bogus, tmp_path / "userpacks")


def test_import_keeps_only_the_known_shapes(tmp_path):
    tricky = tmp_path / "tricky.zip"
    with zipfile.ZipFile(tricky, "w") as archive:
        archive.writestr("world.json", json.dumps({"name": "Trick", "host": "h", "port": 1}))
        archive.writestr("rules.json", '{"version": 1}')
        archive.writestr("stray.exe", "MZ")  # not one of the three shapes: dropped
        archive.writestr("sounds/ping.wav", "RIFF")
    userpacks = tmp_path / "userpacks"
    world = import_world(tricky, userpacks)
    target = userpacks / sanitize_component(world.name)
    assert (target / "rules.json").is_file()
    assert (target / "sounds" / "ping.wav").is_file()
    assert not (target / "stray.exe").exists()
