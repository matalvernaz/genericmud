"""Renaming a saved world from its details.

A world's name is not decoration: it is the log file's name, the /to address from another tab,
and the label on the tab. A tab that is connected is using all three, so the name waits for it
to disconnect; a disconnected tab is using nothing, so it takes the new name and opens the new
log the next time it connects.
"""

from __future__ import annotations

import sys

import pytest

from genericmud.config.worlds import World


class _Connection:
    def __init__(self, *, connected: bool = False, connecting: bool = False) -> None:
        self.connected = connected
        self.connecting = connecting


class _Panel:
    def __init__(self, world: World, connection: _Connection) -> None:
        self.world = world
        self._connection = connection
        self.renamed: list[str] = []

    def take_world_name(self, name: str) -> None:
        self.renamed.append(name)


def _frame(panels: list[_Panel], saved: list[World]):
    """A frame-shaped stub: its tabs, the worlds file, and where announcements went."""
    from genericmud.ui import main_window

    frame = main_window.GenericMudFrame.__new__(main_window.GenericMudFrame)
    frame.book = type(
        "Book",
        (),
        {
            "GetPageCount": lambda _self: len(panels),
            "GetPage": lambda _self, index: panels[index],
            "SetPageText": lambda _self, index, text: setattr(
                panels[index], "label", text
            ),
        },
    )()
    frame._credentials = None
    frame.said = []
    frame.announce = frame.said.append
    frame._update_title = lambda: None
    frame._save_world = lambda world, replacing=None: (
        frame.saved_calls.append((world, replacing)) or True
    )
    frame.saved_calls = []
    return frame


@pytest.fixture
def dialog(monkeypatch):
    """Replace the real dialog: records what it was asked for, and answers as the test says."""
    wx = pytest.importorskip("wx")
    from genericmud.ui import saved_worlds

    class FakeDialog:
        asked: list[dict] = []
        edited: World | None = None
        answer = wx.ID_OK

        def __init__(self, parent, initial=None, **kwargs):
            FakeDialog.asked.append({"initial": initial, **kwargs})

        def ShowModal(self):
            return FakeDialog.answer

        def get_world(self):
            return FakeDialog.edited

        def Destroy(self):
            pass

    FakeDialog.asked = []
    FakeDialog.edited = None
    FakeDialog.answer = wx.ID_OK
    monkeypatch.setattr(saved_worlds, "WorldDialog", FakeDialog)
    return FakeDialog


def test_a_disconnected_world_can_be_renamed_and_its_tab_follows(dialog):
    old = World("Penultimate", "pen.example", 9500)
    panel = _Panel(old, _Connection(connected=False))
    frame = _frame([panel], [old])
    dialog.edited = World("Penultimate Destination", "pen.example", 9500)

    frame._edit_saved_world(old)

    assert dialog.asked[0]["lock_name"] is False
    assert dialog.asked[0]["note"] == ""
    assert panel.renamed == ["Penultimate Destination"]
    assert getattr(panel, "label", "") == "Penultimate Destination"
    assert frame.saved_calls and frame.saved_calls[0][1] == "Penultimate"
    assert any("Renamed Penultimate to Penultimate Destination" in said for said in frame.said)


