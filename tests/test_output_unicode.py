"""Windows UTF-16 character events must forward one complete character from Output."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("wx")
session_panel = importlib.import_module("genericmud.ui.session_panel")


def test_output_waits_for_a_complete_surrogate_pair():
    writes, focused, selections = [], [], []
    command = SimpleNamespace(
        GetSelection=lambda: (2, 4),
        SetFocus=lambda: focused.append(True),
        SetSelection=lambda *selection: selections.append(selection),
        WriteText=writes.append,
    )
    panel = SimpleNamespace(input=command, _output_high_surrogate=0)

    def character(code):
        return SimpleNamespace(
            GetUnicodeKey=lambda: code,
            ControlDown=lambda: False,
            AltDown=lambda: False,
            Skip=lambda: pytest.fail("A handled character must not also reach native output"),
        )

    session_panel.SessionPanel._on_output_char(panel, character(0xD83D))
    assert writes == [] and focused == []
    session_panel.SessionPanel._on_output_char(panel, character(0xDC09))
    assert writes == ["🐉"]
    assert focused == [True] and selections == [(2, 4)]
    assert panel._output_high_surrogate == 0
