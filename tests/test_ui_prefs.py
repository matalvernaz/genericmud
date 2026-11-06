"""Persisted UI/speech toggles."""

from __future__ import annotations

import pytest

from genericmud.config.ui_prefs import UiPrefs, load_ui_prefs, save_ui_prefs


def test_round_trip(tmp_path):
    path = tmp_path / "ui-prefs.toml"
    save_ui_prefs(
        UiPrefs(background_silence=True, numpad_compass=False, follow_mode=True), path
    )
    prefs = load_ui_prefs(path)
    assert prefs.background_silence is True
    assert prefs.numpad_compass is False
    assert prefs.follow_mode is True
    assert prefs.autoretype is False  # untouched fields keep their defaults


def test_missing_file_gives_defaults(tmp_path):
    prefs = load_ui_prefs(tmp_path / "nope.toml")
    assert prefs == UiPrefs()
    assert prefs.numpad_compass is True  # compass ships on (VIPMud muscle memory)


def test_corrupt_file_gives_defaults_not_a_crash(tmp_path):
    path = tmp_path / "ui-prefs.toml"
    path.write_text("[ui\nnot toml", encoding="utf-8")
    assert load_ui_prefs(path) == UiPrefs()


def test_wrong_types_use_defaults_while_valid_values_survive(tmp_path):
    path = tmp_path / "ui-prefs.toml"
    path.write_text(
        '[ui]\nbackground_silence = true\nnumpad_compass = "false"\n',
        encoding="utf-8",
    )
    prefs = load_ui_prefs(path)
    assert prefs.background_silence is True
    assert prefs.numpad_compass is True


def test_non_table_ui_section_uses_defaults(tmp_path):
    path = tmp_path / "ui-prefs.toml"
    path.write_text('ui = "not a table"\n', encoding="utf-8")
    assert load_ui_prefs(path) == UiPrefs()


def test_other_worlds_are_silenced_unless_the_player_says_otherwise(tmp_path):
    # On by default: a player upgrading sees exactly what they saw before, with nine worlds
    # open and one of them talking.
    assert UiPrefs().silence_other_worlds is True

    path = tmp_path / "ui-prefs.toml"
    save_ui_prefs(UiPrefs(silence_other_worlds=False), path)
    assert load_ui_prefs(path).silence_other_worlds is False
    # And it is written out beside the other toggles, so it survives a restart.
    assert "silence_other_worlds = false" in path.read_text(encoding="utf-8")


def test_connection_settings_defaults_and_old_preferences(tmp_path):
    assert UiPrefs().auto_reconnect is True
    assert UiPrefs().reconnect_interval == 5
    path = tmp_path / "ui-prefs.toml"
    path.write_text("[ui]\nnumpad_compass = false\n", encoding="utf-8")
    prefs = load_ui_prefs(path)
    assert prefs.auto_reconnect is True and prefs.reconnect_interval == 5
    assert prefs.numpad_compass is False


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("interval", [1, 5, 300])
def test_connection_settings_round_trip(tmp_path, enabled, interval):
    path = tmp_path / "ui-prefs.toml"
    prefs = UiPrefs(auto_reconnect=enabled, reconnect_interval=interval)
    save_ui_prefs(prefs, path)
    assert load_ui_prefs(path) == prefs
    assert f"reconnect_interval = {interval}" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ['true', 'false', '"5"', '5.0', '0', '-1', '301'])
def test_invalid_interval_uses_default_without_affecting_other_settings(tmp_path, value):
    path = tmp_path / "ui-prefs.toml"
    path.write_text(f"[ui]\nauto_reconnect = false\nreconnect_interval = {value}\n",
                    encoding="utf-8")
    prefs = load_ui_prefs(path)
    assert prefs.reconnect_interval == 5
    assert prefs.auto_reconnect is False


@pytest.mark.parametrize("value", ['1', '"false"', '[]'])
def test_invalid_auto_reconnect_uses_default(tmp_path, value):
    path = tmp_path / "ui-prefs.toml"
    path.write_text(f"[ui]\nauto_reconnect = {value}\nreconnect_interval = 300\n",
                    encoding="utf-8")
    prefs = load_ui_prefs(path)
    assert prefs.auto_reconnect is True and prefs.reconnect_interval == 300


@pytest.mark.parametrize("mode", ["standard", "editable"])
def test_command_input_mode_is_persisted_with_other_preferences(tmp_path, mode):
    path = tmp_path / "ui-prefs.toml"
    prefs = UiPrefs(command_input_mode=mode, numpad_compass=False, reconnect_interval=12)
    save_ui_prefs(prefs, path)
    assert load_ui_prefs(path) == prefs
    assert f'command_input_mode = "{mode}"' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ['"unknown"', 'true', '1', '[]'])
def test_invalid_command_mode_defaults_to_standard(tmp_path, value):
    path = tmp_path / "ui-prefs.toml"
    path.write_text(f'[ui]\ncommand_input_mode = {value}\n', encoding="utf-8")
    assert load_ui_prefs(path).command_input_mode == "standard"


def test_mode_defaults_and_prototype_migration(tmp_path):
    path = tmp_path / "ui-prefs.toml"
    assert load_ui_prefs(path).command_input_mode == "standard"
    path.write_text('[ui]\neditable_command_buffer = true\n', encoding="utf-8")
    assert load_ui_prefs(path).command_input_mode == "editable"
    path.write_text('[ui]\neditable_command_buffer = true\ncommand_input_mode = "standard"\n',
                    encoding="utf-8")
    assert load_ui_prefs(path).command_input_mode == "standard"
