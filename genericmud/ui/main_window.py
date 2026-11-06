"""The main window: its menus, a tab per world, who speaks, the title, and the tool windows.

Saved worlds, soundpack setup and updating genericMud are in three mixins, each in its own
module: saved_worlds, soundpack_setup and self_update_window.
"""

from __future__ import annotations

import asyncio
import threading

import wx

from genericmud import __version__, help_text
from genericmud.config.ui_prefs import load_ui_prefs, save_ui_prefs
from genericmud.config.world_files import migrate_world_files
from genericmud.config.worlds import World, config_dir, load_worlds
from genericmud.packs import PackStore
from genericmud.session.credentials import open_credential_store
from genericmud.session.diaglog import make_diagnostic_log
from genericmud.session.hub import SessionHub
from genericmud.ui.automation_manager import AutomationManagerDialog
from genericmud.ui.connect_dialog import ConnectionSettingsDialog
from genericmud.ui.help_dialog import HelpDialog
from genericmud.ui.pack_dialogs import PackNotepadFrame
from genericmud.ui.rule_editors import _add_data_rule
from genericmud.ui.saved_worlds import SavedWorldsMixin
from genericmud.ui.self_update_window import SelfUpdateMixin, UpdateProgressDialog
from genericmud.ui.session_panel import SessionPanel
from genericmud.ui.soundpack_manager import PackManagerDialog
from genericmud.ui.soundpack_setup import SoundpackSetupMixin
from genericmud.ui.variables_dialog import VariablesDialog
from genericmud.voice.factory import make_voice_backend


def _connection_menu_label(active: bool) -> str:
    """The File-menu label for the active tab's connection toggle.

    One item rather than two enabled/disabled ones: Ctrl+D keeps working in both states,
    so the same key has to mean "drop it" now and "bring it back" a moment later.
    """
    return "&Disconnect\tCtrl+D" if active else "&Reconnect\tCtrl+D"


_APP_NAME = "genericMud"
# The window title when the world is shown, kept close to MUSHclient's own
# ("MUSHclient - [Penworthy]") so the name sits where someone coming from there looks for it.
# One line to change if the app name should come last instead, which puts the changing part of
# the title first when a screen reader reads it out.
_TITLE_FORMAT = "{app} - [{world}]"


