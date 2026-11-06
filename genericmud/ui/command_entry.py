"""Optional native editable command document. No engine or sent-history ownership."""

from __future__ import annotations

import sys

import wx

from genericmud.ui.command_document import CommandDocument


class CommandEntryCtrl(wx.TextCtrl):
    def __init__(self, parent: wx.Window):
        super().__init__(
            parent,
            style=wx.TE_MULTILINE | wx.TE_DONTWRAP | wx.TE_PROCESS_ENTER,
            size=(-1, 110),
        )
        self.document = CommandDocument()
        self._appending = False
        self.Bind(wx.EVT_TEXT, self._on_edit)
        self.Bind(wx.EVT_CHAR, self._on_char)

    def line_number(self) -> int:
        # Convert native CRLF/UTF-16 positions before counting logical lines.
        return self.text_range(0, self.GetSelection()[0]).count("\n")

    def text_range(self, start: int, end: int) -> str:
        if sys.platform == "darwin":
            # Cocoa selection uses UTF-16; wx 3.2.9 GetRange slices wxString in
            # code points instead. Keep all new editor reads in selection units.
            encoded = self.GetValue().encode("utf-16-le")
            return encoded[2 * start:2 * end].decode("utf-16-le", errors="replace")
        return self.GetRange(start, end)

    def selected_text(self) -> str:
        return self.text_range(*self.GetSelection())

    def position_from_offset(self, offset: int) -> int:
        """Translate a Python text offset to the native selection's units."""
        prefix = self.GetValue()[:offset]
        if sys.platform == "darwin":
            return len(prefix.encode("utf-16-le")) // 2
        # Windows CRLF and GTK offsets are interpreted by the control itself.
        low, high = 0, self.GetLastPosition()
        while low < high:
            middle = (low + high) // 2
            if len(self.GetRange(0, middle)) < len(prefix):
                low = middle + 1
            else:
                high = middle
        while low < self.GetLastPosition() and self.GetRange(0, low) != prefix:
            low += 1
        return low

    def line_bounds(self) -> tuple[int, int]:
        lines = self.GetValue().split("\n")
        line = self.line_number()
        start = sum(len(text) + 1 for text in lines[:line])
        return self.position_from_offset(start), self.position_from_offset(start + len(lines[line]))

    def current_command(self) -> str:
        return self.text_range(*self.line_bounds())

    def replace_command(self, text: str, *, caret: int | None = None) -> None:
        """Replace one logical line; caret is a Python offset within the new text."""
        self.SetSelection(*self.line_bounds())
        if caret is None:
            self.WriteText(text)
        else:
            # Let native insertion compute the position, even for non-BMP text.
            self.WriteText(text[:caret])
            position = self.GetInsertionPoint()
            if text[caret:]:
                self.WriteText(text[caret:])
            self.SetInsertionPoint(position)

    def finish_submission(self) -> None:
        self.document.sync(self.GetValue())
        suffix = self.document.complete(self.line_number())
        self._appending = True
        try:
            if suffix:
                self.AppendText(suffix)
            self.move_to_tail()
        finally:
            self._appending = False

    def move_to_tail(self) -> None:
        end = self.GetLastPosition()
        self.SetSelection(end, end)
        self.ShowPosition(end)

    def _on_edit(self, event: wx.CommandEvent) -> None:
        if not self._appending:
            self.document.sync(self.GetValue(), edited=True)
        event.Skip()

    def _on_char(self, event: wx.KeyEvent) -> None:
        # A way to create multiple unsent lines without executing them. Ordinary
        # Enter stays on EVT_TEXT_ENTER, like normal input (including IME handling).
        if (event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and event.ShiftDown() and not event.ControlDown() and not event.AltDown()):
            self.WriteText("\n")
            return
        event.Skip()
