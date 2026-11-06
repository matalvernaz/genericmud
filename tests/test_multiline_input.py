"""The command box is multi-line: Enter sends, a pasted block sends a command per line.

It is multi-line for two reasons. A single-line field leaves the screen reader's line moves with
nothing to move through, so they degenerate into one character per press; and a pasted list is a
list of commands, which is how a world with stacking off gets several at once.
"""

from __future__ import annotations

from tests.ui_source import ui_class_source, ui_source


class _Box:
    def __init__(self, value: str = "") -> None:
        self.value = value
        self.caret = len(value)

    def GetValue(self) -> str:
        return self.value

    def SetValue(self, text: str) -> None:
        self.value = text

    def GetInsertionPoint(self) -> int:
        return self.caret

    def WriteText(self, text: str) -> None:
        self.value = self.value[: self.caret] + text + self.value[self.caret :]
        self.caret += len(text)

    def SetInsertionPointEnd(self) -> None:
        self.caret = len(self.value)


class _Loop:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def call_soon_threadsafe(self, callback, *args):
        self.messages.append(args[0])


def _panel(*, value: str = "", connected: bool = True, masked: bool = False):
    from genericmud.ui import command_box, session_panel

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel.input = _Box(value)
    panel._history = []
    panel._hist_index = 0
    panel._history_limit = command_box._HISTORY_LIMIT
    panel._input_masked = masked
    panel._connect_finished = True
    panel.is_connected = lambda: connected
    panel.is_connecting = lambda: False  # down, and not retrying by itself
    panel.prompts = []
    panel._prompt_reconnect = lambda: panel.prompts.append(True)
    panel._loop = _Loop()
    panel.app = type("App", (), {"on_ws_message": lambda _self, message: None})()
    return panel


def _sent(panel) -> list[str]:
    return [message["text"] for message in panel._loop.messages]


def test_one_line_sends_one_command_and_clears_the_box():
    panel = _panel(value="look")
    panel._on_send(None)
    assert _sent(panel) == ["look"]
    assert panel.input.value == ""
    assert panel._history == ["look"]


def test_a_pasted_block_sends_a_command_per_line_in_order():
    # The way to stack on a world that has stacking off, and how a pasted list arrives.
    panel = _panel(value="stand\nlook\nscore\n\n\ninventory")
    panel._on_send(None)
    assert _sent(panel) == ["stand", "look", "score", "inventory"]
    assert panel.input.value == ""
    assert panel._history == ["stand", "look", "score", "inventory"]


def test_each_line_is_its_own_thing_to_recall():
    panel = _panel(value="a\nb")
    panel._on_send(None)
    assert panel._history_step(-1) is True
    assert panel.input.value == "b"
    assert panel._history_step(-1) is True
    assert panel.input.value == "a"


def test_a_line_goes_out_as_typed_spaces_and_all():
    # In-game editors keep a line's indent, and a password may start or end with a space.
    panel = _panel(value="  look  \n  score  ")
    panel._on_send(None)
    assert _sent(panel) == ["  look  ", "  score  "]
    panel = _panel(value="   indented line")
    panel._on_send(None)
    assert _sent(panel) == ["   indented line"]


def test_an_empty_enter_still_reaches_the_mud():
    # A blank Enter answers a pager ("press return to continue"), and with autoretype on the
    # engine repeats the last command for it. A block of nothing but blank lines is the same.
    for value in ("", "\n\n", "   \n\t\n"):
        panel = _panel(value=value)
        panel._on_send(None)
        assert _sent(panel) == [""]
        assert panel.input.value == ""
        assert panel._history == []


def test_an_empty_enter_repeats_the_last_command_with_autoretype_on():
    # End to end through the engine: what the box sends is what autoretype needs.
    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=sent.append,
        post=[].append,
        keymap=load_keymap("vipmud"),
    )
    app.autoretype = True
    for value in ("kill rat", ""):
        panel = _panel(value=value)
        panel._on_send(None)
        for message in panel._loop.messages:
            app.on_ws_message(message)
    assert sent == ["kill rat", "kill rat"]


