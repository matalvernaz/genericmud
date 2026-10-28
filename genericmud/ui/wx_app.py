"""Native wxPython UI (Windows-first).

The VIPMud-class interaction model on native controls: a read-only multiline
output box NVDA reads like Notepad (Tab to it, arrow/say-line), a separate command
box, Tab/Shift+Tab between them, type-on-output jumps to the command box, and one
wx.Simplebook page per MUD (no visible tab strip, so nothing sits in the keyboard Tab
order; Ctrl+Tab / Ctrl+Shift+Tab switch sessions).

Threading: wx runs on the main thread; an asyncio loop runs in a background thread
for the connections. Engine output is marshaled to the UI with wx.CallAfter; input
and keys are pushed to the loop with call_soon_threadsafe. Each session's engine
objects (connection, voice, EngineApp) are created ON the loop thread so the SAPI
voice's COM apartment is correct.

Build-blind: this module isn't exercised by the test suite (wxPython needs a display
and isn't installed on the dev host); the reused engine is what the tests cover.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import shutil
import tempfile
import threading
import time
import traceback
import webbrowser
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import wx

from genericmud import __version__, help_text
from genericmud.app import CLIENT_PREFIX, EngineApp
from genericmud.automation.engine import AutomationEngine
from genericmud.automation.variables import VariableEntry, filter_variables
from genericmud.bridge import protocol
from genericmud.completion import CompletionCycler
from genericmud.config.keymap import load_keymap
from genericmud.config.ui_prefs import UiPrefs, load_ui_prefs, save_ui_prefs
from genericmud.config.update_prefs import is_snoozed, load_prefs, save_prefs, snooze_timestamp
from genericmud.config.world_files import migrate_world_files
from genericmud.config.worlds import (
    DEFAULT_CONNECT_METHOD,
    DEFAULT_PORT,
    DEFAULT_SEPARATOR,
    LOG_FILES,
    World,
    config_dir,
    load_worlds,
    parse_port,
    save_worlds,
    separator_problem,
)
from genericmud.packs import (
    GitUpdateResult,
    PackError,
    PackStore,
    SetupResult,
    activate_world,
    describe_update,
    detect_entry,
    entry_problem,
    git_sources,
    known_muds,
    manifest_sources,
    setup_pack,
    setup_pack_from_git,
    setup_pack_from_manifest,
    slugify,
    update_pack,
    update_pack_from_git,
    vault,
    world_from_pack,
)
from genericmud.packs.manifest import CODE_EXEC_DIALECTS
from genericmud.packs.store import extract_pack
from genericmud.packs.user_rules import (
    MATCH_CHOICES,
    UserAlias,
    UserChannel,
    UserKey,
    UserRules,
    UserTrigger,
    copy_sound_into_pack,
    load_rules,
    register_rules,
    save_rules,
)
from genericmud.packs.world_share import export_world, import_world
from genericmud.protocol.charset import AUTO, ENCODING_CHOICES, encoding_label
from genericmud.scripting import user_scripts
from genericmud.scripting.api import MESSAGE_BUTTONS, ScriptApi
from genericmud.session.crashlog import install_loop_exception_handler
from genericmud.session.credentials import CredentialStore, open_credential_store
from genericmud.session.diaglog import DiagnosticLog, make_diagnostic_log
from genericmud.session.hub import SessionHub
from genericmud.sound.bass_backend import make_sound_backend
from genericmud.transport.connection import MudConnection
from genericmud.ui.scrollback import FindState, anchor_after_append, find_line
from genericmud.update import self_update
from genericmud.voice.factory import make_voice_backend
from genericmud.voice.router import VoiceRouter

_NAMED_KEYS = {
    wx.WXK_UP: "up",
    wx.WXK_DOWN: "down",
    wx.WXK_LEFT: "left",
    wx.WXK_RIGHT: "right",
    wx.WXK_HOME: "home",
    wx.WXK_END: "end",
    wx.WXK_ESCAPE: "escape",
    # Named so pack accelerators can bind them (Erion's history browser uses
    # alt+pageup/pagedown, alt+space, alt+shift+delete, alt+enter...). Only modified
    # combos reach the keymap -- plain enter/space/tab stay ordinary typing below.
    wx.WXK_PAGEUP: "pageup",
    wx.WXK_PAGEDOWN: "pagedown",
    wx.WXK_DELETE: "delete",
    wx.WXK_INSERT: "insert",
    wx.WXK_RETURN: "enter",
    wx.WXK_TAB: "tab",
    wx.WXK_SPACE: "space",
}


def _key_combo(event: wx.KeyEvent) -> str | None:
    """Build a keymap combo ("ctrl+1", "alt+up", "f11") or None for plain typing."""
    code = event.GetKeyCode()
    mods = []
    if event.ControlDown():
        mods.append("ctrl")
    if event.AltDown():
        mods.append("alt")
    if event.ShiftDown():
        mods.append("shift")

    if wx.WXK_F1 <= code <= wx.WXK_F24:
        name = f"f{code - wx.WXK_F1 + 1}"
    elif code in _NAMED_KEYS:
        name = _NAMED_KEYS[code]
    elif 33 <= code < 127:
        name = chr(code).lower()
    else:
        return None

    is_fkey = name.startswith("f") and name[1:].isdigit()
    is_special = is_fkey or name == "escape"
    if not mods and not is_special:
        return None  # ordinary typing
    if mods == ["shift"] and not is_fkey:
        # Shift+<key> is typing and editing, not a macro chord: capitals, punctuation,
        # shift+arrow/home/end selection, shift+insert paste, shift+delete cut, shift+tab.
        # Consuming these (as combos) made every shifted character vanish from the input
        # box. Shift+F-keys stay combos -- packs bind them (Erion: shift+f1 -> full hp).
        return None
    return "+".join(mods + [name])


# Window/OS commands the input box must NOT swallow -- they have to reach the platform's
# default handler (Alt+F4 -> WM_CLOSE -> our EVT_CLOSE), or the window can't be closed.
# Combos the input box must keep for native editing/system behaviour rather than
# treat as macros: window close, clipboard, undo/redo, select-all, and word-wise
# caret movement/selection -- these matter doubly under a screen reader.
_PASSTHROUGH_COMBOS = frozenset({
    "alt+f4",
    # Top-level menu mnemonics remain reachable even if a user script tries to bind them.
    "alt+f", "alt+p", "alt+a", "alt+v", "alt+h",
    "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+a", "ctrl+z", "ctrl+y",
    "ctrl+left", "ctrl+right", "ctrl+home", "ctrl+end", "ctrl+delete",
    "ctrl+shift+left", "ctrl+shift+right", "ctrl+shift+home", "ctrl+shift+end",
})

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
_PACK_SOUND_SUFFIXES = frozenset({".wav", ".ogg", ".mp3", ".flac"})  # bundled-audio detection

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
_COMPLETE_FORWARD = "ctrl+space"  # cycle a completion of the current input word
_COMPLETE_BACKWARD = "ctrl+shift+space"
# What each keymap namespace is for, so a refused hotkey can say what has the key.
_KEYMAP_AREAS = {
    "recall": "reading back recent messages",
    "review": "reviewing the output",
    "voice": "speech",
    "sound": "stopping sounds",
    "input": "auto-repeat",
    "chan": "browsing channels",
    "nav": "walking and breadcrumbs",
    "map": "the map",
    "log": "logging",
}


def _hotkey_taken(combo: str, keymap: dict[str, str]) -> str | None:
    """Why a hotkey on ``combo`` would never run, or None when the key is free.

    genericMud's own keymap answers a key before any hotkey does, the menu and editing
    keys in _PASSTHROUGH_COMBOS never reach the engine, and the command box keeps the
    completion keys, so a hotkey saved on one of them sat in the list doing nothing.
    """
    combo = combo.strip().lower()
    name = "+".join(part.capitalize() for part in combo.split("+"))
    if combo in keymap:
        area = _KEYMAP_AREAS.get(keymap[combo].partition(":")[0], "one of its own commands")
        reason = f"{name} is already genericMud's key for {area}"
    elif combo in _PASSTHROUGH_COMBOS:
        reason = f"{name} is kept for the menus and text editing"
    elif combo in (_COMPLETE_FORWARD, _COMPLETE_BACKWARD):
        reason = f"{name} completes words in the command box"
    else:
        return None
    return f"{reason}, so a hotkey on it would never run. Choose another key."


def _connection_menu_label(connected: bool) -> str:
    """The File-menu label for the active tab's connection toggle.

    One item rather than two enabled/disabled ones: Ctrl+D keeps working in both states,
    so the same key has to mean "drop it" now and "bring it back" a moment later.
    """
    return "&Disconnect\tCtrl+D" if connected else "&Reconnect\tCtrl+D"


class SessionPanel(wx.Panel):
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
        self._history_draft = ""  # unsent input, parked while Up browses history
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
        # The output put the reader back at the bottom since a search last found something,
        # so the next one searches from there, not on from the line it found.
        self._find_from_bottom = False
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
        self.input = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
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

    # --- engine lifecycle (loop thread) ---

    async def _start(self) -> None:
        # run_coroutine_threadsafe's future is discarded, so an exception below would
        # otherwise vanish entirely: the loop exception handler never fires (the future
        # counts as having retrieved it), the crash log stays banner-only, and the tab
        # just sits dead -- the one failure shape a blind user can't detect. Trace it
        # and say it aloud instead.
        try:
            await self._start_inner()
        except Exception as error:  # noqa: BLE001 - surface session-setup death, never swallow
            if self._diag is not None:
                self._diag.event(
                    "session.start_failed",
                    error=f"{type(error).__name__}: {error}",
                    traceback=traceback.format_exc(),
                )
            self._post(protocol.echo(f"* Session failed to start: {error}"))
            if self._voice is not None:
                self._voice.speak(
                    f"Session failed to start: {type(error).__name__}",
                    channel="system",
                    interrupt=True,
                )

    async def _start_inner(self) -> None:
        self._voice = VoiceRouter(make_voice_backend())
        self._connection = MudConnection()
        self.app = EngineApp(
            self._voice,
            send=self._send,
            send_raw=self._send_raw,
            post=self._post,
            schedule=self._loop.call_later,
            keymap=self._keymap,
            packs=self._packs,
            sound_backend=make_sound_backend(on_error=self._sound_error, diag=self._diag),
            name=self.world.name,  # used for the session log filename
            credentials=self._credentials,
            hub=self._hub,
            diag=self._diag,
            suppress_reconnect=self._connection.suppress_reconnect,
            withdraw_option=self._connection.withdraw_remote,
            encoding=self.world.encoding,
            connect_method=self.world.connect_method,
            post_connect_commands=self.world.post_connect_commands,
            mxp=self.world.mxp,
            other_worlds=lambda: [world.name for world in load_worlds()],
        )
        self.app.on_focus_changed(self._focused)  # what the window said before this existed
        self.app.asker = self._ask_from_pack
        self.app.pack_window = self  # packs' clipboard, command box, notepads and links
        # The address this session reaches may serve its media even on localhost or the LAN.
        self.app.media.trusted_address = self._connection.peer_address
        # One codec for both directions: commands go out in the encoding output is read in.
        self._connection.text_codec = self.app.codec
        # Seed the persisted speech toggles; changes flow back out through pref_sink.
        self.app.follow_mode = self._prefs.follow_mode
        self.app.interrupt_mode = self._prefs.interrupt_mode
        self.app.autoretype = self._prefs.autoretype
        if self._on_pref is not None:
            self.app.pref_sink = self._on_pref
        self._connection._on_event = self.app.on_telnet_event
        self._connection.auto_reconnect = True
        self._connection.on_status = self.app.on_connection_status
        self.app.apply_world_settings(self.world)  # command stacking and logging
        if self.world.sounds:  # point @sppath at the world's sound folder before packs load
            self.app.engine.set_var("sppath", self.world.sounds)
            self.app.engine.sounds_dir = self.world.sounds  # and trust it, as packs can't be
        # Activate packs and arm auto-login before any data; they hear the MUD connected
        # once the connection opens, below.
        self.app.on_connect(self.world.name, socket_open=False)
        if self._diag is not None:
            self._diag.event("connect.begin", host=self.world.host, port=self.world.port)
        try:
            await self._connection.connect(self.world.host, self.world.port, tls=self.world.tls)
            if self._diag is not None:
                self._diag.event("connect.ok", host=self.world.host)
            # First, before anything else can queue up: the socket is finally open, so a
            # blind login (MUSH/MOO, Diku) and the after-connect commands can be typed.
            self.app.after_connect()
            self._post(protocol.echo(f"* Connected to {self.world.name}"))
            # Speak it too: connect failure and reconnect both speak, so success staying
            # echo-only was inconsistent and left a quiet MUD's first moment ambiguous.
            self.app.voice.speak(f"Connected to {self.world.name}.", channel="system")
            # Nothing from the MUD has been read yet: the reading starts once this yields.
            self.app.on_socket_open()
        except OSError as error:
            if self._diag is not None:
                self._diag.event("connect.failed", error=str(error))
            self._post(protocol.echo(f"* Connect failed: {error}"))
            # The engine defaults to connected=True and packs/OnPluginTick read it, so a failed
            # connect must flip it off or ticks run against a socket that never opened. And speak
            # it -- a visual-only echo leaves a blind user with no idea the connect ever failed.
            if self.app is not None:
                self.app.engine.connected = False
            if self._voice is not None:
                self._voice.speak(f"Connect failed: {error}", channel="system", interrupt=True)
        finally:
            # Either way the first attempt is over, so the command box can now tell a real
            # disconnect from a handshake still in flight.
            self._connect_finished = True

    def _send(self, text: str) -> None:
        try:
            if self._connection is not None:
                self._connection.send_line(text)
        except ConnectionError:
            pass

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

    def _send_raw(self, data: bytes) -> None:
        try:
            if self._connection is not None:
                self._connection.send_packet(data)
        except ConnectionError:
            pass

    def _post(self, message: dict) -> None:
        if self._alive:
            wx.CallAfter(self._handle_message, message)

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
        # Sound/status messages are ignored here for now (native SFX is a follow-up).

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
            self.output.SetInsertionPointEnd()
            self.output.ShowPosition(self.output.GetLastPosition())
            self._find_from_bottom = True  # and F3 goes on from here, too
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
            # The action is sent directly because the keymap has no plain "enter": _key_combo
            # keeps it as typing, or it would stop the command box sending anything.
            if self.app is not None:
                self._loop.call_soon_threadsafe(
                    self.app.on_ws_message,
                    {"type": protocol.KEY, "key": "review:link"},
                )
            return
        # Route bound keymap combos here too, so the panic keys and other macros work while
        # the reader is in the output. Plain arrows/typing return no combo (see _key_combo),
        # so the screen reader's own line/char review of the output is untouched.
        if self._dispatch_bound_combo(_key_combo(event)):
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
        self._run_find(forward, from_bottom=self._find_from_bottom)

    def _run_find(
        self, forward: bool, *, restart: bool = False, from_bottom: bool = False
    ) -> None:
        if self.app is None:
            return
        self._find_direction = forward  # _sync_find_caret needs the direction actually used
        # A search from the bottom lands on its edge occurrence, as a new search does.
        self._find_restart = restart or from_bottom
        self._loop.call_soon_threadsafe(
            self.app.on_ws_message,
            protocol.find(
                self._find.term,
                forward=forward,
                case_sensitive=self._find.case_sensitive,
                restart=restart,
                from_bottom=from_bottom,
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
        if not message.get("found"):
            return  # still at the bottom, if that's where it searched from
        self._find_from_bottom = False  # the next search goes on from this match
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
            from_edge=restart,
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

    def _on_send(self, _event: wx.CommandEvent) -> None:
        text = self.input.GetValue()
        # genericMud's own commands ("/to", "/alias", "/map", ...) don't need this tab's
        # connection: "/to" goes out through another session. Asking first left no way to
        # run them here at all, since Cancel keeps the line and Enter asks again. An unknown
        # "/verb" a MUD owns is dropped unasked, as it was before the question existed.
        offline_ok = text.startswith(CLIENT_PREFIX)
        if self._connect_finished and not self.is_connected() and not offline_ok:
            # Nothing typed into a dead session reaches the MUD -- the transport drops it and
            # the client says nothing, which looks exactly like a MUD ignoring the command.
            # Ask instead, and leave the text in the box so it isn't lost while deciding.
            self._prompt_reconnect()
            return
        self.input.SetValue("")
        if text and not self._input_masked:
            self._history.append(text)
        self._hist_index = len(self._history)
        self._history_draft = ""
        if self.app is not None:
            self._loop.call_soon_threadsafe(self.app.on_ws_message, {"type": "input", "text": text})

    def _on_input_key(self, event: wx.KeyEvent) -> None:
        code = event.GetKeyCode()
        combo = _key_combo(event)
        if combo in (_COMPLETE_FORWARD, _COMPLETE_BACKWARD):
            self._cycle_completion(backward=(combo == _COMPLETE_BACKWARD))
            return
        self._completion.reset()  # any other key ends a completion run
        plain = not (event.ControlDown() or event.AltDown() or event.ShiftDown())
        if plain and code in (wx.WXK_UP, wx.WXK_DOWN):
            self._recall_history(-1 if code == wx.WXK_UP else 1)
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
            self.input.SetFocus()
            self.input.WriteText(chr(unicode_key))
            return
        event.Skip()  # arrows etc. -> native screen-reader review of the output

    def _recall_history(self, direction: int) -> None:
        if not self._history:
            return
        if direction < 0 and self._hist_index == len(self._history):
            # Leaving the live edit line for history: park the unsent draft so Down
            # brings it back instead of a blank field (losing typed-but-unsent input
            # is silent data loss for someone who can't glance at the box).
            self._history_draft = self.input.GetValue()
        self._hist_index = max(0, min(len(self._history), self._hist_index + direction))
        value = (
            self._history[self._hist_index]
            if self._hist_index < len(self._history)
            else self._history_draft
        )
        self.input.SetValue(value)
        self.input.SetInsertionPointEnd()
        # Deliberately silent -- do not add a self-voice call back here. Up/Down are
        # caret-movement gestures, so NVDA runs its own move-by-line script and reads the
        # field regardless of this handler consuming the key. Speaking as well raced that
        # read: the user heard NVDA's "blank" and then our text on every recall.

    def _speak_system(self, text: str) -> None:  # loop thread
        if self._voice is not None:
            self._voice.speak(text, channel="system", interrupt=True)

    def _sound_error(self, message: str) -> None:  # loop thread (pygame backend / make_pygame)
        """Surface a sound failure: echo every one to the output, speak only the first.

        A blind user otherwise gets silence with no clue why; the first failure is spoken
        so they know to look, and all of them land in the reviewable output.
        """
        self._post(protocol.echo(f"* {message}"))
        if not self._sound_warned:
            self._sound_warned = True
            if self._voice is not None:
                self._voice.speak(message, channel="system", interrupt=False)

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

    def apply_world_settings(self, world: World) -> None:  # UI thread
        """Take a world's edited command stacking, logging, MXP, login and
        sound downloads now. How
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
            mxp=world.mxp,
            connect_method=world.connect_method,
            post_connect_commands=world.post_connect_commands,
            download_sounds=world.download_sounds,
        )
        if self.app is not None:
            self._loop.call_soon_threadsafe(self.app.apply_world_settings, self.world)

    def _apply_active(self, active: bool) -> None:  # loop thread
        if self._voice is not None:
            self._voice.set_muted(not active)  # only the foreground MUD self-voices
            if not active:
                # Muting gates only FUTURE lines; without this, speech already queued in the
                # async backend keeps talking over the session the user just switched to.
                self._voice.flush()

    def close(self) -> None:
        """Tear down the session; safe to call from the wx thread on tab close."""
        self._alive = False
        self.close_notepads()
        try:
            for question in list(self._questions):  # free the loop thread to tear down
                self._withdraw_question(question)
        except Exception as error:  # noqa: BLE001 - the session must be torn down regardless
            if self._diag is not None:
                self._diag.event(
                    "pack.ask.withdraw_failed", error=f"{type(error).__name__}: {error}"
                )
        self._loop.call_soon_threadsafe(self._teardown)

    def _teardown(self) -> None:  # loop thread
        if self.app is not None:
            self.app.shutdown()  # leave the session hub, stop logging
            self.app.sound.flush()  # a looping ambience/music cue outlives the tab otherwise
        if self._voice is not None:
            self._voice.flush()
        if self._connection is not None:
            asyncio.create_task(self._connection.close())

    def is_connected(self) -> bool:
        return self._connection is not None and self._connection.connected

    def disconnect(self) -> None:
        """Drop the connection but keep the session/tab open (and stop auto-reconnect)."""
        self._loop.call_soon_threadsafe(self._do_disconnect)

    def _do_disconnect(self) -> None:  # loop thread
        if self._connection is not None:
            self._connection.auto_reconnect = False
            asyncio.create_task(self._connection.close())
        if self.app is not None:
            # A deliberate close stays quiet on the transport side -- close() sets _closing,
            # so the read loop's own "disconnected" never fires -- which is why the flush,
            # the timer cancel, the hidden-input reset, the VIPMud packs' OnDisconnect and
            # the wording all have to come from the same place a dropped link uses.
            # Reporting it through that path keeps the two identical instead of
            # maintaining a second copy of them here.
            self.app.on_connection_status("disconnected")

    def reconnect(self) -> None:
        """Reconnect this session in place, keeping its tab, scrollback and loaded packs.

        Two paths lead here with no connection and no way back otherwise: a deliberate
        disconnect (which cancels auto-reconnect on purpose) and the backoff loop giving
        up. Opening the world again from Ctrl+O is not a substitute -- it builds a second
        tab and abandons this one.
        """
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._reconnect()))

    async def _reconnect(self) -> None:  # loop thread
        if not self._alive or self._connection is None or self.app is None:
            return  # the tab closed before this got its turn on the loop
        if self._connection.connected:
            return  # the link came back while this waited its turn on the loop
        if self._reconnecting or self._connection.connecting:
            # Already on its way back: this tab's first connect, a reconnect asked for a
            # moment ago, or the transport's own backoff between tries. A second connect
            # would open a second socket, and the two read loops would then tear down each
            # other's sockets and restart the backoff without end. Say so, so the key press
            # isn't met with silence.
            self._post(protocol.echo(f"* Still connecting to {self.world.name}"))
            if self._voice is not None:
                self._voice.speak(f"Still connecting to {self.world.name}.", channel="system",
                                  interrupt=True)
            return
        self._reconnecting = True
        # disconnect() turned this off; re-arm it so the socket keeps its own safety net.
        self._connection.auto_reconnect = True
        if self._diag is not None:
            self._diag.event("reconnect.manual", host=self.world.host, port=self.world.port)
        self._post(protocol.echo(f"* Reconnecting to {self.world.name}"))
        if self._voice is not None:
            self._voice.speak(f"Reconnecting to {self.world.name}.", channel="system",
                              interrupt=True)
        try:
            await self._connection.connect(self.world.host, self.world.port, tls=self.world.tls)
        except OSError as error:
            if not self._alive:
                return  # the tab closed while this waited; nobody is listening for it
            if self._diag is not None:
                self._diag.event("reconnect.failed", error=f"{type(error).__name__}: {error}")
            self._post(protocol.echo(f"* Reconnect failed: {error}"))
            if self._voice is not None:
                self._voice.speak(f"Reconnect failed: {error}", channel="system", interrupt=True)
            return
        finally:
            self._reconnecting = False
        if not self._alive or not self._connection.connected:
            # The tab closed during the handshake. Either its teardown has closed the
            # connection already (connect() then drops the late socket and returns quietly)
            # or it is about to. There is nothing to reattach, and saying "Reconnected"
            # would be untrue, which is what the transport's own loop stopped doing too.
            return
        # The socket is live again. Report it the way the transport's own backoff loop
        # does, for the reason _do_disconnect reports through that path: the per-socket
        # reset, the wording and the VIPMud packs' OnConnect (run after the announcement,
        # so it can't cut off a pack's welcome) all come from one place, not a copy here.
        self.app.on_connection_status("reconnected")

    def _prompt_reconnect(self) -> None:
        """Ask to reconnect after a command was typed into a dead session.

        The transport drops commands silently while disconnected, so saying nothing left
        a blind user unable to tell a dead session from a command the MUD ignored.
        """
        dialog = wx.MessageDialog(
            self,
            f"Not connected to {self.world.name}.\n\nReconnect now?",
            "Not connected",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        dialog.SetYesNoLabels("&Reconnect", "&Cancel")
        answer = dialog.ShowModal()
        dialog.Destroy()
        if answer == wx.ID_YES:
            self.reconnect()


def _clipboard_text() -> str:  # UI thread
    """The clipboard's text, or "" when it holds none or can't be opened."""
    clipboard = wx.TheClipboard
    if not clipboard.Open():
        return ""
    try:
        data = wx.TextDataObject()
        return data.GetText() if clipboard.GetData(data) else ""
    finally:
        clipboard.Close()


def _set_clipboard_text(text: str) -> None:  # UI thread
    clipboard = wx.TheClipboard
    if not clipboard.Open():
        return
    try:
        clipboard.SetData(wx.TextDataObject(text))
        clipboard.Flush()  # Windows keeps it after genericMud closes
    finally:
        clipboard.Close()


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


# What a world's log file choices are called in its details, in LOG_FILES order.
_LOG_FILE_LABELS = ("A new file each session", "One file per day", "One file for this world")
# In the order CONNECT_METHODS visits them, so the value and its label stay together.
# MXP is absent because genericMud doesn't parse MXP yet (see config/worlds.py).
_CONNECT_LABELS = (
    ("none", "No auto-connect — send no login"),
    ("prompts", "Answer the MUD's prompts — name, then password when asked"),
    ("mxp", "Let the MUD ask (MXP) — send the login only when the MUD requests it"),
    ("mush", "MUSH/MOO — send CONNECT name password"),
    ("diku", "Diku — send name, then password"),
)

_ID_NEW_WORLD = wx.ID_HIGHEST + 1
_ID_EDIT_WORLD = wx.ID_HIGHEST + 2


class WorldDialog(wx.Dialog):
    """Create or edit connection details for one world."""

    def __init__(
        self,
        parent: wx.Window,
        initial: World | None = None,
        *,
        title: str = "New World",
        offer_trust: bool = False,
        save_default: bool = True,
        show_save: bool = True,
        lock_name: bool = False,
        credentials: CredentialStore | None = None,
        note: str = "",
    ) -> None:
        super().__init__(parent, title=title)
        self._world: World | None = None
        self._credentials = credentials
        # Kept so a rename can move the stored login: the store is keyed by world name.
        self._initial_name = initial.name if initial is not None else None
        # Why the dialog is up, first in the window: NVDA reads a dialog's text that labels no
        # control as it opens, and it can be read again. Said aloud just before the dialog
        # instead, it was cut off when the dialog took the focus.
        self._note: wx.StaticText | None = None
        if note:
            self._note = wx.StaticText(self, label=note.replace("&", "&&"))
            self._note.Wrap(420)
        grid = wx.FlexGridSizer(0, 2, 6, 6)
        grid.AddGrowableCol(1)

        # Each StaticText is created (and added) immediately before its control so the
        # label precedes the control in z-order -- the association NVDA reads on
        # Windows. Checkboxes carry their own label= as the accessible name.
        self._name = self._labeled_text(grid, "&Name:", "Name")
        self._host = self._labeled_text(grid, "&Host:", "Host")
        self._port = self._labeled_text(grid, "&Port:", "Port", str(DEFAULT_PORT))
        # Most MUDs are fine on automatic; the rest (Russian, Chinese, older European
        # MUDs) need the encoding they actually send, or every accented letter is garbled.
        grid.Add(wx.StaticText(self, label="&Encoding:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._encoding = wx.Choice(self, choices=[label for _value, label in ENCODING_CHOICES])
        self._encoding.SetName("Encoding")
        self._encoding.SetSelection(0)
        grid.Add(self._encoding, 1, wx.EXPAND)
        # Blank is the usual answer: a soundpack plays its own sounds from where it's installed.
        # A blank box with nothing saying so read as "no sounds".
        self._sounds = self._labeled_text(
            grid, "So&unds folder (blank uses the soundpack's own sounds):",
            "Sounds folder (blank uses the soundpack's own sounds)",
        )
        grid.Add((0, 0))
        self._download_sounds = wx.CheckBox(self, label="Do&wnload sounds the MUD offers")
        self._download_sounds.SetName("Download sounds the MUD offers")
        self._download_sounds.SetValue(True)
        grid.Add(self._download_sounds, 1, wx.EXPAND)
        self._mxp = wx.CheckBox(self, label="Understand M&XP markup")
        self._mxp.SetName("Understand MXP markup")
        self._mxp.SetValue(True)
        grid.Add(self._mxp, 1, wx.EXPAND)
        # Where <IMAGE> looks for a file when the tag names no address of its own. Most MUDs
        # expect the client to be told this; without it an image can't be found, so it's
        # skipped -- which is also what MUSHclient does with an address-less image.
        self._images_url = self._labeled_text(
            grid, "&Images web address:", "Images web address", ""
        )

        grid.Add((0, 0))
        self._tls = wx.CheckBox(self, label="Use &TLS")
        self._tls.SetName("Use TLS")
        grid.Add(self._tls, 1, wx.EXPAND)

        # Command stacking: "n;n;look" sends three commands.
        grid.Add((0, 0))
        self._stacking = wx.CheckBox(self, label="&Split commands at a separator, as in n;n;look")
        self._stacking.SetName("Split commands at a separator, as in n;n;look")
        self._stacking.SetValue(True)
        grid.Add(self._stacking, 1, wx.EXPAND)
        self._separator = self._labeled_text(
            grid, "Comman&d separator:", "Command separator", DEFAULT_SEPARATOR
        )
        self._separator.SetMaxLength(1)
        grid.Add((0, 0))
        separator_note = wx.StaticText(
            self,
            label=(
                "Type the separator twice to send it as itself. It works the same in your "
                "own aliases, hotkeys and triggers. Soundpacks aren't affected."
            ),
        )
        separator_note.Wrap(360)
        grid.Add(separator_note, 1, wx.EXPAND)

        # Logging (Alt+Shift+L still starts and stops it by hand).
        grid.Add((0, 0))
        self._log_automatically = wx.CheckBox(self, label="&Log automatically when connecting")
        self._log_automatically.SetName("Log automatically when connecting")
        grid.Add(self._log_automatically, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Log &files:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._log_files = wx.Choice(self, choices=list(_LOG_FILE_LABELS))
        self._log_files.SetName("Log files")
        self._log_files.SetSelection(0)
        grid.Add(self._log_files, 1, wx.EXPAND)
        grid.Add((0, 0))
        self._log_commands = wx.CheckBox(self, label="Log what &you type (passwords stay hidden)")
        self._log_commands.SetName("Log what you type (passwords stay hidden)")
        self._log_commands.SetValue(True)
        grid.Add(self._log_commands, 1, wx.EXPAND)

        # Login. The username and password go to the credential store, never to worlds.toml:
        # that file is what "Export This World" zips up and what people share. The method and
        # the after-connect commands aren't secret, so they live in the world file with the
        # rest of the per-world settings.
        self._username = self._labeled_text(grid, "&Username:", "Username")
        grid.Add(wx.StaticText(self, label="&Password:"), 0, wx.ALIGN_CENTER_VERTICAL)
        password_row = wx.BoxSizer(wx.HORIZONTAL)
        # Two controls rather than one restyled: wx won't reliably turn the password style on
        # and off on a live control (macOS ignores it), and a half-applied toggle would show
        # dots while claiming to reveal. Only one is shown at a time.
        self._password = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        self._password.SetName("Password")
        self._password_plain = wx.TextCtrl(self)
        self._password_plain.SetName("Password, revealed")
        self._password_plain.Hide()
        password_row.Add(self._password, 1, wx.EXPAND)
        password_row.Add(self._password_plain, 1, wx.EXPAND)
        grid.Add(password_row, 1, wx.EXPAND)
        self._reveal = wx.CheckBox(self, label="&Reveal password")
        self._reveal.SetName("Reveal password")
        self._reveal.SetValue(False)  # always masked to begin with
        self._reveal.Bind(wx.EVT_CHECKBOX, self._on_reveal)
        grid.Add((0, 0))
        grid.Add(self._reveal, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="&Connect:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._connect = wx.Choice(self, choices=[label for _value, label in _CONNECT_LABELS])
        self._connect.SetName("Connect")
        # The list starts on the default, so Down arrow walks away from it rather than back
        # to it: with the login boxes empty there is nothing to send, and a world that
        # promised to log in and then didn't would be worse than one that never offered.
        self._connect.SetSelection(
            [value for value, _label in _CONNECT_LABELS].index(DEFAULT_CONNECT_METHOD)
        )
        grid.Add(self._connect, 1, wx.EXPAND)
        grid.Add(
            wx.StaticText(self, label="Sent after &connecting, one command per line:"),
            0,
            wx.ALIGN_TOP,
        )
        self._post_connect = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 70))
        self._post_connect.SetName("Commands sent after connecting")
        grid.Add(self._post_connect, 1, wx.EXPAND)

        self._save: wx.CheckBox | None = None
        if show_save:
            grid.Add((0, 0))
            self._save = wx.CheckBox(self, label="Sa&ve this world")
            self._save.SetName("Save this world")
            self._save.SetValue(save_default)
            grid.Add(self._save, 1, wx.EXPAND)

        # Offered only for a freshly-installed code-executing pack (MUSHclient): it stays silent
        # until trusted, so this is where the user consents to run it. Checked by default -- they
        # chose to install it -- but visible and clearable, unlike a silent auto-trust.
        self._trust: wx.CheckBox | None = None
        if offer_trust:
            grid.Add((0, 0))
            self._trust = wx.CheckBox(
                self, label="&Trust this soundpack's scripts so its sounds play"
            )
            self._trust.SetName("Trust this soundpack's scripts so its sounds play")
            self._trust.SetValue(True)
            grid.Add(self._trust, 1, wx.EXPAND)

        sizer = wx.BoxSizer(wx.VERTICAL)
        if self._note is not None:
            sizer.Add(self._note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        if initial is not None:
            self._name.SetValue(initial.name)
            self._host.SetValue(initial.host)
            try:
                initial_port = parse_port(initial.port)
            except ValueError:
                initial_port = DEFAULT_PORT
            self._port.SetValue(str(initial_port))
            self._tls.SetValue(initial.tls)
            self._sounds.SetValue(initial.sounds or "")
            values = [value for value, _label in ENCODING_CHOICES]
            self._encoding.SetSelection(values.index(initial.encoding))
            self._stacking.SetValue(initial.command_stacking)
            self._separator.SetValue(initial.command_separator)
            self._log_automatically.SetValue(initial.log_automatically)
            self._log_files.SetSelection(LOG_FILES.index(initial.log_files))
            self._log_commands.SetValue(initial.log_commands)
            self._download_sounds.SetValue(initial.download_sounds)
            methods = [value for value, _label in _CONNECT_LABELS]
            self._connect.SetSelection(methods.index(initial.connect_method))
            self._post_connect.SetValue(initial.post_connect_commands)
            stored = self._credentials.get(initial.name) if self._credentials is not None else None
            if stored is not None:
                # Pre-filled rather than blank: leaving it blank could not tell "keep the
                # password I already have" from "forget it", and one of the two is a trap.
                self._username.SetValue(stored[0])
                self._password.SetValue(stored[1])
                self._password_plain.SetValue(stored[1])
            self._mxp.SetValue(initial.mxp)
            self._images_url.SetValue(initial.images_url)
        if lock_name:
            self._name.Enable(False)
            self._host.SetFocus()
        else:
            self._name.SetFocus()

    def _labeled_text(
        self, grid: wx.FlexGridSizer, label: str, name: str, value: str = ""
    ) -> wx.TextCtrl:
        grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        ctrl = wx.TextCtrl(self, value=value)
        ctrl.SetName(name)
        grid.Add(ctrl, 1, wx.EXPAND)
        return ctrl

    def _on_ok(self, _event: wx.CommandEvent) -> None:
        host = self._host.GetValue().strip()
        if not host:
            wx.MessageBox(
                "Enter the MUD's host name.", "World details", wx.OK | wx.ICON_ERROR, self
            )
            self._host.SetFocus()
            return
        try:
            port = parse_port(self._port.GetValue())
        except ValueError as error:
            wx.MessageBox(str(error), "World details", wx.OK | wx.ICON_ERROR, self)
            self._port.SetFocus()
            self._port.SelectAll()
            return
        separator = self._separator.GetValue()
        problem = separator_problem(separator)
        if problem is not None and self._stacking.GetValue():
            wx.MessageBox(problem, "World details", wx.OK | wx.ICON_ERROR, self)
            self._separator.SetFocus()
            self._separator.SelectAll()
            return
        self._world = World(
            name=self._name.GetValue().strip() or host,
            host=host,
            port=port,
            tls=self._tls.GetValue(),
            sounds=self._sounds.GetValue().strip() or None,
            encoding=ENCODING_CHOICES[self._encoding.GetSelection()][0],
            command_stacking=self._stacking.GetValue(),
            command_separator=separator,  # a bad one with stacking off becomes the default
            log_automatically=self._log_automatically.GetValue(),
            log_files=LOG_FILES[self._log_files.GetSelection()],
            log_commands=self._log_commands.GetValue(),
            download_sounds=self._download_sounds.GetValue(),
            connect_method=_CONNECT_LABELS[self._connect.GetSelection()][0],
            post_connect_commands=self._post_connect.GetValue(),
            mxp=self._mxp.GetValue(),
            images_url=self._images_url.GetValue().strip(),
        )
        self._store_credentials(self._world)
        self.EndModal(wx.ID_OK)

    def _password_value(self) -> str:
        """Whatever is in the password field right now, revealed or not.

        Keyed on which control is visible, not on the Reveal checkbox: by the time the
        checkbox's own handler runs the checkbox has already flipped, and reading it there
        would take the hidden (empty) control's value and drop what the player typed.
        """
        if self._password_plain.IsShown():
            return self._password_plain.GetValue()
        return self._password.GetValue()

    def _on_reveal(self, _event: wx.CommandEvent) -> None:
        """Show the password in plain text, or hide it again."""
        reveal = self._reveal.GetValue()
        typed = self._password_value()  # whichever control was on screen holds the truth
        self._password.SetValue(typed)
        self._password_plain.SetValue(typed)
        self._password.Show(not reveal)
        self._password_plain.Show(reveal)
        self.Layout()
        (self._password_plain if reveal else self._password).SetFocus()

    def _store_credentials(self, world: World) -> None:
        """Save this world's login to the credential store, following a rename.

        The store is keyed by the world's name, so a rename that left the old key behind
        would silently stop the world logging in.
        """
        if self._credentials is None or not self.should_save():
            return
        username = self._username.GetValue().strip()
        password = self._password_value()
        if username or password:
            self._credentials.set(world.name, username, password)
        else:
            self._credentials.delete(world.name)  # both blank means "forget this login"
        if self._initial_name is not None and self._initial_name != world.name:
            self._credentials.delete(self._initial_name)

    def get_world(self) -> World:
        if self._world is None:
            raise RuntimeError("world requested before the dialog was accepted")
        return self._world

    def should_save(self) -> bool:
        return self._save is None or self._save.GetValue()

    def should_trust(self) -> bool:
        """True if the trust checkbox was offered and left checked (else False)."""
        return self._trust is not None and self._trust.GetValue()


class ConnectDialog(wx.Dialog):
    """Choose an existing saved world; creation and editing use ``WorldDialog``."""

    def __init__(self, parent: wx.Window, saved: list[World]) -> None:
        super().__init__(parent, title="Connect to a Saved World", size=(460, 320))
        self._saved = sorted(saved, key=lambda world: world.name.casefold())
        outer = wx.BoxSizer(wx.VERTICAL)

        outer.Add(wx.StaticText(self, label="&Saved worlds:"), 0, wx.LEFT | wx.TOP, 10)
        self._choice = wx.ListBox(self, choices=[world.name for world in self._saved])
        self._choice.SetName("Saved worlds")
        self._choice.Bind(wx.EVT_LISTBOX, self._on_pick)
        self._choice.Bind(wx.EVT_LISTBOX_DCLICK, self._on_connect)
        outer.Add(self._choice, 1, wx.ALL | wx.EXPAND, 10)

        outer.Add(wx.StaticText(self, label="Connection &details:"), 0, wx.LEFT, 10)
        self._details = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 64)
        )
        self._details.SetName("Connection details")
        outer.Add(self._details, 0, wx.ALL | wx.EXPAND, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._connect = wx.Button(self, wx.ID_OK, "&Connect")
        self._connect.Bind(wx.EVT_BUTTON, self._on_connect)
        edit = wx.Button(self, _ID_EDIT_WORLD, "&Edit...")
        edit.Bind(wx.EVT_BUTTON, self._on_edit)
        new = wx.Button(self, _ID_NEW_WORLD, "&New World...")
        new.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(_ID_NEW_WORLD))
        buttons.Add(self._connect, 0, wx.RIGHT, 6)
        buttons.Add(edit, 0, wx.RIGHT, 6)
        buttons.Add(new, 0, wx.RIGHT, 6)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "C&ancel"), 0)
        outer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizer(outer)

        enabled = bool(self._saved)
        self._connect.Enable(enabled)
        edit.Enable(enabled)
        if enabled:
            self._choice.SetSelection(0)
            self._update_details()
            self._connect.SetDefault()
            self._choice.SetFocus()
        else:
            self._details.SetValue("No saved worlds. Choose New World to create one.")
            new.SetDefault()
            new.SetFocus()

    def _on_pick(self, _event: wx.CommandEvent) -> None:
        self._update_details()

    def _update_details(self) -> None:
        world = self.get_world()
        if world is None:
            self._details.SetValue("")
            return
        security = "TLS" if world.tls else "plain connection"
        sounds = f"\nSounds: {world.sounds}" if world.sounds else ""
        encoding = (
            f"\nEncoding: {encoding_label(world.encoding)}" if world.encoding != AUTO else ""
        )
        self._details.SetValue(f"{world.host}:{world.port} ({security}){encoding}{sounds}")

    def _on_connect(self, _event: wx.CommandEvent) -> None:
        if self.get_world() is not None:
            self.EndModal(wx.ID_OK)

    def _on_edit(self, _event: wx.CommandEvent) -> None:
        if self.get_world() is not None:
            self.EndModal(_ID_EDIT_WORLD)

    def get_world(self) -> World | None:
        index = self._choice.GetSelection()
        return self._saved[index] if 0 <= index < len(self._saved) else None


