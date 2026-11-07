"""Switching worlds by number, and hearing which one you landed in.

Rebindable like every other key: the binding lives in the keymap (``tab:select:N``), but the
work is done by the window, because the engine serves one world and has no idea which tabs
are open. Both ways of changing tabs announce where you are — Ctrl+Tab and Alt+<number> —
since a focus move alone says nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

wx = pytest.importorskip("wx")  # main_window imports wx at module level
from genericmud.config.keymap import load_keymap  # noqa: E402 - after the importorskip
from genericmud.ui.main_window import GenericMudFrame  # noqa: E402 - ditto

NAMES = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Iota", "Kappa"]


class _Input:
    def __init__(self, page: _Page) -> None:
        self._page = page

    def SetFocus(self) -> None:  # noqa: N802 - wx API name
        # Where the page is now, not where it started: moving a tab moves its command box.
        self._page.book.focused.append(self._page.book.pages.index(self._page))


class _Page:
    def __init__(self, name: str) -> None:
        self.world = SimpleNamespace(name=name)
        self.book = None  # the book that holds it, set below
        self.input = _Input(self)


class _Book:
    def __init__(self, names: list[str]) -> None:
        self.focused: list[int] = []
        self.pages = [_Page(name) for name in names]
        for page in self.pages:
            page.book = self
        self._selected = 0

    def GetSelection(self) -> int:  # noqa: N802 - wx API name
        return self._selected

    def GetPageCount(self) -> int:  # noqa: N802 - wx API name
        return len(self.pages)

    def GetPage(self, index: int) -> _Page:  # noqa: N802 - wx API name
        return self.pages[index]

    def ChangeSelection(self, index: int) -> None:  # noqa: N802 - wx API name
        self._selected = index

    def RemovePage(self, index: int) -> None:  # noqa: N802 - wx API name
        # A Simplebook has no MovePage, so moving a tab is RemovePage then InsertPage; the
        # page object itself survives both, which is what keeps the session running.
        self.pages.pop(index)
        if self._selected >= len(self.pages):
            self._selected = max(0, len(self.pages) - 1)

    def InsertPage(self, index: int, page: _Page, _title: str) -> None:  # noqa: N802
        self.pages.insert(index, page)


def _frame(names: list[str] | None = None) -> SimpleNamespace:
    """A frame-shaped object carrying the real methods this feature uses."""
    book = _Book(NAMES if names is None else names)
    frame = SimpleNamespace(book=book, said=[])
    frame.announce = frame.said.append
    frame._update_active = lambda: None
    frame._update_focus = lambda: None
    frame._announce_tab = lambda i: GenericMudFrame._announce_tab(frame, i)
    frame._switch_session = lambda i: GenericMudFrame._switch_session(frame, i)
    frame._select_tab = lambda n: GenericMudFrame._select_tab(frame, n)
    frame._move_where = lambda where: GenericMudFrame._move_where(frame, where)
    frame._move_tab = lambda step: GenericMudFrame._move_tab(frame, step)
    frame._move_tab_to = lambda target: GenericMudFrame._move_tab_to(frame, target)
    frame.handle_tab_action = lambda action: GenericMudFrame.handle_tab_action(frame, action)
    return frame


# --- Alt+<number> -----------------------------------------------------------


def test_a_tab_number_switches_to_that_world_and_says_which_it_is():
    frame = _frame()
    assert frame.handle_tab_action("tab:select:2") is True
    assert frame.book.GetSelection() == 1
    assert frame.said == ["Beta, tab 2 of 10"]


def test_alt_zero_is_the_tenth_tab():
    frame = _frame()
    frame.handle_tab_action("tab:select:10")
    assert frame.book.GetSelection() == 9
    assert frame.said == ["Kappa, tab 10 of 10"]


def test_a_number_past_the_last_tab_says_how_many_are_open():
    frame = _frame(NAMES[:3])
    frame.handle_tab_action("tab:select:5")
    assert frame.book.GetSelection() == 0  # nothing moved
    assert frame.said == ["Only 3 worlds open."]


def test_one_world_is_singular():
    frame = _frame(["Solo"])
    frame.handle_tab_action("tab:select:2")
    assert frame.said == ["Only 1 world open."]


def test_no_worlds_open_is_its_own_message():
    frame = _frame([])
    assert frame.handle_tab_action("tab:select:1") is True
    assert frame.said == ["No worlds are open."]


def test_asking_for_the_tab_you_are_already_on_still_says_where_you_are():
    # A deliberate "where am I?" should answer, unlike a focus move that changed nothing.
    frame = _frame()
    frame.book.ChangeSelection(2)
    frame.handle_tab_action("tab:select:3")
    assert frame.book.GetSelection() == 2
    assert frame.said == ["Gamma, tab 3 of 10"]


def test_the_new_tab_gets_the_command_box_and_the_speech():
    frame = _frame()
    frame.handle_tab_action("tab:select:2")
    assert frame.book.focused == [1]  # the command box of the world switched to


# --- Ctrl+Tab ---------------------------------------------------------------


def test_ctrl_tab_says_which_world_it_landed_in():
    frame = _frame()
    frame._switch_session(2)  # what the Ctrl+Tab handler calls
    assert frame.said == ["Gamma, tab 3 of 10"]


def test_a_switch_that_changes_nothing_stays_silent():
    frame = _frame()
    frame._switch_session(0)  # already on the first tab
    assert frame.said == []


# --- the keymap side --------------------------------------------------------


def test_another_action_is_not_ours():
    frame = _frame()
    assert frame.handle_tab_action("voice:flush") is False
    assert frame.said == [] and frame.book.GetSelection() == 0


def test_a_mistyped_number_is_swallowed_rather_than_typed_at_the_mud():
    frame = _frame()
    assert frame.handle_tab_action("tab:select:banana") is True
    assert frame.book.GetSelection() == 0 and frame.said == []

# --- moving a tab in the row -------------------------------------------------


def _order(frame) -> list[str]:
    return [page.world.name for page in frame.book.pages]


def test_a_tab_moves_down_a_place_and_says_where_it_landed():
    frame = _frame(["Alpha", "Beta", "Gamma"])
    assert frame.handle_tab_action("tab:move:down") is True
    assert _order(frame) == ["Beta", "Alpha", "Gamma"]
    assert frame.book.GetSelection() == 1  # it stays in front, so the typing does too
    assert frame.said == ["Moved Alpha to tab 2 of 3."]


def test_a_tab_moves_up_a_place():
    frame = _frame(["Alpha", "Beta", "Gamma"])
    frame.book.ChangeSelection(2)
    frame.handle_tab_action("tab:move:up")
    assert _order(frame) == ["Alpha", "Gamma", "Beta"]
    assert frame.book.GetSelection() == 1
    assert frame.said == ["Moved Gamma to tab 2 of 3."]


def test_the_first_tab_says_it_is_first_rather_than_wrapping_round():
    # Wrapping would undo an arrangement by surprise; the end of the row is a place.
    frame = _frame(["Alpha", "Beta", "Gamma"])
    frame.handle_tab_action("tab:move:up")
    assert _order(frame) == ["Alpha", "Beta", "Gamma"]
    assert frame.said == ["Alpha is already the first tab."]


def test_the_last_tab_says_it_is_last_rather_than_wrapping_round():
    frame = _frame(["Alpha", "Beta"])
    frame.book.ChangeSelection(1)
    frame.handle_tab_action("tab:move:down")
    assert _order(frame) == ["Alpha", "Beta"]
    assert frame.said == ["Beta is already the last tab."]


def test_a_number_puts_the_tab_straight_into_that_place():
    frame = _frame(["Alpha", "Beta", "Gamma", "Delta"])
    frame.handle_tab_action("tab:move:3")
    assert _order(frame) == ["Beta", "Gamma", "Alpha", "Delta"]
    assert frame.book.GetSelection() == 2
    assert frame.said == ["Moved Alpha to tab 3 of 4."]


def test_a_place_past_the_end_of_the_row_says_how_many_are_open():
    frame = _frame(["Alpha", "Beta"])
    frame.handle_tab_action("tab:move:5")
    assert _order(frame) == ["Alpha", "Beta"]
    assert frame.said == ["Only 2 worlds open."]


def test_moving_to_the_place_it_is_already_in_still_answers():
    frame = _frame(["Alpha", "Beta"])
    frame.handle_tab_action("tab:move:1")
    assert _order(frame) == ["Alpha", "Beta"]
    assert frame.said == ["Alpha is already tab 1 of 2."]


def test_one_world_has_nothing_to_reorder():
    frame = _frame(["Solo"])
    frame.handle_tab_action("tab:move:down")
    assert frame.said == ["Only one world is open, so there is nothing to reorder."]


def test_no_worlds_open_is_its_own_message_for_moving():
    frame = _frame([])
    frame.handle_tab_action("tab:move:down")
    assert frame.said == ["No worlds are open."]


def test_first_and_last_are_there_for_a_keymap_that_does_not_list_numbers():
    # The digits stop at ten, and a keymap need not list them at all.
    frame = _frame(["Alpha", "Beta", "Gamma"])
    frame.handle_tab_action("tab:move:last")
    assert _order(frame) == ["Beta", "Gamma", "Alpha"]

    frame = _frame(["Alpha", "Beta", "Gamma"])
    frame.book.ChangeSelection(1)
    frame.handle_tab_action("tab:move:first")
    assert _order(frame) == ["Beta", "Alpha", "Gamma"]


def test_a_mistyped_move_is_swallowed_rather_than_typed_at_the_mud():
    frame = _frame()
    assert frame.handle_tab_action("tab:move:sideways") is True
    assert frame.said == [] and _order(frame) == NAMES


def test_the_moved_tab_keeps_the_command_box():
    frame = _frame(["Alpha", "Beta"])
    frame.handle_tab_action("tab:move:down")
    assert frame.book.focused == [1]


def _keymap_as_windows_and_linux_load_it(monkeypatch) -> dict[str, str]:
    """The shipped keymap as it loads everywhere but a Mac, which leaves Alt+numbers free
    (test_on_a_mac_option_and_a_number_is_left_for_typing covers that)."""
    with monkeypatch.context() as patch:
        patch.setattr("sys.platform", "linux")
        return load_keymap("vipmud")


def test_the_default_keymap_binds_the_alt_digits_to_tabs(monkeypatch):
    keymap = _keymap_as_windows_and_linux_load_it(monkeypatch)
    for number in range(1, 10):
        assert keymap[f"alt+{number}"] == f"tab:select:{number}"
    assert keymap["alt+0"] == "tab:select:10"


def test_the_default_keymap_binds_moving_a_tab(monkeypatch):
    keymap = _keymap_as_windows_and_linux_load_it(monkeypatch)
    assert keymap["alt+shift+up"] == "tab:move:up"
    assert keymap["alt+shift+down"] == "tab:move:down"
    for number in range(1, 10):
        assert keymap[f"alt+shift+{number}"] == f"tab:move:{number}"
    assert keymap["alt+shift+0"] == "tab:move:10"


# --- A soundpack or hotkey on the same key -----------------------------------


def _engine_app():
    from genericmud.app import EngineApp
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    return EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="erion",
    )


def test_a_pack_binding_on_a_tab_key_still_runs_in_the_engine():
    # Erion's channel history reads messages on Alt+1 to Alt+0. The keymap also has a tab
    # action on those keys, which the engine can't carry out, so the pack's binding runs.
    app = _engine_app()
    fired = []
    app.engine.add_key("alt+1", lambda _ctx: fired.append("history 1"), source="pack")
    app._handle_key("alt+1")
    assert fired == ["history 1"]


def test_a_tab_key_nothing_else_uses_does_nothing_in_the_engine():
    # The window switched tabs before the key got here; there is nothing left to do.
    app = _engine_app()
    app._handle_key("alt+2")


class _Loop:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call_soon_threadsafe(self, function, *args) -> None:
        self.calls.append(args)


def _panel(claimed: bool, keymap: dict[str, str]) -> SimpleNamespace:
    """A session-panel-shaped object whose world does or doesn't bind the pressed key."""
    engine = SimpleNamespace(has_key=lambda _combo: claimed)
    return SimpleNamespace(
        _keymap=keymap,
        app=SimpleNamespace(engine=engine, on_ws_message=lambda _message: None),
        _loop=_Loop(),
    )


