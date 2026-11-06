"""Installed soundpacks: install, enable, trust, uninstall, check and update them per world."""

from __future__ import annotations

import traceback

import wx

from genericmud.automation.engine import AutomationEngine
from genericmud.config.worlds import World
from genericmud.packs import (
    GitUpdateResult,
    PackError,
    PackStore,
    activate_world,
    describe_update,
    git_sources,
    manifest_sources,
    setup_pack_from_manifest,
    update_pack,
    update_pack_from_git,
    vault,
)
from genericmud.ui.background_work import _run_async
from genericmud.ui.soundpack_browser import _SOURCE_MAX_BYTES


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
            wx.MessageBox(describe_update(outcome), "Update", wx.OK | wx.ICON_INFORMATION)
        else:
            if self._diag is not None:
                self._diag.event("update.done", id=pack_id, dialect=outcome.manifest.dialect)
            wx.MessageBox(
                f"Updated {pack_id}. Reconnect to apply.", "Update", wx.OK | wx.ICON_INFORMATION,
            )
        self._refresh_packs()
