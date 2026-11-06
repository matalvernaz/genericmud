"""Connect to a saved world, and the automatic reconnect settings every world shares."""

from __future__ import annotations

import sys

import wx

from genericmud.config.ui_prefs import UiPrefs
from genericmud.config.worlds import World
from genericmud.protocol.charset import AUTO, encoding_label


class ConnectionSettingsDialog(wx.Dialog):
    """Global recovery settings; native controls keep Tab, Enter and Escape navigation."""

    def __init__(self, parent: wx.Window, prefs: UiPrefs) -> None:
        super().__init__(parent, title="Connection settings")
        outer = wx.BoxSizer(wx.VERTICAL)
        self._auto = wx.CheckBox(self, label="&Auto reconnect")
        self._auto.SetName("Auto reconnect")
        self._auto.SetValue(prefs.auto_reconnect)
        outer.Add(self._auto, 0, wx.ALL, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._interval_label = wx.StaticText(self, label="Reconnect every (&seconds):")
        row.Add(self._interval_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._interval = wx.SpinCtrl(self, min=1, max=300, initial=prefs.reconnect_interval)
        self._interval.SetName("Reconnect every, seconds")
        row.Add(self._interval, 1)
        outer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        outer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 10)
        self.SetSizerAndFit(outer)
        self.SetEscapeId(wx.ID_CANCEL)
        self._auto.Bind(wx.EVT_CHECKBOX, lambda event: self._update_enabled())
        self._update_enabled()
        self._auto.SetFocus()

    def _update_enabled(self) -> None:
        self._interval.Enable(self._auto.GetValue())
        self._interval_label.Enable(self._auto.GetValue())

    def get_settings(self) -> tuple[bool, int]:
        return self._auto.GetValue(), self._interval.GetValue()


_ID_NEW_WORLD = wx.ID_HIGHEST + 1
_ID_EDIT_WORLD = wx.ID_HIGHEST + 2
_ID_DELETE_WORLD = wx.ID_HIGHEST + 3


class ConnectDialog(wx.Dialog):
    """Choose an existing saved world; creation and editing use ``WorldDialog``."""

    def __init__(
        self, parent: wx.Window, saved: list[World], preselect: str | None = None
    ) -> None:
        super().__init__(parent, title="Connect to a Saved World", size=(460, 320))
        self._saved = sorted(saved, key=lambda world: world.name.casefold())
        outer = wx.BoxSizer(wx.VERTICAL)

        outer.Add(wx.StaticText(self, label="&Saved worlds:"), 0, wx.LEFT | wx.TOP, 10)
        self._choice = wx.ListBox(self, choices=[world.name for world in self._saved])
        self._choice.SetName("Saved worlds")
        self._choice.Bind(wx.EVT_LISTBOX, self._on_pick)
        self._choice.Bind(wx.EVT_LISTBOX_DCLICK, self._on_connect)
        outer.Add(self._choice, 1, wx.ALL | wx.EXPAND, 10)

        # The D here moved to the end of the word so that the Delete button below can have it:
        # Alt+D for the button that says Delete, and Alt+T for the details box, which is read,
        # not written, and is the one of the two nobody presses a key for.
        outer.Add(wx.StaticText(self, label="Connection de&tails:"), 0, wx.LEFT, 10)
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
        # Alt+D, the letter the button is spelled with and the key that deletes things
        # everywhere else: the label above moved its own D along to make room, because two
        # Alt+D's in one dialog is how a key ends up focusing the wrong control. Not Alt+R,
        # which is the way back to a breadcrumb across the whole app -- and a mnemonic rather
        # than a bare accelerator, so the button is read out with its key attached and nobody
        # has to find it by accident or remember it.
        delete = wx.Button(self, _ID_DELETE_WORLD, "&Delete...")
        delete.Bind(wx.EVT_BUTTON, self._on_delete)
        # Delete with the list focused does the same. A list box does nothing with Delete on
        # Windows, so this takes nothing from it.
        self._choice.Bind(wx.EVT_KEY_DOWN, self._on_choice_key)
        buttons.Add(self._connect, 0, wx.RIGHT, 6)
        buttons.Add(edit, 0, wx.RIGHT, 6)
        buttons.Add(delete, 0, wx.RIGHT, 6)
        buttons.Add(new, 0, wx.RIGHT, 6)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "C&ancel"), 0)
        outer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.SetSizer(outer)

        enabled = bool(self._saved)
        self._connect.Enable(enabled)
        edit.Enable(enabled)
        delete.Enable(enabled)
        if enabled:
            self._choice.SetSelection(self._index_of(preselect))
            self._update_details()
            self._connect.SetDefault()
            self._choice.SetFocus()
        else:
            self._details.SetValue("No saved worlds. Choose New World to create one.")
            new.SetDefault()
            new.SetFocus()

    def _index_of(self, name: str | None) -> int:
        """The row holding this world, or the first row: pressing Enter never lands on nothing."""
        wanted = (name or "").casefold()
        for index, world in enumerate(self._saved):
            if world.name.casefold() == wanted:
                return index
        return 0

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

    def _on_choice_key(self, event: wx.KeyEvent) -> None:
        """Delete removes the picked world -- only with the list focused and one selected."""
        code = event.GetKeyCode()
        # The key a Mac keyboard labels "delete" reaches wx as Backspace; forward delete
        # needs fn there, so it counts too. A list box has no use for Backspace either way.
        deleting = code == wx.WXK_DELETE or (code == wx.WXK_BACK and sys.platform == "darwin")
        if deleting and self.get_world() is not None:
            self.EndModal(_ID_DELETE_WORLD)
            return
        event.Skip()

    def _on_delete(self, _event: wx.CommandEvent) -> None:
        if self.get_world() is not None:
            self.EndModal(_ID_DELETE_WORLD)

    def _on_edit(self, _event: wx.CommandEvent) -> None:
        if self.get_world() is not None:
            self.EndModal(_ID_EDIT_WORLD)

    def get_world(self) -> World | None:
        index = self._choice.GetSelection()
        return self._saved[index] if 0 <= index < len(self._saved) else None
