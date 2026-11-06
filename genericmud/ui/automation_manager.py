"""The Automation Manager: a world's triggers, aliases, hotkeys, channels, scripts and MUD data.

All six kinds in one dialog, with the script editor it opens.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import wx

from genericmud import help_text
from genericmud.automation.engine import AutomationEngine
from genericmud.packs.user_rules import (
    UserAlias,
    UserChannel,
    UserKey,
    UserRules,
    UserTrigger,
    UserWatch,
    load_rules,
    register_rules,
    save_rules,
)
from genericmud.scripting import user_scripts
from genericmud.scripting.api import ScriptApi
from genericmud.ui.help_dialog import HelpDialog
from genericmud.ui.keyboard import _NUMPAD_KEYS, _hotkey_taken
from genericmud.ui.rule_editors import (
    AliasEditorDialog,
    ChannelEditorDialog,
    DataRuleEditorDialog,
    KeyEditorDialog,
    TriggerEditorDialog,
)
from genericmud.ui.session_panel import SessionPanel


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
