"""GTK readonly output must forward real keyvals without losing case or Unicode."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

wx = pytest.importorskip("wx")
pytestmark = pytest.mark.skipif(wx.Platform != "__WXGTK__", reason="GTK keyval routing only")
session_panel = importlib.import_module("genericmud.ui.session_panel")


@pytest.mark.parametrize("keyval,character", [
    (ord("x"), "x"),
    (ord("X"), "X"),
    (ord(" "), " "),
    (0x06D1, "я"),  # Legacy X11 Cyrillic keyval, not a Unicode code point.
    (0x01000000 | ord("界"), "界"),
    (0x01000000 | ord("🐉"), "🐉"),
    (0x0100200D, "\u200d"),  # Preserve a typed joiner, as the regular CHAR path does.
    (0xFE51, ""),  # Dead acute accent stays with the native input method.
    (0, ""),
])
@pytest.mark.parametrize("modifier", ["none", "control", "alt", "meta"])
def test_output_key_preserves_native_character(keyval, character, modifier):
    forwarded, skipped = [], []
    event = SimpleNamespace(
        GetKeyCode=lambda: ord("X"),  # wx KEY_DOWN uppercases ASCII regardless of layout/case.
        GetRawKeyCode=lambda: keyval,
        ControlDown=lambda: modifier == "control",
        AltDown=lambda: modifier == "alt",
        MetaDown=lambda: modifier == "meta",
        ShiftDown=lambda: False,
        Skip=lambda: skipped.append(True),
    )
    panel = SimpleNamespace(
        _prefs=SimpleNamespace(numpad_compass=False),
        _dispatch_bound_combo=lambda _combo: False,
        _on_output_char=lambda character_event: forwarded.append(character_event.GetUnicodeKey()),
    )

    session_panel.SessionPanel._on_output_key(panel, event)

    expected = [ord(character)] if character and modifier == "none" else []
    assert forwarded == expected
    assert bool(skipped) == (not expected)
