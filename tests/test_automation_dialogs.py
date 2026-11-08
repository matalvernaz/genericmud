"""The automation editors and the Automation Manager, as a keyboard and a screen reader meet them.

Shortcut letters, the label a screen reader reads for each field, the order Tab visits the fields
in, and what the manager says and saves when its buttons are pressed. The dialogs are built for
real under xvfb, as in test_wx_dialogs.py; none is shown modally.
"""

from __future__ import annotations

import re
import sys

import pytest

wx = pytest.importorskip("wx")

_MNEMONIC = re.compile(r"&([^&])")


@pytest.fixture(scope="module")
def frame():
    if sys.platform == "darwin":
        pytest.skip("wx needs a framework-build Python on macOS")
    try:
        app = wx.GetApp() or wx.App(False)
    except BaseException as error:  # noqa: BLE001 - wx raises SystemExit with no display
        pytest.skip(f"wx can't start here: {error}")
    parent = wx.Frame(None)
    yield parent
    parent.Destroy()
    del app


def _no_variables():
    return [], False


def _tab_stops(window) -> list[tuple[wx.Window, str, wx.StaticText | None]]:
    """Each control Tab reaches, in order, with the text that names it and the label before it.

    A checkbox, button or radio box names itself; any other control is named by the StaticText
    created straight before it, which is the only naming that works on every platform.
    """
    stops = []
    before = None
    for child in window.GetChildren():
        if isinstance(child, wx.StaticText):
            before = child
            continue
        if not (child.AcceptsFocusFromKeyboard() or isinstance(child, wx.RadioBox)):
            before = None
            continue
        own = (
            child.GetLabel()
            if isinstance(child, (wx.CheckBox, wx.Button, wx.RadioBox))
            else ""
        )
        stops.append((child, own or (before.GetLabel() if before else ""), before))
        before = None
    return stops


def _shortcut_clashes(window, *, enabled_only: bool = False) -> dict[str, list[str]]:
    """Letters two controls answer to. OK and Cancel are left out: on Windows they have none."""
    letters: dict[str, list[str]] = {}
    for child, label, _before in _tab_stops(window):
        if child.GetId() in (wx.ID_OK, wx.ID_CANCEL):
            continue
        if enabled_only and not child.IsEnabled():
            continue
        found = _MNEMONIC.search(label.replace("&&", ""))
        if found:
            letters.setdefault(found.group(1).lower(), []).append(label)
    return {letter: labels for letter, labels in letters.items() if len(labels) > 1}


# --- the MUD data rule editor -------------------------------------------------------------


def test_no_two_controls_in_the_mud_data_editor_share_a_shortcut(frame, tmp_path):
    # "&Enabled" and "Choose from the values the MUD has s&ent" both answered to Alt+E, so on
    # Windows Alt+E moved between the two instead of reaching either.
    from genericmud.packs.user_rules import UserWatch
    from genericmud.ui.rule_editors import DataRuleEditorDialog

    dialog = DataRuleEditorDialog(frame, tmp_path, UserWatch(), variables=_no_variables)
    try:
        assert _shortcut_clashes(dialog) == {}
        labels = [label for _child, label, _before in _tab_stops(dialog)]
        assert "Choose f&rom the values the MUD has sent..." in labels
    finally:
        dialog.Destroy()
