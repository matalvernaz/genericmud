"""Adding a world to the list without connecting to it.

The OK button always opened a session, so the only way to keep a world was to connect to it --
which is no way at all when the MUD is down, or when the world is being set up for later.
"""

from __future__ import annotations

import sys

import pytest

wx = pytest.importorskip("wx")

from genericmud.config.worlds import World  # noqa: E402 - after the wx importorskip


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


def _dialog(frame, *args, **kwargs):
    from genericmud.ui.world_dialog import WorldDialog

    return WorldDialog(frame, *args, **kwargs)


def _filled(dialog) -> None:
    """Everything the dialog insists on, so only the box under test can refuse."""
    dialog._name.SetValue("Penultimate")
    dialog._host.SetValue("pen.example")


class _FakeWorldDialog:
    """A dialog that answers without being shown; the codes below speak for the checkboxes."""

    save = True
    connect = True
    world: World | None = None
    asked: list[dict] = []

    def __init__(self, parent, initial=None, **kwargs):
        type(self).asked.append({"initial": initial, **kwargs})
        self._world = type(self).world

    def ShowModal(self):
        return wx.ID_OK

    def Destroy(self):
        pass

    def get_world(self):
        return self._world

    def should_save(self):
        return type(self).save

    def should_connect(self):
        return type(self).connect


def _frame_shape(monkeypatch):
    """A frame-shaped stub: records what is saved, connected and said."""
    from genericmud.ui import main_window, saved_worlds

    frame = main_window.GenericMudFrame.__new__(main_window.GenericMudFrame)
    frame._credentials = None
    frame.credentials_calls = []
    frame.said = []
    frame.announce = frame.said.append
    frame.opened = []
    frame.saved = []

    def _save_world(world, replacing=None):
        frame.saved.append(world)
        return True

    frame._save_world = _save_world
    frame.open_session = frame.opened.append
    frame._apply_to_open_tabs = lambda *_args: None
    monkeypatch.setattr(saved_worlds, "WorldDialog", _FakeWorldDialog)
    _FakeWorldDialog.save = True
    _FakeWorldDialog.connect = True
    _FakeWorldDialog.world = World("Penultimate", "pen.example", 9500)
    _FakeWorldDialog.asked = []
    return frame


def test_the_new_world_dialog_offers_three_ways_to_press_ok(frame):
    # One control holding all three, so nothing is hidden and nothing has to be remembered.
    from genericmud.ui.world_dialog import _OK_CHOICES

    dialog = _dialog(frame, show_connect=True)
    try:
        group = dialog._ok_choice
        assert group is not None
        assert [group.GetString(i) for i in range(group.GetCount())] == [
            label for label, _saves, _connects in _OK_CHOICES
        ]
        assert group.GetSelection() == 0  # connecting is what a new world is usually for
        assert (dialog.should_save(), dialog.should_connect()) == (True, True)
    finally:
        dialog.Destroy()


def test_a_world_that_must_not_be_saved_starts_on_the_choice_that_does_not(frame):
    # save_default is honoured rather than the group always opening on the first row.
    dialog = _dialog(frame, show_connect=True, save_default=False)
    try:
        assert dialog._ok_choice.GetStringSelection() == "Connect, don't save"
        assert (dialog.should_save(), dialog.should_connect()) == (False, True)
    finally:
        dialog.Destroy()


def test_the_group_is_labelled_so_it_is_read_as_a_set(frame):
    dialog = _dialog(frame, show_connect=True)
    try:
        assert dialog._ok_choice.GetLabel() == "When I press OK:"
        assert dialog._ok_choice.GetName() == "When I press OK"
    finally:
        dialog.Destroy()


