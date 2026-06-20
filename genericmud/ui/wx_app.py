"""Native wxPython UI (Windows-first).

The VIPMud-class interaction model on native controls: a read-only multiline
output box NVDA reads like Notepad (Tab to it, arrow/say-line), a separate command
box, Tab/Shift+Tab between them, type-on-output jumps to the command box, and a
wx.Notebook tab per MUD.

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
import threading

import wx

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.config.worlds import World, load_worlds, save_worlds
from genericmud.transport.connection import MudConnection
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


_OUTPUT_CAP_LINES = 5000  # keep the native control bounded so NVDA/UIA stays responsive
_FLUSH_INTERVAL_MS = 50  # batch output appends during floods


class SessionPanel(wx.Panel):
    """One MUD: read-only output + command input, wired to its own engine."""

    def __init__(
        self,
        parent: wx.Window,
        loop: asyncio.AbstractEventLoop,
        keymap: dict,
        world: World,
    ):
        super().__init__(parent)
        self._loop = loop
        self._keymap = keymap
        self.world = world
        self.app: EngineApp | None = None
        self._connection: MudConnection | None = None
        self._voice: VoiceRouter | None = None
        self._history: list[str] = []
        self._hist_index = 0
        self._alive = True
        self._pending: list[str] = []
        self._flush_scheduled = False

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
        self._voice = VoiceRouter(make_voice_backend())
        self._connection = MudConnection()
        self.app = EngineApp(
            self._voice,
            send=self._send,
            post=self._post,
            schedule=self._loop.call_later,
            keymap=self._keymap,
        )
        self._connection._on_event = self.app.on_telnet_event
        try:
            await self._connection.connect(self.world.host, self.world.port, tls=self.world.tls)
            self._post(protocol.echo(f"* Connected to {self.world.name}"))
        except OSError as error:
            self._post(protocol.echo(f"* Connect failed: {error}"))

    def _send(self, text: str) -> None:
        try:
            if self._connection is not None:
                self._connection.send_line(text)
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
        if combo and self.app is not None:
            self._loop.call_soon_threadsafe(self.app.on_ws_message, {"type": "key", "key": combo})
            return
        event.Skip()

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
        if self._voice is not None:
            self._voice.flush()
        if self._connection is not None:
            asyncio.create_task(self._connection.close())


class ConnectDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, saved: list[World]):
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

        grid.Add((0, 0))
        self._tls = wx.CheckBox(self, label="Use &TLS")
        self._tls.SetName("Use TLS")
        grid.Add(self._tls, 1, wx.EXPAND)

        grid.Add((0, 0))
        self._save = wx.CheckBox(self, label="Sa&ve this world")
        self._save.SetName("Save this world")
        grid.Add(self._save, 1, wx.EXPAND)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(sizer)

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
        )

    def should_save(self) -> bool:
        return self._save.GetValue()


class GenericMudFrame(wx.Frame):
    def __init__(self, loop: asyncio.AbstractEventLoop, keymap: dict):
        super().__init__(None, title="genericMud", size=(900, 600))
        self._loop = loop
        self._keymap = keymap

        menubar = wx.MenuBar()
        file_menu = wx.Menu()
        connect_item = file_menu.Append(wx.ID_ANY, "&Connect...\tCtrl+N")
        close_item = file_menu.Append(wx.ID_ANY, "Close &Tab\tCtrl+W")
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
        self.Bind(wx.EVT_MENU, self._on_close_tab, close_item)
        self.Bind(wx.EVT_MENU, lambda _e: self.Close(), quit_item)
        self.Bind(wx.EVT_MENU, self._on_toggle_self_voice, self._self_voice_item)

        self.notebook = wx.Notebook(self)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, lambda _e: self._update_active())

    def open_session(self, world: World) -> None:
        panel = SessionPanel(self.notebook, self._loop, self._keymap, world)
        self.notebook.AddPage(panel, world.name, select=True)
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
        index = self.notebook.GetSelection()
        if index != wx.NOT_FOUND:
            self.notebook.GetPage(index).close()  # cancel connection, stop speech
            self.notebook.DeletePage(index)
            self._update_active()

    def _on_toggle_self_voice(self, _event: wx.CommandEvent) -> None:
        self._self_voice = self._self_voice_item.IsChecked()
        self._update_active()

    def _update_active(self) -> None:
        selected = self.notebook.GetSelection()
        for i in range(self.notebook.GetPageCount()):
            self.notebook.GetPage(i).set_active(i == selected and self._self_voice)


def run(args) -> None:
    loop = asyncio.new_event_loop()
    threading.Thread(target=_run_loop, args=(loop,), daemon=True).start()

    wx_app = wx.App(False)
    frame = GenericMudFrame(loop, load_keymap("vipmud"))
    frame.Show()
    if args.host:
        frame.open_session(
            World(name=args.host, host=args.host, port=args.port, tls=args.tls)
        )
    wx_app.MainLoop()
    loop.call_soon_threadsafe(loop.stop)


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()
