"""The MUD variables window: every value a world knows, to read, copy or watch."""

from __future__ import annotations

from collections.abc import Callable

import wx

from genericmud.automation.variables import (
    SCRIPT,
    VariableEntry,
    filter_variables,
    recent_first,
)

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


def _focus_if_alive(window: wx.Window) -> None:
    """Focus ``window`` from a deferred call, unless it was destroyed in the meantime."""
    if window:  # a destroyed wx window is falsy
        window.SetFocus()
