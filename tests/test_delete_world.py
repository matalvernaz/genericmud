"""Deleting a saved world.

The one thing in the client that destroys something the player made, so it asks first and says
what it is about to take: the world and its login, but not the logs it wrote or its sound folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from genericmud.config.worlds import World


class _Credentials:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, world: str) -> None:
        self.deleted.append(world)


class _Connection:
    def __init__(self, connected: bool) -> None:
        self.connected = connected


class _Panel:
    def __init__(self, world: World, *, connected: bool) -> None:
        self.world = world
        self._connection = _Connection(connected)

    def is_connected(self) -> bool:
        return self._connection.connected


def _frame(*, panels: list[_Panel] | None = None):
    from genericmud.ui import wx_app

    panels = panels or []
    frame = wx_app.GenericMudFrame.__new__(wx_app.GenericMudFrame)
    frame.book = type(
        "Book",
        (),
        {
            "GetPageCount": lambda _self: len(panels),
            "GetPage": lambda _self, index: panels[index],
        },
    )()
    frame._credentials = _Credentials()
    frame.said = []
    frame.announce = frame.said.append
    return frame


@pytest.fixture
def answer(monkeypatch):
    """Answer the confirmation without showing it, and record what it asked."""
    wx = pytest.importorskip("wx")
    from genericmud.ui import wx_app

    class FakeDialog:
        asked: list[str] = []
        result = wx.ID_YES

        def __init__(self, parent, message, title, style):
            FakeDialog.asked.append(message)
            FakeDialog.style = style

        def ShowModal(self):
            return FakeDialog.result

        def Destroy(self):
            pass

    FakeDialog.asked = []
    FakeDialog.result = wx.ID_YES
    monkeypatch.setattr(wx_app.wx, "MessageDialog", FakeDialog)
    return FakeDialog


def _saving(monkeypatch, worlds: list[World]) -> list[list[World]]:
    from genericmud.ui import wx_app

    written: list[list[World]] = []
    monkeypatch.setattr(wx_app, "load_worlds", lambda: list(worlds))
    monkeypatch.setattr(wx_app, "save_worlds", lambda kept: written.append(list(kept)))
    return written


def test_deleting_removes_the_world_from_the_saved_list(answer, monkeypatch):
    worlds = [World("Pen", "pen.example", 9500), World("Keep", "keep.example", 4000)]
    written = _saving(monkeypatch, worlds)
    frame = _frame()

    frame._delete_saved_world(World("Pen", "pen.example", 9500))

    assert [[world.name for world in kept] for kept in written] == [["Keep"]]
    assert any("Pen deleted" in said for said in frame.said)


def test_deleting_forgets_the_saved_login(answer, monkeypatch):
    # A login kept for a world that no longer exists is a credential nobody is looking after.
    _saving(monkeypatch, [World("Pen", "pen.example", 9500)])
    frame = _frame()
    frame._delete_saved_world(World("Pen", "pen.example", 9500))
    assert frame._credentials.deleted == ["Pen"]


def test_saying_no_changes_nothing(answer, monkeypatch):
    wx = pytest.importorskip("wx")
    worlds = [World("Pen", "pen.example", 9500)]
    written = _saving(monkeypatch, worlds)
    frame = _frame()
    answer.result = wx.ID_NO

    frame._delete_saved_world(World("Pen", "pen.example", 9500))

    assert written == []
    assert frame._credentials.deleted == []
    assert frame.said == []


def test_the_question_names_the_world_and_what_survives(answer, monkeypatch):
    _saving(monkeypatch, [World("Pen", "pen.example", 9500)])
    frame = _frame()

    frame._delete_saved_world(World("Pen", "pen.example", 9500))

    asked = answer.asked[0]
    assert "Pen" in asked
    assert "password" in asked  # what goes with it
    assert "Press No to keep it." in asked
    assert "A tab is connected" not in asked
    # Everything else a world owns is named, because it survives and a player should know.
    for survives in ("log files", "sound folder", "map", "saved variables", "automation rules"):
        assert survives in asked, survives


def test_it_says_so_when_a_tab_is_using_the_world(answer, monkeypatch):
    # Deleting the record does not close a running session, so the question says so up front.
    world = World("Pen", "pen.example", 9500)
    _saving(monkeypatch, [world])
    frame = _frame(panels=[_Panel(world, connected=True)])

    frame._delete_saved_world(world)

    assert "A tab is connected to it now" in answer.asked[0]
    assert "keeps running until you close it" in answer.asked[0]


def test_a_tab_that_is_only_disconnected_is_not_mentioned(answer, monkeypatch):
    world = World("Pen", "pen.example", 9500)
    _saving(monkeypatch, [world])
    frame = _frame(panels=[_Panel(world, connected=False)])

    frame._delete_saved_world(world)
    assert "A tab is connected" not in answer.asked[0]


def test_a_failed_save_keeps_the_login(answer, monkeypatch):
    # The world is still in the file, so its password must stay with it.
    from genericmud.ui import wx_app

    monkeypatch.setattr(wx_app, "load_worlds", lambda: [World("Pen", "pen.example", 9500)])

    def refuse(_kept):
        raise OSError("disk full")

    monkeypatch.setattr(wx_app, "save_worlds", refuse)
    told: list[str] = []
    monkeypatch.setattr(wx_app.wx, "MessageBox", lambda text, *_a, **_k: told.append(text))
    frame = _frame()

    frame._delete_saved_world(World("Pen", "pen.example", 9500))

    assert frame._credentials.deleted == []
    assert told and "disk full" in told[0]
    assert frame.said == []


def test_deleting_takes_one_world_and_leaves_a_same_named_different_one_alone(answer, monkeypatch):
    # Names are compared without case, as everywhere else, and the match must be exact otherwise.
    worlds = [World("Pen", "pen.example", 9500), World("Pen Two", "pen2.example", 9500)]
    written = _saving(monkeypatch, worlds)
    frame = _frame()

    frame._delete_saved_world(World("PEN", "pen.example", 9500))

    assert [[world.name for world in kept] for kept in written] == [["Pen Two"]]

# --- the key that reaches it ---------------------------------------------------


@pytest.fixture(scope="module")
def window():
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


def _dialog(window, *names: str):
    from genericmud.ui.wx_app import ConnectDialog

    return ConnectDialog(window, [World(name, "h.example", 4000) for name in names])


def test_the_delete_button_does_not_claim_alt_d(window):
    # "Connection details" already owns Alt+D; a second claim sent focus to the wrong control.
    wx = pytest.importorskip("wx")
    dialog = _dialog(window, "Pen")
    try:
        labels = []
        for child in dialog.GetChildren():
            if isinstance(child, wx.Button):
                labels.append(child.GetLabel())
        assert "Delete..." in labels
        assert not any(label.startswith("&Delete") for label in labels)
    finally:
        dialog.Destroy()


def test_the_delete_key_removes_the_picked_world(window):
    wx = pytest.importorskip("wx")
    dialog = _dialog(window, "Pen")
    ended: list[int] = []
    dialog.EndModal = ended.append
    try:
        assert dialog._choice.GetSelection() == 0
        dialog._on_choice_key(_KeyEvent(wx.WXK_DELETE))
        # The accelerator and the button both end the dialog with the delete result.
        from genericmud.ui import wx_app

        assert ended[0] == wx_app._ID_DELETE_WORLD
    finally:
        dialog.Destroy()


def test_alt_r_is_bound_as_an_accelerator():
    # The button carries no mnemonic, so Alt+R has to be an accelerator of its own.
    source = (Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py").read_text(
        encoding="utf-8"
    )
    call = [line for line in source.splitlines() if "wx.AcceleratorTable(" in line]
    assert len(call) == 1, call  # one table, not one per button
    assert "wx.ACCEL_ALT" in call[0]
    assert 'ord("R")' in call[0]
    assert "_ID_DELETE_WORLD" in call[0]


def test_delete_on_an_empty_list_does_nothing(window):
    wx = pytest.importorskip("wx")
    dialog = _dialog(window)  # no saved worlds
    ended: list[int] = []
    dialog.EndModal = ended.append
    try:
        event = _KeyEvent(wx.WXK_DELETE)
        dialog._on_choice_key(event)
        assert ended == []
        assert event.skipped is True
    finally:
        dialog.Destroy()


def test_other_keys_reach_the_list_as_usual(window):
    wx = pytest.importorskip("wx")
    dialog = _dialog(window, "Pen")
    try:
        event = _KeyEvent(wx.WXK_DOWN)
        dialog._on_choice_key(event)
        assert event.skipped is True  # moving through the list is the list's job
    finally:
        dialog.Destroy()


class _KeyEvent:
    def __init__(self, code: int) -> None:
        self._code = code
        self.skipped = False

    def GetKeyCode(self) -> int:
        return self._code

    def Skip(self) -> None:
        self.skipped = True
