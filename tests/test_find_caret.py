"""The window's half of Find: putting the caret on the line the engine matched.

Windows answers "where does line N begin?" with a message that carries 1,024 characters at most,
so one long line -- a MUD's room description -- is reported as several lines and every number
after it is wrong. These use a real control, because that limit is the whole point of them.
"""

from __future__ import annotations

import pytest

wx = pytest.importorskip("wx")

from genericmud.ui.output_box import _OUTPUT_CAP_LINES  # noqa: E402
from genericmud.ui.scrollback import break_width, position_of_line  # noqa: E402


@pytest.fixture()
def frame():
    application = wx.App()
    window = wx.Frame(None)
    yield window
    window.Destroy()
    del application  # the app has to outlive the windows, so it is held here until the end


class _Voice:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self.spoken.append(text)


class _App:
    def __init__(self) -> None:
        self.voice = _Voice()


class _Panel:
    """Just the parts of SessionPanel._sync_find_caret that it reads."""

    def __init__(self, frame, lines: list[str]) -> None:
        from genericmud.ui.output_box import OutputBoxMixin

        self.app = _App()
        self.output = wx.TextCtrl(
            frame, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP
        )
        self.output.AppendText("\n".join(lines) + "\n")
        self._pending: list[str] = []
        self._find_restart = True
        self._find_anchored = True
        self._find_direction = False  # backwards, the default
        self._sync = OutputBoxMixin._sync_find_caret

    def find(self, matched: str) -> None:
        self._sync(self, {"type": "find_result", "text": matched, "found": True})

    def caret_line(self) -> int:
        before = self.output.GetRange(0, self.output.GetInsertionPoint())
        return before.count("\n")


def _long_description() -> str:
    # Over Windows' 1,024-character limit for one line, as a room description can be.
    return "A long room description. " * 60


def test_the_caret_lands_on_the_announced_line_after_a_long_line(frame):
    lines = [
        "a quiet road",
        _long_description(),
        "and then something you want to find",
    ]
    panel = _Panel(frame, lines)
    panel.find("and then something you want to find")

    assert panel.caret_line() == 2
    # Where the 1,024-character answer bites -- Windows -- the control's own line numbering
    # disagrees with the text's, which is what used to move the caret short: it answers "line 2
    # begins here" for a place still inside the long line. Elsewhere the two agree, and the caret
    # follows the text either way.
    width = break_width(panel.output)
    if width == 2:
        assert panel.output.XYToPosition(0, 2) != position_of_line(lines, 2, width=width)
    else:
        assert panel.output.XYToPosition(0, 2) == position_of_line(lines, 2, width=width)


def test_several_long_lines_do_not_accumulate_an_offset(frame):
    # Each one used to shift the caret by another line, so a search crossing three of them
    # landed three lines short.
    lines = ["first", *[_long_description() for _ in range(3)], "the target line"]
    panel = _Panel(frame, lines)
    panel.find("the target line")
    assert panel.caret_line() == 4


def test_a_search_upwards_still_lands_on_the_line_it_announced(frame):
    lines = ["the target line", _long_description(), "somewhere you were reading", "and more"]
    panel = _Panel(frame, lines)
    # Reading position: the line after the long one, searching upwards.
    panel.output.SetInsertionPoint(position_of_line(lines, 2))
    panel._find_restart = True
    panel._find_anchored = True
    panel.find("the target line")
    assert panel.caret_line() == 0


def test_a_match_the_window_no_longer_holds_says_so_and_leaves_the_caret(frame):
    # The engine searches 50,000 lines; the window keeps the last _OUTPUT_CAP_LINES. A match
    # older than that cannot be reached, and the reader used to be left with a caret that had
    # not moved and no reason for it.
    lines = ["still here", "and here"]
    panel = _Panel(frame, lines)
    panel.output.SetInsertionPoint(position_of_line(lines, 1))
    before = panel.output.GetInsertionPoint()
    panel.find("a line from an hour ago")

    assert panel.output.GetInsertionPoint() == before
    assert panel.app.voice.spoken == [
        "The cursor stayed where it was: what you just heard is further back than the output "
        "keeps."
    ]


def test_the_window_keeps_fewer_lines_than_the_buffer_searches(frame):
    # The reason that last case happens at all, stated where a change to either number will
    # see it: the engine's buffer holds DEFAULT_CAPACITY, this control holds far fewer.
    from genericmud.model.buffer import DEFAULT_CAPACITY

    assert _OUTPUT_CAP_LINES < DEFAULT_CAPACITY
