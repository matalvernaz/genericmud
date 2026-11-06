from __future__ import annotations

import asyncio
import concurrent.futures
import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace

import wx

from genericmud.app import EngineApp
from genericmud.automation.variables import VariableEntry
from genericmud.bridge import protocol
from genericmud.completion import CompletionCycler
from genericmud.config.ui_prefs import UiPrefs
from genericmud.config.worlds import World
from genericmud.packs import PackStore
from genericmud.session.credentials import CredentialStore
from genericmud.session.diaglog import DiagnosticLog
from genericmud.session.hub import SessionHub
from genericmud.transport.connection import MudConnection
from genericmud.ui.clipboard import _clipboard_text, _set_clipboard_text
from genericmud.ui.command_box import _HISTORY_LIMIT, CommandBoxMixin
from genericmud.ui.keyboard import _key_combo
from genericmud.ui.output_dialogs import ChooseDialog, CopyLineDialog, FindDialog
from genericmud.ui.pack_dialogs import (
    PackMessageDialog,
    PackNotepadFrame,
    PackQuestionDialog,
    _PackQuestion,
)
from genericmud.ui.scrollback import FindState, anchor_after_append, find_line
from genericmud.ui.session_connection import SessionConnectionMixin
from genericmud.voice.router import VoiceRouter

_OUTPUT_CAP_LINES = 5000  # keep the native control bounded so NVDA/UIA stays responsive
_FLUSH_INTERVAL_MS = 50  # batch output appends during floods
# How long the variables window waits for the session's loop thread to hand over a
# snapshot. A healthy loop answers in microseconds; this only bounds a wedged one.
_VARIABLE_SNAPSHOT_TIMEOUT_S = 2.0
# How long a soundpack's question waits. Every tab waits with it (they share the loop
# thread), so one the window never manages to show is given up on soon. One on screen is
# modal, so nothing else in genericMud can be used until it's answered anyway, and Cosmic
# Rage's menu items are paragraphs: it gets a long while before it closes as cancelled.
_PACK_QUESTION_SHOW_TIMEOUT_S = 60.0
_PACK_QUESTION_ANSWER_TIMEOUT_S = 900.0
_PACK_QUESTION_POLL_S = 0.05  # how often the asking pack's own timers get a turn meanwhile
# How long a pack's call that needs an answer from the window (its command box, the
# clipboard) waits for it. The window answers at once unless it's busy or closing.
_PACK_WINDOW_TIMEOUT_S = 2.0