def _world_files(root, name):
    """The three files a perfectly set-up world owns, written under the name it is filed by."""
    from genericmud.safepath import world_component

    filed = world_component(name)
    made = []
    for folder, suffix in (("userpacks", ""), ("maps", ".json"), ("state", "-vars.json")):
        path = root / folder / f"{filed}{suffix}"
        if folder == "userpacks":
            path.mkdir(parents=True)
            (path / "rules.json").write_text("{}", encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        made.append(path)
    return made


def test_a_rename_carries_the_map_rules_and_variables_to_the_new_name(
    dialog, monkeypatch, tmp_path
):
    pytest.importorskip("wx")
    from genericmud.safepath import world_component
    from genericmud.ui import saved_worlds

    monkeypatch.setattr(saved_worlds, "config_dir", lambda: tmp_path)
    old = World("Penultimate", "pen.example", 9500)
    panel = _Panel(old, _Connection(connected=False))
    frame = _frame([panel], [old])
    dialog.edited = World("Penultimate Destination", "pen.example", 9500)
    was = _world_files(tmp_path, "Penultimate")

    frame._edit_saved_world(old)

    now = world_component("Penultimate Destination")
    assert (tmp_path / "userpacks" / now / "rules.json").is_file()
    assert (tmp_path / "maps" / f"{now}.json").is_file()
    assert (tmp_path / "state" / f"{now}-vars.json").is_file()
    for path in was:  # nothing is left filed under the name it no longer has
        assert not path.exists(), path
    assert any("moved with it" in said for said in frame.said)


def test_a_file_already_filed_under_the_new_name_is_never_overwritten(
    dialog, monkeypatch, tmp_path
):
    pytest.importorskip("wx")
    from genericmud.safepath import world_component
    from genericmud.ui import saved_worlds

    monkeypatch.setattr(saved_worlds, "config_dir", lambda: tmp_path)
    old = World("Penultimate", "pen.example", 9500)
    frame = _frame([_Panel(old, _Connection())], [old])
    dialog.edited = World("Arrived", "pen.example", 9500)
    _world_files(tmp_path, "Penultimate")
    now = world_component("Arrived")
    mine = tmp_path / "state" / f"{now}-vars.json"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text('{"kept": true}', encoding="utf-8")

    frame._edit_saved_world(old)

    assert mine.read_text(encoding="utf-8") == '{"kept": true}'
    # only the file that was already there is left alone: everything else still moves
    assert (tmp_path / "maps" / f"{now}.json").is_file()
    assert (tmp_path / "userpacks" / now / "rules.json").is_file()


def test_a_rename_with_no_files_to_carry_says_nothing_about_files(
    dialog, monkeypatch, tmp_path
):
    pytest.importorskip("wx")
    from genericmud.ui import saved_worlds

    monkeypatch.setattr(saved_worlds, "config_dir", lambda: tmp_path)
    old = World("Penultimate", "pen.example", 9500)
    frame = _frame([_Panel(old, _Connection())], [old])
    dialog.edited = World("Penultimate Destination", "pen.example", 9500)

    frame._edit_saved_world(old)

    assert not any("moved with it" in said for said in frame.said)
    assert any("Renamed Penultimate to Penultimate Destination" in said for said in frame.said)


def test_a_connected_tab_locks_the_name_and_says_how_to_free_it(dialog):
    old = World("Penultimate", "pen.example", 9500)
    panel = _Panel(old, _Connection(connected=True))
    frame = _frame([panel], [old])
    dialog.edited = World("Renamed", "pen.example", 9500)

    frame._edit_saved_world(old)

    assert dialog.asked[0]["lock_name"] is True
    assert "connected in a tab" in dialog.asked[0]["note"]
    assert "Ctrl+D" in dialog.asked[0]["note"]


def test_a_tab_that_is_still_connecting_counts_as_using_the_name(dialog):
    old = World("Penultimate", "pen.example", 9500)
    panel = _Panel(old, _Connection(connecting=True))
    frame = _frame([panel], [old])
    dialog.edited = World("Renamed", "pen.example", 9500)

    frame._edit_saved_world(old)
    assert dialog.asked[0]["lock_name"] is True


def test_cancelling_the_dialog_saves_and_renames_nothing(dialog):
    old = World("Penultimate", "pen.example", 9500)
    panel = _Panel(old, _Connection())
    frame = _frame([panel], [old])
    dialog.answer = pytest.importorskip("wx").ID_CANCEL
    frame._edit_saved_world(old)
    assert frame.saved_calls == []
    assert panel.renamed == []


def test_an_edit_that_only_changes_settings_never_touches_the_name(dialog):
    old = World("Penultimate", "pen.example", 9500)
    panel = _Panel(old, _Connection())
    frame = _frame([panel], [old])
    dialog.edited = World("Penultimate", "pen.example", 9501)  # same name, new port

    frame._edit_saved_world(old)
    assert panel.renamed == []  # nothing to follow: the name did not change
    assert frame.saved_calls[0][1] == "Penultimate"


def test_the_engine_takes_the_new_name_and_closes_the_old_log():
    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.session.hub import SessionHub
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    stopped: list[bool] = []

    class Logger:
        active = True

        def stop(self) -> None:
            stopped.append(True)

    hub = SessionHub()
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="Penultimate",
        hub=hub,
    )
    app.logger = Logger()
    app.rename_world("Penultimate Destination")

    assert stopped == [True]  # the old log is closed, not left appending
    assert app.logger is None
    assert app.name == "Penultimate Destination"
    assert "Penultimate Destination" in hub.sessions()
    assert "Penultimate" not in hub.sessions()


