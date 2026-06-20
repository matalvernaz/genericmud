"""Saved-worlds config round-trip."""

from __future__ import annotations

from genericmud.config.worlds import World, load_worlds, save_worlds


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
