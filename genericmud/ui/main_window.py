from __future__ import annotations

import asyncio
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import wx

from genericmud import __version__, help_text
from genericmud.config.ui_prefs import load_ui_prefs, save_ui_prefs
from genericmud.config.update_prefs import is_snoozed, load_prefs, save_prefs, snooze_timestamp
from genericmud.config.world_files import WORLD_FILE_ROOTS, migrate_world_files
from genericmud.config.worlds import World, config_dir, load_worlds, save_worlds
from genericmud.packs import PackError, PackStore, detect_entry, entry_problem, setup_pack, slugify
from genericmud.packs.manifest import CODE_EXEC_DIALECTS
from genericmud.packs.world_share import export_world, import_world
from genericmud.safepath import world_component
from genericmud.session.credentials import open_credential_store
from genericmud.session.diaglog import make_diagnostic_log
from genericmud.session.hub import SessionHub
from genericmud.ui.automation_manager import AutomationManagerDialog
from genericmud.ui.background_work import _run_async
from genericmud.ui.connect_dialog import (
    _ID_DELETE_WORLD,
    _ID_EDIT_WORLD,
    _ID_NEW_WORLD,
    ConnectDialog,
    ConnectionSettingsDialog,
)
from genericmud.ui.help_dialog import HelpDialog
from genericmud.ui.pack_dialogs import PackNotepadFrame
from genericmud.ui.rule_editors import _add_data_rule
from genericmud.ui.self_update_window import (
    _ID_RELEASE_PAGE,
    _ID_SKIP,
    _ID_SNOOZE,
    _ID_UPDATE_NOW,
    UpdateNotificationDialog,
    UpdateProgressDialog,
)
from genericmud.ui.session_panel import SessionPanel
from genericmud.ui.soundpack_browser import VaultBrowserDialog
from genericmud.ui.soundpack_manager import PackManagerDialog
from genericmud.ui.variables_dialog import VariablesDialog
from genericmud.ui.world_dialog import WorldDialog
from genericmud.update import self_update
from genericmud.voice.factory import make_voice_backend

# What each kind of per-world file is called when a rename has to mention it. Keyed by the
# folder in WORLD_FILE_ROOTS, which is the list of the kinds that exist.
_WORLD_FILE_WORDS = {
    "userpacks": "Automation Manager rules",
    "maps": "map",
    "state": "saved variables",
}


def _and_list(items: list[str]) -> str:
    """Join words the way a person says them: "a, b and c"."""
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


_PACK_SOUND_SUFFIXES = frozenset({".wav", ".ogg", ".mp3", ".flac"})  # bundled-audio detection


def _connection_menu_label(active: bool) -> str:
    """The File-menu label for the active tab's connection toggle.

    One item rather than two enabled/disabled ones: Ctrl+D keeps working in both states,
    so the same key has to mean "drop it" now and "bring it back" a moment later.
    """
    return "&Disconnect\tCtrl+D" if active else "&Reconnect\tCtrl+D"


# What came of the world dialog. Saving a world without connecting leaves no tab behind,
# so the caller cannot tell from the tabs whether OK did one thing or both.
_CANCELLED = "cancelled"
_SAVED = "saved"
_CONNECTED = "connected"


@dataclass(frozen=True)
class _WorldDialogDone:
    """What a world dialog did, and the world it did it to.

    The world comes back too: a world saved without being connected to is one the saved-world
    list should offer, so the caller needs its name.
    """

    outcome: str
    world: World | None = None


def _saved_on_same_server(world: World) -> bool:
    """Whether a saved world of this name points at the same server. Logins are kept by world
    name, so a soundpack's world that only shares a saved world's name must not be offered
    that world's login: it would be sent to the pack's server instead."""
    return any(
        saved.name == world.name and saved.host == world.host and saved.port == world.port
        for saved in load_worlds()
    )


_APP_NAME = "genericMud"
# The window title when the world is shown, kept close to MUSHclient's own
# ("MUSHclient - [Penworthy]") so the name sits where someone coming from there looks for it.
# One line to change if the app name should come last instead, which puts the changing part of
# the title first when a screen reader reads it out.
_TITLE_FORMAT = "{app} - [{world}]"


