"""The output box: adding the MUD's text without moving the reader, Find, and following links.

Also the MXP menus and copying all or part of a line.
"""

from __future__ import annotations

import wx

from genericmud.bridge import protocol
from genericmud.ui.clipboard import _set_clipboard_text
from genericmud.ui.command_entry import CommandEntryCtrl
from genericmud.ui.keyboard import _key_combo
from genericmud.ui.output_dialogs import ChooseDialog, CopyLineDialog, FindDialog
from genericmud.ui.scrollback import anchor_after_append, find_line

_OUTPUT_CAP_LINES = 5000  # keep the native control bounded so NVDA/UIA stays responsive
_FLUSH_INTERVAL_MS = 50  # batch output appends during floods


class OutputBoxMixin:
    """The output box, for SessionPanel (session_panel.py).

    Adds the MUD's text in batches without moving a reader's caret, handles Find, F3 and Enter on
    a link, and opens MXP menus and the copy box. Uses these attributes from
    SessionPanel.__init__: _alive, _find, _find_anchored, _find_direction, _find_restart,
    _flush_scheduled, _input_masked, _keep_caret_on_focus, _loop, _pending, _prefs, app, input and
    output. Editable sessions use _entry_from_output when focus returns to Command.
    Hands keys to command_box's _dispatch_bound_combo and password changes to
    command_box's _set_input_masked.
    """

    def _fill_command(self, text: str) -> None:
        """Put a link's half-written command in the box, ready to finish or abandon.

        Replaces whatever was there, and focuses the box: the player asked for this by
        following the link, and an unfinished command nobody is looking at is the state that
        sends half a sentence by accident.
        """
        if isinstance(self.input, CommandEntryCtrl):
            if self.output.HasFocus():
                self.input.move_to_tail()
            self.input.replace_command(text)
        else:
            self.input.SetValue(text)
            self.input.SetInsertionPointEnd()
        self.input.SetFocus()


    def _choose_from_menu(self, message: dict) -> None:
        """Ask which of a menu's commands was meant, and send only the one picked.

        The answer goes back through the engine, which decides what a command from the MUD may
        do; the window never sends anything itself. Closing the dialog, or Escape, sends
        nothing -- a menu is a question, and "never mind" is a fair answer to it.
        """
        choices: list[tuple[str, str]] = []
        for item in message.get("choices", []):
            if isinstance(item, (list, tuple)) and len(item) >= 1:
                command = str(item[0])
                caption = str(item[1]) if len(item) > 1 else ""
                if command:
                    choices.append((command, caption))
        if not choices or self.app is None:
            return
        # Back to wherever the player followed the link from: the command box after Alt+Enter,
        # or the output box after Enter on a line, where they were part way through reading.
        focused = wx.Window.FindFocus()
        dialog = ChooseDialog(self, str(message.get("text", "")), choices)
        try:
            picked = dialog.chosen() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()
        (focused or self.input).SetFocus()
        if picked is None:
            return
        command, caption = picked
        self._loop.call_soon_threadsafe(
            self.app.on_ws_message,
            {"type": protocol.MENU_CHOICE, "text": caption or command, "command": command},
        )

    # --- UI updates (main thread) ---

    def _handle_message(self, message: dict) -> None:
        if not self._alive:
            return
        kind = message.get("type")
        if kind in (protocol.LINE, protocol.ECHO):
            if message.get("gagged") and not message.get("display_when_gagged"):
                return
            self._pending.append(message["text"])
            if not self._flush_scheduled:
                self._flush_scheduled = True
                wx.CallLater(_FLUSH_INTERVAL_MS, self._flush_output)
        elif kind == protocol.FIND_RESULT:
            self._sync_find_caret(message)
        elif kind == protocol.INPUT_MASKED:
            self._set_input_masked(bool(message.get("masked")))
        elif kind == protocol.INPUT_TEXT:
            self._fill_command(str(message.get("text", "")))
        elif kind == protocol.MENU:
            self._choose_from_menu(message)
        elif kind == protocol.COPY_LINE:
            self._offer_line_for_copying(str(message.get("text", "")))
        # Sound needs nothing here: this window's engine plays it through its own sound backend.
        # The gauge status messages have nothing to show them in the native window.

    def _offer_line_for_copying(self, text: str) -> None:
        """Ctrl+Shift+C: the reviewed line in a box, to copy all of it or any part.

        Focus goes back to wherever the key was pressed, the command box or the output, and
        the copy is confirmed out loud, since nothing on screen changes to say it happened.
        """
        if not text:
            return
        focused = wx.Window.FindFocus()
        frame = wx.GetTopLevelParent(self)
        dialog = CopyLineDialog(frame, text)
        try:
            copied = dialog.ShowModal() == wx.ID_OK
            chosen = dialog.chosen()
        finally:
            dialog.Destroy()
        if focused is not None:
            focused.SetFocus()
        if not copied or not chosen:
            return
        message = "Copied" if _set_clipboard_text(chosen) else "The clipboard is busy; try again."
        announce = getattr(frame, "announce", None)
        if announce is not None:
            announce(message)

    def _flush_output(self) -> None:
        self._flush_scheduled = False
        if not self._alive or not self._pending:
            return
        # Someone who has tabbed into the output is arrowing back through it, and
        # AppendText would drag their caret to the end on every arriving line. Pin it
        # instead: landing at the bottom is what focusing the control does (see
        # _on_output_focus), not what incoming text does. The Find dialog counts as
        # "reading" too: it holds the focus, but _keep_caret_on_focus promises the
        # search will run from the reader's position, so that position must survive
        # any text arriving while the dialog is up.
        preserve = self.output.HasFocus() or self._keep_caret_on_focus
        anchor = self.output.GetInsertionPoint() if preserve else None
        self.output.AppendText("\n".join(self._pending) + "\n")
        self._pending.clear()
        removed = self._trim_output()
        if anchor is not None:
            target = anchor_after_append(anchor, removed, self.output.GetLastPosition())
            self.output.SetInsertionPoint(target)
            self.output.ShowPosition(target)

    def _trim_output(self) -> int:
        """Drop the oldest lines past the cap; returns the character count removed.

        The count matters to a caller restoring a review caret: removing from the front
        shifts every remaining offset down by exactly this much.
        """
        excess = self.output.GetNumberOfLines() - _OUTPUT_CAP_LINES
        if excess <= 0:
            return 0
        end = self.output.XYToPosition(0, excess)
        if end <= 0:
            return 0
        self.output.Remove(0, end)
        return end

    def _on_output_focus(self, event: wx.FocusEvent) -> None:
        """Tabbing in from the command box lands on the newest line.

        This is the only thing that moves a reader to the bottom; arriving MUD text
        deliberately does not. Done synchronously rather than via CallAfter so the caret
        is already at the end when the screen reader reads the newly focused control.
        """
        self._entry_from_output = True
        if isinstance(self.input, CommandEntryCtrl):
            self.input.reset_output_character()
        if self._keep_caret_on_focus:
            # Focus is coming back from the Find dialog, so the search must run from where
            # the reader actually was. Jumping to the bottom here would throw that away.
            self._keep_caret_on_focus = False
        else:
            # Which line the search goes on from is read off this caret when it is asked for,
            # so nothing has to be remembered here (see _caret_anchor).
            self.output.SetInsertionPointEnd()
            self.output.ShowPosition(self.output.GetLastPosition())
        event.Skip()


    # --- find (output only) ---

    def _on_output_key(self, event: wx.KeyEvent) -> None:
        """Find keys and link following, live only while the output has focus."""
        code = event.GetKeyCode()
        if (getattr(self, "_entry_input", None) is not None and code in
                (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and event.ControlDown()
                and not event.AltDown() and not event.ShiftDown()):
            if not self._dispatch_entry_shortcut("ctrl+enter"):
                if isinstance(self.input, CommandEntryCtrl):
                    self.input.move_to_tail()
                self.input.SetFocus()
            return
        if (
            code == ord("F")
            and event.ControlDown()
            and not event.AltDown()
            and not event.ShiftDown()
        ):
            self._open_find()
            return
        if code == wx.WXK_F3 and not event.ControlDown() and not event.AltDown():
            self._find_again(reverse=event.ShiftDown())
            return
        if (
            code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
            and not event.ControlDown()
            and not event.AltDown()
            and not event.ShiftDown()
        ):
            # Reading the output: there is nothing to send from here, so Enter follows the
            # link on the line the reader is on -- the keyboard form of clicking the words.
            # The engine is told which line that is, by its text and its distance from the
            # end, because its own review cursor knows nothing of this caret: following the
            # newest line's link instead would run a command the reader never heard.
            if self.app is not None:
                if self._pending:
                    self._flush_output()  # the caret's line may still be in the append batch
                lines = self.output.GetValue().split("\n")
                ok, column, caret_line = self.output.PositionToXY(
                    self.output.GetInsertionPoint()
                )
                if not ok or not 0 <= caret_line < len(lines):
                    caret_line = len(lines) - 1
                    column = -1
                # The column goes too: a line can carry several links, and the one the reader
                # means is the one under the caret. Without it the engine can only take the
                # line's only link, which is why Enter did nothing on a line with two.
                self._loop.call_soon_threadsafe(
                    self.app.on_ws_message,
                    {
                        "type": protocol.LINK_AT,
                        "text": lines[caret_line],
                        "from_end": len(lines) - 1 - caret_line,
                        "column": column,
                    },
                )
            return
        # Route bound keymap combos here too, so the panic keys and other macros work while
        # the reader is in the output. Plain arrows/typing return no combo (see _key_combo),
        # so the screen reader's own line/char review of the output is untouched.
        combo = _key_combo(event, allow_numpad=not self._prefs.numpad_compass)
        if self._dispatch_bound_combo(combo):
            return
        event.Skip()


    def _open_find(self) -> None:
        # Closing the modal dialog briefly restores focus to output. Preserve its
        # caret until the result arrives instead of jumping to the bottom on focus.
        self._keep_caret_on_focus = True
        try:
            dialog = FindDialog(self, self._find)
            try:
                state = dialog.state() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()
            if state is None or not state.term:
                return
            self._find = state
            self.output.SetFocus()
        finally:
            self._keep_caret_on_focus = False
        self._run_find(self._find.forward, restart=True)

    def _find_again(self, *, reverse: bool) -> None:
        """F3 repeats the last search; Shift+F3 repeats it the other way for one hop."""
        if not self._find.term:
            self._open_find()
            return
        forward = not self._find.forward if reverse else self._find.forward
        self._run_find(forward)

    def _caret_anchor(self) -> tuple[str, int, bool]:
        """Which output line the reader is on: its text, its distance from the end, and at bottom.

        The engine cannot see this caret, so every search is sent with it: a search that has to
        guess is what skipped the matches between the caret and the last hit, and sent a fresh
        Find back to the newest match the reader had already read.

        Standing below the newest line of text -- where going back to the output puts you -- is
        not a line at all, and is said as such: those searches start at the newest line itself,
        which is what carrying on from down there means.
        """
        if self._pending:
            self._flush_output()  # the line under the caret may still be in the append batch
        ok, _column, index = self.output.PositionToXY(self.output.GetInsertionPoint())
        # The text ends with a newline, and the control counts the empty line after it: the
        # last line by number is not a line of text, and the caret sitting there means "below
        # everything I have", not "on the newest line".
        lines = self.output.GetNumberOfLines() - 1
        if not ok or lines <= 0 or index >= lines:
            return "", 0, True
        return self.output.GetLineText(index), lines - 1 - index, False

    def _run_find(self, forward: bool, *, restart: bool = False) -> None:
        if self.app is None:
            return
        self._find_direction = forward  # _sync_find_caret needs the direction actually used
        text, from_end, at_bottom = self._caret_anchor()
        self._find_anchored = bool(text)
        # A search that starts below the newest line lands on that line inclusively.
        self._find_restart = restart or at_bottom
        self._loop.call_soon_threadsafe(
            self.app.on_ws_message,
            protocol.find(
                self._find.term,
                forward=forward,
                case_sensitive=self._find.case_sensitive,
                restart=restart,
                from_bottom=at_bottom,
                text=text,
                from_end=from_end,
            ),
        )

    def _sync_find_caret(self, message: dict) -> None:
        """Put the output caret on the line the engine's scrollback search matched.

        The engine searches the whole buffer; this control only holds the last
        _OUTPUT_CAP_LINES. A match older than that leaves the caret alone and the spoken
        line is all the reader gets, which beats moving them somewhere wrong.

        The mapping runs in whole lines, converted at the edges with PositionToXY and
        XYToPosition: insertion-point units and GetValue() string offsets disagree on
        Windows (the native control counts a line break as two characters, GetValue
        renders one), so a raw string offset lands the caret above the matched line.
        """
        restart = self._find_restart
        self._find_restart = False
        # A search anchored on the reader's line can land on any occurrence of that line's
        # text, so an edge is only the answer when nothing said where the search began.
        from_edge = restart and not self._find_anchored
        if not message.get("found"):
            return  # still at the bottom, if that's where it searched from
        if self._pending:
            self._flush_output()  # the matched line may still be in the append batch
        lines = self.output.GetValue().split("\n")
        ok, _col, caret_line = self.output.PositionToXY(self.output.GetInsertionPoint())
        if not ok:
            caret_line = len(lines) - 1
        matched_line = message.get("text", "")
        line = find_line(
            lines,
            matched_line,
            caret_line,
            forward=self._find_direction,
            from_edge=from_edge,
        )
        if line is None and not restart:
            # The caret drifted from the engine's cursor (the reader arrowed away, or
            # earlier hits were older than the control holds). Take the edge occurrence
            # rather than leaving the caret while the matched line is on screen.
            line = find_line(
                lines,
                matched_line,
                caret_line,
                forward=self._find_direction,
                from_edge=True,
            )
        if line is None:
            return
        offset = self.output.XYToPosition(0, line)
        if offset == -1:
            return
        self.output.SetInsertionPoint(offset)
        self.output.ShowPosition(offset)

    def _on_output_char(self, event: wx.KeyEvent) -> None:
        unicode_key = event.GetUnicodeKey()
        modified = event.ControlDown() or event.AltDown()
        if unicode_key >= 32 and unicode_key != 127 and not modified:
            entry = isinstance(self.input, CommandEntryCtrl)
            character = self.input.character_from_output(unicode_key) if entry else chr(unicode_key)
            if character is None:
                return  # wait for the low surrogate without moving focus
            selection = self.input.GetSelection()
            if entry:
                self.input.move_to_tail()
            self.input.SetFocus()
            if entry:
                self.input.move_to_tail()
            else:
                # Focusing may select all text; Standard keeps the user's edit range.
                self.input.SetSelection(*selection)
            self.input.WriteText(character)
            return
        if isinstance(self.input, CommandEntryCtrl):
            self.input.reset_output_character()
        event.Skip()  # arrows etc. -> native screen-reader review of the output
