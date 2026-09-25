"""Build the native dialogs for real, instead of checking wx_app.py's source text.

The Linux CI jobs install the gui extra and run the suite under xvfb, so these construct
real controls there. They skip wherever wxPython is missing or can't open a display: a
dev host without wx, or a macOS runner, whose uv-provided Python isn't the framework
build wx needs to reach the window server. No dialog is shown modally, so nothing can
wait on a keypress.
"""

from __future__ import annotations

import sys

import pytest

wx = pytest.importorskip("wx")

from genericmud.config.worlds import World  # noqa: E402 - after the wx importorskip
from genericmud.protocol.charset import AUTO, ENCODING_CHOICES  # noqa: E402

_VALUES = [value for value, _label in ENCODING_CHOICES]


@pytest.fixture(scope="module")
def frame():
    if sys.platform == "darwin":
        pytest.skip("wx needs a framework-build Python on macOS")
    try:
        app = wx.App(False)
    except BaseException as error:  # noqa: BLE001 - wx raises SystemExit with no display
        pytest.skip(f"wx can't start here: {error}")
    parent = wx.Frame(None)
    yield parent
    parent.Destroy()
    del app


def _accept(dialog) -> None:
    """Run the dialog's OK handler without a modal loop to end."""
    dialog.EndModal = lambda _code: None
    dialog._on_ok(None)


def test_world_dialog_offers_every_encoding_and_starts_on_automatic(frame):
    from genericmud.ui.wx_app import WorldDialog

    dialog = WorldDialog(frame)
    try:
        assert dialog._encoding.GetCount() == len(ENCODING_CHOICES)
        assert dialog._encoding.GetSelection() == _VALUES.index(AUTO)
        assert dialog._encoding.GetName() == "Encoding"
    finally:
        dialog.Destroy()


def test_world_dialog_round_trips_the_encoding(frame):
    from genericmud.ui.wx_app import WorldDialog

    dialog = WorldDialog(frame, World("Bylins", "bylins.example", 4000, encoding="koi8-r"))
    try:
        assert _VALUES[dialog._encoding.GetSelection()] == "koi8-r"
        dialog._encoding.SetSelection(_VALUES.index("cp1251"))
        _accept(dialog)
        assert dialog.get_world().encoding == "cp1251"
    finally:
        dialog.Destroy()


def test_world_dialog_label_precedes_the_encoding_choice(frame):
    # NVDA names a control from the StaticText created immediately before it.
    from genericmud.ui.wx_app import WorldDialog

    dialog = WorldDialog(frame)
    try:
        children = list(dialog.GetChildren())
        index = children.index(dialog._encoding)
        assert isinstance(children[index - 1], wx.StaticText)
        assert children[index - 1].GetLabel() == "&Encoding:"
    finally:
        dialog.Destroy()


def test_connect_dialog_names_a_non_automatic_encoding(frame):
    from genericmud.ui.wx_app import ConnectDialog

    dialog = ConnectDialog(
        frame,
        [World("Aard", "aard.example", 4000), World("Bylins", "bylins.example", 4000,
                                                     encoding="cp1251")],
    )
    try:
        details = {}
        for index, world in enumerate(dialog._saved):
            dialog._choice.SetSelection(index)
            dialog._update_details()
            details[world.name] = dialog._details.GetValue()
        assert "Encoding: Cyrillic, Windows-1251" in details["Bylins"]
        assert "Encoding" not in details["Aard"]
    finally:
        dialog.Destroy()
