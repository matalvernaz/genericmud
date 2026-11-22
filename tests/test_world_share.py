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
            "tls": True, "encoding": "cp1251", "connect_method": "none",
            "command_stacking": True, "command_separator": ";",
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


# --- the login method and command stacking travel (issue #128) --------------------------------


def _shared(tmp_path, world: World):
    destination = tmp_path / "share.zip"
    export_world(world, None, destination)
    return destination


def test_the_login_method_and_command_stacking_travel_with_the_world(tmp_path):
    world = World(
        "Crisol", "crisolmud.duckdns.org", 4000, connect_method="diku",
        command_stacking=False, command_separator="|",
    )
    destination = _shared(tmp_path, world)
    with zipfile.ZipFile(destination) as archive:
        meta = json.loads(archive.read("world.json"))
    assert meta["connect_method"] == "diku"
    assert meta["command_stacking"] is False
    assert meta["command_separator"] == "|"
    imported = import_world(destination, tmp_path / "userpacks")
    assert (imported.connect_method, imported.command_stacking, imported.command_separator) == (
        "diku", False, "|"
    )


def _zip_with(tmp_path, meta: dict):
    destination = tmp_path / "hand-made.zip"
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("world.json", json.dumps(meta))
    return destination


def test_an_older_zip_without_them_imports_as_a_new_world_starts(tmp_path):
    imported = import_world(_zip_with(tmp_path, {"name": "Old", "host": "old.example"}),
                            tmp_path / "userpacks")
    assert (imported.connect_method, imported.command_stacking, imported.command_separator) == (
        "none", True, ";"
    )


def test_values_that_cant_be_used_become_what_a_new_world_starts_with(tmp_path):
    meta = {
        "name": "Junk", "host": "junk.example", "connect_method": "steal",
        "command_stacking": "no", "command_separator": "/",
    }
    imported = import_world(_zip_with(tmp_path, meta), tmp_path / "userpacks")
    assert (imported.connect_method, imported.command_stacking, imported.command_separator) == (
        "none", True, ";"
    )


class _Store:
    def __init__(self, answer):
        self._answer = answer

    def has_login(self, _world):
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def test_a_new_world_with_no_login_saved_keeps_its_method():
    from genericmud.packs.world_share import login_method_refusal

    world = World("Crisol", "crisol.example", 4000, connect_method="diku")
    assert login_method_refusal(world, [], _Store(False)) is None


def test_a_login_saved_under_the_name_keeps_the_method_off():
    from genericmud.packs.world_share import login_method_refusal

    world = World("Crisol", "elsewhere.example", 4000, connect_method="diku")
    refusal = login_method_refusal(world, [], _Store(True))
    assert refusal == "a login is already saved under that name"


def test_replacing_a_saved_world_keeps_the_method_off_whatever_the_store_says():
    from genericmud.packs.world_share import login_method_refusal

    # The saved world is "mymud"; the zip calls itself "MyMUD". A store that matches names
    # exactly would say it holds nothing under "MyMUD", and the replaced world's login would
    # still be the one sent.
    world = World("MyMUD", "elsewhere.example", 4000, connect_method="mush")
    saved = [World("mymud", "real.example", 4000)]
    assert login_method_refusal(world, saved, _Store(False)) is not None


def test_a_store_that_cant_say_keeps_the_method_off():
    from genericmud.packs.world_share import login_method_refusal

    world = World("Crisol", "elsewhere.example", 4000, connect_method="prompts")
    assert login_method_refusal(world, [], _Store(None)) == "the saved logins couldn't be checked"
    assert login_method_refusal(world, [], None) == "the saved logins couldn't be checked"
    assert login_method_refusal(world, [], object()) == "the saved logins couldn't be checked"


def test_a_locked_keyring_at_import_never_sends_the_login_once_it_unlocks(tmp_path):
    import keyring
    import keyring.backend
    import keyring.errors

    from genericmud.app import EngineApp
    from genericmud.packs.world_share import login_method_refusal
    from genericmud.session.credentials import KeyringCredentialStore
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    class _Keyring(keyring.backend.KeyringBackend):
        priority = 1
        locked = True
        entries = {("genericmud", "Crisol"): '{"username": "hero", "password": "hunter2"}'}

        def get_password(self, service, username):
            if self.locked:
                raise keyring.errors.KeyringLocked("locked")
            return self.entries.get((service, username))

        def set_password(self, service, username, password):
            self.entries[(service, username)] = password

        def delete_password(self, service, username):
            self.entries.pop((service, username), None)

    backend = _Keyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        store = KeyringCredentialStore()
        meta = {"name": "Crisol", "host": "evil.example", "connect_method": "diku"}
        world = import_world(_zip_with(tmp_path, meta), tmp_path / "userpacks")
        refusal = login_method_refusal(world, [], store)
        assert refusal == "the saved logins couldn't be checked"
        world.connect_method = "none"  # what the import does with a refusal
        _Keyring.locked = False  # unlocked by the time the world is opened
        sent: list[str] = []
        app = EngineApp(
            VoiceRouter(RecordingBackend(), clock=lambda: 0.0), send=sent.append,
            post=[].append, credentials=store, keymap={}, name="Crisol",
            connect_method=world.connect_method,
        )
        app.begin_login("Crisol")
        app.after_connect()
        assert sent == []
    finally:
        keyring.set_keyring(previous)


def test_the_import_menu_applies_the_refusal_before_saving():
    from tests.ui_source import ui_source

    source = ui_source("SavedWorldsMixin._on_import_world")
    assert "login_method_refusal(world, load_worlds(), self._credentials)" in source
    assert source.index("login_method_refusal") < source.index("self._save_world(world)")


# --- what the credential stores can say they hold -------------------------------------------------


def test_the_plaintext_store_says_what_it_holds_and_when_it_cant_tell(tmp_path):
    from genericmud.session.credentials import PlaintextCredentialStore

    path = tmp_path / "credentials.json"
    store = PlaintextCredentialStore(path)
    assert store.has_login("Crisol") is False  # no file at all
    store.set("Crisol", "hero", "hunter2")
    assert store.has_login("Crisol") is True
    assert store.has_login("Other") is False
    path.write_text("{not json", encoding="utf-8")
    assert store.has_login("Crisol") is None
    path.write_text("[1, 2]", encoding="utf-8")
    assert store.has_login("Crisol") is None


def test_the_keyring_store_says_what_it_holds_and_when_it_cant_tell():
    import keyring
    import keyring.backend
    import keyring.errors

    from genericmud.session.credentials import KeyringCredentialStore

    class _Keyring(keyring.backend.KeyringBackend):
        priority = 1
        locked = False
        entries = {("genericmud", "Crisol"): "{}"}

        def get_password(self, service, username):
            if self.locked:
                raise keyring.errors.KeyringLocked("locked")
            return self.entries.get((service, username))

        def set_password(self, service, username, password):
            self.entries[(service, username)] = password

        def delete_password(self, service, username):
            self.entries.pop((service, username), None)

    previous = keyring.get_keyring()
    keyring.set_keyring(_Keyring())
    try:
        store = KeyringCredentialStore()
        assert store.has_login("Crisol") is True  # something is kept, readable or not
        assert store.has_login("Other") is False
        _Keyring.locked = True
        assert store.has_login("Crisol") is None
    finally:
        keyring.set_keyring(previous)
