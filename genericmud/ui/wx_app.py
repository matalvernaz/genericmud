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
import shutil
import tempfile
import threading
import traceback
import webbrowser
import zipfile
from pathlib import Path

import wx

from genericmud.app import EngineApp
from genericmud.automation.engine import AutomationEngine
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.config.update_prefs import is_snoozed, load_prefs, save_prefs, snooze_timestamp
from genericmud.config.worlds import World, config_dir, load_worlds, save_worlds
from genericmud.packs import (
    PackError,
    PackStore,
    SetupResult,
    activate_world,
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
    vault,
    world_from_pack,
)
from genericmud.packs.manifest import CODE_EXEC_DIALECTS
from genericmud.packs.store import extract_pack
from genericmud.session.crashlog import install_loop_exception_handler
from genericmud.session.credentials import PlaintextCredentialStore
from genericmud.session.diaglog import DiagnosticLog, make_diagnostic_log
from genericmud.session.hub import SessionHub
from genericmud.sound.pygame_backend import make_pygame_backend
from genericmud.transport.connection import MudConnection
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

    is_special = name.startswith("f") and name[1:].isdigit() or name == "escape"
    if not mods and not is_special:
        return None  # ordinary typing
    return "+".join(mods + [name])


# Window/OS commands the input box must NOT swallow -- they have to reach the platform's
# default handler (Alt+F4 -> WM_CLOSE -> our EVT_CLOSE), or the window can't be closed.
_PASSTHROUGH_COMBOS = frozenset({"alt+f4"})

_OUTPUT_CAP_LINES = 5000  # keep the native control bounded so NVDA/UIA stays responsive
_FLUSH_INTERVAL_MS = 50  # batch output appends during floods
_PACK_SOUND_SUFFIXES = frozenset({".wav", ".ogg", ".mp3", ".flac"})  # bundled-audio detection