def test_each_choice_does_exactly_what_it_says(frame):
    # Keyed by the words on the button, not by its position: a reordered group that kept its
    # meanings would still pass, and a meaning that changed under a name would not.
    expected = {
        "Save and connect": (True, True),
        "Save, don't connect": (True, False),
        "Connect, don't save": (False, True),
    }
    dialog = _dialog(frame, show_connect=True)
    try:
        group = dialog._ok_choice
        assert {group.GetString(i) for i in range(group.GetCount())} == set(expected)
        for index in range(group.GetCount()):
            group.SetSelection(index)
            assert (
                dialog.should_save(),
                dialog.should_connect(),
            ) == expected[group.GetString(index)]
    finally:
        dialog.Destroy()


def test_there_is_no_choice_that_does_neither(frame):
    # The reason this is a radio group: "neither saved nor connected" is not on offer, so the
    # OK button never has to be told off for doing nothing, and there is no dead state at all.
    dialog = _dialog(frame, show_connect=True)
    try:
        count = dialog._ok_choice.GetCount()
        for index in range(count):
            dialog._ok_choice.SetSelection(index)
            assert dialog.should_save() or dialog.should_connect()
    finally:
        dialog.Destroy()


def test_a_choice_made_is_kept_while_the_dialog_is_filled_in(frame):
    dialog = _dialog(frame, show_connect=True)
    try:
        dialog._ok_choice.SetSelection(1)
        _filled(dialog)
        dialog.EndModal = lambda _code: None
        dialog._on_ok(None)
        assert dialog.get_world().name == "Penultimate"
        assert dialog.should_save() is True
        assert dialog.should_connect() is False
    finally:
        dialog.Destroy()


def test_editing_a_world_is_not_offered_the_choice(frame):
    # Editing saves; there is nothing to decide, and a choice that changed nothing would lie.
    dialog = _dialog(frame, World("Pen", "pen.example", 4000), show_save=False)
    try:
        assert dialog._ok_choice is None
        assert dialog.should_save() is True
        assert dialog.should_connect() is True  # and nothing else changes for that path
    finally:
        dialog.Destroy()


def test_the_soundpack_setup_dialog_keeps_its_single_save_box(frame):
    # That dialog saves and then connects, so there is nothing to choose; it is not made to grow
    # a group of three about a choice it does not offer.
    dialog = _dialog(frame)
    try:
        assert dialog._ok_choice is None
        assert isinstance(dialog._save, wx.CheckBox)
        assert dialog.should_save() is True and dialog.should_connect() is True
    finally:
        dialog.Destroy()


def test_saving_without_connecting_keeps_the_world_and_opens_no_tab(monkeypatch):
    from genericmud.ui import saved_worlds

    frame = _frame_shape(monkeypatch)
    _FakeWorldDialog.connect = False

    done = frame._show_world_dialog(show_connect=True)

    assert done.outcome == saved_worlds._SAVED
    assert [world.name for world in frame.saved] == ["Penultimate"]
    assert frame.opened == []  # no tab: the whole point
    assert any("Penultimate saved" in said for said in frame.said)
    assert any("Control O" in said for said in frame.said)
    assert _FakeWorldDialog.asked[0]["show_connect"] is True


def test_connecting_as_usual_still_saves_and_opens_a_tab(monkeypatch):
    from genericmud.ui import saved_worlds

    frame = _frame_shape(monkeypatch)

    done = frame._show_world_dialog(show_connect=True)

    assert done.outcome == saved_worlds._CONNECTED
    assert [world.name for world in frame.saved] == ["Penultimate"]
    assert [world.name for world in frame.opened] == ["Penultimate"]
    assert not any("Control O" in said for said in frame.said)


def test_connecting_without_saving_opens_a_tab_and_leaves_the_list_alone(monkeypatch):
    # The third choice: try a MUD without adding it to the saved worlds.
    from genericmud.ui import saved_worlds

    frame = _frame_shape(monkeypatch)
    _FakeWorldDialog.save = False

    done = frame._show_world_dialog(show_connect=True)

    assert done.outcome == saved_worlds._CONNECTED
    assert frame.saved == []
    assert [world.name for world in frame.opened] == ["Penultimate"]