class GenericMudFrame(SavedWorldsMixin, SoundpackSetupMixin, SelfUpdateMixin, wx.Frame):
    def __init__(self, loop: asyncio.AbstractEventLoop, keymap: dict):
        super().__init__(None, title=_APP_NAME, size=(900, 600))
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
        connection_settings_item = file_menu.Append(
            wx.ID_ANY, "Connection &settings...", "Automatic reconnect settings for all worlds",
        )
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
        # The tab-level twin of background silence: nine worlds open, one of them in front.
        self._other_worlds_item = view_menu.AppendCheckItem(
            wx.ID_ANY, "Silence &other worlds",
            "Only the world you're looking at speaks; off lets every open world speak",
        )
        self._other_worlds_item.Check(self._prefs.silence_other_worlds)
        self._numpad_item = view_menu.AppendCheckItem(
            wx.ID_ANY, "&Numpad compass walking",
            "Numpad walks: 8/2/4/6 and diagonals, 5 or 0 look, period scans, minus up, plus down",
        )
        self._numpad_item.Check(self._prefs.numpad_compass)
        # Off by default: most people don't want their window renamed under them, and
        # MUSHclient's own title is opt-in in the same way.
        self._title_world_item = view_menu.AppendCheckItem(
            wx.ID_ANY, "World name in the window &title",
            "Call the window after the world you're in, as MUSHclient does",
        )
        self._title_world_item.Check(self._prefs.title_shows_world)
        input_menu = wx.Menu()
        standard_item = input_menu.AppendRadioItem(wx.ID_ANY, "&Standard")
        editable_item = input_menu.AppendRadioItem(
            wx.ID_ANY, "&Editable command buffer (VIPMud-style)",
        )
        standard_item.Check(self._prefs.command_input_mode == "standard")
        editable_item.Check(self._prefs.command_input_mode == "editable")
        view_menu.AppendSubMenu(
            input_menu, "Command &input mode",
            "Applies to newly opened sessions; existing sessions keep their mode",
        )
        self.Bind(wx.EVT_MENU,
                  lambda _e: self._save_pref("command_input_mode", "standard"), standard_item)
        self.Bind(wx.EVT_MENU,
                  lambda _e: self._save_pref("command_input_mode", "editable"), editable_item)
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
        self.Bind(wx.EVT_MENU, self._on_connection_settings, connection_settings_item)
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
        self.Bind(wx.EVT_MENU, self._on_toggle_other_worlds, self._other_worlds_item)
        self.Bind(wx.EVT_MENU, self._on_toggle_numpad, self._numpad_item)
        self.Bind(wx.EVT_MENU, self._on_toggle_title_world, self._title_world_item)

        self.book = wx.Simplebook(self)  # no tab strip -> nothing in the keyboard Tab order
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)  # Ctrl+Tab cycles sessions
        self.Bind(wx.EVT_CLOSE, self._on_frame_close)  # confirm + disconnect before exit
        self.Bind(wx.EVT_ACTIVATE, self._on_app_activate)  # background-silence focus tracking

        self._update_progress_dialog: UpdateProgressDialog | None = None
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
        """Show tab ``index``, and say which world it is when that actually changes it."""
        moved = index != self.book.GetSelection()
        self.book.ChangeSelection(index)  # ChangeSelection: no page-changed event to handle
        self._update_active()
        self._update_focus()
        # Focus the new page's command box; on its own that is not enough to announce the
        # session, since the control keeps focus from tab to tab and a silent focus move says
        # nothing. So it is spoken too, which is also what makes Ctrl+Tab audible.
        self.book.GetPage(index).input.SetFocus()
        if moved:
            self._announce_tab(index)

    def handle_tab_action(self, action: str) -> bool:
        """Do a window-side keymap action ("tab:select:3"); False when it isn't one of ours.

        Called by a session panel for a combo the keymap binds to a ``tab:`` action, before
        the engine gets a chance to swallow or mis-send it.
        """
        namespace, _, rest = action.partition(":")
        if namespace != "tab":
            return False
        verb, _, argument = rest.partition(":")
        if verb == "select":
            try:
                number = int(argument)
            except ValueError:
                return True  # a mistyped keymap entry: swallowed, not typed at the MUD
            self._select_tab(number)
        return True

    def _select_tab(self, number: int) -> None:
        """Switch to world ``number``, counting from the order the tabs were opened."""
        count = self.book.GetPageCount()
        if count == 0:
            self.announce("No worlds are open.")
            return
        if not 1 <= number <= count:
            self.announce(f"Only {count} world{'s' if count != 1 else ''} open.")
            return
        index = number - 1
        if index == self.book.GetSelection():
            # They asked where they are, so say it even though nothing moved.
            self._announce_tab(index)
            return
        self._switch_session(index)

    def _announce_tab(self, index: int) -> None:
        """Say which world is in front now, and which tab it is of how many."""
        count = self.book.GetPageCount()
        name = self.book.GetPage(index).world.name
        self.announce(f"{name}, tab {index + 1} of {count}")

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

    def _save_pref(self, attr: str, value: bool | str) -> None:
        setattr(self._prefs, attr, value)
        save_ui_prefs(self._prefs)

    def _on_connection_settings(self, _event: wx.CommandEvent) -> None:
        dialog = ConnectionSettingsDialog(self, self._prefs)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self._prefs.auto_reconnect, self._prefs.reconnect_interval = dialog.get_settings()
                save_ui_prefs(self._prefs)
                for index in range(self.book.GetPageCount()):
                    self.book.GetPage(index).apply_connection_settings(
                        self._prefs.auto_reconnect, self._prefs.reconnect_interval,
                    )
        finally:
            dialog.Destroy()

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
        # The window's own label follows the world in front, so it is refreshed here with the
        # pack events: this runs on every switch, open and close, which is exactly when it can
        # change.
        self._update_title()

    def _on_toggle_bg_silence(self, _event: wx.CommandEvent) -> None:
        self._save_pref("background_silence", self._bg_silence_item.IsChecked())
        self._update_active()
        self.announce(
            "Background silence on. genericMud stays quiet while you're in another window."
            if self._prefs.background_silence else "Background silence off."
        )

    def _on_toggle_other_worlds(self, _event: wx.CommandEvent) -> None:
        self._save_pref("silence_other_worlds", self._other_worlds_item.IsChecked())
        self._update_active()
        self.announce(
            "Silencing other worlds. Only the world you're looking at speaks."
            if self._prefs.silence_other_worlds
            else "Every open world speaks now."
        )

    def _on_toggle_numpad(self, _event: wx.CommandEvent) -> None:
        self._save_pref("numpad_compass", self._numpad_item.IsChecked())
        self.announce(
            "Numpad compass on." if self._prefs.numpad_compass else "Numpad compass off."
        )

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

    def _active_panel(self) -> SessionPanel | None:
        index = self.book.GetSelection()
        if index == wx.NOT_FOUND or not self.book.GetPageCount():
            return None
        return self.book.GetPage(index)

    def _refresh_connection_item(self) -> None:
        """Point the File item at what it will actually do for the active tab."""
        panel = self._active_panel()
        if panel is not None:
            label = _connection_menu_label(panel.is_connected() or panel.is_connecting())
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
        if panel.is_connected() or panel.is_connecting():
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
            self, panel.variable_snapshot, world_name=panel.world.name, announce=self.announce,
            on_watch=lambda parent, name: _add_data_rule(parent, panel, name, self.announce),
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

    def _window_title(self) -> str:
        """What the window should be called: the world in front, or just the app.

        Asked of the window rather than remembered, so it can't drift from which tab is
        actually selected.
        """
        name = self._active_world_name()
        if name and self._prefs.title_shows_world:
            return _TITLE_FORMAT.format(app=_APP_NAME, world=name)
        return _APP_NAME

    def _update_title(self) -> None:
        self.SetTitle(self._window_title())

    def _on_toggle_title_world(self, _event: wx.CommandEvent) -> None:
        self._save_pref("title_shows_world", self._title_world_item.IsChecked())
        self._update_title()
        self.announce(
            "The window title names the world you're in."
            if self._prefs.title_shows_world
            else "The window title is just genericMud."
        )

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
        # Silence other worlds is the per-tab half of the same idea: with nine worlds open,
        # only the one in front speaks unless the player asks for all of them. Each of them
        # still obeys self-voice and background silence, so the two settings compose.
        every_world = not self._prefs.silence_other_worlds
        for i in range(self.book.GetPageCount()):
            speaks = (i == selected or every_world) and self._self_voice and audible
            self.book.GetPage(i).set_active(speaks)
