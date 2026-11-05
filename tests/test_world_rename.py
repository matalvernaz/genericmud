"""Renaming a saved world from its details.

A world's name is not decoration: it is the log file's name, the /to address from another tab,
and the label on the tab. That is why a tab keeps the name it opened with, and why renaming is
allowed while no tab is using the world and locked while one is.
"""

from __future__ import annotations

import sys

import pytest

from genericmud.config.worlds import World


def _frame(open_worlds: list[World]):
    """A frame-shaped stub: the pages it holds, and the dialog it would open recorded."""
    from genericmud.ui import wx_app

    pages = [type("Page", (), {"world": world})() for world in open_worlds]
    frame = wx_app.GenericMudFrame.__new__(wx_app.GenericMudFrame)
    frame.book = type("Book", (), {"GetPageCount": lambda _self: len(pages),
                                   "GetPage": lambda _self, index: pages[index]})()
    opened: list[dict] = []
    frame._show_world_dialog = lambda **kwargs: opened.append(kwargs)
    return frame, opened


def test_a_world_nobody_has_open_can_be_renamed():
    frame, opened = _frame([])
    frame._edit_saved_world(World("Penultimate", "pen.example", 9500))
    assert opened and opened[0]["lock_name"] is False
    assert opened[0]["note"] == ""
    assert opened[0]["original_name"] == "Penultimate"


def test_a_world_open_in_a_tab_keeps_its_name_and_says_why():
    # The tab's log file, /to address and label all carry this name, so renaming underneath it
    # would leave them disagreeing until the tab closed.
    world = World("Penultimate", "pen.example", 9500)
    frame, opened = _frame([world])
    frame._edit_saved_world(world)
    assert opened[0]["lock_name"] is True
    assert "open in a tab" in opened[0]["note"]
    assert "Close the tab" in opened[0]["note"]


def test_the_comparison_ignores_case_as_the_rest_of_the_client_does():
    frame, opened = _frame([World("penultimate", "pen.example", 9500)])
    frame._edit_saved_world(World("Penultimate", "pen.example", 9500))
    assert opened[0]["lock_name"] is True


def test_renaming_saves_the_new_name_and_forgets_the_old(monkeypatch):
    from genericmud.ui import wx_app

    saved = [World("Old Name", "old.example", 4000)]
    written: list[list[World]] = []
    monkeypatch.setattr(wx_app, "load_worlds", lambda: list(saved))
    monkeypatch.setattr(wx_app, "save_worlds", lambda worlds: written.append(list(worlds)))

    frame = wx_app.GenericMudFrame.__new__(wx_app.GenericMudFrame)
    renamed = World("New Name", "old.example", 4000)
    assert frame._save_world(renamed, replacing="Old Name") is True
    assert [world.name for world in written[-1]] == ["New Name"]


@pytest.fixture(scope="module")
def frame_window():
    wx = pytest.importorskip("wx")
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


def test_the_name_box_is_editable_for_a_world_nobody_has_open(frame_window):
    from genericmud.ui.wx_app import WorldDialog

    dialog = WorldDialog(frame_window, World("Penultimate", "pen.example", 9500), lock_name=False)
    try:
        assert dialog._name.IsEnabled() is True
    finally:
        dialog.Destroy()


def test_the_name_box_is_locked_while_the_world_is_open(frame_window):
    from genericmud.ui.wx_app import WorldDialog

    dialog = WorldDialog(frame_window, World("Penultimate", "pen.example", 9500), lock_name=True)
    try:
        assert dialog._name.IsEnabled() is False
    finally:
        dialog.Destroy()