def test_the_menu_new_world_offers_the_choice(monkeypatch):
    # Ctrl+N is the usual way in, so it is the one that must offer it.
    frame = _frame_shape(monkeypatch)

    frame._on_new_world(None)

    assert _FakeWorldDialog.asked[0]["show_connect"] is True
    assert _FakeWorldDialog.asked[0]["show_save"] is True


def test_cancelling_says_nothing_and_saves_nothing(monkeypatch):
    from genericmud.ui import saved_worlds

    frame = _frame_shape(monkeypatch)
    _FakeWorldDialog.ShowModal = lambda _self: wx.ID_CANCEL
    try:
        done = frame._show_world_dialog(show_connect=True)
    finally:
        _FakeWorldDialog.ShowModal = lambda _self: wx.ID_OK

    assert done.outcome == saved_worlds._CANCELLED
    assert done.world is None
    assert frame.saved == [] and frame.opened == [] and frame.said == []


def test_the_saved_world_list_opens_on_the_world_it_is_given(frame):
    from genericmud.ui.connect_dialog import ConnectDialog

    worlds = [World("Aard", "a.example", 4000), World("Pen", "p.example", 4000)]
    dialog = ConnectDialog(frame, worlds, preselect="Pen")
    try:
        assert dialog.get_world().name == "Pen"  # Enter connects to it, no arrowing needed
    finally:
        dialog.Destroy()


def test_a_preselect_that_is_not_there_picks_the_first_row(frame):
    from genericmud.ui.connect_dialog import ConnectDialog

    dialog = ConnectDialog(
        frame, [World("Aard", "a.example", 4000)], preselect="Not There"
    )
    try:
        assert dialog.get_world().name == "Aard"  # never lands on nothing
    finally:
        dialog.Destroy()


def test_saving_without_connecting_returns_to_the_list_with_it_picked(monkeypatch):
    # The list is where the reader was, so that is where they should land, with the new world
    # under the cursor and one Enter away from connecting.
    from genericmud.ui import connect_dialog, saved_worlds

    frame = _frame_shape(monkeypatch)
    _FakeWorldDialog.connect = False
    seen: list[object] = []

    class FakeConnectDialog:
        answers: list[int] = []

        def __init__(self, parent, saved, preselect=None):
            seen.append(preselect)

        def ShowModal(self):
            return FakeConnectDialog.answers.pop(0)

        def get_world(self):
            return None

        def Destroy(self):
            pass

    FakeConnectDialog.answers = [connect_dialog._ID_NEW_WORLD, wx.ID_CANCEL]
    monkeypatch.setattr(saved_worlds, "ConnectDialog", FakeConnectDialog)

    frame._on_connect(None)

    assert seen == [None, "Penultimate"]  # the list reopened with the saved world picked
    assert frame.opened == []
    assert _FakeWorldDialog.asked[0]["show_connect"] is True  # the New World button offered it


def test_editing_a_world_returns_to_the_list_with_it_still_picked(monkeypatch):
    # After an edit the reader goes back to the list, and should land where they were: on the
    # world they edited, under its new name if they renamed it, not at the top of the list.
    from genericmud.ui import connect_dialog, saved_worlds

    frame = _frame_shape(monkeypatch)
    seen: list[object] = []
    picked = World("Penultimate", "pen.example", 9500)

    class FakeConnectDialog:
        answers: list[int] = []

        def __init__(self, parent, saved, preselect=None):
            seen.append(preselect)

        def ShowModal(self):
            return FakeConnectDialog.answers.pop(0)

        def get_world(self):
            return picked

        def Destroy(self):
            pass

    renamed = World("Penultimate Destination", "pen.example", 9500)
    FakeConnectDialog.answers = [
        connect_dialog._ID_EDIT_WORLD, connect_dialog._ID_EDIT_WORLD, wx.ID_CANCEL
    ]
    results = [renamed, None]  # renamed the first time, cancelled the second
    monkeypatch.setattr(saved_worlds, "ConnectDialog", FakeConnectDialog)
    frame._edit_saved_world = lambda world: results.pop(0)

    frame._on_connect(None)

    assert seen == [None, "Penultimate Destination", "Penultimate"]