def test_alt_number_switches_tabs_when_nothing_in_the_world_uses_it(monkeypatch):
    from genericmud.ui import session_panel

    frame = _frame()
    monkeypatch.setattr(wx, "GetTopLevelParent", lambda _window: frame)
    panel = _panel(claimed=False, keymap=_keymap_as_windows_and_linux_load_it(monkeypatch))
    assert session_panel.SessionPanel._dispatch_bound_combo(panel, "alt+2") is True
    assert frame.book.GetSelection() == 1
    assert panel._loop.calls == []


def test_alt_number_goes_to_the_soundpack_when_it_uses_the_key(monkeypatch):
    from genericmud.ui import session_panel

    frame = _frame()
    monkeypatch.setattr(wx, "GetTopLevelParent", lambda _window: frame)
    panel = _panel(claimed=True, keymap=_keymap_as_windows_and_linux_load_it(monkeypatch))
    assert session_panel.SessionPanel._dispatch_bound_combo(panel, "alt+2") is True
    assert frame.book.GetSelection() == 0
    assert frame.said == []
    assert panel._loop.calls == [({"type": "key", "key": "alt+2"},)]


# --- Macs ---------------------------------------------------------------------


def test_on_a_mac_option_and_a_number_is_left_for_typing(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    keys = load_keymap("vipmud")
    assert not [combo for combo in keys if combo.startswith("alt+") and combo[4:].isdigit()]
    # Shift is no help on a Mac: Option+Shift and a number types a character too.
    assert not [
        combo for combo in keys
        if combo.startswith("alt+shift+") and combo[len("alt+shift+"):].isdigit()
    ]
    assert keys["ctrl+1"] == "recall:1"  # every other key is untouched


def test_elsewhere_alt_and_a_number_switches_tabs(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    keys = load_keymap("vipmud")
    assert keys["alt+1"] == "tab:select:1"
    assert keys["alt+0"] == "tab:select:10"
