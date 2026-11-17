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
    scripts = pack / "scripts"
    scripts.mkdir()
    (scripts / "main.lua").write_text('mud.alias("k", function() mud.send("kill") end)')
    return pack


def test_trigger_route_name_is_shared_without_machine_hardware_configuration(tmp_path):
    from genericmud.packs.user_rules import UserRules, UserTrigger

    rules = UserRules(triggers=[UserTrigger(pattern="danger", audio_route="Headphones")])
    pack = _pack_dir(tmp_path, rules.to_json())
    (pack / "audio-routes.json").write_text('{"private-device-id": "not shared"}')
    destination = tmp_path / "share.zip"
    export_world(World("Example", "mud.example", 4000), pack, destination)
    with zipfile.ZipFile(destination) as archive:
        assert not any("audio-routes" in name for name in archive.namelist())
        assert b"private-device-id" not in b"".join(archive.read(n) for n in archive.namelist())
    imported = import_world(destination, tmp_path / "userpacks")
    target = tmp_path / "userpacks" / sanitize_component(imported.name)
    loaded = UserRules.from_json((target / "rules.json").read_text(encoding="utf-8"))
    assert loaded.triggers[0].audio_route == "Headphones"


def test_export_then_import_round_trips_rules_sounds_and_connection(tmp_path):
    world = World(name="Aardwolf", host="aardmud.org", port=4000, tls=False)
    dest = tmp_path / "share.zip"
    count = export_world(world, _pack_dir(tmp_path), dest)
    assert count == 4  # world.json + rules.json + one sound + one automation script

    userpacks = tmp_path / "userpacks"
    imported = import_world(dest, userpacks)
    assert (imported.name, imported.host, imported.port) == ("Aardwolf", "aardmud.org", 4000)
    target = userpacks / sanitize_component(imported.name)
    assert (target / "rules.json").is_file()
    assert (target / "sounds" / "growl.ogg").read_bytes() == b"OggS"
    assert (target / "scripts" / "main.lua").is_file()


def test_export_without_a_pack_dir_still_shares_the_connection(tmp_path):
    world = World(name="Bare", host="mud.example", port=23)
    dest = tmp_path / "bare.zip"
    assert export_world(world, None, dest) == 1
    with zipfile.ZipFile(dest) as archive:
        meta = json.loads(archive.read("world.json"))
    assert meta["host"] == "mud.example"


def test_export_keeps_unicode_metadata_readable_without_rewriting_rules(tmp_path):
    world = World("Мир", "mud.example", 4000, tls=True, encoding="cp1251")
    pack = _pack_dir(tmp_path, rules=r'{"version": 1, "aliases": [{"send": "\u044f"}]}')
    original_rules = (pack / "rules.json").read_bytes()
    dest = tmp_path / "unicode.zip"

    export_world(world, pack, dest)

    with zipfile.ZipFile(dest) as archive:
        serialized = archive.read("world.json").decode("utf-8")
        assert "Мир" in serialized
        assert r"\u" not in serialized
        assert json.loads(serialized) == {
            "name": "Мир", "host": "mud.example", "port": 4000,
            "tls": True, "encoding": "cp1251",
        }
        assert archive.read("rules.json") == original_rules
    assert (pack / "rules.json").read_bytes() == original_rules
    assert import_world(dest, tmp_path / "userpacks") == world


def test_import_reads_legacy_escaped_unicode_metadata(tmp_path):
    shared = tmp_path / "legacy.zip"
    with zipfile.ZipFile(shared, "w") as archive:
        archive.writestr(
            "world.json",
            r'{"name": "\u041c\u0438\u0440", "host": "mud.example", "port": 23}',
        )

    assert import_world(shared, tmp_path / "userpacks") == World("Мир", "mud.example", 23)


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
        archive.writestr("stray.exe", "MZ")  # not one of the four known shapes: dropped
        archive.writestr("sounds/ping.wav", "RIFF")
        archive.writestr("scripts/main.lua", "-- safe sandboxed automation")
        archive.writestr("scripts/not-lua.txt", "dropped")
    userpacks = tmp_path / "userpacks"
    world = import_world(tricky, userpacks)
    target = userpacks / sanitize_component(world.name)
    assert (target / "rules.json").is_file()
    assert (target / "sounds" / "ping.wav").is_file()
    assert (target / "scripts" / "main.lua").is_file()
    assert not (target / "scripts" / "not-lua.txt").exists()
    assert not (target / "stray.exe").exists()


def test_import_replaces_an_invalid_port_and_does_not_coerce_tls_strings(tmp_path):
    shared = tmp_path / "bad-fields.zip"
    with zipfile.ZipFile(shared, "w") as archive:
        archive.writestr(
            "world.json",
            json.dumps({"name": "Imported", "host": "mud.example", "port": 70000, "tls": "false"}),
        )
    world = import_world(shared, tmp_path / "userpacks")
    assert world.port == 23
    assert world.tls is False


def test_an_imported_name_with_a_line_break_keeps_every_saved_world(tmp_path):
    from genericmud.config.worlds import load_worlds, save_worlds

    archive = tmp_path / "shared.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        meta = {"name": "Friend\nMUD", "host": "mud.example", "port": 4000}
        bundle.writestr("world.json", json.dumps(meta))
    world = import_world(archive, tmp_path / "userpacks")
    assert world.name == "Friend MUD"
    path = tmp_path / "worlds.toml"
    save_worlds([World("Aardwolf", "aardmud.org", 4000), world], path)
    assert [saved.name for saved in load_worlds(path)] == ["Aardwolf", "Friend MUD"]
