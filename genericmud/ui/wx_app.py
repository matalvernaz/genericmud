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
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import wx

from genericmud import __version__, help_text
from genericmud.automation.engine import AutomationEngine
from genericmud.automation.variables import (
    SCRIPT,
    VariableEntry,
    filter_variables,
    recent_first,
)
from genericmud.config.keymap import load_keymap
from genericmud.config.ui_prefs import load_ui_prefs, save_ui_prefs
from genericmud.config.update_prefs import is_snoozed, load_prefs, save_prefs, snooze_timestamp
from genericmud.config.world_files import WORLD_FILE_ROOTS, migrate_world_files
from genericmud.config.worlds import World, config_dir, load_worlds, save_worlds
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
    UserWatch,
    copy_sound_into_pack,
    load_rules,
    register_rules,
    save_rules,
)
from genericmud.packs.world_share import export_world, import_world
from genericmud.protocol.charset import AUTO
from genericmud.safepath import world_component
from genericmud.scripting import user_scripts
from genericmud.scripting.api import ScriptApi
from genericmud.session.crashlog import install_loop_exception_handler
from genericmud.session.credentials import open_credential_store
from genericmud.session.diaglog import make_diagnostic_log
from genericmud.session.hub import SessionHub
from genericmud.ui.background_work import _run_async
from genericmud.ui.connect_dialog import (
    _ID_DELETE_WORLD,
    _ID_EDIT_WORLD,
    _ID_NEW_WORLD,
    ConnectDialog,
    ConnectionSettingsDialog,
)
from genericmud.ui.help_dialog import HelpDialog
from genericmud.ui.keyboard import _NUMPAD_KEYS, _hotkey_taken, _key_combo
from genericmud.ui.pack_dialogs import PackNotepadFrame
from genericmud.ui.session_panel import SessionPanel
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
        on_watch: Callable[[wx.Window, str], None] | None = None,
    ) -> None:
        title = "Insert a Variable" if pick else f"MUD Variables — {world_name}"
        super().__init__(
            parent, title=title, size=(640, 480),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._fetch = fetch
        self._pick = pick
        self._announce = announce or (lambda _text: None)
        self._on_watch = None if pick else on_watch
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

        # Most recent first shows what the MUD just sent at the top, each row saying how long
        # ago: the way to find out which value changes when you do something in the game.
        self._recent = wx.CheckBox(self, label="&Most recent first")
        self._recent.Bind(wx.EVT_CHECKBOX, lambda _event: self._populate())
        outer.Add(self._recent, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

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
        self._watch: wx.Button | None = None
        if self._on_watch is not None:
            self._watch = wx.Button(self, label="&Watch this...")
            self._watch.Bind(wx.EVT_BUTTON, self._on_watch_selected)
            buttons.Add(self._watch, 0, wx.RIGHT, 6)
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
        if self._recent.GetValue():
            self._shown = recent_first(self._shown)
            self._list.Set([entry.row_with_age for entry in self._shown])
        else:
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
        if self._watch is not None:
            # Only what the MUD sends can be watched; a script's value has no arrival.
            self._watch.Enable(entry is not None and entry.source != SCRIPT)

    def _on_watch_selected(self, _event) -> None:
        """Make a MUD data rule for the selected value, in the same editor the Automation
        Manager uses."""
        entry = self._selected()
        if entry is None or entry.source == SCRIPT or self._on_watch is None:
            return
        self._on_watch(self, entry.name)
        wx.CallAfter(_focus_if_alive, self._list)

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
        self, grid: wx.FlexGridSizer, value: str, *, captures: bool, detail: str | None = None
    ) -> wx.TextCtrl:
        label = "&Commands to send, one per line:"
        grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_TOP)
        ctrl = wx.TextCtrl(
            self, value=value, size=(-1, 80),
            style=wx.TE_MULTILINE | wx.TE_DONTWRAP,
        )
        ctrl.SetName(self._field_name(label))
        grid.Add(ctrl, 1, wx.EXPAND)
        detail = detail or (
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


def _data_rule_problem(pack_dir: Path, rule: UserWatch) -> str | None:
    """Why a MUD data rule can't be saved, or None: the same check the Automation Manager
    makes, registering it on a scratch engine, so one bad rule can't stop the rest loading."""
    if not rule.name.strip():
        return "it needs the name of a value the MUD sends"
    try:
        register_rules(
            ScriptApi(AutomationEngine(), source="user-rule-validation", base_dir=str(pack_dir)),
            UserRules(watches=[replace(rule, enabled=True)]),
        )
    except Exception as error:  # noqa: BLE001 - shown to the player, who can fix the field
        return str(error)
    return None


def _add_data_rule(
    parent: wx.Window, panel: SessionPanel, name: str, announce: Callable[[str], None]
) -> bool:
    """"Watch this" in the MUD variables list: a new MUD data rule for one value, made in the
    Automation Manager's editor and saved with the world's other rules."""
    app = panel.app
    if app is None:
        return False
    pack_dir = app.user_rules_dir()
    rule = UserWatch(name=name)
    while True:
        dialog = DataRuleEditorDialog(parent, pack_dir, rule, variables=panel.variable_snapshot)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return False
            rule = dialog.result()
        finally:
            dialog.Destroy()
        problem = _data_rule_problem(pack_dir, rule)
        if problem is None:
            break
        wx.MessageBox(
            f"This MUD data rule needs a change before it can be saved: {problem}",
            "Check automation", wx.OK | wx.ICON_ERROR, parent,
        )
    rules = load_rules(pack_dir)
    rules.watches.append(rule)
    save_rules(pack_dir, rules)
    panel._loop.call_soon_threadsafe(app.reload_user_rules)
    announce("MUD data rule added. It's in the Automation Manager under MUD data.")
    return True


class DataRuleEditorDialog(_RuleEditorBase):
    """Create/edit one MUD data rule: something to do when the MUD sends a value.

    The value is a GMCP package, an MSDP or MSSP variable, or a path inside one, such as
    Char.Vitals.hp; the MUD variables list (Ctrl+Shift+V) shows what this MUD sends.
    """

    def __init__(
        self, parent: wx.Window, pack_dir: Path, watch: UserWatch, *, variables=None
    ) -> None:
        super().__init__(
            parent, title="MUD data rule",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._variables = variables
        self._pack_dir = pack_dir
        self._enabled = watch.enabled
        outer = wx.BoxSizer(wx.VERTICAL)
        grid = self._grid()
        self._name = self._text(
            grid, "When the MUD sends this &value (for example Char.Vitals.hp):", watch.name
        )
        if variables is not None:
            choose = wx.Button(self, label="Choose from the values the MUD has s&ent...")
            choose.Bind(wx.EVT_BUTTON, self._on_choose)
            grid.Add(wx.StaticText(self, label=""))
            grid.Add(choose)
        self._only_changes = wx.CheckBox(
            self, label="&Only when it changes (many MUDs send it with every prompt)"
        )
        self._only_changes.SetValue(watch.only_changes)
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(self._only_changes)
        self._sound = self._text(grid, "&Sound file (optional):", watch.sound)
        browse = wx.Button(self, label="&Browse for sound...")
        browse.Bind(wx.EVT_BUTTON, self._on_browse)
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(browse)
        self._volume = self._slider(grid, "Vol&ume (0-100):", watch.volume, 0, 100)
        self._pan = self._slider(grid, "&Pan (-100 left to 100 right):", watch.pan, -100, 100)
        self._speak = self._text(
            grid, "Spea&k this text (${value} is the value, ${name} what arrived):", watch.speak
        )
        self._variable_button(grid, self._speak, "Insert a v&ariable into the speech...")
        self._send = self._command_text(
            grid, watch.send, captures=False,
            detail=(
                "Variables: ${value} is the value; ${name} is what arrived; ${mud:HEALTH} is "
                "other data the MUD sent; ${script:name} is a script variable."
            ),
        )
        self._interrupt = wx.CheckBox(self, label="&Interrupt current speech when this fires")
        self._interrupt.SetValue(watch.interrupt)
        grid.Add(wx.StaticText(self, label=""))
        grid.Add(self._interrupt)
        self._finish(outer, grid)

    def _on_choose(self, _event: wx.CommandEvent) -> None:
        """Pick a value the MUD has sent; the field takes its name, not a ${mud:...}."""
        picker = VariablesDialog(self, self._variables, world_name="", pick=True)
        try:
            chosen = picker.chosen if picker.ShowModal() == wx.ID_OK else None
        finally:
            picker.Destroy()
        if chosen is not None and chosen.source != SCRIPT:
            self._name.SetValue(chosen.name)
        wx.CallAfter(_focus_if_alive, self._name)

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

    def result(self) -> UserWatch:
        return UserWatch(
            name=self._name.GetValue().strip(),
            only_changes=self._only_changes.GetValue(),
            sound=self._sound.GetValue().strip(),
            volume=self._volume.GetValue(),
            pan=self._pan.GetValue(),
            speak=self._speak.GetValue().strip(),
            send=self._send.GetValue().strip(),
            interrupt=self._interrupt.GetValue(),
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
    if kind == "data":
        actions = [part for part in (
            f"sound {Path(rule.sound).name}" if rule.sound else "",
            f"speak {rule.speak}" if rule.speak else "",
            f"send {commands}" if commands else "",
            "interrupts speech" if rule.interrupt else "",
        ) if part]
        when = "when it changes" if rule.only_changes else "every time"
        return f"{status}MUD data: {rule.name}, {when}: {'; '.join(actions) or 'no action'}"
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
        (
            "data", "MUD data — when the MUD sends a value",
            "A MUD data rule reacts when the MUD sends a value over GMCP, MSDP or MSSP, "
            "such as your health. It can play a sound, speak, or send commands. The MUD "
            "variables list (Ctrl+Shift+V) shows what this MUD sends.",
        ),
    )
    _CATEGORY_WORDS = {
        "trigger": "Triggers", "alias": "Aliases", "key": "Hotkeys",
        "channel": "Channels", "script": "Scripts", "data": "MUD data",
    }
    _KIND_WORD = {
        "trigger": "Trigger", "alias": "Alias", "key": "Hotkey", "channel": "Channel",
        "data": "MUD data rule",
    }
    _RULE_TYPES = {
        "trigger": UserTrigger, "alias": UserAlias,
        "key": UserKey, "channel": UserChannel, "data": UserWatch,
    }
    _RULE_COLLECTIONS = {
        "trigger": "triggers", "alias": "aliases", "key": "keys", "channel": "channels",
        "data": "watches",
    }
    _REQUIRED_FIELD = {
        "trigger": ("pattern", "text to match"),
        "alias": ("pattern", "the shortcut you will type"),
        "key": ("key", "a key combination"),
        "channel": ("name", "a name"),
        "data": ("name", "the name of a value the MUD sends"),
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
            "data": self._rules.watches,
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
            if ord("1") <= code < ord("1") + len(self._CATEGORIES):
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
        elif kind == "data":
            dialog = DataRuleEditorDialog(self, self._pack_dir, rule, variables=variables)
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

    def _announce_rule_change(self, kind: str, rule: object, message: str) -> None:
        if kind == "key" and rule.enabled and self._panel._prefs.numpad_compass:
            combo = rule.key.strip().lower()
            key = combo.removeprefix("ctrl+").removeprefix("alt+")
            if key != combo:
                key = key.removeprefix("shift+")
            if key in _NUMPAD_KEYS.values():
                message += " Turn off Numpad compass walking under View to use it."
        self._announce(message)

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
            self._announce_rule_change(kind, result, f"{self._KIND_WORD[kind]} added.")

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
                self._announce_rule_change(kind, result, f"{self._KIND_WORD[kind]} updated.")

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
            self._announce_rule_change(kind, result, f"{self._KIND_WORD[kind]} duplicated.")

    def _on_toggle(self, _event: wx.CommandEvent) -> None:
        kind = self._kind()
        if kind == "script" or (index := self._selected_index()) is None:
            return
        items = self._collection(kind)
        items[index] = replace(items[index], enabled=not items[index].enabled)
        if self._save_and_reload_rules(index):
            state = "enabled" if items[index].enabled else "disabled"
            self._announce_rule_change(kind, items[index], f"{self._KIND_WORD[kind]} {state}.")

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