class PackManagerDialog(wx.Dialog):
    """In-app PackStore front end: install, enable, and trust soundpacks per world.

    Operates on filesystem state only (install/enable/trust); changes take effect
    the next time the world is connected, since packs activate on connect. Every
    control gets a preceding StaticText label + SetName for NVDA, matching
    WorldDialog.
    """

    _WILDCARD = (
        "Soundpacks (*.zip;*.xml;*.lua;*.set)|*.zip;*.xml;*.lua;*.set|All files (*.*)|*.*"
    )

    def __init__(
        self, parent: wx.Window, store: PackStore, worlds: list[World], active: str | None,
        diag=None, announce=None,
    ) -> None:
        super().__init__(parent, title="Manage Soundpacks", size=(560, 440))
        self._store = store
        self._diag = diag  # durable install trace (DiagnosticLog or None)
        self._announce = announce or (lambda _text: None)  # speak the result of an action
        self._ids: list[str] = []  # pack ids, parallel to the list box rows
        self._alive = True  # a late _run_async callback must not touch a destroyed dialog

        names = [w.name for w in worlds]
        if active and active not in names:
            names.insert(0, active)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label="&World (for enable/disable):"), 0, wx.LEFT | wx.TOP, 8)
        self._world = wx.Choice(self, choices=names or ["(no saved worlds)"])
        self._world.SetName("World for enable and disable")
        self._world.SetSelection(names.index(active) if active in names else 0)
        self._world.Bind(wx.EVT_CHOICE, lambda _e: self._refresh_packs())
        sizer.Add(self._world, 0, wx.EXPAND | wx.ALL, 8)

        sizer.Add(wx.StaticText(self, label="Installed &soundpacks:"), 0, wx.LEFT, 8)
        self._list = wx.ListBox(self, style=wx.LB_SINGLE)
        self._list.SetName("Installed soundpacks")
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)

        buttons = wx.GridSizer(0, 3, 4, 4)
        for label, handler in (
            ("&Install file...", self._on_install_file),
            ("Install f&older...", self._on_install_dir),
            ("&Enable or disable", self._on_toggle_enabled),
            ("&Trust or untrust", self._on_toggle_trust),
            ("&Uninstall", self._on_uninstall),
            ("Check &compatibility", self._on_conflicts),
            ("&Update from source", self._on_update),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.EXPAND)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)

        sizer.Add(
            wx.StaticText(self, label="Changes apply the next time you connect to the world."),
            0, wx.LEFT | wx.BOTTOM, 8,
        )
        sizer.Add(self.CreateButtonSizer(wx.CLOSE), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        close = self.FindWindowById(wx.ID_CLOSE)
        if close is not None:
            close.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self._refresh_packs()
        if self._ids:
            self._list.SetFocus()

    # --- state ---

    def _selected_world(self) -> str:
        world = self._world.GetStringSelection()
        return world if world and not world.startswith("(") else ""

    def _selected_pack(self) -> str | None:
        index = self._list.GetSelection()
        return self._ids[index] if index != wx.NOT_FOUND else None

    def _refresh_packs(self) -> None:
        keep = self._list.GetSelection()
        world = self._selected_world()
        self._ids = []
        self._list.Clear()
        for manifest in sorted(self._store.installed(), key=lambda m: m.id):
            trust = "trusted" if self._store.is_trusted(manifest.id) else "UNTRUSTED"
            marks = [manifest.dialect, trust]
            if world and self._store.is_enabled(manifest.id, world):
                marks.append("enabled")
            self._list.Append(f"{manifest.id}  ({', '.join(marks)})")
            self._ids.append(manifest.id)
        if self._ids:
            self._list.SetSelection(min(keep if keep != wx.NOT_FOUND else 0, len(self._ids) - 1))

    def _install(self, source: str) -> None:
        if self._diag is not None:
            self._diag.event("install.start", source=source)
        try:
            manifest = self._store.install(source, replace=True)
        except (PackError, OSError) as error:
            if self._diag is not None:
                self._diag.event("install.failed", source=source, error=repr(error),
                                 trace="".join(traceback.format_exception(error)))
            wx.MessageBox(str(error), "Install failed", wx.OK | wx.ICON_ERROR)
            return
        if self._diag is not None:
            self._diag.event("install.done", id=manifest.id, dialect=manifest.dialect)
        self._refresh_packs()
        wx.MessageBox(
            f"Installed {manifest.id} ({manifest.dialect}). Enable it for a world and "
            f"trust it, then reconnect.",
            "Installed", wx.OK | wx.ICON_INFORMATION,
        )

    # --- buttons ---

    def _on_install_file(self, _event: wx.CommandEvent) -> None:
        with wx.FileDialog(
            self, "Install a soundpack", wildcard=self._WILDCARD,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._install(dialog.GetPath())

    def _on_install_dir(self, _event: wx.CommandEvent) -> None:
        with wx.DirDialog(self, "Install a soundpack folder") as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._install(dialog.GetPath())

    def _on_toggle_enabled(self, _event: wx.CommandEvent) -> None:
        pack_id, world = self._selected_pack(), self._selected_world()
        if pack_id is None:
            return
        if not world:
            wx.MessageBox("Pick a world first.", "No world", wx.OK | wx.ICON_INFORMATION, self)
            return
        if self._store.is_enabled(pack_id, world):
            self._store.disable(pack_id, world)
            self._announce(f"{pack_id} disabled for {world}.")
        else:
            self._store.enable(pack_id, world)
            self._announce(f"{pack_id} enabled for {world}.")
        self._refresh_packs()

    def _on_toggle_trust(self, _event: wx.CommandEvent) -> None:
        pack_id = self._selected_pack()
        if pack_id is None:
            return
        if self._store.is_trusted(pack_id):
            self._store.untrust(pack_id)
            self._announce(f"{pack_id} untrusted. Its scripts won't run.")
        else:
            self._store.trust(pack_id)
            self._announce(f"{pack_id} trusted. Its sounds will play on connect.")
        self._refresh_packs()

    def _on_uninstall(self, _event: wx.CommandEvent) -> None:
        pack_id = self._selected_pack()
        if pack_id is None:
            return
        confirm = wx.MessageBox(f"Uninstall {pack_id}?", "Uninstall", wx.YES_NO | wx.ICON_QUESTION)
        if confirm != wx.YES:
            return
        try:
            self._store.uninstall(pack_id)
        except (PackError, OSError) as error:
            # A pack file held open (its looping music still playing) makes rmtree raise on
            # Windows; without this the crash vanished into the log and the list looked frozen.
            wx.MessageBox(f"Couldn't uninstall {pack_id}: {error}", "Uninstall",
                          wx.OK | wx.ICON_ERROR, self)
            return
        self._refresh_packs()
        wx.MessageBox(f"Uninstalled {pack_id}.", "Uninstall", wx.OK | wx.ICON_INFORMATION, self)

    def _on_conflicts(self, _event: wx.CommandEvent) -> None:
        world = self._selected_world()
        if not world:
            wx.MessageBox("Pick a world first.", "No world", wx.OK | wx.ICON_INFORMATION, self)
            return
        result = activate_world(self._store, world, AutomationEngine(), require_trust=False)
        count = len(result.loaded)
        packs = "pack" if count == 1 else "packs"
        lines = [f"{count} {packs} loaded for {world}."]
        for pack_id, error in result.failed.items():
            lines.append(f"Couldn't load {pack_id}: {error}")
        for conflict in result.conflicts:
            who = " and ".join(conflict.sources)
            lines.append(f"{who} both define the {conflict.kind} '{conflict.token}'.")
        for pack_id, items in result.skipped_plugins.items():
            names = ", ".join(name for name, _reason in items[:3])
            more = f", plus {len(items) - 3} more" if len(items) > 3 else ""
            lines.append(
                f"{pack_id}: {len(items)} optional or client-only plugins skipped "
                f"({names}{more})."
            )
        for pack_id, items in result.skipped_rules.items():
            lines.append(f"{pack_id}: {len(items)} malformed upstream rules skipped.")
        for pack_id, items in result.plugin_errors.items():
            lines.append(f"{pack_id}: {len(items)} plugin compatibility errors.")
        for pack_id, items in result.external_script_errors.items():
            lines.append(f"{pack_id}: {len(items)} external-script compatibility errors.")
        for pack_id, items in result.module_errors.items():
            lines.append(f"{pack_id}: {len(items)} bundled-module compatibility errors.")
        wx.MessageBox(
            "\n".join(lines), "Soundpack compatibility", wx.OK | wx.ICON_INFORMATION, self
        )

    def _on_update(self, _event: wx.CommandEvent) -> None:
        pack_id = self._selected_pack()
        if pack_id is None:
            return
        if not self._store.manifest(pack_id).origin:
            wx.MessageBox(
                "This pack has no recorded source to update from (it was set up from a "
                "local folder).", "Update", wx.OK | wx.ICON_INFORMATION,
            )
            return

        if self._diag is not None:
            self._diag.event(
                "update.start", id=pack_id, origin=self._store.manifest(pack_id).origin
            )

        def work():
            manifest_source = manifest_sources.by_id(pack_id)
            if manifest_source is not None:
                return setup_pack_from_manifest(
                    self._store, manifest_source, diag=self._diag
                )
            # A pack whose upstream is a git repository fetches only the files that changed.
            update_source = git_sources.update_source(pack_id)
            if update_source is not None:
                return update_pack_from_git(
                    self._store, pack_id, update_source, diag=self._diag
                )
            return update_pack(
                self._store, pack_id,
                fetch=lambda url, dest: vault.download(url, dest, max_bytes=_SOURCE_MAX_BYTES),
            )

        _run_async(work, lambda outcome: self._on_updated(pack_id, outcome))

    def _on_close(self, _event: wx.CommandEvent) -> None:
        self._alive = False  # a still-running "update from source" callback must not touch us
        self.EndModal(wx.ID_CLOSE)

    def _on_updated(self, pack_id: str, outcome) -> None:
        if not self._alive:
            return  # the dialog was closed before the background update finished
        if isinstance(outcome, Exception):
            if self._diag is not None:
                self._diag.event("update.failed", id=pack_id, error=repr(outcome),
                                 trace="".join(traceback.format_exception(outcome)))
            wx.MessageBox(f"Update failed: {outcome}", "Update", wx.OK | wx.ICON_ERROR)
        elif isinstance(outcome, GitUpdateResult):
            wx.MessageBox(_git_update_message(outcome), "Update", wx.OK | wx.ICON_INFORMATION)
        else:
            if self._diag is not None:
                self._diag.event("update.done", id=pack_id, dialect=outcome.manifest.dialect)
            wx.MessageBox(
                f"Updated {pack_id}. Reconnect to apply.", "Update", wx.OK | wx.ICON_INFORMATION,
            )
        self._refresh_packs()


def _git_update_message(result: GitUpdateResult) -> str:
    """What updating a pack from its repository did (shared with the /update command)."""
    return describe_update(result)


_SOURCE_MAX_BYTES = 3_000_000_000  # cap when following an installer's source repo (~3 GB)


def _run_async(work, on_done) -> None:
    """Run ``work()`` on a daemon thread; deliver its result (or exception) to
    ``on_done`` back on the wx main thread. Keeps network/IO off the UI thread."""

    def runner() -> None:
        try:
            outcome = work()
        except Exception as error:  # noqa: BLE001 - surfaced to the UI via on_done
            outcome = error
        wx.CallAfter(on_done, outcome)

    threading.Thread(target=runner, daemon=True).start()


class VaultBrowserDialog(wx.Dialog):
    """Browse mudsoundpack.com, download a pack, and run it through setup_pack.

    Network and the (potentially large) download run off the UI thread via
    :func:`_run_async`; progress marshals back with ``wx.CallAfter``. On success
    ``self.result`` holds the SetupResult and the dialog ends with ``wx.ID_OK`` so the
    frame can confirm the world and connect. Build-blind (no wx on the dev host).
    """

    def __init__(self, parent: wx.Window, store: PackStore, announce, diag=None) -> None:
        super().__init__(parent, title="Browse soundpacks (mudsoundpack.com)", size=(640, 480))
        self._store = store
        self._announce = announce  # speak status for screen-reader users
        self._diag = diag  # durable install trace (DiagnosticLog or None)
        self._last_milestone = 0  # throttles the spoken MB / file-count lines, not the gauge
        self._all_packs: list = []  # the full catalogue
        self._packs: list = []  # visible subset, parallel to the list box (what _selected indexes)
        self.result = None  # SetupResult once a pack is downloaded + set up
        self._alive = True  # a late download/setup callback must not touch a destroyed dialog

        sizer = wx.BoxSizer(wx.VERTICAL)
        # A read-only, focusable status LOG (not a StaticText): NVDA can Tab to it and
        # review every step, and each step is also spoken. Append-only, one line per step.
        sizer.Add(wx.StaticText(self, label="S&tatus:"), 0, wx.LEFT | wx.TOP, 8)
        self._status_log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
        self._status_log.SetName("Status")
        sizer.Add(self._status_log, 0, wx.EXPAND | wx.ALL, 8)

        sizer.Add(wx.StaticText(self, label="&Soundpacks:"), 0, wx.LEFT, 8)
        self._list = wx.ListBox(self, style=wx.LB_SINGLE)
        self._list.SetName("Soundpacks")
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)

        # Packs for clients genericMud can't load are hidden by default so the list is all
        # things that actually work here; tick this to see (and try) the rest anyway.
        self._show_all = wx.CheckBox(
            self, label="Also show packs for &other clients (may not work here)"
        )
        self._show_all.Bind(wx.EVT_CHECKBOX, self._on_toggle_unsupported)
        sizer.Add(self._show_all, 0, wx.LEFT | wx.BOTTOM, 8)

        self._gauge = wx.Gauge(self, range=100)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.ALL, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._setup_btn = wx.Button(self, label="&Download and set up")
        self._setup_btn.Bind(wx.EVT_BUTTON, self._on_download)
        self._setup_btn.Disable()
        browser_btn = wx.Button(self, label="Open in &browser")
        browser_btn.Bind(wx.EVT_BUTTON, self._on_open_browser)
        buttons.Add(self._setup_btn, 0, wx.RIGHT, 4)
        buttons.Add(browser_btn, 0, wx.RIGHT, 4)
        sizer.Add(buttons, 0, wx.ALL, 8)
        sizer.Add(self.CreateButtonSizer(wx.CLOSE), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        close = self.FindWindowById(wx.ID_CLOSE)
        if close is not None:
            close.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self._status("Loading the catalogue from mudsoundpack.com.")
        _run_async(vault.list_packs, self._on_listed)

    def _on_close(self, _event: wx.CommandEvent) -> None:
        self._alive = False  # a still-running catalogue/download thread must not touch us
        self.EndModal(wx.ID_CLOSE)

    def _status(self, message: str) -> None:
        """Append a step to the readable status log and speak it; safe from any thread."""
        if self._diag is not None:  # durable copy of every step, survives a later crash
            self._diag.event("vault", msg=message)
        wx.CallAfter(self._append_status, message)

    def _append_status(self, message: str) -> None:  # main thread
        if not self._alive:
            return
        self._status_log.AppendText(message + "\n")
        self._announce(message)

    def _on_listed(self, outcome) -> None:
        if not self._alive:
            return
        if isinstance(outcome, Exception):
            self._status(f"Couldn't load the catalogue: {outcome}")
            return
        self._all_packs = outcome
        shown, hidden = self._populate_list()
        hint = (
            f" {hidden} for other clients are hidden — tick Also show packs for other clients "
            "to see them."
            if hidden
            else ""
        )
        self._status(f"{shown} soundpacks loaded.{hint} Choose one, then Download and set up.")
        if self._packs:
            self._list.SetSelection(0)
            self._list.SetFocus()

    def _populate_list(self) -> tuple[int, int]:
        """Rebuild the list box from the catalogue; return (shown, hidden) counts.

        Other-client packs are hidden unless the checkbox is ticked. ``self._packs`` is kept
        parallel to the list box (the visible rows), since ``_selected`` indexes into it.
        """
        show_all = self._show_all.GetValue()
        self._packs = [pack for pack in self._all_packs if show_all or pack.supported]
        self._list.Clear()
        for pack in self._packs:
            version = f" v{pack.version}" if pack.version else ""
            tag = "" if pack.supported else " (other client)"
            # Commas, not dashes: the screen reader reads each " - " as "dash" (see _rule_summary).
            self._list.Append(
                f"{pack.name}, {pack.mud}, {pack.client}{version}, {pack.status}{tag}"
            )
        self._setup_btn.Enable(bool(self._packs))
        return len(self._packs), len(self._all_packs) - len(self._packs)

    def _on_toggle_unsupported(self, _event: wx.CommandEvent) -> None:
        shown, _hidden = self._populate_list()
        others = sum(1 for pack in self._all_packs if not pack.supported)
        if self._show_all.GetValue():
            self._status(f"Showing all {shown} packs, including {others} for other clients.")
        else:
            self._status(f"Showing {shown} supported packs; {others} for other clients hidden.")
        if self._packs:
            self._list.SetSelection(0)

    def _selected(self):
        index = self._list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._packs):
            return None
        return self._packs[index]

    def _on_download(self, _event: wx.CommandEvent) -> None:
        pack = self._selected()
        if pack is None:
            return
        if not pack.supported:
            warn = wx.MessageBox(
                f"{pack.client} packs aren't supported and probably won't work. Try anyway?",
                "Unsupported client", wx.YES_NO | wx.ICON_WARNING,
            )
            if warn != wx.YES:
                return
        self._setup_btn.Disable()
        self._last_milestone = 0
        self._status(f"Downloading {pack.name}. Large packs can take a while.")
        _run_async(lambda: self._fetch_and_setup(pack), self._on_setup_done)

    def _fill_world(self, result: SetupResult, mud_name: str) -> SetupResult:
        """Pack carried no world (a VIPMud .set): fall back to the known-MUD table, else a
        name-only stub, so the setup flow can still create and offer the world."""
        if result.world is not None:
            return result
        world = known_muds.lookup(mud_name) or World(name=mud_name, host="", port=0)
        return SetupResult(result.manifest, world, result.enabled_for)

    def _fetch_and_setup(self, pack):  # background thread
        source = manifest_sources.for_labels(pack.mud, pack.name)
        if source is not None:  # served as an HTTP file tree (Mush-Z): sync it, don't fetch a zip
            return self._setup_from_manifest(source, pack)
        git_source = git_sources.for_labels(pack.mud, pack.name)
        if git_source is not None:  # installer wrapping a git repo (Erion): fetch the repo directly
            self._status(f"Fetching {git_source.name} straight from its repository, no installer.")

            def fetch(url, dest, **kwargs):
                return vault.download(url, dest, progress=self._progress, **kwargs)

            result = setup_pack_from_git(self._store, git_source, download=fetch, diag=self._diag)
            if not self._store.is_trusted(result.manifest.id):
                self._status(
                    f"{git_source.name} is installed. Trust it in the Connect dialog so its "
                    "sounds load when you connect."
                )
            return self._fill_world(result, pack.mud)
        pack_id = slugify(pack.name)
        if pack_id in {manifest.id for manifest in self._store.installed()}:
            self._status(f"{pack.name} is already installed; using the cached copy.")
            world = world_from_pack(self._store.pack_dir(pack_id))
            return self._fill_world(
                SetupResult(
                    manifest=self._store.manifest(pack_id),
                    world=world,
                    enabled_for=world.name if world else None,
                ),
                pack.mud,
            )
        candidates = [item for item in vault.pack_downloads(pack.id) if item.installable]
        if not candidates:
            raise vault.SourceUnavailable(
                "no downloadable archive is published; use Open in browser for the author page"
            )
        tmp = Path(tempfile.mkdtemp(prefix="genericmud-pack-"))
        try:
            archive = tmp / "pack.zip"
            errors: list[str] = []
            selected = None
            for candidate in candidates:
                try:
                    vault.download(candidate.url, archive, progress=self._progress)
                except Exception as exc:  # noqa: BLE001 - try the pack's next published source
                    errors.append(f"{candidate.role}: {type(exc).__name__}: {exc}")
                    continue
                if zipfile.is_zipfile(archive):
                    selected = candidate
                    break
                errors.append(f"{candidate.role}: response was not a ZIP archive")
            if selected is None:
                raise vault.SourceUnavailable(
                    "the published archives are unavailable (" + "; ".join(errors) + ")"
                )
            extracted = tmp / slugify(pack.name)  # pack-named dir -> a stable, unique pack id
            self._status(f"Extracting {pack.name}.")
            try:
                # Route through the guarded extractor (zip-bomb quota + nested-zip descent), not a
                # bare extractall -- this is the primary download path and must not bypass the cap.
                extract_pack(archive, extracted)
            except zipfile.BadZipFile as exc:
                raise PackError(
                    "the download wasn't a ZIP (the site may have served a web page)"
                ) from exc
            entry = detect_entry(extracted, mud_name=pack.mud)
            origin = selected.url  # record the source that actually yielded a valid archive
            if entry is None:  # an installer bundle? follow the repo it clones
                extracted, entry, followed = self._follow_installer(extracted, tmp)
                if followed:
                    origin = followed  # update from the real source, not the installer
            if entry is None:
                raise PackError(entry_problem(extracted))
            self._status(f"Setting up {pack.name}.")
            return self._fill_world(
                setup_pack(self._store, extracted, entry=entry, origin=origin), pack.mud
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _setup_from_manifest(self, source, pack):  # background thread
        """Install/update a manifest-style pack (Mush-Z): sync its file tree in place.

        A fresh install pulls the whole tree; re-running fetches only what changed. The pack
        installs enabled-but-untrusted (it runs its own Lua), so the user is told to trust it.
        """
        installed = source.id in {manifest.id for manifest in self._store.installed()}
        verb = "Updating" if installed else "Downloading"
        self._status(f"{verb} {source.name}. The first install fetches the whole pack.")
        result = setup_pack_from_manifest(
            self._store, source, progress=self._sync_progress, diag=self._diag
        )
        if not self._store.is_trusted(result.manifest.id):
            self._status(
                f"{source.name} is installed. Open Manage Soundpacks and trust it so its "
                "sounds load when you connect."
            )
        return self._fill_world(result, pack.mud)

    def _sync_progress(self, done: int, total: int, relpath: str) -> None:  # background thread
        """Per-file sync progress: drive the gauge, speak file counts every 10%.

        The percentage is the gauge's job. The counts are spoken because a ~9000-file
        sync is long enough that "how many files, out of how many" is the reassuring
        part, and no progress bar conveys it.
        """
        if not total:
            return
        pct = min(int(done * 100 / total), 100)
        wx.CallAfter(self._set_gauge, pct)
        milestone = pct - pct % 10
        if milestone and milestone != self._last_milestone:
            self._last_milestone = milestone
            self._status(f"Synced {done} of {total} files.")

    def _follow_installer(self, extracted, tmp):  # background thread
        """If the download is just a Windows installer, fetch the repo it git-clones
        and retry from there. Size-capped, so a huge source aborts and surfaces its URL."""
        source = vault.installer_source(extracted)
        if not source:
            return extracted, None, None
        self._status(f"This is an installer. Fetching the pack from its source: {source}")
        src_dir = tmp / "source"
        self._last_milestone = 0  # reset progress for the second (source) download
        for archive_url in vault.git_archive_urls(source):
            try:
                src_zip = vault.download(
                    archive_url, tmp / "source.zip",
                    progress=self._progress, max_bytes=_SOURCE_MAX_BYTES,
                )
            except vault.DownloadTooLarge as exc:
                raise PackError(f"{exc}; get the pack directly from {source}") from exc
            except Exception:  # noqa: BLE001 - wrong branch / not found -> try the next URL
                continue
            try:
                extract_pack(src_zip, src_dir)  # guarded: quota + nested-zip descent
            except (zipfile.BadZipFile, PackError):
                continue  # wrong branch / not a zip / over quota -> try the next candidate URL
            entry = detect_entry(src_dir)
            if entry:
                return src_dir, entry, archive_url
        return src_dir, None, None  # the pack is copied into the store

    def _set_gauge(self, pct: int) -> None:  # main thread
        if self._alive:
            self._gauge.SetValue(pct)

    def _progress(self, done: int, total: int) -> None:  # background thread
        if total:
            wx.CallAfter(self._set_gauge, min(int(done * 100 / total), 100))
            return  # NVDA reports the gauge itself; see UpdateProgressDialog's docstring
        # No Content-Length (GitLab archives): nothing drives the gauge, so there's no
        # progress bar for NVDA to read and speech is the only channel left. Every 50 MB.
        milestone = done // 50_000_000
        if milestone and milestone != self._last_milestone:
            self._last_milestone = milestone
            self._status(f"Downloaded {done // 1_000_000} MB.")

    def _on_setup_done(self, outcome) -> None:
        if not self._alive:
            return
        if isinstance(outcome, Exception):
            if isinstance(outcome, vault.SourceUnavailable):
                if self._diag is not None:
                    self._diag.event("vault.unavailable", error=str(outcome))
                self._status(f"Source unavailable: {outcome}")
                self._setup_btn.Enable()
                return
            if self._diag is not None:
                self._diag.event("vault.failed", error=repr(outcome),
                                 trace="".join(traceback.format_exception(outcome)))
            self._status(f"Setup failed: {outcome}")
            self._setup_btn.Enable()
            return
        self.result = outcome
        if self._diag is not None:
            w = outcome.world
            world_str = f"{w.host}:{w.port}" if w and w.host else (w.name if w else "")
            self._diag.event("vault.done", id=outcome.manifest.id,
                             dialect=outcome.manifest.dialect, world=world_str)
        # speak directly (not via the deferred log) -- the dialog is about to close
        self._announce("Download and set up complete. Confirm the connection details.")
        self.EndModal(wx.ID_OK)

    def _on_open_browser(self, _event: wx.CommandEvent) -> None:
        pack = self._selected()
        if pack is not None:
            webbrowser.open(f"{vault.BASE_URL}/pack.php?id={pack.id}")


# ShowModal return values for UpdateNotificationDialog (distinct from wx.ID_OK/CANCEL so the
# caller can tell the buttons apart). wx.ID_HIGHEST is the top of wx's own reserved range.
_NO_VARIABLES = (
    "Nothing yet. MUD data arrives only from MUDs that support GMCP, MSDP or MSSP, "
    "usually once you've logged in. Script values appear when a soundpack or one of "
    "your scripts saves one."
)
_SNAPSHOT_FAILED = "The session didn't answer in time. Press Refresh to try again."


class VariablesDialog(wx.Dialog):
    """Every value this world knows: browse them, or pick one for a rule's field.

    ``fetch`` returns ``(rows, truncated)`` from the session, or None when it didn't
    answer. Browsing copies a reference to the clipboard; picking returns the chosen row
    through :attr:`chosen` so the rule editor can insert its reference.
    """

    def __init__(
        self,
        parent: wx.Window,
        fetch: Callable[[], tuple[list[VariableEntry], bool] | None],
        *,
        world_name: str,
        pick: bool = False,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        title = "Insert a Variable" if pick else f"MUD Variables — {world_name}"
        super().__init__(
            parent, title=title, size=(640, 480),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._fetch = fetch
        self._pick = pick
        self._announce = announce or (lambda _text: None)
        self._entries: list[VariableEntry] = []
        self._shown: list[VariableEntry] = []
        self._truncated = False
        self._failed = False
        self.chosen: VariableEntry | None = None

        outer = wx.BoxSizer(wx.VERTICAL)
        purpose = (
            "Choose a value and press Insert to put it in the field."
            if pick else
            "Copy a value's reference to use it in a trigger, alias or hotkey."
        )
        intro = wx.StaticText(
            self,
            label=(
                "Values this world has sent over GMCP, MSDP or MSSP, then values saved by "
                f"scripts. {purpose}"
            ),
        )
        intro.Wrap(600)
        outer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        filter_row = wx.BoxSizer(wx.HORIZONTAL)
        filter_row.Add(
            wx.StaticText(self, label="&Filter:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6
        )
        self._filter = wx.TextCtrl(self)
        self._filter.SetName("Filter")
        self._filter.Bind(wx.EVT_TEXT, lambda _event: self._populate())
        self._filter.Bind(wx.EVT_KEY_DOWN, self._on_filter_key)
        filter_row.Add(self._filter, 1, wx.EXPAND)
        outer.Add(filter_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self._list_label = wx.StaticText(self, label="&Variables:")
        outer.Add(self._list_label, 0, wx.LEFT | wx.RIGHT, 10)
        self._list = wx.ListBox(self, style=wx.LB_SINGLE)
        self._list.SetName("Variables")
        self._list.Bind(wx.EVT_LISTBOX, lambda _event: self._show_details())
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_activate)
        self._list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        outer.Add(self._list, 1, wx.ALL | wx.EXPAND, 10)

        outer.Add(wx.StaticText(self, label="&Details:"), 0, wx.LEFT | wx.RIGHT, 10)
        self._details = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 72)
        )
        self._details.SetName("Details")
        outer.Add(self._details, 0, wx.ALL | wx.EXPAND, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        if pick:
            self._action = wx.Button(self, wx.ID_OK, "&Insert")
            self._action.Bind(wx.EVT_BUTTON, self._on_activate)
            buttons.Add(self._action, 0, wx.RIGHT, 6)
        else:
            self._action = wx.Button(self, label="&Copy reference")
            self._action.Bind(wx.EVT_BUTTON, self._on_activate)
            buttons.Add(self._action, 0, wx.RIGHT, 6)
        refresh = wx.Button(self, label="&Refresh")
        refresh.Bind(wx.EVT_BUTTON, lambda _event: self._refresh(announce=True))
        buttons.Add(refresh, 0, wx.RIGHT, 6)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "Cancel" if pick else "Cl&ose"), 0)
        outer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizer(outer)
        self.SetMinSize((520, 380))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        self._refresh()
        if self._shown:
            self._list.SetFocus()
        else:
            self._filter.SetFocus()

    def _refresh(self, *, announce: bool = False) -> None:
        """Read the session's values again, keeping the selected row where it still exists."""
        keep = self._selected()
        snapshot = self._fetch()
        self._failed = snapshot is None
        self._entries, self._truncated = snapshot if snapshot is not None else ([], False)
        self._populate(keep)
        if announce:
            count = len(self._entries)
            self._announce(
                _SNAPSHOT_FAILED if self._failed
                else f"{count} variable{'s' if count != 1 else ''}."
            )

    def _populate(self, keep: VariableEntry | None = None) -> None:
        keep = keep or self._selected()
        self._shown = filter_variables(self._entries, self._filter.GetValue())
        self._list.Set([entry.row for entry in self._shown])
        total = len(self._entries)
        if self._filter.GetValue().strip():
            count = f"{len(self._shown)} of {total}"
        else:
            count = f"{total}{', the first ones only' if self._truncated else ''}"
        self._list_label.SetLabel(f"&Variables ({count}):")
        if self._shown:
            index = next(
                (i for i, entry in enumerate(self._shown)
                 if keep is not None and (entry.source, entry.name) == (keep.source, keep.name)),
                0,
            )
            self._list.SetSelection(index)
        self._show_details()

    def _selected(self) -> VariableEntry | None:
        index = self._list.GetSelection()
        return self._shown[index] if 0 <= index < len(self._shown) else None

    def _show_details(self) -> None:
        entry = self._selected()
        if entry is not None:
            self._details.SetValue(entry.details)
        elif self._failed:
            self._details.SetValue(_SNAPSHOT_FAILED)
        elif self._entries:
            self._details.SetValue("No variable name contains that text.")
        else:
            self._details.SetValue(_NO_VARIABLES)
        self._action.Enable(entry is not None)

    def _on_activate(self, _event) -> None:
        entry = self._selected()
        if entry is None:
            return
        if self._pick:
            self.chosen = entry
            self.EndModal(wx.ID_OK)
            return
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(entry.reference))
            finally:
                wx.TheClipboard.Close()
            self._announce(f"Copied the reference to {entry.name}.")
        else:
            self._announce("The clipboard is busy; try again.")

    def _on_filter_key(self, event: wx.KeyEvent) -> None:
        # Down from the filter goes straight to the matches, as in a search box.
        if event.GetKeyCode() == wx.WXK_DOWN and self._shown:
            self._list.SetFocus()
            return
        event.Skip()

    def _on_list_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_activate(event)
            return
        event.Skip()

    def _on_char_hook(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_F5:
            self._refresh(announce=True)
            return
        event.Skip()


class _KeyCaptureCtrl(wx.TextCtrl):
    """A read-only-ish field that records the key combination pressed in it.

    Focus it and press the combo (e.g. Ctrl+H, Alt+Shift+F2); the combo text lands
    in the field. Tab/Shift+Tab still navigate and Escape still cancels, so a
    keyboard-only (screen reader) user is never trapped in the control.
    """

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)

    def _on_key(self, event: wx.KeyEvent) -> None:
        code = event.GetKeyCode()
        mods_besides_shift = event.ControlDown() or event.AltDown()
        # Tab and Shift+Tab must both reach dialog navigation (forward AND reverse) -- swallowing
        # Shift+Tab trapped reverse-tab. Escape cancels. Only a Ctrl/Alt-modified Tab is a combo.
        if code == wx.WXK_TAB and not mods_besides_shift:
            event.Skip()
            return
        if code == wx.WXK_ESCAPE and not (mods_besides_shift or event.ShiftDown()):
            event.Skip()  # keep dialog navigation working
            return
        combo = _key_combo(event)
        if combo:
            self.SetValue(combo)
        # swallow everything else: this field records combos, it doesn't edit text


def _focus_if_alive(window: wx.Window) -> None:
    """Focus ``window`` from a deferred call, unless it was destroyed in the meantime."""
    if window:  # a destroyed wx window is falsy
        window.SetFocus()


class _RuleEditorBase(wx.Dialog):
    """Shared layout helpers for automation dialogs (NVDA: label precedes control)."""

    # The session's variables, for the Insert a variable buttons; None hides the buttons
    # (no live session to read from). Set by each editor before it builds its fields.
    _variables: Callable[[], tuple[list[VariableEntry], bool] | None] | None = None

    def _grid(self) -> wx.FlexGridSizer:
        grid = wx.FlexGridSizer(0, 2, 6, 6)
        grid.AddGrowableCol(1)
        return grid

    @staticmethod
    def _field_name(label: str) -> str:
        """A spoken control name from a field label (drop the mnemonic & and trailing colon)."""
        return label.replace("&", "").rstrip(":").strip()

    def _text(self, grid: wx.FlexGridSizer, label: str, value: str = "") -> wx.TextCtrl:
        grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        ctrl = wx.TextCtrl(self, value=value)
        ctrl.SetName(self._field_name(label))  # NVDA needs the name as well as the label
        grid.Add(ctrl, 1, wx.EXPAND)
        return ctrl

    def _command_text(
        self, grid: wx.FlexGridSizer, value: str, *, captures: bool
    ) -> wx.TextCtrl:
        label = "&Commands to send, one per line:"
        grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_TOP)
        ctrl = wx.TextCtrl(
            self, value=value, size=(-1, 80),
            style=wx.TE_MULTILINE | wx.TE_DONTWRAP,
        )
        ctrl.SetName(self._field_name(label))
        grid.Add(ctrl, 1, wx.EXPAND)
        detail = (
            "Variables: ${1} is the first matched part; ${script:name} is a script "
            "variable; ${mud:HEALTH} is data sent by the MUD."
            if captures else
            "Variables: ${script:name} is a script variable; ${mud:HEALTH} is data "
            "sent by the MUD."
        )
        grid.Add(wx.StaticText(self, label=""))
        help_text_ctrl = wx.StaticText(self, label=detail)
        help_text_ctrl.Wrap(480)
        grid.Add(help_text_ctrl, 1, wx.EXPAND)
        self._variable_button(grid, ctrl, "Insert a variable into the co&mmands...")
        return ctrl

    def _variable_button(self, grid: wx.FlexGridSizer, target: wx.TextCtrl, label: str) -> None:
        """A button, right after ``target``, that picks a variable and inserts its reference.

        One button per field rather than one for "whichever field had focus": tabbing to a
        shared button always passes through the field just before it, so a shared button
        could never reach the other one.
        """
        if self._variables is None:
            return
        button = wx.Button(self, label=label)
        button.Bind(wx.EVT_BUTTON, lambda _event: self._insert_variable(target))
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(button)

    def _insert_variable(self, target: wx.TextCtrl) -> None:
        if self._variables is None:
            return
        picker = VariablesDialog(self, self._variables, world_name="", pick=True)
        try:
            chosen = picker.chosen if picker.ShowModal() == wx.ID_OK else None
        finally:
            picker.Destroy()
        if chosen is not None:
            target.WriteText(chosen.reference)  # at the caret, like typing it
        # Back where the reference went, so it's read out -- after the picker's own
        # teardown. Ending a modal hands focus back to the control that opened it (this
        # button), and a SetFocus made before that has run can lose the race, leaving the
        # screen reader on the button with no word about what was inserted.
        wx.CallAfter(_focus_if_alive, target)

    def _slider(
        self, grid: wx.FlexGridSizer, label: str, value: int, low: int, high: int
    ) -> wx.Slider:
        grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        ctrl = wx.Slider(self, value=value, minValue=low, maxValue=high)
        ctrl.SetName(self._field_name(label))
        grid.Add(ctrl, 1, wx.EXPAND)
        return ctrl

    def _finish(self, outer: wx.BoxSizer, grid: wx.FlexGridSizer) -> None:
        outer.Add(grid, 1, wx.ALL | wx.EXPAND, 10)
        outer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 10)
        self.SetSizerAndFit(outer)


