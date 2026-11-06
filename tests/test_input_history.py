"""Up and Down in the command box: history only from an empty box or from a sent message.

Typing an unsent draft and pressing Down used to empty the box. The rules are: history moves
only when the box is empty or holds a message that is itself in history, and only from the
caret's first or last position. Anything else is a draft still being written, and the key is
left to the field so the caret moves and the text stays.
"""

from __future__ import annotations


class _Box:
    """The parts of the command box these rules touch, without wx."""

    def __init__(self, value: str = "", caret: int | None = None) -> None:
        self.value = value
        self.caret = len(value) if caret is None else caret

    def GetValue(self) -> str:
        return self.value

    def SetValue(self, text: str) -> None:
        self.value = text

    def GetInsertionPoint(self) -> int:
        return self.caret

    def SetInsertionPointEnd(self) -> None:
        self.caret = len(self.value)


def _panel(history: list[str], value: str = "", caret: int | None = None):
    from genericmud.ui import session_panel

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel.input = _Box(value, caret)
    panel._history = list(history)
    panel._hist_index = len(history)
    return panel


def test_up_from_an_empty_box_shows_the_newest_command():
    panel = _panel(["look", "score"], "")
    assert panel._history_step(-1) is True
    assert panel.input.value == "score"
    assert panel.input.caret == len("score")


def test_down_from_an_empty_box_does_nothing():
    panel = _panel(["look"], "")
    assert panel._history_step(1) is False
    assert panel.input.value == ""


def test_a_draft_that_matches_nothing_sent_is_never_replaced():
    # The reported bug: Down emptied the box and the message was gone.
    for direction in (-1, 1):
        panel = _panel(["look"], "half a thought", caret=0)
        assert panel._history_step(direction) is False
        assert panel.input.value == "half a thought"


def test_a_draft_is_safe_from_the_middle_of_itself_too():
    panel = _panel(["look"], "half a thought", caret=6)
    assert panel._history_step(-1) is False
    assert panel.input.value == "half a thought"


def test_up_walks_back_through_history_from_a_message_already_in_it():
    panel = _panel(["look", "score", "inventory"], "inventory")
    assert panel._history_step(-1) is True
    assert panel.input.value == "score"
    assert panel._history_step(-1) is True
    assert panel.input.value == "look"


def test_the_caret_must_be_at_an_edge_to_walk():
    # Caret in the middle of a recalled message: this is editing, not history.
    panel = _panel(["look", "score"], "score", caret=2)
    assert panel._history_step(-1) is False
    assert panel.input.value == "score"


def test_the_caret_at_the_start_counts_as_an_edge():
    panel = _panel(["look", "score"], "score", caret=0)
    assert panel._history_step(-1) is True
    assert panel.input.value == "look"


def test_down_past_the_newest_entry_empties_the_box():
    panel = _panel(["look", "score"], "score")
    assert panel._history_step(1) is True
    assert panel.input.value == ""
    # And from there Up starts again at the newest.
    assert panel._history_step(-1) is True
    assert panel.input.value == "score"


def test_up_from_the_oldest_entry_has_nowhere_to_go():
    panel = _panel(["look", "score"], "look")
    assert panel._history_step(-1) is False
    assert panel.input.value == "look"


def test_an_edited_recall_behaves_like_any_other_draft():
    # "score" was recalled and then changed, so it matches nothing sent.
    panel = _panel(["look", "score"], "score north")
    assert panel._history_step(-1) is False
    assert panel.input.value == "score north"


def test_walking_back_the_way_you_came_reaches_the_same_entries():
    panel = _panel(["look", "score"], "")
    panel._history_step(-1)  # "score"
    panel._history_step(-1)  # "look"
    assert panel._history_step(1) is True
    assert panel.input.value == "score"
    assert panel._history_step(1) is True
    assert panel.input.value == ""
