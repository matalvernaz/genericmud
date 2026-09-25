"""Each world's own data lives under its own name, in any alphabet."""

from __future__ import annotations

import json
import zipfile

from genericmud.app import EngineApp
from genericmud.config.worlds import World
from genericmud.packs import PackStore
from genericmud.packs.world_share import export_world, import_world
from genericmud.safepath import legacy_world_component, world_component
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app(tmp_path, name: str) -> EngineApp:
    return EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        packs=PackStore(tmp_path / "soundpacks"),
        map_dir=tmp_path / "maps",
        name=name,
    )


def test_names_keep_their_own_alphabet_and_still_cannot_escape():
    assert world_component("Былины") == "Былины"
    assert world_component("北大侠客行") == "北大侠客行"
    assert world_component("Café del Mar") == "Café_del_Mar"
    assert world_component("Café") == world_component("Café")  # one spelling, one folder
    assert world_component("../../etc/passwd") == "etc_passwd"
    assert world_component("..") == "session"
    assert world_component("a\\b:c") == "a_b_c"


def test_two_cyrillic_worlds_no_longer_share_rules_maps_or_saved_values(tmp_path):
    # Filed ASCII-only, both of these became "session": one world's triggers fired on
    # the other, and their room maps merged.
    first, second = _app(tmp_path, "Былины"), _app(tmp_path, "Адамант")
    assert first.user_rules_dir() != second.user_rules_dir()
    assert first._map_path() != second._map_path()
    assert first._pack_vars_path() != second._pack_vars_path()
    assert first.user_rules_dir().name == "Былины"


def test_data_an_older_build_filed_ascii_only_is_still_found(tmp_path):
    name = "Café del Mar"
    old = legacy_world_component(name)
    assert old == "Caf_del_Mar"
    (tmp_path / "userpacks" / old).mkdir(parents=True)
    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / f"{old}.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / f"{old}-vars.json").write_text("{}", encoding="utf-8")
    app = _app(tmp_path, name)
    assert app.user_rules_dir() == tmp_path / "userpacks" / old
    assert app._map_path() == tmp_path / "maps" / f"{old}.json"
    assert app._pack_vars_path() == tmp_path / "state" / f"{old}-vars.json"


def test_the_old_shared_fallback_is_never_adopted(tmp_path):
    (tmp_path / "userpacks" / "session").mkdir(parents=True)
    assert _app(tmp_path, "Былины").user_rules_dir() == tmp_path / "userpacks" / "Былины"


def test_ascii_names_are_filed_exactly_as_before(tmp_path):
    app = _app(tmp_path, "Aardwolf MUD")
    assert app.user_rules_dir() == tmp_path / "userpacks" / "Aardwolf_MUD"
    assert app._map_path() == tmp_path / "maps" / "Aardwolf_MUD.json"


def test_imported_cyrillic_world_lands_where_its_rules_are_looked_for(tmp_path):
    source = tmp_path / "shared.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("world.json", json.dumps({"name": "Былины", "host": "h", "port": 1}))
        archive.writestr("rules.json", '{"version": 1}')
    world = import_world(source, tmp_path / "userpacks")
    assert (_app(tmp_path, world.name).user_rules_dir() / "rules.json").is_file()


def test_import_does_not_take_over_a_folder_an_older_build_filed(tmp_path):
    (tmp_path / "userpacks" / "Caf_del_Mar").mkdir(parents=True)  # a local world's rules
    source = tmp_path / "shared.zip"
    export_world(World("Café del Mar", "h", 1), None, source)
    world = import_world(source, tmp_path / "userpacks")
    assert world.name == "Café del Mar 2"
