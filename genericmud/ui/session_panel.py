"""One world's tab: its output box and command box, wired to that world's engine.

SessionPanel builds the tab and keeps its state. Its work is spread over four mixins, each in its
own module: session_connection, command_box, output_box and pack_window.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable
from dataclasses import replace

import wx

from genericmud.app import EngineApp
from genericmud.automation.variables import VariableEntry
from genericmud.completion import CompletionCycler
from genericmud.config.ui_prefs import UiPrefs
from genericmud.config.worlds import World
from genericmud.packs import PackStore
from genericmud.session.credentials import CredentialStore
from genericmud.session.diaglog import DiagnosticLog
from genericmud.session.hub import SessionHub
from genericmud.transport.connection import MudConnection
from genericmud.ui.command_box import _HISTORY_LIMIT, CommandBoxMixin
from genericmud.ui.command_entry import CommandEntryCtrl
from genericmud.ui.output_box import OutputBoxMixin
from genericmud.ui.pack_dialogs import PackNotepadFrame, _PackQuestion
from genericmud.ui.pack_window import PackWindowMixin
from genericmud.ui.scrollback import FindState
from genericmud.ui.session_connection import SessionConnectionMixin
from genericmud.voice.router import VoiceRouter

# How long the variables window waits for the session's loop thread to hand over a
# snapshot. A healthy loop answers in microseconds; this only bounds a wedged one.
_VARIABLE_SNAPSHOT_TIMEOUT_S = 2.0


class SessionPanel(
    SessionConnectionMixin, CommandBoxMixin, OutputBoxMixin, PackWindowMixin, wx.Panel
):
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
        self._entry_input = (CommandEntryCtrl(self)
                             if self._prefs.command_input_mode == "editable" else None)
        self._entry_from_output = False
        if self._entry_input is not None:
            self.input = self._entry_input
            self._entry_input.execute = self._send_entry
        else:
            self.input = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER)
        # Standard keeps its two-line compose box. The optional editor sizes itself.
        if self._entry_input is None:
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
        self.input.Bind(wx.EVT_SET_FOCUS, self._on_input_focus)
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
