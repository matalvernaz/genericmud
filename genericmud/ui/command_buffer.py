"""Native document editing for the optional VIPMud-style command input mode."""

from __future__ import annotations

import sys
from collections.abc import Callable

import wx

from genericmud.completion import CompletionCycler
from genericmud.ui.command_document import (
    down_creates_tail,
    insert_blank_line,
    line_bounds,
    submit_command,
)


class CommandEntryCtrl(wx.TextCtrl):
    def __init__(self, parent: wx.Window):
        super().__init__(parent, style=wx.TE_MULTILINE | wx.TE_DONTWRAP | wx.TE_PROCESS_ENTER)
        self.SetMinSize((-1, self.GetCharHeight() * 5 + 8))
        self.execute: Callable[[bool], None] | None = None
        self._recall = CompletionCycler()
        self._recall_tail = ""
        self._recall_selection = (-1, -1)
        self._recalling = False
        self.Bind(wx.EVT_TEXT, self._on_edit)
        self.Bind(wx.EVT_CHAR, self._on_char)

    def text_range(self, start: int, end: int) -> str:
        value = self.GetValue()
        if sys.platform == "darwin" and self.GetLastPosition() != len(value):
            # Cocoa selections use UTF-16; wx 3.2.9 GetRange slices code points.
            return value.encode("utf-16-le")[2 * start:2 * end].decode(
                "utf-16-le", errors="replace",
            )
        return self.GetRange(start, end).replace("\r\n", "\n")

    def offset_from_position(self, position: int) -> int:
        return len(self.text_range(0, position))

    def position_from_offset(self, offset: int) -> int:
        value = self.GetValue()
        prefix = value[:offset]
        if sys.platform == "darwin" and self.GetLastPosition() != len(value):
            return len(prefix.encode("utf-16-le")) // 2
        low, high = 0, self.GetLastPosition()
        while low < high:
            middle = (low + high) // 2
            if len(self.GetRange(0, middle).replace("\r\n", "\n")) < len(prefix):
                low = middle + 1
            else:
                high = middle
        while low < self.GetLastPosition() and self.text_range(0, low) != prefix:
            low += 1
        if self.text_range(0, low) != prefix:
            raise ValueError("The editor could not locate the command's text position")
        return low

    def line_number(self) -> int:
        return self.text_range(0, self.GetSelection()[0]).count("\n")

    def line_bounds(self) -> tuple[int, int]:
        start, end = line_bounds(
            self.GetValue(), self.offset_from_position(self.GetSelection()[0]),
        )
        return self.position_from_offset(start), self.position_from_offset(end)

    def current_command(self) -> str:
        return self.text_range(*self.line_bounds())

    def selected_text(self) -> str:
        return self.text_range(*self.GetSelection())

    def select_command(self, first: int, last: int) -> None:
        text = self.GetValue()
        start, end = line_bounds(text, self.offset_from_position(self.GetSelection()[0]))
        first_offset = min(start + max(first - 1, 0), end)
        last_offset = end if last < 0 else min(max(start + last, first_offset), end)
        self.SetSelection(
            self.position_from_offset(first_offset), self.position_from_offset(last_offset),
        )

    def replace_command(self, text: str, *, caret: int | None = None) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.SetSelection(*self.line_bounds())
        if caret is None:
            self.WriteText(text)
        else:
            self.WriteText(text[:caret])
            position = self.GetInsertionPoint()
            if text[caret:]:
                self.WriteText(text[caret:])
            self.SetInsertionPoint(position)

    def paste_command(self, text: str) -> str:
        start, end = self.GetSelection()
        if "\n" in self.selected_text():
            raise ValueError("Command insertion needs a selection within one line")
        replaced = self.selected_text()
        self.Replace(start, end, text.replace("\r\n", "\n").replace("\r", "\n"))
        return replaced

    def _apply(self, value: str, caret: int) -> None:
        """Replace only changed text; do not reset the native document on each edit."""
        old = self.GetValue()
        if value != old:
            first = 0
            limit = min(len(old), len(value))
            while first < limit and old[first] == value[first]:
                first += 1
            old_end, new_end = len(old), len(value)
            while old_end > first and new_end > first and old[old_end - 1] == value[new_end - 1]:
                old_end -= 1
                new_end -= 1
            self.Replace(
                self.position_from_offset(first), self.position_from_offset(old_end),
                value[first:new_end],
            )
        position = self.position_from_offset(caret)
        self.SetSelection(position, position)
        self.ShowPosition(position)

    def finish_submission(self) -> None:
        value = submit_command(
            self.GetValue(), self.offset_from_position(self.GetSelection()[0]),
        )
        self._apply(value, len(value))

    def create_tail(self) -> bool:
        value = self.GetValue()
        if not down_creates_tail(value, self.offset_from_position(self.GetSelection()[0])):
            return False
        self.AppendText("\n")
        self.move_to_tail()
        return True

    def clear_line(self) -> None:
        value, caret = insert_blank_line(
            self.GetValue(), self.offset_from_position(self.GetSelection()[0]),
        )
        self._apply(value, caret)

    def protects_backspace(self) -> bool:
        start, end = self.GetSelection()
        return start == end and self.text_range(0, start).endswith("\n")

    def move_to_tail(self) -> None:
        end = self.GetLastPosition()
        self.SetSelection(end, end)
        self.ShowPosition(end)
        self.reset_recall()

    def reset_recall(self) -> None:
        self._recall.reset()

    def recall_command(self) -> str:
        """Complete at the caret from earlier editable rows, including drafts."""
        if self.GetSelection() != self._recall_selection:
            self.reset_recall()
        if not self._recall.active:
            value = self.GetValue()
            caret = self.offset_from_position(self.GetInsertionPoint())
            start, end = line_bounds(value, caret)
            prefix = value[start:caret]
            if not prefix:
                return "Type the beginning of a command before pressing Control R"
            candidates = [
                prefix + line[len(prefix):]
                for line in reversed(value[:start].split("\n"))
                if line[:len(prefix)].upper() == prefix.upper() and len(line) > len(prefix)
            ]
            if not candidates:
                return "No earlier command matches " + prefix
            self._recall.begin(prefix, candidates)
            self._recall_tail = value[caret:end]
        word = self._recall.next()
        self._recalling = True
        try:
            self.replace_command(word + self._recall_tail, caret=len(word))
            self._recall_selection = self.GetSelection()
        finally:
            self._recalling = False
        # Ctrl+R is completion, like Ctrl+Space, rather than a screen-reader line
        # movement gesture. The caller announces the completed command once.
        return self.current_command()

    def _on_edit(self, event: wx.CommandEvent) -> None:
        if not self._recalling:
            self.reset_recall()
        event.Skip()

    def _on_char(self, event: wx.KeyEvent) -> None:
        code = event.GetKeyCode()
        plain = not (event.ControlDown() or event.AltDown() or event.ShiftDown())
        if code == wx.WXK_BACK and plain and self.protects_backspace():
            return  # also guard ports that deliver CHAR after a handled KEY_DOWN
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if event.ControlDown() or event.AltDown():
                return  # a mode/pack shortcut must not also submit a command
            if event.ShiftDown():
                if self.execute is not None:
                    self.execute(False)
                return
        event.Skip()  # ordinary Enter uses native TEXT_ENTER, including IME handling
