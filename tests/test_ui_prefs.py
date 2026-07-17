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