class GenericMudFrame(wx.Frame):
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

    def _save_pref(self, attr: str, value: bool) -> None:
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

    def _on_new_world(self, _event: wx.CommandEvent) -> None:
        self._show_world_dialog(show_connect=True)

    def _show_world_dialog(
        self,
        *,
        initial: World | None = None,
        title: str = "New World",
        connect: bool = True,
        original_name: str | None = None,
        show_save: bool = True,
        show_connect: bool = False,
        lock_name: bool = False,
        note: str = "",
    ) -> _WorldDialogDone:
        """Show the world dialog and do what it was told: save, connect, or both."""
        dialog = WorldDialog(
            self,
            initial,
            title=title,
            save_default=True,
            show_save=show_save,
            show_connect=show_connect,
            lock_name=lock_name,
            credentials=self._credentials,
            note=note,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return _WorldDialogDone(_CANCELLED)
            world = dialog.get_world()
            saved = True
            if dialog.should_save():
                saved = self._save_world(world, replacing=original_name)
            if saved and original_name is not None:
                self._apply_to_open_tabs(original_name, world)
            if not connect:
                return _WorldDialogDone(_SAVED if saved else _CANCELLED, world)
            if not dialog.should_connect():
                if saved:
                    self.announce(
                        f"{world.name} saved. Press Control O when you want to connect to it."
                    )
                return _WorldDialogDone(_SAVED if saved else _CANCELLED, world)
            self.open_session(world)
            return _WorldDialogDone(_CONNECTED, world)
        finally:
            dialog.Destroy()

    def _connected_world_names(self) -> set[str]:
        """The names of the worlds a tab is using right now, folded for comparison.

        A disconnected tab does not count: nothing live is holding its name. Renaming closes
        the log the last session wrote, and the next connection opens the new one.
        """
        names: set[str] = set()
        for index in range(self.book.GetPageCount()):
            panel = self.book.GetPage(index)
            open_world = getattr(panel, "world", None)
            connection = getattr(panel, "_connection", None)
            busy = connection is not None and (
                getattr(connection, "connected", False)
                or getattr(connection, "connecting", False)
            )
            if open_world is not None and busy:
                names.add(open_world.name.casefold())
        return names

    def _edit_saved_world(self, world: World) -> World | None:
        """Edit a saved world, letting its name change while no tab is connected to it.

        Returns the world as saved, or None when nothing was, so the list can keep it picked.

        The name is the log file, the /to address and the tab's label, so a tab that is using
        it keeps it: the box is locked and the dialog says why. A disconnected tab is using
        nothing, so the name is free -- and such a tab follows the rename, opening the new log
        the next time it connects.
        """
        connected = world.name.casefold() in self._connected_world_names()
        dialog = WorldDialog(
            self,
            world,
            title=f"Edit {world.name}",
            save_default=True,
            show_save=False,
            lock_name=connected,
            credentials=self._credentials,
            name_check=lambda name: self._rename_problem(world.name, name),
            note=(
                f"{world.name} is connected in a tab, which is using its name right now. "
                "Disconnect it (Ctrl+D) to rename the world."
                if connected
                else ""
            ),
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            edited = dialog.get_world()
            if not self._save_world(edited, replacing=world.name):
                return None
        finally:
            dialog.Destroy()
        self._apply_to_open_tabs(world.name, edited)
        # Exact, not folded: a change of case is a rename too, and on Linux the files under the
        # old spelling would no longer be found under the new one.
        if edited.name != world.name:
            moved, left = self._move_world_files(world.name, edited.name)
            self._rename_open_tabs(world.name, edited.name)
            if moved:
                self.announce(f"Its {_and_list(moved)} moved with it.")
            if left:
                self.announce(f"Its {_and_list(left)} stayed under the old name.")
        return edited

    def _rename_problem(self, old_name: str, new_name: str) -> str:
        """Why this rename may not take that name, or "" when it may.

        A rename onto a name another world already has would replace that world -- and take its
        stored login with it, because logins are kept by world name. So it is refused where the
        name is entered, rather than offered as a prompt to replace.
        """
        if new_name.casefold() == old_name.casefold():
            return ""
        if any(world.name.casefold() == new_name.casefold() for world in load_worlds()):
            return f"There is already a world called {new_name}. Choose a different name."
        return ""

    def _move_world_files(self, old_name: str, new_name: str) -> tuple[list[str], list[str]]:
        """Carry a renamed world's own files to its new name; return what moved, for saying.

        A world's data is filed under its name: its Automation Manager rules and scripts, its
        room map and its saved pack variables. Left behind, a renamed world comes back with no
        map and no rules, and the files sit under a name nothing points at -- which is exactly
        how the wheels of an earlier rename were found lying about in a real config folder.

        Only ever moves into a name nothing occupies: a file already filed under the new name
        belongs to a world that had it, and is not replaced. Returns the kinds that moved and
        the kinds left behind, named the way the rest of the app names them, for saying.
        """
        old = world_component(old_name)
        new = world_component(new_name)
        if old == new:
            return [], []
        root = config_dir()
        moved: list[str] = []
        left: list[str] = []
        for folder, suffix in WORLD_FILE_ROOTS:
            source = root / folder / f"{old}{suffix}"
            if not source.exists():
                continue
            target = root / folder / f"{new}{suffix}"
            # On Windows and macOS a change of case names the same file: that is this world's
            # own file being respelled, not another world's in the way.
            if target.exists() and not source.samefile(target):
                left.append(_WORLD_FILE_WORDS.get(folder, folder))
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
            except OSError:
                left.append(_WORLD_FILE_WORDS.get(folder, folder))
                continue  # left where it is rather than half moved
            moved.append(_WORLD_FILE_WORDS.get(folder, folder))
        return moved, left

    def _rename_open_tabs(self, old_name: str, new_name: str) -> None:
        """Let a disconnected tab take the world's new name, label and all."""
        followed = False
        for index in range(self.book.GetPageCount()):
            panel = self.book.GetPage(index)
            panel_world = getattr(panel, "world", None)
            if panel_world is None or panel_world.name.casefold() != old_name.casefold():
                continue
            panel.take_world_name(new_name)
            self.book.SetPageText(index, new_name)
            followed = True
        self._update_title()
        if followed:
            self.announce(
                f"Renamed {old_name} to {new_name}. Its tab follows on the next connection."
            )
        else:
            self.announce(f"Renamed {old_name} to {new_name}.")

    def _a_tab_is_connected_to(self, name: str) -> bool:
        """Whether a tab is using this world right now."""
        for index in range(self.book.GetPageCount()):
            panel = self.book.GetPage(index)
            panel_world = getattr(panel, "world", None)
            if panel_world is None or panel_world.name.casefold() != name.casefold():
                continue
            is_connected = getattr(panel, "is_connected", None)
            if is_connected is not None and is_connected():
                return True
        return False

    def _delete_saved_world(self, world: World) -> None:
        """Delete a saved world and its login, and nothing else.

        Asked first, because it is the one thing in the client that destroys something the
        player made. Everything else the world owns stays where it is -- its logs, its sound
        folder, its map, its saved variables and its own automation rules -- so re-adding a
        world by the same name finds its map and its rules again. The password is the exception:
        a login kept for a world that no longer exists is a credential nobody is looking after,
        and the world list is the only place that would have said so.
        """
        connected = self._a_tab_is_connected_to(world.name)
        message = (
            f"Delete the saved world {world.name}?\n\n"
            "Only the saved world and its login -- the saved username and password -- go.\n"
            "Everything else it owns stays where it is, in case you add it back: its log "
            "files, its sound folder, its map, its saved variables and any automation rules "
            "of its own.\n"
            + (
                "A tab is connected to it now: that tab keeps running until you close it.\n"
                if connected
                else ""
            )
            + "\nPress No to keep it."
        )
        dialog = wx.MessageDialog(
            self, message, "Delete World", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING
        )
        try:
            if dialog.ShowModal() != wx.ID_YES:
                return
        finally:
            dialog.Destroy()
        remaining = [
            saved for saved in load_worlds() if saved.name.casefold() != world.name.casefold()
        ]
        try:
            save_worlds(remaining)
        except (OSError, ValueError) as error:
            wx.MessageBox(
                f"Couldn't delete {world.name}: {error}",
                "Delete World",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return
        if self._credentials is not None:
            self._credentials.delete(world.name)
        self.announce(f"{world.name} deleted. Its log files are still there.")

    def _apply_to_open_tabs(self, name: str, world: World) -> None:
        """An edited world's command stacking and logging take effect in its open tabs at
        once; the rest (host, encoding, sounds) when it next connects. A rename lands in the
        window title straight away too."""
        for index in range(self.book.GetPageCount()):
            panel = self.book.GetPage(index)
            if isinstance(panel, SessionPanel) and panel.world.name.casefold() == name.casefold():
                panel.apply_world_settings(world)
        self._update_title()

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
        preselect: str | None = None
        while True:
            dialog = ConnectDialog(self, load_worlds(), preselect=preselect)
            action = dialog.ShowModal()
            world = dialog.get_world()
            dialog.Destroy()
            preselect = None
            if action == wx.ID_OK and world is not None:
                self.open_session(world)
                return
            if action == _ID_NEW_WORLD:
                done = self._show_world_dialog(show_connect=True)
                if done.outcome == _SAVED and done.world is not None:
                    # Saved, not connected: it is in the list now, so show the list with it
                    # picked rather than dropping the reader back into nothing.
                    preselect = done.world.name
                    continue
                return
            if action == _ID_DELETE_WORLD and world is not None:
                self._delete_saved_world(world)
                continue
            if action == _ID_EDIT_WORLD and world is not None:
                edited = self._edit_saved_world(world)
                # Back to the list with the world still picked, under its new name if it has
                # one, rather than at the top of the list.
                preselect = (edited or world).name
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
        # Decided before the pack's world is saved over a same-named one.
        same_server = _saved_on_same_server(world)
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
            prefill_login=same_server,
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
            prefill_login=result.world is not None and _saved_on_same_server(result.world),
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
        # Silence other worlds is the per-tab half of the same idea: with nine worlds open,
        # only the one in front speaks unless the player asks for all of them. Each of them
        # still obeys self-voice and background silence, so the two settings compose.
        every_world = not self._prefs.silence_other_worlds
        for i in range(self.book.GetPageCount()):
            speaks = (i == selected or every_world) and self._self_voice and audible
            self.book.GetPage(i).set_active(speaks)