class TriggerEditorDialog(_RuleEditorBase):
    """Create/edit one user trigger: everything a scripted trigger can do, as fields."""

    def __init__(
        self, parent: wx.Window, pack_dir: Path, trigger: UserTrigger, *, variables=None
    ) -> None:
        super().__init__(
            parent, title="Trigger",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._variables = variables
        self._pack_dir = pack_dir
        self._enabled = trigger.enabled
        outer = wx.BoxSizer(wx.VERTICAL)
        grid = self._grid()
        self._pattern = self._text(
            grid, "When the MUD sends &text matching:", trigger.pattern
        )
        # Order must mirror MATCH_CHOICES: ("contains", "wildcard", "exact", "regex").
        self._match = wx.RadioBox(
            self, label="Ho&w to match",
            choices=[
                "The line contains this text",
                "Wildcard (* = anything, ? = one character)",
                "The whole line, exactly",
                "Regular expression",
            ],
            majorDimension=1, style=wx.RA_SPECIFY_COLS,
        )
        self._match.SetSelection(MATCH_CHOICES.index(trigger.match_kind()))
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(self._match, 1, wx.EXPAND)
        self._sound = self._text(grid, "&Sound file (optional):", trigger.sound)
        browse = wx.Button(self, label="&Browse for sound...")
        browse.Bind(wx.EVT_BUTTON, self._on_browse)
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(browse)
        self._volume = self._slider(grid, "&Volume (0-100):", trigger.volume, 0, 100)
        self._pan = self._slider(grid, "&Pan (-100 left to 100 right):", trigger.pan, -100, 100)
        self._loop = wx.CheckBox(self, label="&Loop the sound until stopped")
        self._loop.SetValue(trigger.loop)
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(self._loop)
        self._speak = self._text(grid, "Spea&k this text (%1 = first wildcard):", trigger.speak)
        self._variable_button(grid, self._speak, "Insert a v&ariable into the speech...")
        self._send = self._command_text(grid, trigger.send, captures=True)
        self._interrupt = wx.CheckBox(
            self, label="&Interrupt current speech the moment this fires"
        )
        self._interrupt.SetValue(trigger.interrupt)
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(self._interrupt)
        self._gag = wx.RadioBox(
            self, label="&What happens to the matched line",
            choices=["Read and show it normally", "Silence it but keep it in the window",
                     "Remove it entirely"],
            majorDimension=1, style=wx.RA_SPECIFY_COLS,
        )
        self._gag.SetSelection({"none": 0, "speech": 1, "line": 2}.get(trigger.gag, 0))
        outer_gag = self._gag
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(outer_gag, 1, wx.EXPAND)
        self._channel = self._text(
            grid, "Route to &channel (optional, e.g. chat):", trigger.channel
        )
        self._stop_channel = self._text(
            grid, "S&top a looping sound on this channel first (optional):", trigger.stop_channel
        )
        self._finish(outer, grid)

    def _on_browse(self, _event: wx.CommandEvent) -> None:
        dialog = wx.FileDialog(
            self, "Choose a sound file",
            wildcard="Sound files (*.wav;*.ogg;*.mp3;*.flac)|*.wav;*.ogg;*.mp3;*.flac|"
                     "All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dialog.ShowModal() == wx.ID_OK:
            self._sound.SetValue(copy_sound_into_pack(self._pack_dir, dialog.GetPath()))
        dialog.Destroy()

    def result(self) -> UserTrigger:
        match = MATCH_CHOICES[self._match.GetSelection()]
        return UserTrigger(
            pattern=self._pattern.GetValue().strip(),
            regex=(match == "regex"),  # kept in sync for files older builds read
            sound=self._sound.GetValue().strip(),
            volume=self._volume.GetValue(),
            pan=self._pan.GetValue(),
            loop=self._loop.GetValue(),
            speak=self._speak.GetValue().strip(),
            send=self._send.GetValue().strip(),
            gag=("none", "speech", "line")[self._gag.GetSelection()],
            channel=self._channel.GetValue().strip(),
            stop_channel=self._stop_channel.GetValue().strip(),
            match=match,
            interrupt=self._interrupt.GetValue(),
            enabled=self._enabled,
        )


class AliasEditorDialog(_RuleEditorBase):
    def __init__(self, parent: wx.Window, alias: UserAlias, *, variables=None) -> None:
        super().__init__(
            parent, title="Alias",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._variables = variables
        self._enabled = alias.enabled
        outer = wx.BoxSizer(wx.VERTICAL)
        grid = self._grid()
        self._pattern = self._text(
            grid, "&When I type (* = anything, e.g. sh *):", alias.pattern
        )
        self._regex = wx.CheckBox(self, label="Pattern is a &regular expression")
        self._regex.SetValue(alias.regex)
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(self._regex)
        self._send = self._command_text(grid, alias.send, captures=True)
        self._speak = self._text(grid, "Spea&k this confirmation (optional):", alias.speak)
        self._variable_button(grid, self._speak, "Insert a v&ariable into the speech...")
        self._finish(outer, grid)

    def result(self) -> UserAlias:
        return UserAlias(
            pattern=self._pattern.GetValue().strip(),
            regex=self._regex.GetValue(),
            send=self._send.GetValue().strip(),
            speak=self._speak.GetValue().strip(),
            enabled=self._enabled,
        )


class KeyEditorDialog(_RuleEditorBase):
    def __init__(
        self, parent: wx.Window, pack_dir: Path, key: UserKey, *, variables=None
    ) -> None:
        super().__init__(
            parent, title="Hotkey",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._variables = variables
        self._pack_dir = pack_dir
        self._enabled = key.enabled
        outer = wx.BoxSizer(wx.VERTICAL)
        grid = self._grid()
        grid.Add(wx.StaticText(self, label="&Press the key combination:"), 0,
                 wx.ALIGN_CENTER_VERTICAL)
        self._key = _KeyCaptureCtrl(self)
        self._key.SetValue(key.key)
        grid.Add(self._key, 1, wx.EXPAND)
        self._send = self._command_text(grid, key.send, captures=False)
        self._speak = self._text(grid, "Spea&k this text:", key.speak)
        self._variable_button(grid, self._speak, "Insert a v&ariable into the speech...")
        self._sound = self._text(grid, "Play this s&ound (optional):", key.sound)
        browse = wx.Button(self, label="&Browse for sound...")
        browse.Bind(wx.EVT_BUTTON, self._on_browse)
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(browse)
        self._finish(outer, grid)

    def _on_browse(self, _event: wx.CommandEvent) -> None:
        dialog = wx.FileDialog(
            self, "Choose a sound file",
            wildcard="Sound files (*.wav;*.ogg;*.mp3;*.flac)|*.wav;*.ogg;*.mp3;*.flac",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dialog.ShowModal() == wx.ID_OK:
            self._sound.SetValue(copy_sound_into_pack(self._pack_dir, dialog.GetPath()))
        dialog.Destroy()

    def result(self) -> UserKey:
        return UserKey(
            key=self._key.GetValue().strip(),
            send=self._send.GetValue().strip(),
            speak=self._speak.GetValue().strip(),
            sound=self._sound.GetValue().strip(),
            enabled=self._enabled,
        )


class ChannelEditorDialog(_RuleEditorBase):
    def __init__(self, parent: wx.Window, channel: UserChannel) -> None:
        super().__init__(parent, title="Channel")
        self._enabled = channel.enabled
        outer = wx.BoxSizer(wx.VERTICAL)
        grid = self._grid()
        self._name = self._text(grid, "Channel &name:", channel.name)
        self._speak = wx.CheckBox(self, label="&Speak lines on this channel")
        self._speak.SetValue(channel.speak)
        self._display = wx.CheckBox(self, label="Show lines in the &window")
        self._display.SetValue(channel.display)
        self._interrupt = wx.CheckBox(self, label="&Interrupt current speech (alerts)")
        self._interrupt.SetValue(channel.interrupt)
        for box in (self._speak, self._display, self._interrupt):
            grid.Add(wx.StaticText(self, label=""))
            grid.Add(box)
        self._finish(outer, grid)

    def result(self) -> UserChannel:
        return UserChannel(
            name=self._name.GetValue().strip(),
            speak=self._speak.GetValue(),
            display=self._display.GetValue(),
            interrupt=self._interrupt.GetValue(),
            enabled=self._enabled,
        )


def _rule_summary(kind: str, rule) -> str:
    """One spoken line per rule for the manager list."""
    # Plain words, no arrow/dash glyphs: these lines are read by a screen reader.
    status = "Disabled. " if not rule.enabled else ""
    commands = "; ".join(
        line.strip() for line in rule.send.splitlines() if line.strip()
    ) if hasattr(rule, "send") else ""
    if len(commands) > 180:
        commands = f"{commands[:177]}..."
    if kind == "trigger":
        actions = [part for part in (
            f"sound {Path(rule.sound).name}" if rule.sound else "",
            f"speak {rule.speak}" if rule.speak else "",
            f"send {commands}" if commands else "",
            "interrupts speech" if rule.interrupt else "",
            {"speech": "silence line", "line": "remove line"}.get(rule.gag, ""),
            f"channel {rule.channel}" if rule.channel else "",
        ) if part]
        how = rule.match_kind()
        label = f"{rule.pattern}" if how == "wildcard" else f"{rule.pattern} ({how})"
        return f"{status}Trigger: {label}: {'; '.join(actions) or 'no action'}"
    if kind == "alias":
        return f"{status}Alias: {rule.pattern} sends {commands or 'nothing'}"
    if kind == "key":
        action = commands or rule.speak or rule.sound or "nothing"
        return f"{status}Hotkey: {rule.key} runs {action}"
    return (
        f"{status}Channel: {rule.name}"
        f" ({'speaks' if rule.speak else 'silent'},"
        f" {'shown' if rule.display else 'hidden'}"
        f"{', interrupts' if rule.interrupt else ''})"
    )


class AutomationManagerDialog(wx.Dialog):
    """Manage every kind of per-world automation in one accessible dialog."""

    _CATEGORIES = (
        (
            "trigger", "Triggers — when the MUD sends text",
            "A trigger reacts to a matching line. It can send commands, speak text, "
            "play sound, hide the line, or route it to a channel.",
        ),
        (
            "alias", "Aliases — when I type a shortcut",
            "An alias replaces a short command you type with one or more MUD commands.",
        ),
        (
            "key", "Hotkeys — when I press a key",
            "A hotkey sends commands, speaks text or a value the MUD sent, or plays a "
            "sound when you press a chosen key combination.",
        ),
        (
            "channel", "Channels — how matching lines are handled",
            "A channel controls whether routed lines are spoken, shown, or allowed to "
            "interrupt current speech.",
        ),
        (
            "script", "Scripts — reusable Lua automation",
            "A script handles automation that needs decisions, timers, saved variables, "
            "or shared functions. Scripts load alphabetically.",
        ),
    )
    _CATEGORY_WORDS = {
        "trigger": "Triggers", "alias": "Aliases", "key": "Hotkeys",
        "channel": "Channels", "script": "Scripts",
    }
    _KIND_WORD = {
        "trigger": "Trigger", "alias": "Alias", "key": "Hotkey", "channel": "Channel",
    }
    _RULE_TYPES = {
        "trigger": UserTrigger, "alias": UserAlias,
        "key": UserKey, "channel": UserChannel,
    }
    _RULE_COLLECTIONS = {
        "trigger": "triggers", "alias": "aliases", "key": "keys", "channel": "channels",
    }
    _REQUIRED_FIELD = {
        "trigger": ("pattern", "text to match"),
        "alias": ("pattern", "the shortcut you will type"),
        "key": ("key", "a key combination"),
        "channel": ("name", "a name"),
    }
    _last_category = 0

    def __init__(self, parent: wx.Window, panel: SessionPanel, announce=None) -> None:
        super().__init__(
            parent, title=f"Automation Manager — {panel.world.name}", size=(760, 590),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._panel = panel
        self._announce = announce or (lambda _text: None)
        self._pack_dir = panel.app.user_rules_dir() if panel.app is not None else None
        self._rules = load_rules(self._pack_dir) if self._pack_dir else UserRules()
        self._selections: dict[str, int] = {}

        outer = wx.BoxSizer(wx.VERTICAL)
        introduction = wx.StaticText(
            self,
            label=(
                "Choose what you want to automate. Basic commands and Lua scripts live "
                "here together and are saved only for this world."
            ),
        )
        introduction.Wrap(700)
        outer.Add(introduction, 0, wx.ALL | wx.EXPAND, 10)

        category_row = wx.BoxSizer(wx.HORIZONTAL)
        category_row.Add(
            wx.StaticText(self, label="&Show:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6
        )
        self._category = wx.Choice(
            self, choices=[label for _kind, label, _description in self._CATEGORIES]
        )
        self._category.SetName("Automation type")
        self._category.SetSelection(min(self._last_category, len(self._CATEGORIES) - 1))
        self._category.Bind(wx.EVT_CHOICE, self._on_category)
        category_row.Add(self._category, 1, wx.EXPAND)
        outer.Add(category_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self._description = wx.StaticText(self, label="")
        self._description.Wrap(700)
        outer.Add(self._description, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        self._items_label = wx.StaticText(self, label="&Items:")
        outer.Add(self._items_label, 0, wx.LEFT | wx.RIGHT, 10)
        self._list = wx.ListBox(self, style=wx.LB_SINGLE)
        self._list.Bind(wx.EVT_LISTBOX, self._on_list_selection)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_edit)
        self._list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        outer.Add(self._list, 1, wx.ALL | wx.EXPAND, 10)

        button_grid = wx.GridSizer(0, 4, 6, 6)
        self._buttons: dict[str, wx.Button] = {}
        for key, label, handler in (
            ("new", "&New...", self._on_new),
            ("edit", "&Edit...", self._on_edit),
            ("duplicate", "D&uplicate...", self._on_duplicate),
            ("toggle", "Disa&ble", self._on_toggle),
            ("rename", "Rena&me...", self._on_rename),
            ("delete", "&Delete", self._on_delete),
            ("reload", "&Reload scripts", self._on_reload_scripts),
            ("folder", "Open scripts &folder", self._on_open_scripts_folder),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            button_grid.Add(button, 0, wx.EXPAND)
            self._buttons[key] = button
        outer.Add(button_grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        help_button = wx.Button(self, label="Automation &help...")
        help_button.Bind(wx.EVT_BUTTON, self._on_help)
        bottom.Add(help_button)
        bottom.AddStretchSpacer()
        close = wx.Button(self, wx.ID_CANCEL, "Cl&ose")
        bottom.Add(close)
        outer.Add(bottom, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        self.SetSizer(outer)
        self.SetMinSize((660, 500))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_manager_key)

        self._active_kind = self._kind()
        self._refresh()

    def _kind(self) -> str:
        return self._CATEGORIES[self._category.GetSelection()][0]

    def _collection(self, kind: str) -> list:
        return {
            "trigger": self._rules.triggers, "alias": self._rules.aliases,
            "key": self._rules.keys, "channel": self._rules.channels,
        }[kind]

    def _refresh(self, select: int | str | None = None, *, focus: bool = False) -> None:
        kind = self._kind()
        _kind, _label, description = self._CATEGORIES[self._category.GetSelection()]
        self._description.SetLabel(description)
        self._description.Wrap(max(self.GetClientSize().GetWidth() - 40, 300))

        if kind == "script":
            labels = user_scripts.list_scripts(self._pack_dir) if self._pack_dir else []
        else:
            labels = [_rule_summary(kind, rule) for rule in self._collection(kind)]
        previous = self._selections.get(kind, 0)
        if isinstance(select, int):
            previous = select
        elif isinstance(select, str) and select in labels:
            previous = labels.index(select)
        self._list.Set(labels)
        words = self._CATEGORY_WORDS[kind]
        self._items_label.SetLabel(f"&{words}:")
        self._list.SetName(f"{words} for this world, {len(labels)} items")
        if labels:
            selection = min(max(previous, 0), len(labels) - 1)
            self._list.SetSelection(selection)
            self._selections[kind] = selection
        else:
            self._selections[kind] = 0
        self._update_buttons()
        self.Layout()
        if focus:
            self._list.SetFocus()

    def _selected_index(self) -> int | None:
        index = self._list.GetSelection()
        return index if index != wx.NOT_FOUND else None

    def _selected_script(self) -> str | None:
        index = self._selected_index()
        return self._list.GetString(index) if index is not None else None

    def _on_list_selection(self, _event: wx.CommandEvent) -> None:
        if (index := self._selected_index()) is not None:
            self._selections[self._kind()] = index
        self._update_buttons()

    def _update_buttons(self) -> None:
        kind = self._kind()
        index = self._selected_index()
        has_item = index is not None
        is_script = kind == "script"
        singular = "script" if is_script else self._KIND_WORD[kind].lower()
        self._buttons["new"].SetLabel(f"&New {singular}...")
        self._buttons["edit"].Enable(has_item)
        self._buttons["duplicate"].Enable(has_item)
        self._buttons["delete"].Enable(has_item)
        self._buttons["toggle"].Enable(has_item and not is_script)
        self._buttons["rename"].Enable(has_item and is_script)
        self._buttons["reload"].Enable(is_script)
        self._buttons["folder"].Enable(is_script)
        if has_item and not is_script:
            enabled = self._collection(kind)[index].enabled
            self._buttons["toggle"].SetLabel("Disa&ble" if enabled else "E&nable")
        else:
            self._buttons["toggle"].SetLabel("Disa&ble")

    def _on_category(self, _event: wx.CommandEvent) -> None:
        if (index := self._selected_index()) is not None:
            self._selections[self._active_kind] = index
        self._last_category = self._category.GetSelection()
        type(self)._last_category = self._last_category
        self._active_kind = self._kind()
        self._refresh(focus=True)

    def _on_manager_key(self, event: wx.KeyEvent) -> None:
        code = event.GetKeyCode()
        if event.ControlDown() and not event.AltDown() and not event.ShiftDown():
            if ord("1") <= code <= ord("5"):
                self._category.SetSelection(code - ord("1"))
                self._on_category(event)
                return
            if code == ord("N"):
                self._on_new(event)
                return
        event.Skip()

    def _on_list_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_edit(event)
            return
        if event.GetKeyCode() == wx.WXK_DELETE:
            self._on_delete(event)
            return
        if event.GetKeyCode() == wx.WXK_F2 and self._kind() == "script":
            self._on_rename(event)
            return
        event.Skip()

    def _save_and_reload_rules(self, select: int | None = None) -> bool:
        if self._pack_dir is None:
            return False
        try:
            register_rules(
                ScriptApi(
                    AutomationEngine(), source="user-rule-validation",
                    base_dir=str(self._pack_dir),
                ),
                self._rules,
            )
            save_rules(self._pack_dir, self._rules)
        except Exception as error:  # noqa: BLE001 - validation/storage errors stay in editor
            wx.MessageBox(
                f"Automation not saved: {error}", "Automation Manager",
                wx.OK | wx.ICON_ERROR, self,
            )
            self._rules = load_rules(self._pack_dir)
            self._refresh()
            return False
        if self._panel.app is not None:
            self._panel._loop.call_soon_threadsafe(self._panel.app.reload_user_rules)
        self._refresh(select)
        return True

    def _edit_rule(self, kind: str, rule):
        variables = self._panel.variable_snapshot
        if kind == "trigger":
            dialog = TriggerEditorDialog(self, self._pack_dir, rule, variables=variables)
        elif kind == "alias":
            dialog = AliasEditorDialog(self, rule, variables=variables)
        elif kind == "key":
            dialog = KeyEditorDialog(self, self._pack_dir, rule, variables=variables)
        else:
            dialog = ChannelEditorDialog(self, rule)
        try:
            while True:
                if dialog.ShowModal() != wx.ID_OK:
                    return None
                result = dialog.result()
                if self._complete(kind, result) and self._valid_rule(kind, result):
                    if self._key_is_free(kind, result):
                        return result
        finally:
            dialog.Destroy()

    def _valid_rule(self, kind: str, rule: object) -> bool:
        if self._pack_dir is None:
            return False
        candidate = UserRules()
        getattr(candidate, self._RULE_COLLECTIONS[kind]).append(
            replace(rule, enabled=True)
        )
        try:
            register_rules(
                ScriptApi(
                    AutomationEngine(), source="user-rule-validation",
                    base_dir=str(self._pack_dir),
                ),
                candidate,
            )
        except Exception as error:  # noqa: BLE001 - keep the user's editor open on any error
            word = self._KIND_WORD[kind].lower()
            wx.MessageBox(
                f"This {word} needs a change before it can be saved: {error}",
                "Check automation", wx.OK | wx.ICON_ERROR, self,
            )
            return False
        return True

    def _key_is_free(self, kind: str, rule: object) -> bool:
        if kind != "key":
            return True
        reason = _hotkey_taken(rule.key, self._panel._keymap)
        if reason is None:
            return True
        wx.MessageBox(f"Not saved: {reason}", "Hotkey", wx.OK | wx.ICON_INFORMATION, self)
        return False

    def _complete(self, kind: str, rule: object) -> bool:
        attr, label = self._REQUIRED_FIELD[kind]
        if getattr(rule, attr):
            return True
        word = self._KIND_WORD[kind].lower()
        wx.MessageBox(
            f"Not saved: this {word} needs {label}.", "Incomplete automation",
            wx.OK | wx.ICON_INFORMATION, self,
        )
        return False

    def _on_new(self, _event: wx.CommandEvent) -> None:
        kind = self._kind()
        if kind == "script":
            self._new_script()
            return
        result = self._edit_rule(kind, self._RULE_TYPES[kind]())
        if result is None:
            return
        items = self._collection(kind)
        items.append(result)
        if self._save_and_reload_rules(len(items) - 1):
            self._announce(f"{self._KIND_WORD[kind]} added.")

    def _on_edit(self, _event: wx.CommandEvent) -> None:
        if self._kind() == "script":
            self._edit_selected_script()
            return
        if (index := self._selected_index()) is None:
            return
        kind = self._kind()
        items = self._collection(kind)
        result = self._edit_rule(kind, items[index])
        if result is not None:
            items[index] = result
            if self._save_and_reload_rules(index):
                self._announce(f"{self._KIND_WORD[kind]} updated.")

    def _on_duplicate(self, _event: wx.CommandEvent) -> None:
        if (index := self._selected_index()) is None:
            return
        kind = self._kind()
        if kind == "script":
            name = self._selected_script()
            if name is None or self._pack_dir is None:
                return
            try:
                source = user_scripts.read_script(self._pack_dir, name)
            except OSError as error:
                wx.MessageBox(str(error), "Couldn't open script", wx.OK | wx.ICON_ERROR, self)
                return
            self._new_script(source, default_name=f"{Path(name).stem}-copy.lua")
            return
        items = self._collection(kind)
        result = self._edit_rule(kind, replace(items[index]))
        if result is None:
            return
        items.append(result)
        if self._save_and_reload_rules(len(items) - 1):
            self._announce(f"{self._KIND_WORD[kind]} duplicated.")

    def _on_toggle(self, _event: wx.CommandEvent) -> None:
        kind = self._kind()
        if kind == "script" or (index := self._selected_index()) is None:
            return
        items = self._collection(kind)
        items[index] = replace(items[index], enabled=not items[index].enabled)
        if self._save_and_reload_rules(index):
            state = "enabled" if items[index].enabled else "disabled"
            self._announce(f"{self._KIND_WORD[kind]} {state}.")

    def _on_delete(self, _event: wx.CommandEvent) -> None:
        if self._kind() == "script":
            self._delete_script()
            return
        if (index := self._selected_index()) is None:
            return
        kind = self._kind()
        word = self._KIND_WORD[kind]
        if wx.MessageBox(
            f"Delete this {word.lower()}?", "Delete automation",
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        del self._collection(kind)[index]
        if self._save_and_reload_rules(index):
            self._announce(f"{word} deleted.")

    def _request_script_reload(self) -> None:
        if self._panel.app is not None:
            self._panel._loop.call_soon_threadsafe(
                self._panel.app.reload_user_scripts, True
            )

    def _save_script(self, name: str, source: str) -> bool:
        if self._pack_dir is None:
            return False
        try:
            user_scripts.validate_script(self._pack_dir, source)
            user_scripts.save_script(self._pack_dir, name, source)
        except Exception as error:  # noqa: BLE001 - syntax/storage errors stay in editor
            wx.MessageBox(
                f"Script not saved: {type(error).__name__}: {error}",
                "Automation script", wx.OK | wx.ICON_ERROR, self,
            )
            return False
        self._announce(f"{name} saved. Reloading automation scripts.")
        self._request_script_reload()
        self._refresh(name)
        return True

    def _edit_script(self, name: str, source: str) -> bool:
        current = source
        while True:
            dialog = AutomationScriptEditorDialog(self, name, current)
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return False
                current = dialog.source()
            finally:
                dialog.Destroy()
            if self._save_script(name, current):
                return True

    def _new_script(
        self, source: str | None = None, *, default_name: str | None = None
    ) -> None:
        if self._pack_dir is None:
            return
        proposed = default_name or user_scripts.DEFAULT_SCRIPT_NAME
        while True:
            dialog = wx.TextEntryDialog(
                self, "Name the new Lua script. Scripts load alphabetically.",
                "New automation script", value=proposed,
            )
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                proposed = dialog.GetValue()
            finally:
                dialog.Destroy()
            try:
                name = user_scripts.normalize_script_name(proposed)
            except ValueError as error:
                wx.MessageBox(str(error), "Invalid script name", wx.OK | wx.ICON_ERROR, self)
                continue
            existing = {item.casefold() for item in user_scripts.list_scripts(self._pack_dir)}
            if name.casefold() in existing:
                wx.MessageBox(
                    f"A script named {name} already exists.", "Automation script",
                    wx.OK | wx.ICON_INFORMATION, self,
                )
                proposed = name
                continue
            break
        self._edit_script(name, user_scripts.DEFAULT_SCRIPT if source is None else source)

    def _edit_selected_script(self) -> None:
        if self._pack_dir is None or (name := self._selected_script()) is None:
            return
        try:
            source = user_scripts.read_script(self._pack_dir, name)
        except OSError as error:
            wx.MessageBox(str(error), "Couldn't open script", wx.OK | wx.ICON_ERROR, self)
            return
        self._edit_script(name, source)

    def _on_rename(self, _event: wx.CommandEvent) -> None:
        if self._kind() != "script" or self._pack_dir is None:
            return
        name = self._selected_script()
        if name is None:
            return
        proposed = name
        while True:
            dialog = wx.TextEntryDialog(
                self, "New script filename:", "Rename automation script", value=proposed,
            )
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                proposed = dialog.GetValue()
            finally:
                dialog.Destroy()
            try:
                renamed = user_scripts.rename_script(self._pack_dir, name, proposed)
            except ValueError as error:
                wx.MessageBox(str(error), "Couldn't rename script", wx.OK | wx.ICON_ERROR, self)
                continue
            except OSError as error:
                wx.MessageBox(str(error), "Couldn't rename script", wx.OK | wx.ICON_ERROR, self)
                return
            break
        if renamed == name:
            return
        self._announce(f"{name} renamed to {renamed}. Reloading automation scripts.")
        self._request_script_reload()
        self._refresh(renamed)

    def _delete_script(self) -> None:
        if self._pack_dir is None or (name := self._selected_script()) is None:
            return
        if wx.MessageBox(
            f"Delete {name}?", "Delete automation script",
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        try:
            user_scripts.delete_script(self._pack_dir, name)
        except OSError as error:
            wx.MessageBox(str(error), "Couldn't delete script", wx.OK | wx.ICON_ERROR, self)
            return
        self._announce(f"{name} deleted. Reloading automation scripts.")
        self._request_script_reload()
        self._refresh()

    def _on_reload_scripts(self, _event: wx.CommandEvent) -> None:
        if self._kind() != "script":
            return
        selected = self._selected_script()
        self._refresh(selected)
        self._request_script_reload()

    def _on_open_scripts_folder(self, _event: wx.CommandEvent) -> None:
        if self._kind() != "script" or self._pack_dir is None:
            return
        folder = user_scripts.scripts_dir(self._pack_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            opened = wx.LaunchDefaultApplication(str(folder))
        except OSError as error:
            opened = False
            detail = f": {error}"
        else:
            detail = ""
        if not opened:
            wx.MessageBox(
                f"Couldn't open {folder}{detail}", "Automation scripts",
                wx.OK | wx.ICON_ERROR, self,
            )

    def _on_help(self, _event: wx.CommandEvent) -> None:
        dialog = HelpDialog(self, "Automation help", help_text.SCRIPTING)
        dialog.ShowModal()
        dialog.Destroy()


class AutomationScriptEditorDialog(wx.Dialog):
    """One accessible multiline Lua editor; its manager owns validation and storage."""

    def __init__(self, parent: wx.Window, name: str, source: str) -> None:
        super().__init__(
            parent, title=f"Edit automation script — {name}", size=(720, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(
            wx.StaticText(
                self,
                label="&Lua source (scripts are sandboxed):",
            ),
            0, wx.LEFT | wx.TOP, 10,
        )
        self._source = wx.TextCtrl(
            self, value=source,
            style=wx.TE_MULTILINE | wx.TE_DONTWRAP | wx.TE_RICH2,
        )
        self._source.SetName(f"{name} Lua source")
        outer.Add(self._source, 1, wx.ALL | wx.EXPAND, 10)
        buttons = wx.StdDialogButtonSizer()
        save = wx.Button(self, wx.ID_OK, "&Save and reload")
        cancel = wx.Button(self, wx.ID_CANCEL, "&Cancel")
        buttons.AddButton(save)
        buttons.AddButton(cancel)
        buttons.Realize()
        outer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizer(outer)
        self.SetMinSize((600, 420))
        self._source.SetFocus()

    def source(self) -> str:
        return self._source.GetValue()


class HelpDialog(wx.Dialog):
    """One help page: a read-only text box NVDA reads line by line, plus Close.

    The same accessibility shape as the release-notes dialog: label precedes the
    control, SetName gives it a spoken name, and focus starts at the TOP of the
    text (a fresh TextCtrl caret sits at the end, which reads as an empty line).
    """

    def __init__(self, parent: wx.Window, title: str, text: str) -> None:
        super().__init__(
            parent, title=title, size=(640, 480),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=f"&{title}:"), 0, wx.LEFT | wx.TOP, 8)
        body = wx.TextCtrl(
            self, value=text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        body.SetName(title)
        body.SetInsertionPoint(0)
        sizer.Add(body, 1, wx.EXPAND | wx.ALL, 8)
        close = wx.Button(self, wx.ID_CANCEL, "Cl&ose")
        close.SetDefault()
        sizer.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(sizer)
        body.SetFocus()


_ID_UPDATE_NOW = wx.ID_HIGHEST + 101
_ID_RELEASE_PAGE = wx.ID_HIGHEST + 102
_ID_SNOOZE = wx.ID_HIGHEST + 103
_ID_SKIP = wx.ID_HIGHEST + 104


class UpdateNotificationDialog(wx.Dialog):
    """Announce a newer genericMud and offer what to do about it.

    Follows WorldDialog's accessibility pattern: a StaticText precedes each control and
    every control gets SetName so NVDA reads it. Release notes sit in a focusable read-only
    text box the user can review line by line. ShowModal returns one of the module ``_ID_*``
    actions, or wx.ID_CANCEL if the dialog is closed. "Update Now" only appears on a build
    that can self-replace; elsewhere the release page is the only install route.
    """

    def __init__(self, parent: wx.Window, info: dict, current: str) -> None:
        super().__init__(parent, title="genericMud update available")
        heading = f"genericMud {info['tag']} is available. You have {current}."
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=heading), 0, wx.ALL, 8)

        sizer.Add(wx.StaticText(self, label="Release &notes:"), 0, wx.LEFT, 8)
        notes = wx.TextCtrl(
            self, value=info.get("notes") or "(no release notes)",
            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(460, 180),
        )
        notes.SetName("Release notes")
        sizer.Add(notes, 1, wx.EXPAND | wx.ALL, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        default_button = None
        if self_update.can_self_replace():
            default_button = self._button(buttons, "&Update Now", _ID_UPDATE_NOW)
        page_button = self._button(buttons, "Open Release &Page", _ID_RELEASE_PAGE)
        self._button(buttons, "&Remind Me Later", _ID_SNOOZE)
        self._button(buttons, "&Skip This Version", _ID_SKIP)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "&Close"), 0)
        sizer.Add(buttons, 0, wx.ALL, 8)

        (default_button or page_button).SetDefault()
        self.SetSizerAndFit(sizer)

    def _button(self, sizer: wx.BoxSizer, label: str, action_id: int) -> wx.Button:
        button = wx.Button(self, action_id, label)
        button.Bind(wx.EVT_BUTTON, lambda _event, a=action_id: self.EndModal(a))
        sizer.Add(button, 0, wx.RIGHT, 4)
        return button


class UpdateProgressDialog(wx.Dialog):
    """Self-update progress: a status log, a gauge, spoken phase changes, and Cancel.

    Division of labour between the two channels, which is easy to get wrong in both
    directions: the gauge carries *how far along*, speech carries *which phase*.

    Don't speak percentages. A wx.Gauge is a native msctls_progress32 reporting
    ROLE_SYSTEM_PROGRESSBAR, so NVDA already tracks it -- beeping at every 1% by
    default, or speaking every 10% if the user chose speech instead. Announcing our
    own milestones on top of that is both redundant and coarser than what the user
    asked for, and it talks over a deliberate "progress bar output: off".

    Do speak phase changes. _status_log is an unfocused read-only TextCtrl, and wx has
    no live-region equivalent on MSW, so appended text reaches no screen reader. Speech
    is the only channel for "download finished, now extracting" -- which no
    accessibility API infers from a gauge sitting at 100%.

    Deliberately a plain owned dialog, NOT wx.ProgressDialog: on MSW that runs a native
    task dialog on its own thread and PD_AUTO_HIDE dismisses it the instant Update()
    reaches the maximum. Tearing that window down while the screen reader still has COM
    calls in flight against it faulted the whole process (RPC_E_SERVER_DIED_DNE /
    RPC_E_DISCONNECTED, then an access violation) right as extraction began. Keeping the
    dialog on the main thread and destroying it only from _on_update_finished removes the
    race entirely; VaultBrowserDialog survives far larger downloads with this same shape.

    Cancel sets the shared event; the download worker notices at its next progress
    callback. After the last callback (download done, extraction running) cancellation no
    longer takes effect -- the old dialog had the same window, now stated instead of implied.
    """

    def __init__(self, parent: wx.Window, tag: str, announce, cancelled: threading.Event) -> None:
        super().__init__(parent, title="Updating genericMud")
        self._announce = announce
        self._cancelled = cancelled
        self._setup_announced = False  # one-time "setting up" line after the last callback

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label="S&tatus:"), 0, wx.LEFT | wx.TOP, 8)
        self._status_log = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(460, 90)
        )
        self._status_log.SetName("Status")
        sizer.Add(self._status_log, 1, wx.EXPAND | wx.ALL, 8)

        self._gauge = wx.Gauge(self, range=100)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.ALL, 8)

        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, "&Cancel")
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        sizer.Add(self._cancel_btn, 0, wx.ALL, 8)
        self.Bind(wx.EVT_CLOSE, self._on_cancel)
        self.SetSizerAndFit(sizer)

        self._status(f"Downloading genericMud {tag}.")

    def _status(self, message: str) -> None:  # main thread only
        self._status_log.AppendText(message + "\n")
        self._announce(message)

    def pump(self, done: int, total: int) -> None:  # main thread, via wx.CallAfter
        """Reflect download progress; total may be 0 when no size was advertised."""
        if total <= 0:
            self._gauge.Pulse()
            return
        pct = min(done * 100 // total, 100)
        self._gauge.SetValue(pct)  # NVDA reports this itself; see the class docstring
        if pct >= 100 and not self._setup_announced:
            self._setup_announced = True
            self._status("Download finished. Setting up the update.")

    def _on_cancel(self, event) -> None:
        if isinstance(event, wx.CloseEvent) and event.CanVeto():
            event.Veto()  # the frame destroys us once the worker unwinds; don't die early
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        self._cancel_btn.Disable()
        self._status("Cancelling. This takes effect at the next download step.")


class GenericMudFrame(wx.Frame):
    def __init__(self, loop: asyncio.AbstractEventLoop, keymap: dict):
        super().__init__(None, title="genericMud", size=(900, 600))
        self._loop = loop
        self._keymap = keymap
        self._packs = PackStore(config_dir() / "soundpacks")
        # Logins go to the OS credential store where one is available, and any older plaintext
        # logins are migrated out of the file on the way past. Where there is none, the reason
        # is kept and spoken below: a silent fallback to a readable file is the one thing a
        # user must never have to discover later.
        self._credentials, plain_reason = open_credential_store(config_dir() / "credentials.json")
        self._credentials_note = (
            None
            if plain_reason is None
            else f"Login details are stored unencrypted in the credentials file: {plain_reason}."
        )
        self._hub = SessionHub()  # shared across all open sessions for cross-character play
        self._announcer = make_voice_backend()  # speaks UI status for screen-reader users
        if self._credentials_note is not None:
            self._announcer.speak(self._credentials_note)
        self._diag = make_diagnostic_log()  # one sound-path trace file for the whole process
        # Before any session opens: move each saved world's files off the ASCII-only name an
        # older build filed them under, so its rules and map are where it looks for them.
        for change in migrate_world_files(config_dir(), [world.name for world in load_worlds()]):
            if self._diag is not None:
                self._diag.event("worlds.migrate", change=change)

        menubar = wx.MenuBar()
        # File is world-level actions. Installed third-party packs stay under Soundpacks;
        # user-authored rules and scripts live under Automation.
        file_menu = wx.Menu()
        new_world_item = file_menu.Append(wx.ID_ANY, "&New World...\tCtrl+N")
        connect_item = file_menu.Append(wx.ID_ANY, "&Connect to Saved World...\tCtrl+O")
        disconnect_item = file_menu.Append(wx.ID_ANY, _connection_menu_label(True))
        self._connection_item = disconnect_item  # relabelled as the active tab connects/drops
        close_item = file_menu.Append(wx.ID_ANY, "Close &Tab\tCtrl+W")
        export_item = file_menu.Append(
            wx.ID_ANY, "&Export This World...",
            "Save this world's rules, scripts, sounds, and connection details as one zip",
        )
        import_item = file_menu.Append(
            wx.ID_ANY, "&Import a World...",
            "Load a world zip a friend sent you; it appears in the Connect dialog",
        )
        file_menu.AppendSeparator()
        quit_item = file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl+Q")
        menubar.Append(file_menu, "&File")

        # Mnemonic Alt+P: doesn't collide with a command-box keymap binding (unlike Alt+R,
        # which is nav:retrace). Item labels spell out how each install differs.
        packs_menu = wx.Menu()
        packs_item = packs_menu.Append(wx.ID_ANY, "&Manage installed soundpacks...\tCtrl+P")
        browse_item = packs_menu.Append(
            wx.ID_ANY, "Browse soundpacks &online...\tCtrl+Shift+B"
        )
        setup_item = packs_menu.Append(wx.ID_ANY, "Set &up a soundpack from a folder...")
        packs_menu.AppendSeparator()
        notepads_item = packs_menu.Append(
            wx.ID_ANY, "Soundpack &notepads...",
            "Read what this world's soundpacks have written to their notepads",
        )
        menubar.Append(packs_menu, "Sound&packs")

        automation_menu = wx.Menu()
        automation_item = automation_menu.Append(
            wx.ID_ANY, "&Manage automation...\tCtrl+B"
        )
        variables_item = automation_menu.Append(
            wx.ID_ANY, "MUD &variables...\tCtrl+Shift+V",
            "Read the values this MUD has sent (GMCP, MSDP, MSSP) and the ones scripts saved",
        )
        automation_menu.AppendSeparator()
        automation_help_item = automation_menu.Append(wx.ID_ANY, "Automation &help...")
        menubar.Append(automation_menu, "&Automation")

        self._prefs = load_ui_prefs()
        self._app_focused = True  # tracked via EVT_ACTIVATE for background silence

        view_menu = wx.Menu()
        self._self_voice_item = view_menu.AppendCheckItem(wx.ID_ANY, "Self-&voice\tCtrl+M")
        self._self_voice_item.Check(True)
        self._self_voice = True
        self._bg_silence_item = view_menu.AppendCheckItem(
            wx.ID_ANY, "&Background silence",
            "Stay quiet while another window has focus; triggers and sounds keep running",
        )
        self._bg_silence_item.Check(self._prefs.background_silence)
        self._numpad_item = view_menu.AppendCheckItem(
            wx.ID_ANY, "&Numpad compass walking",
            "Numpad walks: 8/2/4/6 and diagonals, 5 or 0 look, period scans, minus up, plus down",
        )
        self._numpad_item.Check(self._prefs.numpad_compass)
        menubar.Append(view_menu, "&View")

        help_menu = wx.Menu()
        started_item = help_menu.Append(wx.ID_ANY, "&Getting Started...")
        shortcuts_item = help_menu.Append(wx.ID_ANY, "&Keyboard Shortcuts...")
        updates_item = help_menu.Append(wx.ID_ANY, "Check for &Updates...")
        about_item = help_menu.Append(wx.ID_ABOUT, "&About genericMud...")
        menubar.Append(help_menu, "&Help")

        self.SetMenuBar(menubar)
        self.Bind(
            wx.EVT_MENU,
            lambda _e: self._show_help("Getting started", help_text.GETTING_STARTED),
            started_item,
        )
        self.Bind(
            wx.EVT_MENU,
            lambda _e: self._show_help("Keyboard shortcuts", help_text.KEYBOARD_SHORTCUTS),
            shortcuts_item,
        )
        self.Bind(wx.EVT_MENU, self._on_about, about_item)
        self.Bind(wx.EVT_MENU, self._on_new_world, new_world_item)
        self.Bind(wx.EVT_MENU, self._on_connect, connect_item)
        self.Bind(wx.EVT_MENU, self._on_disconnect, disconnect_item)
        self.Bind(wx.EVT_MENU_OPEN, self._on_menu_open)  # label read aloud must be current
        self.Bind(wx.EVT_MENU, self._on_close_tab, close_item)
        self.Bind(wx.EVT_MENU, self._on_manage_packs, packs_item)
        self.Bind(wx.EVT_MENU, self._on_setup_pack, setup_item)
        self.Bind(wx.EVT_MENU, self._on_browse_online, browse_item)
        self.Bind(wx.EVT_MENU, self._on_pack_notepads, notepads_item)
        self.Bind(wx.EVT_MENU, self._on_export_world, export_item)
        self.Bind(wx.EVT_MENU, self._on_import_world, import_item)
        self.Bind(wx.EVT_MENU, self._on_automation_manager, automation_item)
        self.Bind(wx.EVT_MENU, self._on_mud_variables, variables_item)
        self.Bind(
            wx.EVT_MENU,
            lambda _e: self._show_help("Automation help", help_text.SCRIPTING),
            automation_help_item,
        )
        self.Bind(wx.EVT_MENU, lambda _e: self.check_for_updates(manual=True), updates_item)
        self.Bind(wx.EVT_MENU, lambda _e: self.Close(), quit_item)
        self.Bind(wx.EVT_MENU, self._on_toggle_self_voice, self._self_voice_item)
        self.Bind(wx.EVT_MENU, self._on_toggle_bg_silence, self._bg_silence_item)
        self.Bind(wx.EVT_MENU, self._on_toggle_numpad, self._numpad_item)

        self.book = wx.Simplebook(self)  # no tab strip -> nothing in the keyboard Tab order
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)  # Ctrl+Tab cycles sessions
        self.Bind(wx.EVT_CLOSE, self._on_frame_close)  # confirm + disconnect before exit
        self.Bind(wx.EVT_ACTIVATE, self._on_app_activate)  # background-silence focus tracking

        self._update_progress_dialog: wx.ProgressDialog | None = None
        self._update_cancelled = threading.Event()
        self._alive = True  # an in-flight update callback must not touch the frame after close

    def _on_char_hook(self, event: wx.KeyEvent) -> None:
        # Grab Ctrl+Tab before the focused control sees it; let everything else
        # (crucially plain Tab, which traverses Output <-> Command) fall through.
        if event.GetKeyCode() == wx.WXK_TAB and event.ControlDown():
            count = self.book.GetPageCount()
            if count > 1:
                step = -1 if event.ShiftDown() else 1
                self._switch_session((self.book.GetSelection() + step) % count)
            return  # swallow Ctrl+Tab
        event.Skip()

    def _switch_session(self, index: int) -> None:
        self.book.ChangeSelection(index)  # ChangeSelection: no page-changed event to handle
        self._update_active()
        self._update_focus()
        # Focus the new page's command box; NVDA reads its name, announcing the session.
        self.book.GetPage(index).input.SetFocus()

    def open_session(self, world: World) -> None:
        panel = SessionPanel(
            self.book, self._loop, self._keymap, world,
            self._packs, self._credentials, self._hub, self._diag,
            prefs=self._prefs, on_pref=self._on_engine_pref,
        )
        self.book.AddPage(panel, world.name, select=True)
        panel.input.SetFocus()
        self._update_active()
        self._update_focus()
        # Opening any menu with no tab open greys the File item out, and a greyed item's
        # Ctrl+D never fires. Bring it back with the tab, not at the next menu opening.
        self._refresh_connection_item()

    def _on_engine_pref(self, attr: str, value: bool) -> None:  # loop thread
        """A keymap toggle changed a speech pref; persist it (prefs mutate on the wx thread)."""
        wx.CallAfter(self._save_pref, attr, value)

    def _save_pref(self, attr: str, value: bool) -> None:
        setattr(self._prefs, attr, value)
        save_ui_prefs(self._prefs)

    def _on_app_activate(self, event: wx.ActivateEvent) -> None:
        self._app_focused = event.GetActive()
        if self._prefs.background_silence:
            self._update_active()
        # Which world is in front once the activation has settled: one of our own dialogs
        # coming up leaves it in front, another program doesn't (see _world_in_front).
        wx.CallAfter(self._update_focus)
        event.Skip()

    def _world_in_front(self) -> bool:
        """Whether the player is in genericMud's main window: genericMud is the program in
        front, with the main window or a dialog over it active, as MUSHclient's own dialogs
        leave a world in front. A native message box is no window of ours to wx, so it's
        asked of the app rather than the active window. Another program, or a pack's notepad
        window, puts the world behind, as a notepad does in MUSHclient."""
        app = wx.GetApp()
        if app is None or not app.IsActive():
            return False
        return not isinstance(wx.GetActiveWindow(), PackNotepadFrame)

    def _update_focus(self) -> None:
        """Tell each session whether its world is the one in front: the selected tab, while
        _world_in_front. Packs hear it as MUSHclient's focus events."""
        if not self._alive:
            return
        in_front = self._world_in_front()
        selected = self.book.GetSelection()
        for index in range(self.book.GetPageCount()):
            self.book.GetPage(index).set_focused(in_front and index == selected)

    def _on_toggle_bg_silence(self, _event: wx.CommandEvent) -> None:
        self._save_pref("background_silence", self._bg_silence_item.IsChecked())
        self._update_active()
        self.announce(
            "Background silence on. genericMud stays quiet while you're in another window."
            if self._prefs.background_silence else "Background silence off."
        )

    def _on_toggle_numpad(self, _event: wx.CommandEvent) -> None:
        self._save_pref("numpad_compass", self._numpad_item.IsChecked())
        self.announce(
            "Numpad compass on." if self._prefs.numpad_compass else "Numpad compass off."
        )

    def _on_new_world(self, _event: wx.CommandEvent) -> None:
        self._show_world_dialog()

    def _show_world_dialog(
        self,
        *,
        initial: World | None = None,
        title: str = "New World",
        connect: bool = True,
        original_name: str | None = None,
        show_save: bool = True,
        lock_name: bool = False,
    ) -> bool:
        dialog = WorldDialog(
            self,
            initial,
            title=title,
            save_default=True,
            show_save=show_save,
            lock_name=lock_name,
            credentials=self._credentials,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return False
            world = dialog.get_world()
            saved = True
            if dialog.should_save():
                saved = self._save_world(world, replacing=original_name)
            if saved and original_name is not None:
                self._apply_to_open_tabs(original_name, world)
            if connect:
                self.open_session(world)
            return saved
        finally:
            dialog.Destroy()

    def _apply_to_open_tabs(self, name: str, world: World) -> None:
        """An edited world's command stacking and logging take effect in its open tabs at
        once; the rest (host, encoding, sounds) when it next connects."""
        for index in range(self.book.GetPageCount()):
            panel = self.book.GetPage(index)
            if isinstance(panel, SessionPanel) and panel.world.name.casefold() == name.casefold():
                panel.apply_world_settings(world)

    def _save_world(self, world: World, *, replacing: str | None = None) -> bool:
        existing = load_worlds()
        # A New World whose name matches a saved one would silently overwrite its host/port/
        # sounds. Only confirm when it's genuinely a different world being clobbered -- editing
        # a world in place (replacing == its own name) is not an overwrite.
        clobbers = world.name.casefold()
        if clobbers != (replacing or "").casefold() and any(
            saved.name.casefold() == clobbers for saved in existing
        ):
            confirm = wx.MessageBox(
                f"A saved world named {world.name} already exists. Replace it?",
                "Replace World", wx.YES_NO | wx.ICON_QUESTION, self,
            )
            if confirm != wx.YES:
                return False
        replaced = {world.name.casefold()}
        if replacing:
            replaced.add(replacing.casefold())
        worlds = [saved for saved in existing if saved.name.casefold() not in replaced]
        try:
            save_worlds(worlds + [world])
        except (OSError, ValueError) as error:
            wx.MessageBox(
                f"Couldn't save {world.name}: {error}",
                "Save World",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return False
        return True

    def _on_connect(self, _event: wx.CommandEvent) -> None:
        while True:
            dialog = ConnectDialog(self, load_worlds())
            action = dialog.ShowModal()
            world = dialog.get_world()
            dialog.Destroy()
            if action == wx.ID_OK and world is not None:
                self.open_session(world)
                return
            if action == _ID_NEW_WORLD:
                self._show_world_dialog()
                return
            if action == _ID_EDIT_WORLD and world is not None:
                self._show_world_dialog(
                    initial=world,
                    title=f"Edit {world.name}",
                    connect=False,
                    original_name=world.name,
                    show_save=False,
                    lock_name=True,
                )
                continue
            return

    def _on_close_tab(self, _event: wx.CommandEvent) -> None:
        index = self.book.GetSelection()
        if index != wx.NOT_FOUND:
            self.book.GetPage(index).close()  # cancel connection, stop speech
            self.book.DeletePage(index)
            self._update_active()
            self._update_focus()
            self._refresh_connection_item()  # greys out again once the last tab is gone
            if self.book.GetPageCount():  # no tab strip to fall back on; place focus
                self.book.GetPage(self.book.GetSelection()).input.SetFocus()
            else:
                # Closing the last tab destroyed the panel that held focus; without this the
                # screen reader is stranded on a dead window with nothing to read. Focus the
                # frame (so Alt reaches the menu) and say what happened.
                self.SetFocus()
                self.announce("All sessions closed. Press Control N for a new world.")

    def _on_export_world(self, _event: wx.CommandEvent) -> None:
        index = self.book.GetSelection()
        if index == wx.NOT_FOUND or not self.book.GetPageCount():
            self.announce("Open a session for the world you want to export first.")
            return
        panel = self.book.GetPage(index)
        dialog = wx.FileDialog(
            self, "Export this world as a zip",
            defaultFile=f"{slugify(panel.world.name)}.zip",
            wildcard="World zips (*.zip)|*.zip",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        if dialog.ShowModal() == wx.ID_OK:
            pack_dir = panel.app.user_rules_dir() if panel.app is not None else None
            try:
                count = export_world(panel.world, pack_dir, Path(dialog.GetPath()))
            except OSError as error:
                self.announce(f"Export failed: {error}")
            else:
                self.announce(
                    f"Exported {panel.world.name}: {count} "
                    f"file{'s' if count != 1 else ''}. Send the zip to a friend."
                )
        dialog.Destroy()

    def _on_import_world(self, _event: wx.CommandEvent) -> None:
        dialog = wx.FileDialog(
            self, "Import a shared world zip",
            wildcard="World zips (*.zip)|*.zip|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dialog.ShowModal() == wx.ID_OK:
            try:
                world = import_world(Path(dialog.GetPath()), config_dir() / "userpacks")
            except (OSError, ValueError, PackError) as error:
                self.announce(f"Import failed: {error}")
            else:
                if self._save_world(world):
                    self.announce(f"Imported {world.name}. It's in the saved-world list now.")
        dialog.Destroy()

    def _active_panel(self) -> SessionPanel | None:
        index = self.book.GetSelection()
        if index == wx.NOT_FOUND or not self.book.GetPageCount():
            return None
        return self.book.GetPage(index)

    def _refresh_connection_item(self) -> None:
        """Point the File item at what it will actually do for the active tab."""
        panel = self._active_panel()
        if panel is not None:
            label = _connection_menu_label(panel.is_connected())
            if self._connection_item.GetItemLabel() != label:
                self._connection_item.SetItemLabel(label)
        # No tab open: nothing to toggle, so the item greys out instead of advertising a
        # reconnect with no session behind it.
        self._connection_item.Enable(panel is not None)

    def _on_menu_open(self, event: wx.MenuEvent) -> None:
        # Refreshed as the menu opens rather than pushed on every status change: the label
        # only has to be true at the moment the screen reader reads it, and this needs no
        # cross-thread plumbing from the session's loop.
        self._refresh_connection_item()
        event.Skip()

    def _on_disconnect(self, _event: wx.CommandEvent) -> None:
        panel = self._active_panel()
        if panel is None:
            return
        if panel.is_connected():
            panel.disconnect()  # keeps the tab open; stops auto-reconnect
            # The panel writes and speaks "Disconnected from ..." itself, so saying it here
            # too would read the same sentence twice.
        else:
            panel.reconnect()

    def _on_automation_manager(self, _event: wx.CommandEvent) -> None:
        index = self.book.GetSelection()
        if index == wx.NOT_FOUND or not self.book.GetPageCount():
            self.announce("Open a session first; automation is saved per world.")
            return
        panel = self.book.GetPage(index)
        if panel.app is None or panel.app.user_rules_dir() is None:
            self.announce("This session isn't ready yet.")
            return
        dialog = AutomationManagerDialog(self, panel, announce=self.announce)
        dialog.ShowModal()
        dialog.Destroy()

    def _on_pack_notepads(self, _event: wx.CommandEvent) -> None:
        """Pick one of this world's soundpack notepads and open it (MUSHclient lists them in
        its Window menu). Packs write to them as you play; most never open them themselves."""
        index = self.book.GetSelection()
        if index == wx.NOT_FOUND or not self.book.GetPageCount():
            self.announce("Open a session first; notepads belong to a world's soundpacks.")
            return
        panel = self.book.GetPage(index)
        notepads = panel.notepad_snapshot()
        if notepads is None:
            self.announce("The session is busy; try again in a moment.")
            return
        if not notepads:
            self.announce("No soundpack notepads yet. Packs write to them as you play.")
            return
        titles = list(notepads)
        dialog = wx.SingleChoiceDialog(
            self, "Notepad to read:", f"Soundpack notepads - {panel.world.name}", titles
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            title = titles[dialog.GetSelection()]
        finally:
            dialog.Destroy()
        panel._show_notepad(title, notepads[title])

    def _on_mud_variables(self, _event: wx.CommandEvent) -> None:
        index = self.book.GetSelection()
        if index == wx.NOT_FOUND or not self.book.GetPageCount():
            self.announce("Open a session first; variables belong to a connected world.")
            return
        panel = self.book.GetPage(index)
        dialog = VariablesDialog(
            self, panel.variable_snapshot, world_name=panel.world.name, announce=self.announce
        )
        dialog.ShowModal()
        dialog.Destroy()

    def _on_frame_close(self, event: wx.CloseEvent) -> None:
        """Confirm before quitting if any session is live, then disconnect them all."""
        connected = [
            self.book.GetPage(i)
            for i in range(self.book.GetPageCount())
            if self.book.GetPage(i).is_connected()
        ]
        if connected and event.CanVeto():
            names = ", ".join(p.world.name for p in connected)
            if wx.MessageBox(
                f"Disconnect from {names} and exit genericMud?",
                "Quit genericMud", wx.YES_NO | wx.ICON_QUESTION, self,
            ) != wx.YES:
                event.Veto()
                return
        for i in range(self.book.GetPageCount()):
            self.book.GetPage(i).close()  # graceful teardown: leave hub, stop log, close socket
        self._alive = False  # a background update callback must not touch the destroyed frame
        self.Destroy()

    def _on_manage_packs(self, _event: wx.CommandEvent) -> None:
        dialog = PackManagerDialog(
            self, self._packs, load_worlds(), self._active_world_name(), self._diag,
            announce=self.announce,
        )
        dialog.ShowModal()
        dialog.Destroy()

    def _active_world_name(self) -> str | None:
        index = self.book.GetSelection()
        if index == wx.NOT_FOUND or not self.book.GetPageCount():
            return None
        return self.book.GetPage(index).world.name

    def announce(self, text: str) -> None:
        """Speak a UI status update through the screen reader (the app's self-voice)."""
        self._announcer.speak(text)

    def _show_help(self, title: str, text: str) -> None:
        dialog = HelpDialog(self, title, text)
        dialog.ShowModal()
        dialog.Destroy()

    def _on_about(self, _event: wx.CommandEvent) -> None:
        self._show_help(
            "About genericMud",
            f"genericMud {__version__}\n\n"
            "An accessible, self-voicing MUD client.\n"
            "https://github.com/matalvernaz/genericmud\n\n"
            "New releases install themselves: Help menu, Check for Updates.",
        )

    # --- self-update ---

    def check_for_updates(self, *, manual: bool) -> None:
        """Kick off a background release check.

        A manual check (menu) reports "up to date" or an error; the automatic startup check
        stays silent unless there is a release to offer, so it never interrupts a launch.
        """
        if manual:
            self.announce("Checking for updates.")
        if self._diag is not None:
            # Trace what the build thinks it is + that the check ran, so "the updater can't
            # detect the new version" is answerable from the log instead of by inference.
            self._diag.event("update.check", phase="start", manual=manual,
                             current=self_update.current_version() or "")
        _run_async(
            self_update.check_for_update,
            lambda outcome: self._on_update_checked(outcome, manual=manual),
        )

    def _on_update_checked(self, outcome, *, manual: bool) -> None:
        if isinstance(outcome, Exception):
            self._log_update_check("error", error=repr(outcome))
            if manual:
                # Defer the modal to a fresh event-loop turn (as _on_update_finished already
                # does for its own MessageBox). This callback runs as the _run_async completion
                # *inside* wx's pending-event dispatch; showing a native modal there raises
                # RPC_E_CANTCALLOUT_ININPUTSYNCCALL (0x8001010d) while a screen reader's
                # input-synchronous COM call is in flight -- the faulthandler "Windows fatal
                # exception" recorded in the crash log. CallAfter re-posts it to run cleanly.
                wx.CallAfter(
                    wx.MessageBox, f"Couldn't check for updates: {outcome}",
                    "Check for Updates", wx.OK | wx.ICON_ERROR,
                )
            return
        if outcome is None:
            self._log_update_check("up_to_date")
            if manual:
                self.announce("genericMud is up to date.")
                wx.CallAfter(
                    wx.MessageBox, "genericMud is up to date.",
                    "Check for Updates", wx.OK | wx.ICON_INFORMATION,
                )
            return
        prefs = load_prefs()
        # A snooze suppresses only the version it was set on (scoped): a newer release than the
        # one you clicked "Remind me later" on must still prompt. Skip is likewise per-version.
        # A manual check ignores both -- asking explicitly overrides any earlier deferral.
        snoozed_this = is_snoozed(prefs) and outcome["tag"] == prefs.snoozed_version
        if not manual and (outcome["tag"] == prefs.skipped_version or snoozed_this):
            self._log_update_check(
                "suppressed", tag=outcome["tag"],
                reason="skipped" if outcome["tag"] == prefs.skipped_version else "snoozed",
            )
            return
        self._log_update_check("offer", tag=outcome["tag"])
        self._offer_update(outcome)

    def _log_update_check(self, decision: str, **fields: object) -> None:
        if self._diag is not None:
            self._diag.event("update.check", phase="result", decision=decision, **fields)

    def _offer_update(self, info: dict) -> None:
        current = self_update.current_version() or "an earlier version"
        dialog = UpdateNotificationDialog(self, info, current)
        action = dialog.ShowModal()
        dialog.Destroy()
        if action == _ID_UPDATE_NOW:
            self._perform_update(info)
        elif action == _ID_RELEASE_PAGE:
            if info.get("release_url"):
                webbrowser.open(info["release_url"])
        elif action == _ID_SNOOZE:
            prefs = load_prefs()
            prefs.snoozed_until = snooze_timestamp()
            prefs.snoozed_version = info["tag"]  # scope the snooze; a newer release still prompts
            save_prefs(prefs)
        elif action == _ID_SKIP:
            prefs = load_prefs()
            prefs.skipped_version = info["tag"]
            save_prefs(prefs)

    def _perform_update(self, info: dict) -> None:
        self._update_cancelled = threading.Event()
        self._update_progress_dialog = UpdateProgressDialog(
            self, info["tag"], self.announce, self._update_cancelled
        )
        # Owner-disabled instead of PD_APP_MODAL: the frame takes no input while the
        # dialog is up, without wx.ProgressDialog's separate-thread native machinery.
        self.Disable()
        self._update_progress_dialog.Show()

        def work():
            return self_update.download_and_replace(info, progress_cb=self._on_update_progress)

        _run_async(work, self._on_update_finished)

    def _on_update_progress(self, done: int, total: int) -> None:  # background thread
        # Raising here aborts the download; download_and_replace cleans up and re-raises, so
        # _on_update_finished sees the cancellation. The Cancel button sets the flag this
        # checks, on the main thread, in UpdateProgressDialog._on_cancel.
        if self._update_cancelled.is_set():
            raise RuntimeError("Update cancelled by user.")
        wx.CallAfter(self._pump_update_progress, done, total)

    def _pump_update_progress(self, done: int, total: int) -> None:  # main thread
        if not self._alive or self._update_progress_dialog is None:
            return
        self._update_progress_dialog.pump(done, total)

    def _on_update_finished(self, outcome) -> None:
        if not self._alive:
            return  # the frame was closed while the update was downloading
        # Re-enable before destroying the owned dialog: destroying the focused window while
        # its owner is still disabled makes Windows throw focus to another application.
        self.Enable()
        if self._update_progress_dialog is not None:
            self._update_progress_dialog.Destroy()
            self._update_progress_dialog = None
        if isinstance(outcome, Exception):
            if self._update_cancelled.is_set():
                self.announce("Update cancelled.")
            else:
                # Defer off the dialog-teardown stack: opening a window while the screen
                # reader still has input-synchronous queries against the closing dialog is
                # the 0x8001010d trap (see _on_browse_online).
                wx.CallAfter(
                    wx.MessageBox, f"Update failed: {outcome}", "Update", wx.OK | wx.ICON_ERROR
                )
            return
        # Success: the helper is blocked on our PID; it overlays the files and relaunches us
        # once we exit.
        self.announce("Update downloaded. genericMud will restart to finish installing.")
        wx.CallAfter(self._quit_for_update)

    def _quit_for_update(self) -> None:
        for i in range(self.book.GetPageCount()):
            self.book.GetPage(i).close()  # graceful teardown before we exit for the swap
        self.Destroy()  # ends MainLoop -> process exits -> the helper swaps and relaunches

    def show_recovery(self, recovery) -> None:
        """Tell the user a failed update was rolled back (called once at startup)."""
        self.announce(recovery.title)
        wx.MessageBox(recovery.message, recovery.title, wx.OK | wx.ICON_WARNING)

    def _on_setup_pack(self, _event: wx.CommandEvent) -> None:
        """Wizard: pick an extracted pack folder, derive its world, confirm, connect."""
        with wx.DirDialog(self, "Choose an already-unzipped soundpack folder") as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            folder = dialog.GetPath()
        entry = detect_entry(folder)
        if entry is None:
            wx.MessageBox(
                f"Can't set up this folder: {entry_problem(folder)}.",
                "Set up a soundpack", wx.OK | wx.ICON_ERROR,
            )
            return
        self.announce("Setting up the soundpack.")
        try:
            result = setup_pack(self._packs, folder, entry=entry)
        except (PackError, OSError) as error:
            wx.MessageBox(str(error), "Set up failed", wx.OK | wx.ICON_ERROR)
            return
        self._finish_setup(result)

    def _on_browse_online(self, _event: wx.CommandEvent) -> None:
        """Browse mudsoundpack.com, download a pack, then confirm the world and connect."""
        dialog = VaultBrowserDialog(self, self._packs, self.announce, self._diag)
        completed = dialog.ShowModal() == wx.ID_OK
        result = dialog.result
        dialog.Destroy()
        if completed and result is not None:
            # Defer off the dialog-teardown stack: opening another window here, while the screen
            # reader is still issuing input-synchronous COM queries against the closing dialog,
            # raised RPC_E_CANTCALLOUT_ININPUTSYNCCALL (0x8001010d). A fresh loop turn is clean.
            wx.CallAfter(self._finish_setup, result)

    def _finish_setup(self, result) -> None:
        """Create the pack's world and confirm the connection; only open the full form if
        details are missing. A complete world (from the pack or the known-MUD table) is saved
        and bound, then the world dialog confirms before connecting -- prefilled, so the common
        case is a single Enter, but the user is still asked rather than dropped into a session."""
        world = result.world
        if world is not None and world.host and world.port and self._pack_bundles_sounds(result):
            self._packs.enable(result.manifest.id, world.name)
            self._confirm_and_connect(world, result.manifest)
            return
        # No bundled audio (e.g. Cosmic Rage streams its cues and ships sounds as a separate
        # download), or no connection details: open the dialog so the Sounds folder / host can
        # be set before connecting -- otherwise it would connect silent.
        self._finish_setup_via_dialog(result)

    def _confirm_and_connect(self, world: World, manifest) -> None:
        """Save the pack's world, then confirm the connection in the world dialog instead of
        connecting unprompted. A code-executing pack (MUSHclient, e.g. Erion) is offered trust in
        the same dialog: it stays silent until trusted, so without this it installs and connects
        but plays nothing. The world is persisted first, so cancelling still leaves it ready under
        the Connect menu."""
        needs_trust = (
            manifest.dialect in CODE_EXEC_DIALECTS and not self._packs.is_trusted(manifest.id)
        )
        self._save_world(world)
        dialog = WorldDialog(
            self,
            initial=world,
            title="Confirm World",
            offer_trust=needs_trust,
            show_save=False,
            note=(
                f"The {world.name} soundpack is installed, with its own sounds: the Sounds "
                "folder can stay blank. Press Enter to connect."
            ),
            credentials=self._credentials,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                self.announce(
                    f"{world.name} is set up. Connect to it any time from the Connect menu."
                )
                return
            if dialog.should_trust():
                self._packs.trust(manifest.id)  # consent to run its scripts, so its sounds load
            chosen = dialog.get_world()
            self._save_world(chosen, replacing=world.name)
            self.open_session(chosen)
        finally:
            dialog.Destroy()

    def _pack_bundles_sounds(self, result) -> bool:
        """True if the installed pack carries its own audio. A pack with none (Cosmic Rage
        streams cues, keeping sounds in a separate download) needs the world's Sounds folder
        pointed at a local copy first, so it routes through the world-details dialog instead."""
        try:
            pack_dir = self._packs.pack_dir(result.manifest.id)
        except Exception:  # noqa: BLE001 - if we can't tell, fall through to the dialog (safe)
            return False
        return any(p.suffix.lower() in _PACK_SOUND_SUFFIXES for p in pack_dir.rglob("*"))

    def _finish_setup_via_dialog(self, result) -> None:
        """Fallback when the pack has no connection details and the MUD isn't in the known-MUD
        table: open the world form with the name prefilled, explaining what's needed."""
        note = ""
        if result.world is not None and not result.world.host:
            note = (
                f"The {result.world.name} soundpack is installed, but it doesn't say where its "
                "MUD is. Enter the MUD's host and port to connect."
            )
        elif result.world is not None and result.world.host:
            note = (
                f"The {result.world.name} soundpack is installed. It ships no sounds of its "
                "own: set the Sounds folder to where you keep them, then connect."
            )
        needs_trust = (
            result.manifest.dialect in CODE_EXEC_DIALECTS
            and not self._packs.is_trusted(result.manifest.id)
        )
        connect = WorldDialog(
            self,
            initial=result.world,
            title="Finish World Setup",
            offer_trust=needs_trust,
            save_default=True,
            note=note,
            credentials=self._credentials,
        )
        if connect.ShowModal() == wx.ID_OK:
            world = connect.get_world()
            if connect.should_save():
                self._save_world(world)
            self._packs.enable(result.manifest.id, world.name)  # (re)bind to final name
            if connect.should_trust():
                self._packs.trust(result.manifest.id)
            self.announce(f"Connecting to {world.name}.")
            self.open_session(world)
        else:
            # Cancelling isn't a dead-end: the pack is installed and reachable later.
            self.announce(
                f"{result.world.name if result.world else 'The pack'} is set up. "
                "Connect to it any time from the Connect menu."
            )
        connect.Destroy()

    def _on_toggle_self_voice(self, _event: wx.CommandEvent) -> None:
        self._self_voice = self._self_voice_item.IsChecked()
        self._update_active()
        # The most consequential audio switch was the only silent toggle: with self-voice
        # off there's no MUD speech to confirm the change, so announce it explicitly.
        self.announce("Self-voice on." if self._self_voice else "Self-voice off.")

    def _update_active(self) -> None:
        selected = self.book.GetSelection()
        # Background silence treats the whole app losing focus like a background tab:
        # self-voice mutes but triggers and sounds keep running.
        audible = self._app_focused or not self._prefs.background_silence
        for i in range(self.book.GetPageCount()):
            self.book.GetPage(i).set_active(i == selected and self._self_voice and audible)


def _world_from_args(args) -> World | None:
    """The world to auto-connect for ``genericmud host port [--tls] [--sounds] [--encoding]``."""
    if not args.host:
        return None
    return World(
        name=args.host, host=args.host, port=args.port, tls=args.tls,
        sounds=args.sounds, encoding=getattr(args, "encoding", AUTO),
        images_url=getattr(args, "images", "") or "",
    )


def run(args, recovery=None) -> None:
    loop = asyncio.new_event_loop()
    install_loop_exception_handler(loop)  # capture engine-thread coroutine crashes
    threading.Thread(target=_run_loop, args=(loop,), daemon=True).start()

    wx_app = wx.App(False)
    frame = GenericMudFrame(loop, load_keymap("vipmud"))
    frame.Show()
    if (world := _world_from_args(args)) is not None:
        frame.open_session(world)
    else:
        # A blank first launch was silent; give a blind user the way in. Deferred so it
        # speaks after the window is up, not over the screen reader announcing the window.
        wx.CallAfter(
            frame.announce,
            "Welcome to genericMud. Press Control N for a new world, or Control O for a "
            "saved one. The Help menu has Getting Started.",
        )
    if recovery is not None:  # a prior in-app update was rolled back at startup; tell the user
        wx.CallAfter(frame.show_recovery, recovery)
    prefs = load_prefs()
    # Always run the check when enabled; a snooze no longer blocks it (it would hide a newer
    # release too). Suppression is applied per-version at the offer stage in _on_update_checked.
    if self_update.is_frozen() and prefs.check_enabled:
        frame.check_for_updates(manual=False)
    wx_app.MainLoop()
    loop.call_soon_threadsafe(loop.stop)


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


