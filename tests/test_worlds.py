"""Saved-worlds config round-trip."""

from __future__ import annotations

import pytest

from genericmud.config.worlds import World, load_worlds, parse_port, save_worlds


def test_worlds_roundtrip(tmp_path):
    path = tmp_path / "worlds.toml"
    worlds = [
        World("Cosmic Rage", "cosmicrage.earth", 3000, True, "C:/sounds"),
        World("Local", "127.0.0.1", 4000),
    ]
    save_worlds(worlds, path)
    assert load_worlds(path) == worlds


def test_load_missing_returns_empty(tmp_path):
    assert load_worlds(tmp_path / "nope.toml") == []


def test_load_corrupt_returns_empty_instead_of_crashing_startup(tmp_path):
    path = tmp_path / "worlds.toml"
    path.write_text("[[world]\nnot valid TOML", encoding="utf-8")
    assert load_worlds(path) == []


def test_load_skips_bad_rows_but_keeps_valid_worlds(tmp_path):
    path = tmp_path / "worlds.toml"
    path.write_text(
        """\
[[world]]
name = "Bad"
host = "bad.example"
port = 70000

[[world]]
name = "Good"
host = "good.example"
port = 23
tls = true
""",
        encoding="utf-8",
    )
    assert load_worlds(path) == [World("Good", "good.example", 23, True)]


@pytest.mark.parametrize("value", ("abc", 0, 65536, -1, True, "23.5"))
def test_parse_port_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="1 to 65535"):
        parse_port(value)


def test_parse_port_accepts_numeric_text():
    assert parse_port(" 4000 ") == 4000


def test_a_control_character_cannot_cost_the_other_saved_worlds(tmp_path):
    # TOML forbids raw control characters; one used to make tomllib reject the whole file,
    # so every saved world vanished and the next save kept only the world being saved.
    path = tmp_path / "worlds.toml"
    worlds = [
        World("Aardwolf", "aardmud.org", 4000),
        World("Odd", "odd.example", 23, sounds="C:/snd\x7f\tx"),
    ]
    save_worlds(worlds, path)
    assert load_worlds(path) == worlds


def test_a_world_name_or_host_drops_control_characters():
    world = World("Friend\nMUD\x7f", " mud.example\x00 ", 4000)
    assert (world.name, world.host) == ("Friend MUD", "mud.example")


def test_command_stacking_and_logging_settings_round_trip(tmp_path):
    path = tmp_path / "worlds.toml"
    worlds = [
        World(
            "Chat", "chatmud.com", 7777, command_stacking=False, command_separator="|",
            log_automatically=True, log_files="day", log_commands=False,
        ),
        World("Local", "127.0.0.1", 4000, log_files="world"),
    ]
    save_worlds(worlds, path)
    assert load_worlds(path) == worlds


def test_a_world_saved_by_an_older_build_keeps_todays_behaviour(tmp_path):
    path = tmp_path / "worlds.toml"
    path.write_text('[[world]]\nname = "Old"\nhost = "old.example"\nport = 23\n',
                    encoding="utf-8")
    (world,) = load_worlds(path)
    assert world.command_stacking and world.command_separator == ";"
    assert not world.log_automatically
    assert world.log_files == "session"
    assert world.log_commands
    save_worlds([world], path)  # and a default world writes none of the new settings
    text = path.read_text(encoding="utf-8")
    assert "command_" not in text and "log_" not in text


def test_a_separator_that_cant_work_falls_back_to_a_semicolon(tmp_path):
    from genericmud.config.worlds import separator_problem

    assert World("A", "h", 23, command_separator=" ").command_separator == ";"
    assert World("A", "h", 23, command_separator="/").command_separator == ";"
    assert World("A", "h", 23, command_separator="ab").command_separator == ";"
    assert World("A", "h", 23, command_separator="|").command_separator == "|"
    assert World("A", "h", 23, log_files="weekly").log_files == "session"
    assert separator_problem("") == "The command separator has to be one character."
    assert separator_problem("/").startswith("The command separator can't be /")
    assert separator_problem("|") is None
    path = tmp_path / "worlds.toml"
    path.write_text(
        '[[world]]\nname = "A"\nhost = "h"\nport = 23\ncommand_separator = "  "\n'
        'log_files = "hourly"\n',
        encoding="utf-8",
    )
    (world,) = load_worlds(path)
    assert world.command_separator == ";" and world.log_files == "session"