def test_hidden_input_goes_out_exactly_as_typed():
    panel = _panel(value=" pass word ", masked=True)
    panel._on_send(None)
    assert _sent(panel) == [" pass word "]


def test_a_dead_session_keeps_the_whole_block_while_it_asks():
    panel = _panel(value="look\nscore", connected=False)
    panel._on_send(None)
    assert _sent(panel) == []
    assert panel.prompts == [True]
    assert panel.input.value == "look\nscore"  # nothing typed is lost while deciding


def test_a_block_of_client_commands_works_offline():
    # "/to" and friends go through another session, so this tab's connection is irrelevant.
    panel = _panel(value="/to alter aeon look\n/logs", connected=False)
    panel._on_send(None)
    assert _sent(panel) == ["/to alter aeon look", "/logs"]
    assert panel.prompts == []


def test_a_mixed_block_asks_first_because_part_of_it_needs_the_mud():
    panel = _panel(value="/logs\nlook", connected=False)
    panel._on_send(None)
    assert _sent(panel) == []
    assert panel.prompts == [True]


def test_hidden_input_never_reaches_the_history():
    panel = _panel(value="hunter2", masked=True)
    panel._on_send(None)
    assert _sent(panel) == ["hunter2"]
    assert panel._history == []


# --- the field itself --------------------------------------------------------


def test_the_command_box_is_a_multi_line_field():
    # Not cosmetic: a single-line field is what makes the screen reader's line moves walk one
    # character at a time, and what stops a pasted list from arriving as a block.
    panel = ui_source("SessionPanel.__init__")
    assert "self.input = wx.TextCtrl(" in panel
    box = panel.split("self.input = wx.TextCtrl(", 1)[1].split(")", 1)[0]
    assert "wx.TE_MULTILINE" in box
    assert "wx.TE_PROCESS_ENTER" in box


def test_enter_sends_rather_than_inserting_a_newline():
    # A multi-line field's own Enter inserts a line break; the key is taken before it gets there.
    handler = ui_source("SessionPanel._on_input_key")
    assert "wx.WXK_RETURN" in handler
    assert "self._on_send(None)" in handler

def test_shift_enter_starts_another_command_line():
    # As in a chat box, except a line break separates commands here: a MUD reads a newline as
    # Enter, so a newline can never be part of one command.
    panel = _panel(value="look")
    panel.input.caret = len(panel.input.value)
    panel._start_new_command_line()
    assert panel.input.value == "look\n"


def test_a_block_built_with_shift_enter_sends_one_command_per_line():
    panel = _panel(value="look")
    panel.input.caret = len(panel.input.value)
    panel._start_new_command_line()
    panel.input.value += "score"
    panel._on_send(None)
    assert _sent(panel) == ["look", "score"]


def test_shift_enter_is_taken_by_the_handler_not_left_to_the_field():
    # The field's own Shift+Enter would insert a break anyway; doing it here means the rule is
    # ours, and stays put if the field's default ever differs.
    handler = ui_source("SessionPanel._on_input_key")
    assert "wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER" in handler
    assert "_start_new_command_line()" in handler

def test_the_history_does_not_grow_without_end():
    # A pasted file can be thousands of lines; each is a history entry, so it is capped.
    from genericmud.ui import command_box

    panel = _panel()
    panel._history_limit = 5
    panel._remember(["one", "two", "three", "four", "five", "six", "seven"])
    assert panel._history == ["three", "four", "five", "six", "seven"]
    assert panel._hist_index == len(panel._history)
    assert command_box._HISTORY_LIMIT >= 100  # generous enough to be useful


def test_the_box_height_follows_the_font_rather_than_a_guess():
    # A reader who has scaled their text up would get a clipped box from a pixel height.
    panel = ui_source("SessionPanel.__init__")
    assert "self.input.SetMinSize((-1, self.input.GetCharHeight() * 2 + 8))" in panel
    # The guessed height it replaced.
    assert "size=(-1, 44)" not in ui_class_source("SessionPanel")
