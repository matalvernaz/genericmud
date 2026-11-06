"""The windows a soundpack can open: a notepad, a question, and a message box.

Also the record of a question a pack's script is waiting on, which the loop thread asks and the
window's thread answers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import wx

from genericmud.scripting.api import MESSAGE_BUTTONS


class PackNotepadFrame(wx.Frame):
    """A soundpack's MUSHclient notepad: text to read in a window of its own, while the game
    carries on. Read-only; Escape or Close puts it away."""

    def __init__(self, parent: wx.Window | None, title: str, text: str) -> None:
        super().__init__(parent, title=title, size=(640, 480))
        panel = wx.Panel(self)
        label = wx.StaticText(panel, label="&Text:")
        self.text = wx.TextCtrl(
            panel, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.text.SetName(title)
        close = wx.Button(panel, wx.ID_CLOSE, "&Close")
        close.Bind(wx.EVT_BUTTON, lambda _event: self.Close())
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(label, 0, wx.LEFT | wx.TOP, 4)
        sizer.Add(self.text, 1, wx.EXPAND | wx.ALL, 4)
        sizer.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

    def set_text(self, text: str) -> None:
        self.text.SetValue(text)
        self.text.SetInsertionPoint(0)

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE and not event.HasAnyModifiers():
            self.Close()
            return
        event.Skip()


class PackQuestionDialog(wx.Dialog):
    """A soundpack's question: a list to choose one or several items from, or text to type.

    Built from the request a pack's script made through ScriptApi's ask_* methods. The
    prompt is a StaticText placed right before the control and the control is named after
    it, the pattern NVDA reads reliably here. :meth:`answer` reads what the player chose.
    """

    def __init__(self, parent: wx.Window, request: dict, title: str) -> None:
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._kind = request["kind"]
        prompt = request.get("prompt") or ""
        outer = wx.BoxSizer(wx.VERTICAL)
        if prompt:
            label = wx.StaticText(self, label=prompt.replace("&", "&&"))
            label.Wrap(560)
            outer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)
        if self._kind == "text":
            multiline = bool(request.get("multiline"))
            self._control = wx.TextCtrl(
                self, value=request.get("default") or "",
                style=wx.TE_MULTILINE if multiline else 0,
                size=(560, 200) if multiline else (560, -1),
            )
            self._control.SelectAll()  # typing replaces what the pack filled in
        elif self._kind == "choices":
            # A multiple-selection list: NVDA says "selected" or "not selected" as the
            # player arrows through, and Space switches the item.
            self._control = wx.ListBox(
                self, choices=request["items"], style=wx.LB_MULTIPLE, size=(560, 300)
            )
            for index in request.get("selected") or []:
                self._control.SetSelection(index)
        else:
            self._control = wx.ListBox(
                self, choices=request["items"], style=wx.LB_SINGLE, size=(560, 300)
            )
            # Nothing is selected unless the pack names an item, as in MUSHclient, where OK
            # alone then answers nothing: a mapper's "Choose exit to delete" deletes whatever
            # comes back. A VIPMud pick list always names one.
            selected = request.get("selected")
            if selected is not None:
                self._control.SetSelection(selected)
            self._control.Bind(wx.EVT_LISTBOX_DCLICK, lambda _event: self.EndModal(wx.ID_OK))
        self._control.SetName(prompt or title)
        outer.Add(self._control, 1, wx.ALL | wx.EXPAND, 10)
        outer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 10)
        self.SetSizerAndFit(outer)
        self._control.SetFocus()

    def answer(self, result: int = wx.ID_OK) -> object | None:
        """What the player chose, given how the dialog closed (ShowModal's result)."""
        if result != wx.ID_OK:
            return None
        if self._kind == "text":
            return self._control.GetValue()
        if self._kind == "choices":
            return list(self._control.GetSelections())
        index = self._control.GetSelection()
        return index if index != wx.NOT_FOUND else None


# A msgbox button's id and label, by the name MESSAGE_BUTTONS gives it.
_MESSAGE_BUTTON_IDS = {
    "ok": (wx.ID_OK, "OK"),
    "cancel": (wx.ID_CANCEL, "Cancel"),
    "yes": (wx.ID_YES, "&Yes"),
    "no": (wx.ID_NO, "&No"),
    "retry": (wx.ID_RETRY, "&Retry"),
    "abort": (wx.ID_ABORT, "&Abort"),
    "ignore": (wx.ID_IGNORE, "&Ignore"),
}
_MESSAGE_ICONS = {
    "!": wx.ART_WARNING, "?": wx.ART_QUESTION, "i": wx.ART_INFORMATION, ".": wx.ART_ERROR,
}


class PackMessageDialog(wx.Dialog):
    """A soundpack's message box (MUSHclient's utils.msgbox): its text and its buttons.

    genericMud's own dialog, not the system's, so a closing tab or a question that ran out
    of time can take it down (EndModal fails on a native one). The text is a StaticText the
    screen reader reads with the dialog, and the default button has the focus. Escape is
    Cancel where there is one and OK on a lone OK, and does nothing on Yes/No or
    Abort/Retry/Ignore, as on Windows' own message boxes. :meth:`answer` names the button.
    """

    def __init__(self, parent: wx.Window, request: dict, title: str) -> None:
        super().__init__(parent, title=title)
        names = MESSAGE_BUTTONS[request["buttons"]]
        self._names = {_MESSAGE_BUTTON_IDS[name][0]: name for name in names}
        outer = wx.BoxSizer(wx.VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        art = _MESSAGE_ICONS.get(request.get("icon"), wx.ART_WARNING)
        icon = wx.StaticBitmap(self, bitmap=wx.ArtProvider.GetBitmap(art, wx.ART_MESSAGE_BOX))
        row.Add(icon, 0, wx.ALL, 10)
        message = wx.StaticText(self, label=(request.get("prompt") or "").replace("&", "&&"))
        message.Wrap(480)
        row.Add(message, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 10)
        outer.Add(row, 1, wx.EXPAND)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._buttons: list[wx.Button] = []
        for name in names:
            button_id, label = _MESSAGE_BUTTON_IDS[name]
            button = wx.Button(self, button_id, label=label)
            button.Bind(wx.EVT_BUTTON, lambda _event, ident=button_id: self.EndModal(ident))
            buttons.Add(button, 0, wx.ALL, 5)
            self._buttons.append(button)
        outer.Add(buttons, 0, wx.ALIGN_CENTER | wx.BOTTOM, 5)
        self.SetSizerAndFit(outer)
        if "cancel" in names:
            self.SetEscapeId(wx.ID_CANCEL)
        elif names == ("ok",):
            self.SetEscapeId(wx.ID_OK)
        else:
            self.SetEscapeId(wx.ID_NONE)
        default = self._buttons[min(max(request.get("default", 1), 1), len(names)) - 1]
        default.SetDefault()
        default.SetFocus()

    def answer(self, result: int) -> str | None:
        """The name of the button pressed, given ShowModal's result; None when the dialog
        was closed some other way."""
        return self._names.get(result)


@dataclass
class _PackQuestion:
    """A question a pack's script is waiting on: asked on the loop thread, shown and
    answered on the UI thread."""

    request: dict
    answer: object = None
    done: threading.Event = field(default_factory=threading.Event)
    dialog: wx.Dialog | None = None  # while it's on screen
    shown_at: float | None = None  # time.monotonic() when it came up

    def finish(self, answer: object) -> None:
        if not self.done.is_set():
            self.answer = answer
            self.done.set()
