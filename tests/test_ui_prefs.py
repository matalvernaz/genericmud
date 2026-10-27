"""Persisted UI/speech toggles."""

from __future__ import annotations

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
