"""The main window's saved-world actions.

New World, connecting to a saved world, editing, renaming with the world's own files, deleting,
exporting and importing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import wx

from genericmud.config.world_files import WORLD_FILE_ROOTS
from genericmud.config.worlds import World, config_dir, load_worlds, save_worlds
from genericmud.packs import PackError, slugify
from genericmud.packs.world_share import export_world, import_world
from genericmud.safepath import world_component
from genericmud.ui.connect_dialog import (
    _ID_DELETE_WORLD,
    _ID_EDIT_WORLD,
    _ID_NEW_WORLD,
    ConnectDialog,
)
from genericmud.ui.session_panel import SessionPanel
from genericmud.ui.world_dialog import WorldDialog

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


class SavedWorldsMixin:
    """The main window's saved-world actions, for GenericMudFrame (main_window.py).

    Uses _credentials and book from GenericMudFrame.__init__, and the window's announce,
    open_session and _update_title.
    """

    def _on_new_world(self, _event: wx.CommandEvent) -> None:
        self._show_world_dialog(show_connect=True)

    def _show_world_dialog(self, *, show_connect: bool = False) -> _WorldDialogDone:
        """Show the New World dialog and do what it was told: save, connect, or both."""
        dialog = WorldDialog(
            self,
            title="New World",
            save_default=True,
            show_save=True,
            show_connect=show_connect,
            credentials=self._credentials,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return _WorldDialogDone(_CANCELLED)
            world = dialog.get_world()
            saved = True
            if dialog.should_save():
                saved = self._save_world(world)
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
