"""What soundpack scripts can ask of the window, as MUSHclient lets them.

The clipboard, the command box, opening links, notepads, questions and message boxes.
"""

from __future__ import annotations

import concurrent.futures
import time
import webbrowser
from collections.abc import Callable

import wx

from genericmud.bridge import protocol
from genericmud.ui.clipboard import _clipboard_text, _set_clipboard_text
from genericmud.ui.command_entry import CommandEntryCtrl
from genericmud.ui.pack_dialogs import (
    PackMessageDialog,
    PackNotepadFrame,
    PackQuestionDialog,
    _PackQuestion,
)

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


class PackWindowMixin:
    """What soundpack scripts ask of the window, for SessionPanel (session_panel.py).

    These are the PackWindow calls (automation/engine.py), MUSHclient's window calls. They arrive
    on the loop thread and are done on the window's thread. Uses these attributes from
    SessionPanel.__init__: _alive, _diag, _notepad_windows, _questions, input and world, and
    session_connection's _post.
    """

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
        def read() -> str:
            if isinstance(self.input, CommandEntryCtrl):
                return self.input.current_command()
            return self.input.GetValue()

        return str(self._on_ui_thread(read, ""))


    def set_command(self, text: str) -> bool:
        def fill() -> bool:
            if isinstance(self.input, CommandEntryCtrl):
                if self.input.current_command():
                    return False
                self.input.replace_command(text)
            else:
                if self.input.GetValue():
                    return False
                self.input.SetValue(text)
                self.input.SetInsertionPointEnd()
            return True

        return bool(self._on_ui_thread(fill, False))


    def paste_command(self, text: str) -> str:
        def paste() -> str:
            if isinstance(self.input, CommandEntryCtrl):
                try:
                    return self.input.paste_command(text)
                except ValueError as error:
                    self._loop.call_soon_threadsafe(self._speak_system, str(error))
                    return ""
            start, end = self.input.GetSelection()
            replaced = self.input.GetValue()[start:end]
            self.input.Replace(start, end, text)
            return replaced

        return str(self._on_ui_thread(paste, ""))


    def select_command(self, first: int, last: int) -> None:
        def select() -> None:
            if isinstance(self.input, CommandEntryCtrl):
                self.input.select_command(first, last)
                return
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
