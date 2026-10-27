"""Switching worlds by number, and hearing which one you landed in.

Rebindable like every other key: the binding lives in the keymap (``tab:select:N``), but the
work is done by the window, because the engine serves one world and has no idea which tabs
are open. Both ways of changing tabs announce where you are — Ctrl+Tab and Alt+<number> —
since a focus move alone says nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

wx = pytest.importorskip("wx")  # wx_app imports wx at module level
from genericmud.config.keymap import load_keymap  # noqa: E402 - after the importorskip
from genericmud.ui.wx_app import GenericMudFrame  # noqa: E402 - ditto

NAMES = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Iota", "Kappa"]


class _Input:
    def __init__(self, focused: list[int], index: int) -> None:
        self._focused = focused
        self._index = index

    def SetFocus(self) -> None:  # noqa: N802 - wx API name
        self._focused.append(self._index)


class _Page:
    def __init__(self, name: str, focused: list[int], index: int) -> None:
        self.world = SimpleNamespace(name=name)
        self.input = _Input(focused, index)


class _Book:
    def __init__(self, names: list[str]) -> None:
        self.focused: list[int] = []
        self.pages = [_Page(name, self.focused, i) for i, name in enumerate(names)]
        self._selected = 0

    def GetSelection(self) -> int:  # noqa: N802 - wx API name
        return self._selected

    def GetPageCount(self) -> int:  # noqa: N802 - wx API name
        return len(self.pages)

    def GetPage(self, index: int) -> _Page:  # noqa: N802 - wx API name
        return self.pages[index]

    def ChangeSelection(self, index: int) -> None:  # noqa: N802 - wx API name
        self._selected = index


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


def test_the_default_keymap_binds_the_alt_digits_to_tabs():
    keymap = load_keymap("vipmud")
    for number in range(1, 10):
        assert keymap[f"alt+{number}"] == f"tab:select:{number}"
    assert keymap["alt+0"] == "tab:select:10"