def test_renaming_to_the_same_name_changes_nothing():
    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="Penultimate",
    )
    app.logger = object()  # not a real logger: it must not be touched
    app.rename_world("Penultimate")
    assert app.logger is not None


# --- the dialog's own name box --------------------------------------------------


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


def test_the_name_box_is_editable_for_a_world_nobody_is_connected_to(window):
    from genericmud.ui.world_dialog import WorldDialog

    dialog = WorldDialog(window, World("Penultimate", "pen.example", 9500), lock_name=False)
    try:
        assert dialog._name.IsEnabled() is True
    finally:
        dialog.Destroy()


def test_the_name_box_is_locked_while_the_world_is_connected(window):
    from genericmud.ui.world_dialog import WorldDialog

    dialog = WorldDialog(window, World("Penultimate", "pen.example", 9500), lock_name=True)
    try:
        assert dialog._name.IsEnabled() is False
    finally:
        dialog.Destroy()


def test_the_dialog_keeps_the_name_it_has_when_the_rule_refuses_it(window, monkeypatch):
    # The refusal happens before anything is written, so no world and no login are touched.
    wx = pytest.importorskip("wx")
    from genericmud.ui import world_dialog

    told: list[str] = []
    dialog = world_dialog.WorldDialog(
        window,
        World("Penultimate", "pen.example", 9500),
        name_check=lambda name: "" if name == "Penultimate" else "that name is taken",
    )
    ended: list[int] = []
    dialog.EndModal = ended.append
    monkeypatch.setattr(wx, "MessageBox", lambda text, *_a, **_k: told.append(text))
    try:
        dialog._name.SetValue("Aardwolf")
        dialog._on_ok(None)
        assert ended == []  # the dialog stayed open
        assert told == ["that name is taken"]
    finally:
        dialog.Destroy()

def test_renaming_onto_another_worlds_name_is_refused(monkeypatch):
    # Logins are kept by world name, so this would replace that world and take its login with it.
    from genericmud.ui import saved_worlds

    worlds = [World("Penultimate", "pen.example", 9500), World("Aardwolf", "aard.example", 4000)]
    monkeypatch.setattr(saved_worlds, "load_worlds", lambda: list(worlds))
    frame = _frame([], worlds)

    refusal = frame._rename_problem("Penultimate", "Aardwolf")
    assert refusal.startswith("There is already a world called Aardwolf")
    assert refusal.endswith("Choose a different name.")


def test_its_own_name_is_no_clash_and_a_free_one_is_fine(monkeypatch):
    from genericmud.ui import saved_worlds

    worlds = [World("Penultimate", "pen.example", 9500)]
    monkeypatch.setattr(saved_worlds, "load_worlds", lambda: list(worlds))
    frame = _frame([], worlds)

    assert frame._rename_problem("Penultimate", "Penultimate") == ""
    assert frame._rename_problem("Penultimate", "penultimate") == ""  # case, as everywhere
    assert frame._rename_problem("Penultimate", "Penultimate Destination") == ""


