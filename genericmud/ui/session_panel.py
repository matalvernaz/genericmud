from __future__ import annotations

import asyncio
import concurrent.futures
import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace

import wx

from genericmud.app import CLIENT_PREFIX, EngineApp
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
from genericmud.ui.keyboard import (
    _COMPLETE_BACKWARD,
    _COMPLETE_FORWARD,
    _PASSTHROUGH_COMBOS,
    _key_combo,
)
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


class SessionPanel(SessionConnectionMixin, wx.Panel):
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
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            modified = event.ControlDown() or event.AltDown()
            if event.ShiftDown() and not modified:
                self._start_new_command_line()
                return
            if not modified:
                # A multi-line field inserts a newline here. Sending is what the box is for.
                self._on_send(None)
                return
        combo = _key_combo(event, allow_numpad=not self._prefs.numpad_compass)
        if combo in (_COMPLETE_FORWARD, _COMPLETE_BACKWARD):
            self._cycle_completion(backward=(combo == _COMPLETE_BACKWARD))
            return
        self._completion.reset()  # any other key ends a completion run
        plain = not (event.ControlDown() or event.AltDown() or event.ShiftDown())
        if plain and code in (wx.WXK_UP, wx.WXK_DOWN):
            if self._history_step(-1 if code == wx.WXK_UP else 1):
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
        if not self._completion.active:
            text = self.input.GetValue()
            caret = self.input.GetInsertionPoint()
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
        prefix_text = self.input.GetValue()[: self._completion_start]
        self.input.SetValue(prefix_text + word + self._completion_tail)
        self.input.SetInsertionPoint(self._completion_start + len(word))
        # Tab is not a caret-movement gesture, so the screen reader announces nothing here
        # (unlike history recall, where it reads the line itself) -- speak the word ourselves.
        self._loop.call_soon_threadsafe(self._speak_system, word)

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
