"""The command box: sending what was typed, history, word completion and bound keys.

Also the numpad compass, which walks with the number pad.
"""

from __future__ import annotations

import wx

from genericmud.app import CLIENT_PREFIX
from genericmud.ui.command_entry import CommandEntryCtrl
from genericmud.ui.keyboard import (
    _COMPLETE_BACKWARD,
    _COMPLETE_FORWARD,
    _PASSTHROUGH_COMBOS,
    _key_combo,
)

# Numpad compass (VIPMud/MUDBall convention): digits walk, 5/0 look, . scans,
# minus/plus go up/down. Single-letter directions so the breadcrumb trail records them.
# NVDA desktop-layout users keep the numpad for object review -- the View menu
# toggle turns this off for them.
_NUMPAD_COMPASS = {
    wx.WXK_NUMPAD8: "n", wx.WXK_NUMPAD2: "s", wx.WXK_NUMPAD4: "w", wx.WXK_NUMPAD6: "e",
    wx.WXK_NUMPAD7: "nw", wx.WXK_NUMPAD9: "ne", wx.WXK_NUMPAD1: "sw", wx.WXK_NUMPAD3: "se",
    wx.WXK_NUMPAD5: "look", wx.WXK_NUMPAD0: "look",
    wx.WXK_NUMPAD_DECIMAL: "scan", wx.WXK_NUMPAD_SUBTRACT: "u", wx.WXK_NUMPAD_ADD: "d",
}
# How many sent commands can be recalled. Generous, because a pasted block fills it quickly.
_HISTORY_LIMIT = 2000


