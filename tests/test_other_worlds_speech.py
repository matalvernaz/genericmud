"""Which open worlds are allowed to speak.

Nine worlds, one in front. Before this setting only that one spoke, which meant the other
eight could burn down unnoticed. The setting is the tab-level twin of Background silence and
composes with it: it is asked first, and self-voice and app focus still have the last word.

These call the real ``_update_active`` through a stub frame, so the decision is exercised
rather than just grepped for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

wx = pytest.importorskip("wx")  # wx_app imports wx at module level
from genericmud.config.ui_prefs import UiPrefs  # noqa: E402 - after the importorskip
from genericmud.ui.wx_app import GenericMudFrame  # noqa: E402 - ditto


class _Panel:
    def __init__(self) -> None:
        self.active: bool | None = None

    def set_active(self, active: bool) -> None:
        self.active = active


class _Book:
    def __init__(self, count: int, selected: int) -> None:
        self.pages = [_Panel() for _ in range(count)]
        self._selected = selected

    def GetSelection(self) -> int:
        return self._selected

    def GetPageCount(self) -> int:
        return len(self.pages)

    def GetPage(self, index: int) -> _Panel:
        return self.pages[index]


def _frame(*, worlds: int = 3, selected: int = 0, self_voice: bool = True,
           focused: bool = True, **prefs: bool) -> SimpleNamespace:
    """A frame-shaped object: just the attributes _update_active reads."""
    return SimpleNamespace(
        book=_Book(worlds, selected),
        _prefs=UiPrefs(**prefs),
        _self_voice=self_voice,
        _app_focused=focused,
    )


def _speaking(frame: SimpleNamespace) -> list[bool]:
    GenericMudFrame._update_active(frame)
    return [page.active for page in frame.book.pages]


def test_by_default_only_the_world_in_front_speaks():
    assert _speaking(_frame()) == [True, False, False]
    assert _speaking(_frame(selected=1)) == [False, True, False]


def test_every_open_world_speaks_when_the_player_asks_for_them():
    assert _speaking(_frame(silence_other_worlds=False)) == [True, True, True]
    assert _speaking(_frame(selected=2, silence_other_worlds=False)) == [True, True, True]


def test_self_voice_still_silences_them_all():
    assert _speaking(_frame(self_voice=False, silence_other_worlds=False)) == [
        False,
        False,
        False,
    ]


def test_background_silence_still_wins_when_the_app_is_behind():
    # The two settings compose: asking to hear every world doesn't override "be quiet while
    # another window has focus", and the reverse is true too.
    frame = _frame(silence_other_worlds=False, background_silence=True, focused=False)
    assert _speaking(frame) == [False, False, False]
    still_focused = _frame(silence_other_worlds=False, background_silence=True)
    assert _speaking(still_focused) == [True, True, True]
    # And with background silence off, losing focus costs nothing at all.
    assert _speaking(_frame(silence_other_worlds=False, focused=False)) == [True, True, True]


def test_a_new_world_opened_while_the_setting_is_off_speaks_too():
    # _update_active is what every tab change and every session start goes through, so a
    # ninth world added later is covered without anything else knowing about the setting.
    assert _speaking(_frame(worlds=9, selected=8, silence_other_worlds=False)) == [True] * 9


def test_the_menu_toggle_saves_and_announces_what_changed():
    saved: list[tuple[str, bool]] = []
    said: list[str] = []
    frame = _frame()

    def save_pref(name: str, value: bool) -> None:  # the real one also updates the prefs
        saved.append((name, value))
        setattr(frame._prefs, name, value)

    frame._save_pref = save_pref
    frame.announce = said.append
    frame._update_active = lambda: GenericMudFrame._update_active(frame)
    frame._other_worlds_item = SimpleNamespace(IsChecked=lambda: False)

    GenericMudFrame._on_toggle_other_worlds(frame, None)

    assert saved == [("silence_other_worlds", False)]
    assert frame._prefs.silence_other_worlds is False
    # Unchecked now means every world speaks, so every panel is active.
    assert [page.active for page in frame.book.pages] == [True, True, True]
    assert said[-1] == "Every open world speaks now."