class SessionPanel(SessionConnectionMixin, CommandBoxMixin, wx.Panel):
    """One MUD: read-only output + command input, wired to its own engine."""

    def __init__(
        self,
        parent: wx.Window,
        loop: asyncio.AbstractEventLoop,
        keymap: dict,
        world: World,
        packs: PackStore | None = None,
        credentials: CredentialStore | None = None,
        hub: SessionHub | None = None,
        diag: DiagnosticLog | None = None,
        prefs: UiPrefs | None = None,
        on_pref: Callable[[str, bool], None] | None = None,
    ):
        super().__init__(parent)
        self._loop = loop
        self._keymap = keymap
        self.world = world
        self._packs = packs
        self._credentials = credentials
        self._hub = hub
        self._diag = diag
        self._prefs = prefs or UiPrefs()  # shared with the frame; read live per keypress
        self._on_pref = on_pref
        self.app: EngineApp | None = None
        self._connection: MudConnection | None = None
        self._voice: VoiceRouter | None = None
        self._history: list[str] = []
        self._hist_index = 0
        self._history_limit = _HISTORY_LIMIT
        # No parked draft: history moves only from an empty box or from a message that is
        # itself in history, so nothing unsent is ever replaced.
        self._input_masked = False  # the server hides typed input: keep it out of history
        self._alive = True
        self._connect_finished = False  # the first connect attempt has returned (either way)
        self._reconnecting = False  # a manual reconnect is in flight; ignore repeat presses
        self._pending: list[str] = []
        self._flush_scheduled = False
        self._sound_warned = False  # speak the first sound problem; echo the rest
        self._completion = CompletionCycler()
        self._completion_start = 0  # where the word being completed begins in the input
        self._completion_tail = ""  # text after the caret when the completion run began
        self._find = FindState()  # sticky across searches; F3 repeats whatever is in here
        self._find_direction = False  # direction of the search in flight, for the caret sync
        self._find_restart = False  # next result starts from an output edge, inclusively
        self._find_anchored = False  # the search went from the reader's own line, not an edge
        self._focused = False  # this world is the one in front (GenericMudFrame._update_focus)
        self._keep_caret_on_focus = False  # one-shot: don't jump to the bottom this time
        self._questions: list[_PackQuestion] = []  # pack dialogs the loop thread waits on
        self._notepad_windows: dict[str, PackNotepadFrame] = {}  # packs' notepads, by title

        # NVDA reads a control's name from a wx.StaticText created immediately
        # before it plus SetName() (the proven ffn-dl pattern). Both are required.
        output_label = wx.StaticText(self, label="&Output:")
        self.output = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.output.SetName(f"{world.name} output")
        input_label = wx.StaticText(self, label="&Command:")
        # Multi-line on purpose. A single-line field gives the screen reader's "previous line"
        # and "next line" nothing to move through, so they degenerate into one character each
        # way on every Up and Down; with real lines they behave. It also lets a pasted list
        # arrive as one block, which is how a world with stacking off gets several commands at
        # once. Enter still sends -- it cannot insert a newline here (see _on_input_key).
        self.input = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER)
        # Two lines tall, measured from the font rather than assumed in pixels: the reader may
        # have their text scaled up, and a guessed height would clip the line being typed.
        self.input.SetMinSize((-1, self.input.GetCharHeight() * 2 + 8))
        self.input.SetName(f"{world.name} command")

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(output_label, 0, wx.LEFT | wx.TOP, 2)
        sizer.Add(self.output, 1, wx.EXPAND | wx.ALL, 2)
        sizer.Add(input_label, 0, wx.LEFT, 2)
        sizer.Add(self.input, 0, wx.EXPAND | wx.ALL, 2)
        self.SetSizer(sizer)

        self.input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
        self.input.Bind(wx.EVT_KEY_DOWN, self._on_input_key)
        self.output.Bind(wx.EVT_CHAR, self._on_output_char)
        self.output.Bind(wx.EVT_SET_FOCUS, self._on_output_focus)
        self.output.Bind(wx.EVT_KEY_DOWN, self._on_output_key)

        asyncio.run_coroutine_threadsafe(self._start(), loop)

    def variable_snapshot(self) -> tuple[list[VariableEntry], bool] | None:
        """This session's variables, read on the loop thread where engine state changes.

        Called from the UI thread; blocks until the loop hands over the rows. None when
        the loop doesn't answer in time, which the dialog reports instead of freezing.
        """
        app = self.app
        if app is None:
            return [], False
        if self._questions:  # the loop thread is waiting on a pack's dialog, not us
            return None
        result: concurrent.futures.Future = concurrent.futures.Future()

        def snapshot() -> None:
            try:
                result.set_result(app.variable_listing())
            except Exception as error:  # noqa: BLE001 - reported to the UI thread, not lost
                result.set_exception(error)

        try:
            self._loop.call_soon_threadsafe(snapshot)
            return result.result(timeout=_VARIABLE_SNAPSHOT_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - timeout, closed loop, or a failed read: say so
            return None

    # --- packs' MUSHclient window calls (PackWindow); called on the loop thread ---

    def _on_ui_thread(self, action: Callable[[], object], fallback: object) -> object:
        """Do ``action`` on the window's thread and wait for its answer; ``fallback`` when the
        tab is closing or the window doesn't answer within _PACK_WINDOW_TIMEOUT_S."""
        if not self._alive:
            return fallback
        if wx.IsMainThread():
            return action()
        result: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            if not self._alive:
                result.set_result(fallback)
                return
            try:
                result.set_result(action())
            except Exception as error:  # noqa: BLE001 - the pack gets the fallback instead
                result.set_exception(error)

        wx.CallAfter(run)
        try:
            return result.result(timeout=_PACK_WINDOW_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a busy window or a failed read: the fallback
            return fallback

    def get_clipboard(self) -> str:
        return str(self._on_ui_thread(_clipboard_text, ""))

    def set_clipboard(self, text: str) -> None:
        if self._alive:
            wx.CallAfter(_set_clipboard_text, text)

    def get_command(self) -> str:
        return str(self._on_ui_thread(self.input.GetValue, ""))

    def set_command(self, text: str) -> bool:
        def fill() -> bool:
            if self.input.GetValue():
                return False
            self.input.SetValue(text)
            self.input.SetInsertionPointEnd()
            return True

        return bool(self._on_ui_thread(fill, False))

    def _fill_command(self, text: str) -> None:
        """Put a link's half-written command in the box, ready to finish or abandon.

        Replaces whatever was there, and focuses the box: the player asked for this by
        following the link, and an unfinished command nobody is looking at is the state that
        sends half a sentence by accident.
        """
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

    def paste_command(self, text: str) -> str:
        def paste() -> str:
            start, end = self.input.GetSelection()
            replaced = self.input.GetValue()[start:end]
            self.input.Replace(start, end, text)
            return replaced

        return str(self._on_ui_thread(paste, ""))

    def select_command(self, first: int, last: int) -> None:
        def select() -> None:
            length = self.input.GetLastPosition()
            start = min(max(first - 1, 0), length)
            end = length if last < 0 else min(max(last, start), length)
            self.input.SetSelection(start, end)

        self._on_ui_thread(select, None)

    def open_url(self, url: str) -> bool:
        if not self._alive:
            return False
        wx.CallAfter(self._open_url, url)
        return True

    def _open_url(self, url: str) -> None:  # UI thread
        # Said in the output too: a browser taking the focus is otherwise a surprise.
        self._post(protocol.echo(f"* Opening {url}"))
        webbrowser.open(url)

    def show_notepad(self, title: str, text: str) -> None:
        if self._alive:
            wx.CallAfter(self._show_notepad, title, text)

    def _show_notepad(self, title: str, text: str) -> None:  # UI thread
        """A pack's notepad in a window of its own beside the game, as MUSHclient shows it.
        Showing it again brings the same window back with the newest text."""
        if not self._alive:
            return
        window = self._notepad_windows.get(title)
        if window is None:
            window = PackNotepadFrame(
                wx.GetTopLevelParent(self), f"{title} - {self.world.name}", text
            )
            window.Bind(wx.EVT_CLOSE, lambda event, name=title: self._notepad_closed(name, event))
            self._notepad_windows[title] = window
        else:
            window.set_text(text)
        window.Show()
        window.Raise()
        window.text.SetFocus()

    def _notepad_closed(self, title: str, event: wx.CloseEvent) -> None:  # UI thread
        self._notepad_windows.pop(title, None)
        event.Skip()  # let the window go

    def close_notepads(self) -> None:  # UI thread
        for window in list(self._notepad_windows.values()):
            window.Destroy()
        self._notepad_windows.clear()

    def notepad_snapshot(self) -> dict[str, str] | None:  # UI thread
        """This session's soundpack notepads, read on the loop thread like the variables;
        None when the loop is busy with a pack's dialog or doesn't answer in time."""
        app = self.app
        if app is None:
            return {}
        if self._questions:
            return None
        result: concurrent.futures.Future = concurrent.futures.Future()

        def snapshot() -> None:
            try:
                result.set_result(app.pack_notepads())
            except Exception as error:  # noqa: BLE001 - reported to the UI thread, not lost
                result.set_exception(error)

        try:
            self._loop.call_soon_threadsafe(snapshot)
            return result.result(timeout=_VARIABLE_SNAPSHOT_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - timeout, closed loop, or a failed read: say so
            return None

    def _ask_from_pack(
        self, request: dict, while_waiting: Callable[[], None] | None = None
    ) -> object | None:  # loop thread
        """Show a pack's question and wait for the answer; None when cancelled.

        The pack's script stops here until the player answers, as it does in VIPMud and
        MUSHclient, and every tab waits with it because they share this thread.
        ``while_waiting`` runs every _PACK_QUESTION_POLL_S meanwhile (the pack's own alarms).
        A question that isn't on screen within _PACK_QUESTION_SHOW_TIMEOUT_S, or isn't
        answered within _PACK_QUESTION_ANSWER_TIMEOUT_S of appearing, counts as cancelled.
        """
        if not self._alive:
            return None
        question = _PackQuestion(request)
        self._questions.append(question)
        try:
            wx.CallAfter(self._show_question, question)
            give_up = time.monotonic() + _PACK_QUESTION_SHOW_TIMEOUT_S
            while not question.done.wait(_PACK_QUESTION_POLL_S):
                if question.shown_at is not None:
                    give_up = question.shown_at + _PACK_QUESTION_ANSWER_TIMEOUT_S
                if time.monotonic() > give_up:
                    wx.CallAfter(self._withdraw_question, question)
                    return None
                if while_waiting is not None:
                    self._run_while_waiting(while_waiting)
            return question.answer
        finally:
            self._questions.remove(question)

    def _run_while_waiting(self, while_waiting: Callable[[], None]) -> None:  # loop thread
        try:
            while_waiting()
        except Exception as error:  # noqa: BLE001 - a pack's timer mustn't end the wait
            if self._diag is not None:
                self._diag.event(
                    "pack.ask.while_waiting_failed", error=f"{type(error).__name__}: {error}"
                )

    def _show_question(self, question: _PackQuestion) -> None:  # UI thread
        if question.done.is_set() or not self._alive:
            question.finish(None)
            return
        try:
            question.finish(self._run_question_dialog(question))
        finally:
            question.finish(None)  # a dialog that failed is a cancel, never a stuck script

    def _run_question_dialog(self, question: _PackQuestion) -> object | None:  # UI thread
        request = question.request
        title = self._question_title(request)
        if request["kind"] == "message":
            dialog = PackMessageDialog(self, request, title)
        else:
            dialog = PackQuestionDialog(self, request, title)
        question.dialog = dialog
        question.shown_at = time.monotonic()
        try:
            return dialog.answer(dialog.ShowModal())
        finally:
            question.dialog = None
            dialog.Destroy()

    def _withdraw_question(self, question: _PackQuestion) -> None:  # UI thread
        """Cancel a question nobody answered in time, or one the closing tab leaves behind."""
        question.finish(None)
        dialog = question.dialog
        if dialog is not None and dialog.IsModal():
            dialog.EndModal(wx.ID_CANCEL)

    def _question_title(self, request: dict) -> str:
        """The pack's title, or the world's name when it gave none. With several worlds open
        the world is named as well: any tab's pack can ask, and the dialog covers them all."""
        title = (request.get("title") or "").strip() or self.world.name
        book = self.GetParent()
        sessions = book.GetPageCount() if isinstance(book, wx.BookCtrlBase) else 1
        if sessions > 1 and title != self.world.name:
            title = f"{title} - {self.world.name}"
        return title

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
            self._input_masked = bool(message.get("masked"))
        elif kind == protocol.INPUT_TEXT:
            self._fill_command(str(message.get("text", "")))
        elif kind == protocol.MENU:
            self._choose_from_menu(message)
        elif kind == protocol.COPY_LINE:
            self._offer_line_for_copying(str(message.get("text", "")))
        # Sound/status messages are ignored here for now (native SFX is a follow-up).

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
            selection = self.input.GetSelection()
            self.input.SetFocus()
            # Focusing may select all text; WriteText must use the user's edit range.
            self.input.SetSelection(*selection)
            self.input.WriteText(chr(unicode_key))
            return
        event.Skip()  # arrows etc. -> native screen-reader review of the output

    def set_active(self, active: bool) -> None:
        self._loop.call_soon_threadsafe(self._apply_active, active)

    def set_focused(self, focused: bool) -> None:  # UI thread
        """Whether this world is the one in front; its packs hear each change."""
        if focused == self._focused:
            return
        self._focused = focused
        self._loop.call_soon_threadsafe(self._apply_focus, focused)

    def _apply_focus(self, focused: bool) -> None:  # loop thread
        if self.app is not None:
            self.app.on_focus_changed(focused)

    def take_world_name(self, name: str) -> None:  # UI thread
        """Adopt a new name for this world, for a tab that is disconnected.

        The name is the log file, the /to address and the tab's label, so all three follow it:
        the log the last session wrote is closed, and the next connection opens the new one.
        """
        self.world = replace(self.world, name=name)
        if self.app is not None:
            self._loop.call_soon_threadsafe(self.app.rename_world, name)

    def apply_world_settings(self, world: World) -> None:  # UI thread
        """Take a world's edited stacking, logging, echo, login, MXP and sound downloads now. How
        the tab connects (host, port, TLS, encoding, sounds) stays as it opened, so a reconnect
        from this tab doesn't reach a new host with the old encoding; those apply when the world
        is opened again."""
        self.world = replace(
            self.world,
            command_stacking=world.command_stacking,
            command_separator=world.command_separator,
            log_automatically=world.log_automatically,
            log_files=world.log_files,
            log_commands=world.log_commands,
            echo_input=world.echo_input,
            connect_method=world.connect_method,
            post_connect_commands=world.post_connect_commands,
            mxp=world.mxp,
            pueblo=world.pueblo,
            download_sounds=world.download_sounds,
        )
        if self.app is not None:
            self._loop.call_soon_threadsafe(self.app.apply_world_settings, self.world)

    def _apply_active(self, active: bool) -> None:  # loop thread
        if self._voice is not None:
            # Who may speak is the window's call (see _update_active): the world in front, or
            # every open world when the player asks to hear them all.
            self._voice.set_muted(not active)
            if not active:
                # Muting gates only FUTURE lines; without this, speech already queued in the
                # async backend keeps talking over the session the user just switched to.
                self._voice.flush()