class CommandBoxMixin:
    """The command box, for SessionPanel (session_panel.py).

    Sends what was typed, walks the history, completes words and runs bound keys. Uses these
    attributes from SessionPanel.__init__: _completion, _completion_start, _completion_tail,
    _connect_finished, _hist_index, _history, _history_limit, _input_masked, _keymap, _loop,
    _prefs, _voice, app and input. Editable sessions also use _entry_input and
    _entry_from_output; Standard follows the original compose/history path. Asks
    session_connection whether the tab is connected, and to reconnect.
    """

    def _remember(self, lines: list[str]) -> None:
        """Add sent commands to the history, keeping the newest few thousand at most.

        A pasted block adds one entry per line, so without a limit a long file pasted twice
        would sit in memory for the rest of the session and make Up walk all of it.
        """
        self._history.extend(lines)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        self._hist_index = len(self._history)

    def _start_new_command_line(self) -> None:
        """Begin another command on its own line, where Enter will send it with the rest.

        Shift+Enter, as in a chat box -- with one difference worth knowing, because a MUD reads
        a newline as Enter itself: a line break can separate two commands but can never be part
        of one. A long `say` wraps on screen without any of this.
        """
        self.input.WriteText("\n")

    def _on_send(self, _event: wx.CommandEvent | None) -> None:
        """Send what is in the box: one command per line, then clear it.

        A pasted block is several commands, and they go out one at a time, in order, each down
        the same input path a typed line takes -- so aliases apply to each, and a world with
        stacking off still gets several commands from one paste.

        One line goes out exactly as typed, an empty one included: a blank Enter is how a pager
        ("press return to continue") is answered, and with autoretype on the engine turns it
        into the last command again. Spaces are kept as well, for in-game editors and
        passwords. Only a block is split, and its blank lines are dropped; a block holding
        nothing but blank lines is one blank Enter.
        """
        if isinstance(self.input, CommandEntryCtrl):
            self._send_entry(True)
            return
        value = self.input.GetValue()
        if "\n" in value or "\r" in value:
            lines = [line for line in value.splitlines() if line.strip()] or [""]
        else:
            lines = [value]
        # genericMud's own commands ("/to", "/alias", "/map", ...) don't need this tab's
        # connection: "/to" goes out through another session. Asking first left no way to
        # run them here at all, since Cancel keeps the line and Enter asks again. An unknown
        # "/verb" a MUD owns is dropped unasked, as it was before the question existed.
        offline_ok = all(line.startswith(CLIENT_PREFIX) for line in lines)
        if self._connect_finished and not self.is_connected() and not offline_ok:
            if self.is_connecting():
                # Already trying again by itself: "Reconnect now?" could only be answered with
                # "Still connecting", so that is said straight away, and the text stays put.
                self.reconnect()
                return
            # Nothing typed into a dead session reaches the MUD -- the transport drops it and
            # the client says nothing, which looks exactly like a MUD ignoring the command.
            # Ask instead, and leave the text in the box so it isn't lost while deciding.
            self._prompt_reconnect()
            return
        self.input.SetValue("")
        self.input.SetInsertionPointEnd()
        if not self._input_masked:
            # Each line is its own command, so each is its own thing to recall.
            self._remember([line for line in lines if line])
        else:
            self._hist_index = len(self._history)
        if self.app is not None:
            for line in lines:
                self._loop.call_soon_threadsafe(
                    self.app.on_ws_message, {"type": "input", "text": line}
                )


    def _on_input_key(self, event: wx.KeyEvent) -> None:
        code = event.GetKeyCode()
        entry = isinstance(getattr(self, "input", None), CommandEntryCtrl)
        secret_entry = getattr(self, "_entry_input", None) is not None and self._input_masked
        if entry and code in (0, wx.WXK_SHIFT, wx.WXK_CONTROL, wx.WXK_ALT, wx.WXK_RAW_CONTROL):
            # Modifier transitions do not end a completion run. Cocoa reports
            # flagsChanged as KEY_DOWN/0 even when releasing Command, while
            # other ports report the modifier key. The next editing key resets it.
            event.Skip()
            return
        combo = _key_combo(event, allow_numpad=not self._prefs.numpad_compass)
        if ((entry or secret_entry) and combo in ("escape", "ctrl+r", "ctrl+enter")
                and self._dispatch_entry_shortcut(combo)):
            return
        if (secret_entry and code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and event.ControlDown() and not event.AltDown() and not event.ShiftDown()):
            self.input.SetFocus()
            return
        if entry:
            plain = not (event.ControlDown() or event.AltDown() or event.ShiftDown())
            combo = _key_combo(event, allow_numpad=not self._prefs.numpad_compass)
            if combo != "ctrl+r":
                self.input.reset_recall()
            if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not (
                event.ControlDown() or event.AltDown()
            ):
                event.Skip()  # native CHAR/TEXT_ENTER handling, including IME confirmation
                return
            if plain and code == wx.WXK_DOWN and self.input.create_tail():
                return
            if plain and code == wx.WXK_BACK and self.input.protects_backspace():
                return
            if plain and code == wx.WXK_ESCAPE:
                self.input.clear_line()
                return
            if combo == "ctrl+r":
                feedback = self.input.recall_command()
                if feedback:
                    self._loop.call_soon_threadsafe(self._speak_system, feedback)
                return
            if combo == "ctrl+enter":
                self.input.move_to_tail()
                self.input.SetFocus()
                return
        if not entry and code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            modified = event.ControlDown() or event.AltDown()
            if event.ShiftDown() and not modified and not secret_entry:
                self._start_new_command_line()
                return
            if not modified:
                # A multi-line field inserts a newline here. Sending is what the box is for.
                self._on_send(None)
                return
        combo = _key_combo(event, allow_numpad=not self._prefs.numpad_compass)
        if combo in (_COMPLETE_FORWARD, _COMPLETE_BACKWARD) and not secret_entry:
            self._cycle_completion(backward=(combo == _COMPLETE_BACKWARD))
            return
        self._completion.reset()  # any other key ends a completion run
        plain = not (event.ControlDown() or event.AltDown() or event.ShiftDown())
        if plain and code in (wx.WXK_UP, wx.WXK_DOWN):
            if not entry and not secret_entry and self._history_step(
                -1 if code == wx.WXK_UP else 1
            ):
                return
            event.Skip()  # not history: leave the arrows to the field and the reader
            return
        if plain and self._prefs.numpad_compass and code in _NUMPAD_COMPASS:
            self._send_command(_NUMPAD_COMPASS[code])
            return
        if self._dispatch_bound_combo(combo):
            return
        event.Skip()  # passthrough/unbound combos -> default handling (Alt+F4 -> EVT_CLOSE)


    def _dispatch_bound_combo(self, combo: str | None) -> bool:
        """Send a bound keymap/pack combo to the engine; return whether it was consumed.

        Only a combo that's actually bound is consumed -- everything else falls through to
        wx (swallowing unbound combos is what killed Alt+F menu access keys and the Ctrl
        accelerators for keyboard-only users). Shared by the command box and the output box
        so the panic keys (F11 stops speech, Shift+F11 stops sound) and every other
        bound action work even while the reader is focused in the output.
        """
        if combo and combo not in _PASSTHROUGH_COMBOS:
            # Tab switching is the window's, not this world's: the engine serves one MUD and
            # has no idea which tabs are open. The binding still comes from the keymap, so
            # these keys are rebindable like every other one.
            action = self._keymap.get(combo)
            # A soundpack or the player's own hotkey on the same key wins in this world:
            # Erion's channel history, for one, reads messages on Alt+1 to Alt+0.
            claimed = self.app is not None and self.app.engine.has_key(combo)
            if action is not None and action.startswith("tab:") and not claimed:
                frame = wx.GetTopLevelParent(self)
                handle = getattr(frame, "handle_tab_action", None)
                if handle is not None and handle(action):
                    return True
        if (
            combo
            and combo not in _PASSTHROUGH_COMBOS
            and self.app is not None
            and (combo in self._keymap or self.app.engine.has_key(combo))
        ):
            self._loop.call_soon_threadsafe(self.app.on_ws_message, {"type": "key", "key": combo})
            return True
        return False

    def _send_command(self, command: str) -> None:
        """Send one command through the engine's input path (aliases + breadcrumbs apply)."""
        if self.app is not None:
            self._loop.call_soon_threadsafe(
                self.app.on_ws_message, {"type": "input", "text": command}
            )

    def _cycle_completion(self, backward: bool) -> None:
        """Complete the word at the caret from words seen in recent output, cycling.

        The first press starts a run from the current prefix; repeated presses swap
        in the next/previous candidate. Any other key ends the run.
        """
        if self.app is None:
            return
        entry = isinstance(self.input, CommandEntryCtrl)
        if not self._completion.active:
            text = self.input.current_command() if entry else self.input.GetValue()
            caret = self.input.GetInsertionPoint()
            if entry:
                start, end = self.input.line_bounds()
                if not start <= caret <= end:
                    return
                caret = len(self.input.text_range(start, caret))
            head = text[:caret]
            start = max(head.rfind(" "), head.rfind(";")) + 1
            prefix = head[start:caret]
            if not prefix:
                self._loop.call_soon_threadsafe(self._speak_system, "nothing to complete")
                return
            candidates = self.app.word_index.complete(prefix)
            if not candidates:
                self._loop.call_soon_threadsafe(self._speak_system, f"no match for {prefix}")
                return
            self._completion.begin(prefix, candidates)
            self._completion_start = start
            # The caret may sit inside a word ("get swo|xx"); replace the WHOLE word, not
            # graft the completion onto the leftover ("get swordxx" -- a command never heard).
            rest = text[caret:]
            boundary = min((i for i in (rest.find(" "), rest.find(";")) if i != -1),
                           default=len(rest))
            self._completion_tail = rest[boundary:]
        word = self._completion.prev() if backward else self._completion.next()
        if word is None:
            return
        text = self.input.current_command() if entry else self.input.GetValue()
        value = text[: self._completion_start] + word + self._completion_tail
        caret = self._completion_start + len(word)
        if entry:
            self.input.replace_command(value, caret=caret)
        else:
            self.input.SetValue(value)
            self.input.SetInsertionPoint(caret)
        # Tab is not a caret-movement gesture, so the screen reader announces nothing here
        # (unlike history recall, where it reads the line itself) -- speak the word ourselves.
        self._loop.call_soon_threadsafe(self._speak_system, word)


    def _history_step(self, direction: int) -> bool:
        """Move one entry through command history, or leave the key to the field.

        History moves only from an empty box or from a message that is itself in history, and
        only from the caret's first or last position. Anything else is a draft the player is
        still writing: an arrow that replaced it would be silent data loss for someone who
        cannot glance at the box, so the key is left alone and moves the caret instead.

        Down from the newest entry empties the box, which is where Up starts again. Nothing is
        lost by that: what was there is a history entry, one arrow away.

        Deliberately silent -- Up and Down are caret-movement gestures, so the screen reader
        runs its own line-reading script whatever this handler does with the key. Speaking as
        well raced that read: the user heard the empty field and then our text on every recall.
        """
        value = self.input.GetValue()
        caret = self.input.GetInsertionPoint()
        if caret not in (0, len(value)):
            return False  # the caret is inside the message: this is editing, not history
        if not value:
            if direction > 0 or not self._history:
                return False  # nothing newer than an empty field, and nothing to go back to
            self._show_history_entry(len(self._history) - 1)
            return True
        if value not in self._history:
            return False  # a draft that matches nothing sent: leave it where it is
        index = self._entry_index(value)
        target = index + direction
        if target < 0:
            return False  # the oldest entry: the arrow has nowhere older to go
        if target >= len(self._history):
            self.input.SetValue("")  # past the newest: an empty box, as it was before the first Up
            self.input.SetInsertionPointEnd()
            self._hist_index = len(self._history)
            return True
        self._show_history_entry(target)
        return True

    def _entry_index(self, value: str) -> int:
        """Which history entry the box is showing.

        The stored position while it still agrees with the text keeps repeated presses walking
        the same way; otherwise the newest matching entry is taken, because a recall lands on
        the entry it recalled.
        """
        if 0 <= self._hist_index < len(self._history):
            if self._history[self._hist_index] == value:
                return self._hist_index
        return max(number for number, entry in enumerate(self._history) if entry == value)

    def _show_history_entry(self, index: int) -> None:
        self._hist_index = index
        self.input.SetValue(self._history[index])
        self.input.SetInsertionPointEnd()

    def _speak_system(self, text: str) -> None:  # loop thread
        if self._voice is not None:
            self._voice.speak(text, channel="system", interrupt=True)

    def _send_entry(self, append: bool) -> None:
        entry = self.input
        if not isinstance(entry, CommandEntryCtrl):
            return
        self._completion.reset()
        if self.app is None:
            self._loop.call_soon_threadsafe(
                self._speak_system, "Session is still starting; command kept",
            )
            return
        text = (entry.selected_text() if not append and entry.GetSelection()[0] !=
                entry.GetSelection()[1] else entry.current_command())
        if not append and not text.strip(" "):
            return
        # Multiline execution is possible only for an explicit Shift+Enter selection.
        # Use the same blank-line handling as Standard's deliberate block submission.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = ([line for line in text.split("\n") if line.strip()] or [""]
                 if "\n" in text else [text])
        offline_ok = all(line.startswith(CLIENT_PREFIX) for line in lines)
        if self._connect_finished and not self.is_connected() and not offline_ok:
            if self.is_connecting():
                self.reconnect()
            else:
                self._prompt_reconnect()
            return  # no document/selection changes while deciding whether to reconnect
        if append:
            entry.finish_submission()
        self._remember([line for line in lines if line])
        for line in lines:
            self._loop.call_soon_threadsafe(self.app.on_ws_message, {"type": "input", "text": line})


    def _dispatch_entry_shortcut(self, combo: str) -> bool:
        """An explicit pack binding wins over this mode's editing shortcuts.

        Dispatch on the loop thread directly: the shared engine keymap would
        otherwise interpret Ctrl+Enter as Standard's autoretype toggle.
        """
        engine = getattr(self.app, "engine", None)
        if engine is None or not engine.has_key(combo):
            return False
        self._loop.call_soon_threadsafe(engine.press_key, combo)
        return True


    def _set_input_masked(self, masked: bool) -> None:
        self._input_masked = masked
        if getattr(self, "_entry_input", None) is None:
            return
        if masked == (self.input is not self._entry_input):
            return
        focused = self.input.HasFocus()
        selection = self._entry_input.GetSelection()
        self._completion.reset()
        self._entry_input.reset_recall()
        sizer = self.GetSizer()
        if masked:
            self._entry_input.Hide()
            self._password_label = wx.StaticText(self, label="Pass&word:")
            self.input = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER | wx.TE_PASSWORD)
            self.input.SetName(f"{self.world.name} password")
            self.input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
            self.input.Bind(wx.EVT_KEY_DOWN, self._on_input_key)
            sizer.Add(self._password_label, 0, wx.LEFT, 2)
            sizer.Add(self.input, 0, wx.EXPAND | wx.ALL, 2)
        else:
            secret = self.input
            if secret.GetValue():
                self._loop.call_soon_threadsafe(self._speak_system, "Unsent password cleared")
            sizer.Detach(secret)
            sizer.Detach(self._password_label)
            secret.Hide()
            self._password_label.Destroy()
            self.input = self._entry_input
            self.input.Show()
        self.Layout()
        if focused:
            self.input.SetFocus()
            if not masked:
                self.input.SetSelection(*selection)
        if not masked:
            secret.Destroy()  # secret text and its native undo buffer never enter the document


    def _on_input_focus(self, event: wx.FocusEvent) -> None:
        if self._entry_from_output and isinstance(self.input, CommandEntryCtrl):
            self.input.move_to_tail()
        self._entry_from_output = False
        event.Skip()
