"""Moving saved worlds' files off the ASCII-only names older builds used."""

from __future__ import annotations

from pathlib import Path

import pytest

from genericmud.app import EngineApp
from genericmud.config.world_files import migrate_world_files
from genericmud.packs import PackStore
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _old_world(config: Path, filed_as: str, rules: str = '{"version": 1}') -> None:
    """Lay out one world's files the way an older build filed them."""
    (config / "userpacks" / filed_as).mkdir(parents=True, exist_ok=True)
    (config / "userpacks" / filed_as / "rules.json").write_text(rules, encoding="utf-8")
    (config / "maps").mkdir(exist_ok=True)
    (config / "maps" / f"{filed_as}.json").write_text("{}", encoding="utf-8")
    (config / "state").mkdir(exist_ok=True)
    (config / "state" / f"{filed_as}-vars.json").write_text("{}", encoding="utf-8")


def _files(config: Path, name: str) -> list[bool]:
    return [
        (config / "userpacks" / name / "rules.json").is_file(),
        (config / "maps" / f"{name}.json").is_file(),
        (config / "state" / f"{name}-vars.json").is_file(),
    ]


def test_one_worlds_old_files_move_to_its_name_now(tmp_path):
    _old_world(tmp_path, "Caf_del_Mar")
    report = migrate_world_files(tmp_path, ["Café del Mar", "Aardwolf"])
    assert _files(tmp_path, "Café_del_Mar") == [True, True, True]
    assert _files(tmp_path, "Caf_del_Mar") == [False, False, False]
    assert len(report) == 3 and all(line.startswith("moved") for line in report)


def test_an_all_cyrillic_worlds_session_folder_moves_to_it(tmp_path):
    _old_world(tmp_path, "session")
    migrate_world_files(tmp_path, ["Былины"])
    assert _files(tmp_path, "Былины") == [True, True, True]


def test_a_folder_several_worlds_shared_is_copied_to_each(tmp_path):
    # Both used to be "session", so both have been reading and writing these rules. Each
    # keeps them, and from here on they stop sharing.
    _old_world(tmp_path, "session", rules='{"version": 1, "keys": []}')
    migrate_world_files(tmp_path, ["Былины", "Адамант"])
    for name in ("Былины", "Адамант", "session"):
        assert _files(tmp_path, name) == [True, True, True], name
    assert (tmp_path / "userpacks" / "Адамант" / "rules.json").read_text(
        encoding="utf-8") == '{"version": 1, "keys": []}'


def test_another_worlds_own_folder_is_never_touched(tmp_path):
    # A world literally named "Caf" is filed under "Caf" today; that isn't Café's old copy.
    _old_world(tmp_path, "Caf")
    assert migrate_world_files(tmp_path, ["Café", "Caf"]) == []
    assert _files(tmp_path, "Caf") == [True, True, True]
    assert _files(tmp_path, "Café") == [False, False, False]


def test_nothing_is_overwritten_and_a_second_launch_changes_nothing(tmp_path):
    _old_world(tmp_path, "Caf")
    (tmp_path / "userpacks" / "Café").mkdir(parents=True)  # already has its own rules
    migrate_world_files(tmp_path, ["Café"])
    assert not (tmp_path / "userpacks" / "Café" / "rules.json").exists()
    assert migrate_world_files(tmp_path, ["Café"]) == []  # the map and vars moved; done


def test_ascii_worlds_are_left_exactly_as_they_are(tmp_path):
    _old_world(tmp_path, "Aardwolf_MUD")
    assert migrate_world_files(tmp_path, ["Aardwolf MUD"]) == []
    assert _files(tmp_path, "Aardwolf_MUD") == [True, True, True]


def test_a_failed_move_is_reported_not_raised(tmp_path, monkeypatch):
    _old_world(tmp_path, "Caf")

    def refuse(self, target):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "rename", refuse)
    report = migrate_world_files(tmp_path, ["Café"])
    assert report and all("where it was" in line for line in report)
    assert _files(tmp_path, "Caf") == [True, True, True]


@pytest.mark.parametrize("names", [["Café", "Cafè"], ["Café", "Café中"]])
def test_a_session_never_adopts_a_folder_two_worlds_claim(tmp_path, names):
    # The launch-time copy settles these; a session alone can't tell whose it is, and
    # adopting it would keep the two worlds sharing.
    _old_world(tmp_path, "Caf")
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        packs=PackStore(tmp_path / "soundpacks"),
        map_dir=tmp_path / "maps",
        name=names[0],
        other_worlds=lambda: names,
    )
    assert app.user_rules_dir() == tmp_path / "userpacks" / "Café"