# --- after the rename: the session's files and log follow the name --------------------------


def _engine(tmp_path, name="Penultimate"):
    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    return EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name=name,
        map_dir=tmp_path / "maps",
        log_dir=tmp_path / "logs",
    )


def test_the_session_saves_its_map_under_the_new_name_after_the_files_moved(tmp_path):
    # The session looked its map up under the old name when it connected. The window then moved
    # the file to the new name; saving back to the old path would leave the renamed world with
    # a stale map and the newer one under a name nothing reads.
    app = _engine(tmp_path)
    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / "Penultimate.json").write_text("{}", encoding="utf-8")
    assert app._map_path().name == "Penultimate.json"
    (tmp_path / "maps" / "Penultimate.json").rename(tmp_path / "maps" / "Pen.json")

    app.rename_world("Pen")

    assert app._map_path().name == "Pen.json"


def test_a_file_that_stayed_under_the_old_name_stays_the_sessions(tmp_path):
    # The new name already had a map of its own, so the window left this one where it was
    # ("stayed under the old name"). The session keeps writing the one it read.
    app = _engine(tmp_path)
    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / "Penultimate.json").write_text("{}", encoding="utf-8")
    (tmp_path / "maps" / "Pen.json").write_text("{}", encoding="utf-8")
    assert app._map_path().name == "Penultimate.json"

    app.rename_world("Pen")

    assert app._map_path().name == "Penultimate.json"


def test_a_log_the_rename_closed_opens_again_under_the_new_name(tmp_path):
    # Started by hand with Alt+Shift+L, not by the world's automatic logging: the rename closed
    # it, so the next connection carries it on under the new name instead of silently ending it.
    app = _engine(tmp_path)
    app.start_logging(announce=False)
    app.rename_world("Pen")
    assert app.logger is None

    app.after_reconnect()

    assert app.logger is not None and app.logger.active
    assert app.logger.path.name.startswith("Pen-")


def test_a_log_stopped_by_hand_stays_stopped_through_a_rename(tmp_path):
    app = _engine(tmp_path)
    app.start_logging(announce=False)
    app._toggle_log()  # Alt+Shift+L again: stopped by hand
    app.rename_world("Pen")

    app.after_reconnect()

    assert app.logger is None


def test_a_rename_with_no_tab_open_says_nothing_about_a_tab(dialog, monkeypatch, tmp_path):
    pytest.importorskip("wx")
    from genericmud.ui import saved_worlds

    monkeypatch.setattr(saved_worlds, "config_dir", lambda: tmp_path)
    old = World("Penultimate", "pen.example", 9500)
    frame = _frame([], [old])
    dialog.edited = World("Penultimate Destination", "pen.example", 9500)

    frame._edit_saved_world(old)

    assert frame.said == ["Renamed Penultimate to Penultimate Destination."]


def test_a_change_of_case_carries_the_files_too(dialog, monkeypatch, tmp_path):
    # On Linux "penultimate" and "Penultimate" are two names, so the files must follow; on
    # Windows and macOS they are one file, which is respelled rather than seen as a clash.
    import os

    pytest.importorskip("wx")
    from genericmud.ui import saved_worlds

    monkeypatch.setattr(saved_worlds, "config_dir", lambda: tmp_path)
    old = World("penultimate", "pen.example", 9500)
    panel = _Panel(old, _Connection(connected=False))
    frame = _frame([panel], [old])
    dialog.edited = World("Penultimate", "pen.example", 9500)
    _world_files(tmp_path, "penultimate")

    frame._edit_saved_world(old)

    assert os.listdir(tmp_path / "maps") == ["Penultimate.json"]
    assert os.listdir(tmp_path / "state") == ["Penultimate-vars.json"]
    assert os.listdir(tmp_path / "userpacks") == ["Penultimate"]
    assert panel.renamed == ["Penultimate"]
    assert not any("stayed under the old name" in said for said in frame.said)
