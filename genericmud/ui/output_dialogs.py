"""The small dialogs the output box opens: Find, copy from a line, and an MXP menu."""

from __future__ import annotations

import wx

from genericmud.ui.scrollback import FindState


class ChooseDialog(wx.Dialog):
    """Pick one of the commands an MXP menu offers.

    Following a menu link never sends anything: it asks. The MUD's own words for the link are
    in the title, so the question arrives with its context, and each row is numbered so a
    number key picks it -- the same "type the number" the rest of the client answers to.
    Arrow keys and Enter work the ordinary way, and Cancel sends nothing at all.
    """

    def __init__(self, parent: wx.Window, text: str, choices: list[tuple[str, str]]) -> None:
        super().__init__(parent, title=f"Choose: {text}" if text else "Choose")
        self._choices = list(choices)
        outer = wx.BoxSizer(wx.VERTICAL)
        rows = [
            f"{number}. {caption or command}"
            for number, (command, caption) in enumerate(self._choices, 1)
        ]
        self._list = wx.ListBox(self, choices=rows, style=wx.LB_SINGLE)
        self._list.SetName("Choices")
        outer.Add(self._list, 1, wx.EXPAND | wx.ALL, 10)
        outer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizer(outer)
        self.SetMinSize((420, 280))
        self._list.SetSelection(0)
        self._list.SetFocus()
        self._list.Bind(wx.EVT_CHAR, self._on_char)

    def _on_char(self, event: wx.KeyEvent) -> None:
        code = event.GetKeyCode()
        # A number picks its row at once only while every row has a one-digit number: with ten
        # or more, "1" could be the start of "12", so the list keeps its ordinary keys.
        if len(self._choices) <= 9 and ord("1") <= code <= ord("9"):
            index = code - ord("1")
            if index < len(self._choices):
                self._list.SetSelection(index)
                self.EndModal(wx.ID_OK)
                return
        event.Skip()

    def chosen(self) -> tuple[str, str] | None:
        """The (command, caption) the player picked, or None if the list has no choice."""
        index = self._list.GetSelection()
        return self._choices[index] if 0 <= index < len(self._choices) else None


class FindDialog(wx.Dialog):
    """Search the output: what to find, which way, and whether case matters.

    Opens holding the previous search, so reopening and pressing Enter repeats it. Every
    label is a StaticText placed immediately before its control and the text field is
    named as well, which is the pattern NVDA reads reliably here.
    """

    def __init__(self, parent: wx.Window, state: FindState) -> None:
        super().__init__(parent, title="Find in output")
        outer = wx.BoxSizer(wx.VERTICAL)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Find &what:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._term = wx.TextCtrl(self, value=state.term)
        self._term.SetName("Find what")
        row.Add(self._term, 1, wx.EXPAND)
        outer.Add(row, 0, wx.ALL | wx.EXPAND, 10)

        # Order must mirror the boolean: index 0 = backwards, index 1 = forwards.
        self._direction = wx.RadioBox(
            self,
            label="&Direction",
            choices=["Up, towards older lines", "Down, towards newer lines"],
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        self._direction.SetSelection(1 if state.forward else 0)
        outer.Add(self._direction, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self._case = wx.CheckBox(self, label="Match &case")
        self._case.SetValue(state.case_sensitive)
        outer.Add(self._case, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        outer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 10)
        self.SetSizerAndFit(outer)
        self._term.SetFocus()
        self._term.SelectAll()  # typing replaces the old term; Enter alone repeats it

    def state(self) -> FindState:
        return FindState(
            term=self._term.GetValue(),
            forward=self._direction.GetSelection() == 1,
            case_sensitive=self._case.GetValue(),
        )


class CopyLineDialog(wx.Dialog):
    """Copy all or part of the line being reviewed (Ctrl+Shift+C), an idea from MUDBall.

    The line sits in a read-only box with all of it selected, so Enter copies the whole line,
    and the ordinary selection keys (Shift with the arrows, Ctrl+Shift for whole words) pick
    out part of it first: a name, a number, an address. The label comes straight before the
    box and the box is named as well, the pattern NVDA reads reliably here.
    """

    def __init__(self, parent: wx.Window, line: str) -> None:
        super().__init__(parent, title="Copy from line")
        outer = wx.BoxSizer(wx.VERTICAL)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="&Line:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._line = wx.TextCtrl(self, value=line, style=wx.TE_READONLY)
        self._line.SetName("Line")
        self._line.SetMinSize((480, -1))  # MUD lines run long; it is still one line
        row.Add(self._line, 1, wx.EXPAND)
        outer.Add(row, 0, wx.ALL | wx.EXPAND, 10)

        buttons = wx.StdDialogButtonSizer()
        copy = wx.Button(self, wx.ID_OK, "&Copy")
        copy.SetDefault()
        buttons.AddButton(copy)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, "Close"))
        buttons.Realize()
        outer.Add(buttons, 0, wx.ALL | wx.EXPAND, 10)
        self.SetSizerAndFit(outer)
        self._line.SetFocus()
        self._line.SelectAll()

    def chosen(self) -> str:
        """What to copy: the part selected, or the whole line when nothing is."""
        return self._line.GetStringSelection() or self._line.GetValue()