class SessionPanel(wx.Panel):
    """One MUD: read-only output + command input, wired to its own engine."""

    def __init__(
        self,
        parent: wx.Window,
        loop: asyncio.AbstractEventLoop,
        keymap: dict,
        world: World,
        packs: PackStore | None = None,
        credentials: PlaintextCredentialStore | None = None,
        hub: SessionHub | None = None,
        diag: DiagnosticLog | None = None,
    ):
        super().__init__(parent)
        self._loop = loop
        self._keymap = keymap
        self.world = world
        self._packs = packs
        self._credentials = credentials
        self._hub = hub
        self._diag = diag
        self.app: EngineApp | None = None
        self._connection: MudConnection | None = None
        self._voice: VoiceRouter | None = None
        self._history: list[str] = []
        self._hist_index = 0
        self._alive = True
        self._pending: list[str] = []
        self._flush_scheduled = False
        self._sound_warned = False  # speak the first sound problem; echo the rest

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
            sound_backend=make_pygame_backend(on_error=self._sound_error, diag=self._diag),
            name=self.world.name,  # used for the session log filename
            credentials=self._credentials,
            hub=self._hub,
            diag=self._diag,
        )
        self._connection._on_event = self.app.on_telnet_event
        self._connection.auto_reconnect = True
        self._connection.on_status = self.app.on_connection_status
        if self.world.sounds:  # point @sppath at the world's sound folder before packs load
            self.app.engine.set_var("sppath", self.world.sounds)
        self.app.on_connect(self.world.name)  # activate packs + arm auto-login before data
        if self._diag is not None:
            self._diag.event("connect.begin", host=self.world.host, port=self.world.port)
        try:
            await self._connection.connect(self.world.host, self.world.port, tls=self.world.tls)
            if self._diag is not None:
                self._diag.event("connect.ok", host=self.world.host)
            self._post(protocol.echo(f"* Connected to {self.world.name}"))
        except OSError as error:
            if self._diag is not None:
                self._diag.event("connect.failed", error=str(error))
            self._post(protocol.echo(f"* Connect failed: {error}"))

    def _send(self, text: str) -> None:
        try:
            if self._connection is not None:
                self._connection.send_line(text)
        except ConnectionError:
            pass

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
        # Sound/status messages are ignored here for now (native SFX is a follow-up).

    def _flush_output(self) -> None:
        self._flush_scheduled = False
        if not self._alive or not self._pending:
            return
        self.output.AppendText("\n".join(self._pending) + "\n")
        self._pending.clear()
        self._trim_output()

    def _trim_output(self) -> None:
        excess = self.output.GetNumberOfLines() - _OUTPUT_CAP_LINES
        if excess > 0:
            end = self.output.XYToPosition(0, excess)
            if end > 0:
                self.output.Remove(0, end)

    def _on_send(self, _event: wx.CommandEvent) -> None:
        text = self.input.GetValue()
        self.input.SetValue("")
        if text:
            self._history.append(text)
        self._hist_index = len(self._history)
        if self.app is not None:
            self._loop.call_soon_threadsafe(self.app.on_ws_message, {"type": "input", "text": text})

    def _on_input_key(self, event: wx.KeyEvent) -> None:
        code = event.GetKeyCode()
        plain = not (event.ControlDown() or event.AltDown() or event.ShiftDown())
        if plain and code in (wx.WXK_UP, wx.WXK_DOWN):
            self._recall_history(-1 if code == wx.WXK_UP else 1)
            return
        combo = _key_combo(event)
        if combo and combo not in _PASSTHROUGH_COMBOS and self.app is not None:
            self._loop.call_soon_threadsafe(self.app.on_ws_message, {"type": "key", "key": combo})
            return
        event.Skip()  # passthrough/unbound combos -> default handling (Alt+F4 -> EVT_CLOSE)

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
        self._hist_index = max(0, min(len(self._history), self._hist_index + direction))
        value = self._history[self._hist_index] if self._hist_index < len(self._history) else ""
        self.input.SetValue(value)
        self.input.SetInsertionPointEnd()
        # NVDA doesn't announce a programmatic SetValue, so speak the recalled command.
        if value:
            self._loop.call_soon_threadsafe(self._speak_system, value)

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

    def _apply_active(self, active: bool) -> None:  # loop thread
        if self._voice is not None:
            self._voice.set_muted(not active)  # only the foreground MUD self-voices

    def close(self) -> None:
        """Tear down the session; safe to call from the wx thread on tab close."""
        self._alive = False
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
            # A deliberate close is silent (no "disconnected" status fires), so the
            # status-side flush never runs; cut the pack's looping cues here.
            self.app.sound.flush()


