"""Keymap loader test."""

from __future__ import annotations

from genericmud.config.keymap import load_keymap


def test_vipmud_keymap_loads_expected_bindings():
    keymap = load_keymap("vipmud")
    assert keymap["ctrl+1"] == "recall:1"
    assert keymap["ctrl+9"] == "recall:9"
    assert keymap["alt+up"] == "review:prev_line"
    assert keymap["f11"] == "voice:flush"
