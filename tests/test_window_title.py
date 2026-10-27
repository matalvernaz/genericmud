"""The window title, and whether it names the world you're in.

MUSHclient calls its window after the world ("MUSHclient - [Penworthy]"); so does this, when
asked. Off by default, because renaming someone's window is not a change to spring on them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

wx = pytest.importorskip("wx")  # wx_app imports wx at module level
from genericmud.config.ui_prefs import UiPrefs  # noqa: E402 - after the importorskip
from genericmud.ui.wx_app import GenericMudFrame  # noqa: E402 - ditto

NAMES = ["Alpha", "Beta", "Gamma"]


class _Book:
    def __init__(self, names: list[str], selected: int = 0) -> None:
        self.pages = [SimpleNamespace(world=SimpleNamespace(name=n)) for n in names]
        self._selected = selected

    def GetSelection(self):  # noqa: N802 - wx API name
        return wx.NOT_FOUND if not self.pages else self._selected

    def GetPageCount(self):  # noqa: N802 - wx API name
        return len(self.pages)

    def GetPage(self, index):  # noqa: N802 - wx API name
        return self.pages[index]

    def ChangeSelection(self, index):  # noqa: N802 - wx API name
        self._selected = index


def _frame(names: list[str] | None = None, selected: int = 0, **prefs: bool) -> SimpleNamespace:
    frame = SimpleNamespace(
        book=_Book(NAMES if names is None else names, selected),
        _prefs=UiPrefs(**prefs),
        titles=[],
        said=[],
    )
    frame.SetTitle = frame.titles.append
    frame.announce = frame.said.append
    frame._window_title = lambda: GenericMudFrame._window_title(frame)
    frame._update_title = lambda: GenericMudFrame._update_title(frame)
    frame._active_world_name = lambda: GenericMudFrame._active_world_name(frame)
    return frame


def test_the_title_is_just_the_app_by_default():
    frame = _frame()
    frame._update_title()
    assert frame.titles == ["genericMud"]


def test_the_title_names_the_world_when_asked():
    frame = _frame(title_shows_world=True)
    frame._update_title()
    assert frame.titles == ["genericMud - [Alpha]"]


def test_the_title_follows_the_selected_tab():
    frame = _frame(title_shows_world=True, selected=2)
    assert frame._window_title() == "genericMud - [Gamma]"


def test_no_worlds_open_still_says_just_the_app():
    frame = _frame([], title_shows_world=True)
    frame._update_title()
    assert frame.titles == ["genericMud"]


def test_turning_it_off_puts_the_old_title_back():
    frame = _frame(title_shows_world=True)
    frame._update_title()
    frame._prefs.title_shows_world = False
    frame._update_title()
    assert frame.titles == ["genericMud - [Alpha]", "genericMud"]


def test_the_menu_toggle_saves_the_preference_and_says_what_it_did():
    saved: list[tuple[str, bool]] = []
    frame = _frame()

    def save_pref(name: str, value: bool) -> None:
        saved.append((name, value))
        setattr(frame._prefs, name, value)

    frame._save_pref = save_pref
    frame._title_world_item = SimpleNamespace(IsChecked=lambda: True)

    GenericMudFrame._on_toggle_title_world(frame, None)

    assert saved == [("title_shows_world", True)]
    assert frame.titles == ["genericMud - [Alpha]"]
    assert frame.said == ["The window title names the world you're in."]

    frame._title_world_item = SimpleNamespace(IsChecked=lambda: False)
    GenericMudFrame._on_toggle_title_world(frame, None)
    assert frame.titles[-1] == "genericMud"
    assert frame.said[-1] == "The window title is just genericMud."


def test_the_title_is_refreshed_wherever_the_focus_is():
    # _update_focus runs on every switch, open and close, so the label cannot go stale while a
    # different world is in front.
    frame = _frame()
    frame._prefs.title_shows_world = True
    frame._world_in_front = lambda: True
    frame._alive = True
    frame._update_title = lambda: GenericMudFrame._update_title(frame)
    for page in frame.book.pages:
        page.set_focused = lambda _focused: None

    GenericMudFrame._update_focus(frame)
    assert frame.titles == ["genericMud - [Alpha]"]


def test_the_preference_survives_a_restart(tmp_path):
    from genericmud.config.ui_prefs import load_ui_prefs, save_ui_prefs

    path = tmp_path / "ui-prefs.toml"
    assert UiPrefs().title_shows_world is False
    save_ui_prefs(UiPrefs(title_shows_world=True), path)
    assert load_ui_prefs(path).title_shows_world is True
    assert "title_shows_world = true" in path.read_text(encoding="utf-8")