class ConnectDialog(wx.Dialog):
    def __init__(
        self,
        parent: wx.Window,
        saved: list[World],
        initial: World | None = None,
        *,
        offer_trust: bool = False,
        save_default: bool = False,
    ):
        super().__init__(parent, title="Connect to a MUD")
        self._saved = saved
        grid = wx.FlexGridSizer(0, 2, 6, 6)
        grid.AddGrowableCol(1)

        # Each StaticText is created (and added) immediately before its control so the
        # label precedes the control in z-order -- the association NVDA reads on
        # Windows. Checkboxes carry their own label= as the accessible name.
        grid.Add(wx.StaticText(self, label="&Saved world:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._choice = wx.Choice(self, choices=["(new)"] + [w.name for w in saved])
        self._choice.SetName("Saved world")
        self._choice.SetSelection(0)
        self._choice.Bind(wx.EVT_CHOICE, self._on_pick)
        grid.Add(self._choice, 1, wx.EXPAND)

        self._name = self._labeled_text(grid, "&Name:", "Name")
        self._host = self._labeled_text(grid, "&Host:", "Host")
        self._port = self._labeled_text(grid, "&Port:", "Port", "4000")
        self._sounds = self._labeled_text(grid, "So&unds folder:", "Sounds folder")

        grid.Add((0, 0))
        self._tls = wx.CheckBox(self, label="Use &TLS")
        self._tls.SetName("Use TLS")
        grid.Add(self._tls, 1, wx.EXPAND)

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
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(sizer)

        if initial is not None:  # prefill from a pack-derived world (the setup wizard)
            self._name.SetValue(initial.name)
            self._host.SetValue(initial.host)
            self._port.SetValue(str(initial.port))
            self._tls.SetValue(initial.tls)
            self._sounds.SetValue(initial.sounds or "")

    def _labeled_text(
        self, grid: wx.FlexGridSizer, label: str, name: str, value: str = ""
    ) -> wx.TextCtrl:
        grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        ctrl = wx.TextCtrl(self, value=value)
        ctrl.SetName(name)
        grid.Add(ctrl, 1, wx.EXPAND)
        return ctrl

    def _on_pick(self, _event: wx.CommandEvent) -> None:
        index = self._choice.GetSelection() - 1
        if 0 <= index < len(self._saved):
            world = self._saved[index]
            self._name.SetValue(world.name)
            self._host.SetValue(world.host)
            self._port.SetValue(str(world.port))
            self._tls.SetValue(world.tls)
            self._sounds.SetValue(world.sounds or "")

    def get_world(self) -> World:
        name = self._name.GetValue().strip() or self._host.GetValue().strip()
        try:
            port = int(self._port.GetValue().strip())
        except ValueError:
            port = 4000
        return World(
            name=name,
            host=self._host.GetValue().strip(),
            port=port,
            tls=self._tls.GetValue(),
            sounds=self._sounds.GetValue().strip() or None,
        )

    def should_save(self) -> bool:
        return self._save.GetValue()

    def should_trust(self) -> bool:
        """True if the trust checkbox was offered and left checked (else False)."""
        return self._trust is not None and self._trust.GetValue()


class PackManagerDialog(wx.Dialog):
    """In-app PackStore front end: install, enable, and trust soundpacks per world.

    Operates on filesystem state only (install/enable/trust); changes take effect
    the next time the world is connected, since packs activate on connect. Every
    control gets a preceding StaticText label + SetName for NVDA, matching
    ConnectDialog.
    """

    _WILDCARD = (
        "Soundpacks (*.zip;*.xml;*.lua;*.set)|*.zip;*.xml;*.lua;*.set|All files (*.*)|*.*"
    )

    def __init__(
        self, parent: wx.Window, store: PackStore, worlds: list[World], active: str | None,
        diag=None,
    ) -> None:
        super().__init__(parent, title="Manage Soundpacks", size=(560, 440))
        self._store = store
        self._diag = diag  # durable install trace (DiagnosticLog or None)
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
            ("Toggle &enabled", self._on_toggle_enabled),
            ("Toggle &trust", self._on_toggle_trust),
            ("&Uninstall", self._on_uninstall),
            ("Check &conflicts", self._on_conflicts),
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
            wx.MessageBox("Pick a world first.", "No world", wx.OK | wx.ICON_INFORMATION)
            return
        if self._store.is_enabled(pack_id, world):
            self._store.disable(pack_id, world)
        else:
            self._store.enable(pack_id, world)
        self._refresh_packs()

    def _on_toggle_trust(self, _event: wx.CommandEvent) -> None:
        pack_id = self._selected_pack()
        if pack_id is None:
            return
        if self._store.is_trusted(pack_id):
            self._store.untrust(pack_id)
        else:
            self._store.trust(pack_id)
        self._refresh_packs()

    def _on_uninstall(self, _event: wx.CommandEvent) -> None:
        pack_id = self._selected_pack()
        if pack_id is None:
            return
        confirm = wx.MessageBox(f"Uninstall {pack_id}?", "Uninstall", wx.YES_NO | wx.ICON_QUESTION)
        if confirm == wx.YES:
            self._store.uninstall(pack_id)
            self._refresh_packs()

    def _on_conflicts(self, _event: wx.CommandEvent) -> None:
        world = self._selected_world()
        if not world:
            wx.MessageBox("Pick a world first.", "No world", wx.OK | wx.ICON_INFORMATION)
            return
        result = activate_world(self._store, world, AutomationEngine(), require_trust=False)
        lines = [f"{len(result.loaded)} pack(s) loaded clean for {world}."]
        for pack_id, error in result.failed.items():
            lines.append(f"FAILED {pack_id}: {error}")
        for conflict in result.conflicts:
            lines.append(
                f"CONFLICT {conflict.kind} {conflict.token} ({', '.join(conflict.sources)})"
            )
        if not result.failed and not result.conflicts:
            lines.append("No load failures or binding conflicts.")
        wx.MessageBox("\n".join(lines), "Conflicts", wx.OK | wx.ICON_INFORMATION)

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
        else:
            if self._diag is not None:
                self._diag.event("update.done", id=pack_id, dialect=outcome.manifest.dialect)
            wx.MessageBox(
                f"Updated {pack_id}. Reconnect to apply.", "Update", wx.OK | wx.ICON_INFORMATION,
            )
        self._refresh_packs()


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
        self._last_milestone = 0  # throttle spoken download progress to 25% steps
        self._packs: list = []  # VaultPack list, parallel to the list box
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

        self._gauge = wx.Gauge(self, range=100)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.ALL, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._setup_btn = wx.Button(self, label="&Download && Set Up")
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
        self._packs = outcome
        self._list.Clear()
        for pack in outcome:
            version = f" v{pack.version}" if pack.version else ""
            unsupported = "" if pack.supported else "  [unsupported client]"
            self._list.Append(
                f"{pack.name} - {pack.mud} - {pack.client}{version} ({pack.status}){unsupported}"
            )
        self._status(f"{len(outcome)} soundpacks loaded. Choose one, then Download and Set Up.")
        if outcome:
            self._list.SetSelection(0)
            self._setup_btn.Enable()
            self._list.SetFocus()

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
        best = vault.best_download(vault.pack_downloads(pack.id))
        if best is None:
            raise PackError("no downloadable archive for this pack; use Open in browser")
        tmp = Path(tempfile.mkdtemp(prefix="genericmud-pack-"))
        try:
            archive = vault.download(best.url, tmp / "pack.zip", progress=self._progress)
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
            entry = detect_entry(extracted)
            origin = best.url  # record where the content came from, so it can be updated
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
        """Per-file sync progress: drive the gauge and speak every 10% (packs have ~9000 files)."""
        if not total:
            return
        pct = min(int(done * 100 / total), 100)
        wx.CallAfter(self._set_gauge, pct)
        milestone = pct - pct % 10
        if milestone and milestone != self._last_milestone:
            self._last_milestone = milestone
            self._status(f"Synced {done} of {total} files ({pct} percent).")

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
            pct = min(int(done * 100 / total), 100)
            wx.CallAfter(self._set_gauge, pct)
            milestone, label = pct - pct % 25, f"{pct - pct % 25} percent"  # 25/50/75/100
        else:  # no Content-Length (GitLab archives) -> report MB, every 50 MB
            milestone, label = done // 50_000_000, f"{done // 1_000_000} MB"
        if milestone and milestone != self._last_milestone:
            self._last_milestone = milestone
            self._status(f"Downloaded {label}.")

    def _on_setup_done(self, outcome) -> None:
        if not self._alive:
            return
        if isinstance(outcome, Exception):
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
_ID_UPDATE_NOW = wx.ID_HIGHEST + 101
_ID_RELEASE_PAGE = wx.ID_HIGHEST + 102
_ID_SNOOZE = wx.ID_HIGHEST + 103
_ID_SKIP = wx.ID_HIGHEST + 104


class UpdateNotificationDialog(wx.Dialog):
    """Announce a newer genericMud and offer what to do about it.

    Follows ConnectDialog's accessibility pattern: a StaticText precedes each control and
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
    """Self-update progress: a status log, a gauge, spoken 25% milestones, and Cancel.

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
        self._last_milestone = 0  # throttle spoken download progress to 25% steps
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
        self._gauge.SetValue(pct)
        milestone = pct // 25 * 25
        if pct < 100 and milestone > self._last_milestone:
            self._last_milestone = milestone
            self._status(f"Downloaded {milestone} percent.")
        elif pct >= 100 and not self._setup_announced:
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
        self._credentials = PlaintextCredentialStore(config_dir() / "credentials.json")
        self._hub = SessionHub()  # shared across all open sessions for cross-character play
        self._announcer = make_voice_backend()  # speaks UI status for screen-reader users
        self._diag = make_diagnostic_log()  # one sound-path trace file for the whole process

        menubar = wx.MenuBar()
        file_menu = wx.Menu()
        connect_item = file_menu.Append(wx.ID_ANY, "&Connect...\tCtrl+N")
        disconnect_item = file_menu.Append(wx.ID_ANY, "&Disconnect\tCtrl+D")
        close_item = file_menu.Append(wx.ID_ANY, "Close &Tab\tCtrl+W")
        packs_item = file_menu.Append(wx.ID_ANY, "&Manage Soundpacks...\tCtrl+P")
        setup_item = file_menu.Append(wx.ID_ANY, "Set &Up a Soundpack...")
        browse_item = file_menu.Append(wx.ID_ANY, "&Browse Soundpacks Online...")
        updates_item = file_menu.Append(wx.ID_ANY, "Check for &Updates...")
        file_menu.AppendSeparator()
        quit_item = file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl+Q")
        menubar.Append(file_menu, "&File")

        view_menu = wx.Menu()
        self._self_voice_item = view_menu.AppendCheckItem(wx.ID_ANY, "Self-&voice\tCtrl+M")
        self._self_voice_item.Check(True)
        self._self_voice = True
        menubar.Append(view_menu, "&View")

        self.SetMenuBar(menubar)
        self.Bind(wx.EVT_MENU, self._on_connect, connect_item)
        self.Bind(wx.EVT_MENU, self._on_disconnect, disconnect_item)
        self.Bind(wx.EVT_MENU, self._on_close_tab, close_item)
        self.Bind(wx.EVT_MENU, self._on_manage_packs, packs_item)
        self.Bind(wx.EVT_MENU, self._on_setup_pack, setup_item)
        self.Bind(wx.EVT_MENU, self._on_browse_online, browse_item)
        self.Bind(wx.EVT_MENU, lambda _e: self.check_for_updates(manual=True), updates_item)
        self.Bind(wx.EVT_MENU, lambda _e: self.Close(), quit_item)
        self.Bind(wx.EVT_MENU, self._on_toggle_self_voice, self._self_voice_item)

        self.book = wx.Simplebook(self)  # no tab strip -> nothing in the keyboard Tab order
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)  # Ctrl+Tab cycles sessions
        self.Bind(wx.EVT_CLOSE, self._on_frame_close)  # confirm + disconnect before exit

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
        # Focus the new page's command box; NVDA reads its name, announcing the session.
        self.book.GetPage(index).input.SetFocus()

    def open_session(self, world: World) -> None:
        panel = SessionPanel(
            self.book, self._loop, self._keymap, world,
            self._packs, self._credentials, self._hub, self._diag,
        )
        self.book.AddPage(panel, world.name, select=True)
        panel.input.SetFocus()
        self._update_active()

    def _on_connect(self, _event: wx.CommandEvent) -> None:
        dialog = ConnectDialog(self, load_worlds())
        if dialog.ShowModal() == wx.ID_OK:
            world = dialog.get_world()
            if world.host:
                if dialog.should_save():
                    worlds = [w for w in load_worlds() if w.name != world.name] + [world]
                    save_worlds(worlds)
                self.open_session(world)
        dialog.Destroy()

    def _on_close_tab(self, _event: wx.CommandEvent) -> None:
        index = self.book.GetSelection()
        if index != wx.NOT_FOUND:
            self.book.GetPage(index).close()  # cancel connection, stop speech
            self.book.DeletePage(index)
            self._update_active()
            if self.book.GetPageCount():  # no tab strip to fall back on; place focus
                self.book.GetPage(self.book.GetSelection()).input.SetFocus()

    def _on_disconnect(self, _event: wx.CommandEvent) -> None:
        index = self.book.GetSelection()
        if index == wx.NOT_FOUND or not self.book.GetPageCount():
            return
        panel = self.book.GetPage(index)
        if panel.is_connected():
            panel.disconnect()  # keeps the tab open; stops auto-reconnect
            self.announce(f"Disconnecting from {panel.world.name}.")
        else:
            self.announce("Not connected.")

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
            self, self._packs, load_worlds(), self._active_world_name(), self._diag
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
        with wx.DirDialog(self, "Choose the extracted soundpack folder") as dialog:
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
        and bound, then the Connect dialog confirms before connecting -- prefilled, so the common
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
        """Save the pack's world, then confirm the connection in the Connect dialog instead of
        connecting unprompted. A code-executing pack (MUSHclient, e.g. Erion) is offered trust in
        the same dialog: it stays silent until trusted, so without this it installs and connects
        but plays nothing. The world is persisted first, so cancelling still leaves it ready under
        the Connect menu."""
        needs_trust = (
            manifest.dialect in CODE_EXEC_DIALECTS and not self._packs.is_trusted(manifest.id)
        )
        save_worlds([w for w in load_worlds() if w.name != world.name] + [world])
        dialog = ConnectDialog(
            self, load_worlds(), initial=world, offer_trust=needs_trust, save_default=True
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
            if dialog.should_save():
                save_worlds([w for w in load_worlds() if w.name != chosen.name] + [chosen])
            self.open_session(chosen)
        finally:
            dialog.Destroy()

    def _pack_bundles_sounds(self, result) -> bool:
        """True if the installed pack carries its own audio. A pack with none (Cosmic Rage
        streams cues, keeping sounds in a separate download) needs the world's Sounds folder
        pointed at a local copy first, so it routes through the Connect dialog instead."""
        try:
            pack_dir = self._packs.pack_dir(result.manifest.id)
        except Exception:  # noqa: BLE001 - if we can't tell, fall through to the dialog (safe)
            return False
        return any(p.suffix.lower() in _PACK_SOUND_SUFFIXES for p in pack_dir.rglob("*"))

    def _finish_setup_via_dialog(self, result) -> None:
        """Fallback when the pack has no connection details and the MUD isn't in the known-MUD
        table: open the Connect form with the world name prefilled, explaining what's needed."""
        if result.world is not None and not result.world.host:
            self.announce(
                f"The {result.world.name} soundpack installed, but carries no connection "
                "details. Enter the MUD's host and port to connect."
            )
        elif result.world is not None and result.world.host:
            self.announce(
                f"The {result.world.name} soundpack installed. It ships no sounds of its own — "
                "set the Sounds folder to your local sound files, then connect."
            )
        needs_trust = (
            result.manifest.dialect in CODE_EXEC_DIALECTS
            and not self._packs.is_trusted(result.manifest.id)
        )
        connect = ConnectDialog(self, load_worlds(), initial=result.world, offer_trust=needs_trust)
        if connect.ShowModal() == wx.ID_OK:
            world = connect.get_world()
            if world.host:
                worlds = [w for w in load_worlds() if w.name != world.name] + [world]
                save_worlds(worlds)
                self._packs.enable(result.manifest.id, world.name)  # (re)bind to final name
                if connect.should_trust():
                    self._packs.trust(result.manifest.id)
                self.announce(f"Connecting to {world.name}.")
                self.open_session(world)
        connect.Destroy()

    def _on_toggle_self_voice(self, _event: wx.CommandEvent) -> None:
        self._self_voice = self._self_voice_item.IsChecked()
        self._update_active()

    def _update_active(self) -> None:
        selected = self.book.GetSelection()
        for i in range(self.book.GetPageCount()):
            self.book.GetPage(i).set_active(i == selected and self._self_voice)


def run(args, recovery=None) -> None:
    loop = asyncio.new_event_loop()
    install_loop_exception_handler(loop)  # capture engine-thread coroutine crashes
    threading.Thread(target=_run_loop, args=(loop,), daemon=True).start()

    wx_app = wx.App(False)
    frame = GenericMudFrame(loop, load_keymap("vipmud"))
    frame.Show()
    if args.host:
        frame.open_session(
            World(name=args.host, host=args.host, port=args.port, tls=args.tls)
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
