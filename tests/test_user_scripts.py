"""Per-world advanced Lua automation storage and transactional live reload."""

from __future__ import annotations

import pytest
from lupa import LuaError

from genericmud.app import EngineApp
from genericmud.packs import PackStore
from genericmud.scripting import user_scripts
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app(tmp_path) -> tuple[EngineApp, list[str], RecordingBackend]:
    sent: list[str] = []
    backend = RecordingBackend()
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=sent.append,
        post=[].append,
        keymap={},
        packs=PackStore(tmp_path / "soundpacks"),
        name="Test World",
    )
    return app, sent, backend


def test_multiple_scripts_load_alphabetically_and_share_script_variables(tmp_path):
    app, sent, _backend = _app(tmp_path)
    pack_dir = app.user_rules_dir()
    assert pack_dir is not None
    user_scripts.save_script(pack_dir, "20-alias.lua", """
        mud.alias("hit *", function() mud.command("${attack} ${1}") end)
    """)
    user_scripts.save_script(pack_dir, "10-settings.lua", 'mud.set_var("attack", "kick")')

    assert app.reload_user_scripts()
    assert [item.name for item in app._user_script_runtimes] == [
        "10-settings.lua", "20-alias.lua",
    ]
    assert app.engine.process_input("hit rat") == []
    assert sent == ["kick rat"]


def test_failed_reload_keeps_previous_working_script_set(tmp_path):
    app, sent, backend = _app(tmp_path)
    pack_dir = app.user_rules_dir()
    assert pack_dir is not None
    user_scripts.save_script(
        pack_dir, "main.lua",
        'mud.alias("go", function() mud.command("north") end)',
    )
    assert app.reload_user_scripts()

    user_scripts.save_script(
        pack_dir, "main.lua",
        'mud.alias("go", function() mud.command("south") end); error("broken")',
    )
    assert not app.reload_user_scripts()
    assert len(app.engine._aliases) == 1  # failed generation was removed, old one stayed
    assert app.engine.process_input("go") == []
    assert sent == ["north"]
    assert any("Scripts not applied" in spoken for spoken in backend.spoken)


def test_empty_script_directory_removes_previous_rules(tmp_path):
    app, sent, _backend = _app(tmp_path)
    pack_dir = app.user_rules_dir()
    assert pack_dir is not None
    user_scripts.save_script(
        pack_dir, "main.lua",
        'mud.alias("go", function() mud.command("north") end)',
    )
    assert app.reload_user_scripts()
    user_scripts.delete_script(pack_dir, "main.lua")
    assert app.reload_user_scripts()
    assert app.engine.process_input("go") == ["go"]
    assert sent == []


def test_script_names_are_portable_and_confined(tmp_path):
    pack_dir = tmp_path / "pack"
    assert user_scripts.normalize_script_name("10-combat") == "10-combat.lua"
    with pytest.raises(ValueError):
        user_scripts.save_script(pack_dir, "../outside.lua", "")
    with pytest.raises(ValueError):
        user_scripts.save_script(pack_dir, "CON.lua", "")
    with pytest.raises(ValueError):
        user_scripts.save_script(pack_dir, "CON.extra.lua", "")
    assert not (tmp_path / "outside.lua").exists()


def test_validation_rejects_invalid_lua_without_writing(tmp_path):
    pack_dir = tmp_path / "pack"
    with pytest.raises(LuaError):
        user_scripts.validate_script(pack_dir, "this is not valid Lua !!!")
    assert user_scripts.list_scripts(pack_dir) == []


def test_a_rule_finds_the_worlds_scripts_on_the_first_connect(tmp_path):
    # Connect reloads rules before scripts, so the runner a rule calls through is built before
    # any script has loaded. Before this it never heard about them, and a rule sending
    # "on_backup" ran that as code and spoke a Lua syntax error.
    from genericmud.model.buffer import Line
    from genericmud.packs.user_rules import UserRules, UserTrigger, save_rules

    app, _sent, backend = _app(tmp_path)
    pack_dir = app.user_rules_dir()
    user_scripts.save_script(
        pack_dir, "10-hello.lua", 'function on_backup() mud.speak("backup seen") end'
    )
    save_rules(
        pack_dir,
        UserRules(
            triggers=[UserTrigger(pattern="backup", send="on_backup", send_to="script")]
        ),
    )
    app.reload_user_rules()  # connect's order: the rules, then the scripts
    app.reload_user_scripts()
    app.engine.process_line(Line("backup"))
    assert "backup seen" in backend.spoken
