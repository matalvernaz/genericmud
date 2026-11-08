"""The Automation Manager: a world's triggers, aliases, hotkeys, channels, scripts and MUD data.

All six kinds in one dialog, with the script editor it opens.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from pathlib import Path

import wx

from genericmud import help_text
from genericmud.automation.engine import AutomationEngine
from genericmud.packs import user_rules
from genericmud.packs.user_rules import (
    DEFAULT_ORDER,
    UserAlias,
    UserChannel,
    UserKey,
    UserRules,
    UserTimer,
    UserTrigger,
    UserWatch,
    engine_rule_state,
    load_rules,
    register_rules,
    save_rules,
    send_to_kind,
)
from genericmud.scripting import rule_scripts, user_scripts
from genericmud.scripting.api import ScriptApi
from genericmud.ui.help_dialog import HelpDialog
from genericmud.ui.keyboard import _NUMPAD_KEYS, _hotkey_taken
from genericmud.ui.rule_editors import (
    AliasEditorDialog,
    ChannelEditorDialog,
    DataRuleEditorDialog,
    KeyEditorDialog,
    TimerEditorDialog,
    TriggerEditorDialog,
)
from genericmud.ui.session_panel import SessionPanel

# What each "Send it to" destination does with a rule's text, in the manager list's words:
# first as one of a rule's actions ("send look"), then as the words after an alias's pattern
# ("sh * sends shout %1"). Only the MUD is sent anything; a note is written and a script is run.
_DESTINATION_WORDS = {
    "world": ("send {text}", "sends {text}"),
    "command": ("put {text} in the command box", "puts {text} in the command box"),
    "output": ("note {text}", "notes {text}"),
    "print": ("print {text}", "prints {text}"),
    "log": ("log {text}", "logs {text}"),
    "execute": ("run {text} as if typed", "runs {text} as if typed"),
    "script": ("run script {text}", "runs script {text}"),
    "variable": ("store {text} in variable {name}", "stores {text} in variable {name}"),
}

# Where a world's own scripts register their rules: its script files, and the code a rule sends
# to a script. "From your scripts" lists what came from these.
_SCRIPT_SOURCES = (user_scripts.SOURCE_PREFIX, rule_scripts.RULE_SOURCE)
# How long the list waits for the session's loop to answer before saying it is busy, as long as
# the variables window waits.
_SESSION_ANSWER_TIMEOUT_S = 2.0
# Why each way of making or changing a rule does nothing under "From your scripts". The script's
# text is the only copy of a rule it made, so a change here would be undone when it next loads.
_SCRIPT_RULE_REFUSALS = {
    "new": "Rules from scripts can't be made here; write them in a script, under Scripts.",
    "edit": "Rules from scripts can't be edited here; change the script.",
    "duplicate": "Rules from scripts can't be duplicated here; change the script.",
    "delete": (
        "Rules from scripts can't be deleted here; change the script, or press Disable to "
        "switch one off."
    ),
}


def _sends(rule, commands: str, *, after_pattern: bool = False) -> str:
    """One rule's response text in the words its destination deserves, for the manager list.

    ``after_pattern`` is for an alias, whose line reads as a sentence: "sh * sends shout %1".
    """
    if not commands:
        return ""
    as_action, as_sentence = _DESTINATION_WORDS[send_to_kind(getattr(rule, "send_to", "world"))]
    name = str(getattr(rule, "variable", "") or "").strip() or "(no variable name)"
    return (as_sentence if after_pattern else as_action).format(text=commands, name=name)


def _rule_summary(kind: str, rule) -> str:
    """One spoken line per rule for the manager list."""
    # Plain words, no arrow/dash glyphs: these lines are read by a screen reader.
    status = "Disabled. " if not rule.enabled else ""
    # The label first when it has one: it is what the player named the rule, and what a script
    # refers to it by, so it is the thing they are looking for in the list.
    named = f"{rule.label}: " if getattr(rule, "label", "") else ""
    grouped = f" (group {rule.group})" if getattr(rule, "group", "") else ""
    # The order decides which of two matching rules is heard first, so it belongs beside the
    # kind where it can be compared, not after the action. A rule left at the default says
    # nothing, the way a group you never set says nothing.
    ordered = (
        f", order {rule.order}"
        if getattr(rule, "order", DEFAULT_ORDER) != DEFAULT_ORDER
        else ""
    )
    commands = "; ".join(
        line.strip() for line in rule.send.splitlines() if line.strip()
    ) if hasattr(rule, "send") else ""
    if len(commands) > 180:
        commands = f"{commands[:177]}..."
    if kind == "trigger":
        actions = [part for part in (
            f"sound {Path(rule.sound).name}" if rule.sound else "",
            f"speak {rule.speak}" if rule.speak else "",
            _sends(rule, commands),
            "interrupts speech" if rule.interrupt else "",
            {"speech": "silence line", "line": "remove line"}.get(rule.gag, ""),
            "left out of the log" if getattr(rule, "omit_log", False) else "",
            f"channel {rule.channel}" if rule.channel else "",
        ) if part]
        how = rule.match_kind()
        label = f"{rule.pattern}" if how == "wildcard" else f"{rule.pattern} ({how})"
        return (
            f"{status}{named}Trigger{ordered}{grouped}: {label}: "
            f"{'; '.join(actions) or 'no action'}"
        )
    if kind == "timer":
        when = (
            f"every {rule.every:g} seconds"
            if rule.every > 0 and not rule.at.strip()
            else f"each day at {rule.at or '?'}"
        )
        if getattr(rule, "offset", 0) and rule.every > 0 and not rule.at.strip():
            # Saying it matters: two timers on one interval take turns, and the list is where
            # you see which one goes first.
            when += f", offset {rule.offset:g} seconds"
        once = ", once only" if rule.one_shot else ""
        if getattr(rule, "run_disconnected", False):
            # Worth hearing in the list: it is the difference between a timer that quietly
            # waits for the world and one that speaks while nothing is coming back.
            once += ", runs while disconnected"
        # "every 30 seconds: look" already reads as what happens; any other destination needs
        # saying, because "every 60 seconds: check_buffs" would not tell you it runs a script.
        doing = (
            _sends(rule, commands)
            if send_to_kind(getattr(rule, "send_to", "world")) != "world"
            else commands
        )
        return f"{status}{named}Timer{grouped}: {when}{once}: {doing or 'nothing'}"

    if kind == "alias":
        return (
            f"{status}{named}Alias{ordered}{grouped}: {rule.pattern} "
            f"{_sends(rule, commands, after_pattern=True) or 'sends nothing'}"
        )
    if kind == "key":
        actions = [part for part in (
            _sends(rule, commands),
            f"speak {rule.speak}" if rule.speak else "",
            f"sound {Path(rule.sound).name}" if rule.sound else "",
        ) if part]
        return f"{status}Hotkey{grouped}: {rule.key}: {'; '.join(actions) or 'nothing'}"
    if kind == "data":
        actions = [part for part in (
            f"sound {Path(rule.sound).name}" if rule.sound else "",
            f"speak {rule.speak}" if rule.speak else "",
            _sends(rule, commands),
            "interrupts speech" if rule.interrupt else "",
        ) if part]
        when = "when it changes" if rule.only_changes else "every time"
        return (
            f"{status}MUD data{grouped}: {rule.name}, {when}: "
            f"{'; '.join(actions) or 'no action'}"
        )
    return (
        f"{status}Channel: {rule.name}"
        f" ({'speaks' if rule.speak else 'silent'},"
        f" {'shown' if rule.display else 'hidden'}"
        f"{', interrupts' if rule.interrupt else ''})"
    )


def _live_rule_line(rule) -> str:
    """One spoken line for a rule a script registered: what it is, and what it matches.

    No editing words in it: these rules have no file of their own. What a script wrote is the only
    copy of them, so the line says what they are and whether they are on, and the button to switch
    one off works the way the others do.
    """
    status = "Disabled. " if not rule.enabled else ""
    named = f"{rule.name}: " if rule.name else ""
    what = {
        "trigger": "Trigger", "alias": "Alias", "key": "Hotkey",
        "timer": "Timer", "data": "MUD data rule",
    }[rule.kind]
    grouped = f" (group {rule.group})" if rule.group else ""
    return f"{status}{named}{what} from a script{grouped}: {rule.summary}"


def _live_rule_name(rule) -> str:
    """A rule a script made, as an announcement names it: its name if it has one, and what it
    matches, since two timers on one interval read the same without their names."""
    return f"{rule.name}: {rule.summary}" if rule.name else rule.summary


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
        (
            "timer", "Timers — when a clock reaches a time",
            "A timer fires every so often, or once a day at a time. What it does is the "
            "same question every rule answers: send to the MUD, fill your command box, "
            "write a note, or run a script.",
        ),
        (
            "live", "From your scripts — what a script registered",
            "Rules your Lua scripts made with mud.trigger, mud.alias, mud.key, mud.add_timer "
            "or mud.on_data. They are listed here so a rule a script got wrong is visible, and "
            "can be switched off. The script's own text stays where it is, so they cannot be "
            "edited from this window. A mud.timer is not listed: it has no name, and it runs "
            "once.",
        ),
    )
    _CATEGORY_WORDS = {
        "trigger": "Triggers", "alias": "Aliases", "key": "Hotkeys", "timer": "Timers",
        "channel": "Channels", "script": "Scripts", "data": "MUD data",
        # Not "Script rules": S is the Show box's letter.
        "live": "Your script rules",
    }
    _KIND_WORD = {
        "trigger": "Trigger", "alias": "Alias", "key": "Hotkey", "timer": "Timer",
        "channel": "Channel", "data": "MUD data rule", "live": "Script rule",
    }
    _RULE_TYPES = {
        "trigger": UserTrigger, "alias": UserAlias, "key": UserKey, "timer": UserTimer,
        "channel": UserChannel, "data": UserWatch,
    }
    _RULE_COLLECTIONS = {
        "trigger": "triggers", "alias": "aliases", "key": "keys", "timer": "timers",
        "channel": "channels", "data": "watches",
    }
    _REQUIRED_FIELD = {
        "trigger": ("pattern", "text to match"),
        "timer": ("send", "something for it to do"),
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
        # An unlabelled timer is known to the engine by its place in the file. Once a timer is
        # deleted the places here no longer match the engine's until its reload has run, and
        # asking by place in between would give a timer the state of the one that moved away.
        self._timer_places_stale = False
        self._selections: dict[str, int] = {}
        # The script rules the list shows, as read from the session when it was drawn: a row
        # and its button act on the rule the player heard, even if a script has since changed
        # the engine's own list.
        self._live_shown: list = []
        self._live_switch_waiting = False

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
            "key": self._rules.keys, "timer": self._rules.timers,
            "channel": self._rules.channels, "data": self._rules.watches,
        }[kind]

    def _refresh(
        self,
        select: int | str | None = None,
        *,
        focus: bool = False,
        except_rule=None,
        live_rules: list | None = None,
    ) -> None:
        """Redraw the list for the category being shown.

        ``except_rule`` is a rule saved only now: the reload that gives the engine its new state
        is still waiting on the session's loop, so the engine's word for that one is out of date.
        ``live_rules`` is the script rules as the session's loop has just read them, when the
        caller has them; otherwise they are read here.
        """
        # A script may have switched something since the last look.
        self._sync_from_engine(except_rule=except_rule)
        kind = self._kind()
        _kind, _label, description = self._CATEGORIES[self._category.GetSelection()]
        self._description.SetLabel(description)
        self._description.Wrap(max(self.GetClientSize().GetWidth() - 40, 300))

        if kind == "script":
            labels = user_scripts.list_scripts(self._pack_dir) if self._pack_dir else []
        elif kind == "live":
            shown = live_rules if live_rules is not None else self._read_live_rules()
            if shown is None:
                shown = []
                number = [entry[0] for entry in self._CATEGORIES].index("live") + 1
                self._announce(
                    "The session is busy, so the rules your scripts made can't be listed now. "
                    f"Press Ctrl+{number} to try again."
                )
            self._live_shown = shown
            labels = [_live_rule_line(rule) for rule in shown]
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
        # A rule a script registered has no file to edit and no text to create: it can be listed
        # and switched off, and that is all.
        from_script = kind == "live"
        singular = "script" if is_script else self._KIND_WORD[kind].lower()
        self._buttons["new"].SetLabel(f"&New {singular}...")
        self._buttons["new"].Enable(not from_script)
        self._buttons["edit"].Enable(has_item and not from_script)
        self._buttons["duplicate"].Enable(has_item and not from_script)
        self._buttons["delete"].Enable(has_item and not from_script)
        self._buttons["toggle"].Enable(has_item and not is_script)
        self._buttons["rename"].Enable(has_item and is_script)
        self._buttons["reload"].Enable(is_script)
        self._buttons["folder"].Enable(is_script)
        if has_item and from_script and index < len(self._live_shown):
            enabled = self._live_shown[index].enabled  # the row as drawn, not a fresh read
            self._buttons["toggle"].SetLabel("Disa&ble" if enabled else "E&nable")
        elif has_item and not is_script:
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

    def _sync_from_engine(self, *, except_rule=None) -> None:
        """Take each rule's on/off state from the engine before showing or saving it.

        A script's EnableTrigger, EnableGroup or EnableTimer switches the rule the client runs,
        not the copy in the rules file, so the manager has to ask. ``except_rule`` is the one the
        dialog has only now returned: its Enabled box is the player's decision of this moment, and
        the engine's older state must not overwrite it.
        """
        app = getattr(self._panel, "app", None)
        engine = getattr(app, "engine", None)
        if engine is None:
            return  # no live session (a test, or the manager before a world opens)
        for kind, collection in (
            ("trigger", self._rules.triggers), ("alias", self._rules.aliases),
            ("timer", self._rules.timers), ("key", self._rules.keys),
            ("data", self._rules.watches),
        ):
            if kind == "timer" and self._timer_places_stale:
                continue  # read before the places moved; the reload has not caught up yet
            for index, rule in enumerate(collection):
                if rule is except_rule:
                    continue
                live = engine_rule_state(engine, kind, rule, place=index)
                if live is not None and live != rule.enabled:
                    collection[index] = replace(rule, enabled=live)

    def _read_live_rules(self) -> list | None:
        """The rules this world's scripts registered, read on the session's loop thread.

        Read there, not here: the engine belongs to that thread, where a script adds and drops
        its rules, and a list read from under it could be half changed. The window waits for the
        answer, as the variables window does (SessionPanel.variable_snapshot), so the list is
        filled before the screen reader reads it. ``[]`` with no session; None when the loop is
        busy with a soundpack's question or doesn't answer in time.
        """
        engine = getattr(getattr(self._panel, "app", None), "engine", None)
        if engine is None:
            return []  # no session (a test, or a world still opening): nothing to ask
        if getattr(self._panel, "_questions", None):
            return None  # the loop is waiting on a soundpack's dialog, so it can't answer
        answer: concurrent.futures.Future = concurrent.futures.Future()

        def read() -> None:  # loop thread
            if not answer.set_running_or_notify_cancel():
                return  # the window stopped waiting
            try:
                answer.set_result(engine.live_rules(_SCRIPT_SOURCES))
            except Exception as error:  # noqa: BLE001 - reported to the UI thread, not lost
                answer.set_exception(error)

        try:
            self._panel._loop.call_soon_threadsafe(read)
            return answer.result(timeout=_SESSION_ANSWER_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a timeout, a closed loop or a failed read: say so
            answer.cancel()
            return None

    def _switch_live_rule(self, index: int) -> None:
        """Switch the script rule on that row, on the session's loop thread.

        The row is the rule as it was read when the list was drawn. A script may have dropped it
        since, so the loop checks the engine still runs it before switching it: switching a
        timer the engine has let go of would start it again where nothing can reach it. What
        happened comes back to this thread to be said.
        """
        if self._live_switch_waiting:
            self._announce("Still switching the last one: the session hasn't answered yet.")
            return
        if index >= len(self._live_shown):
            return  # can't happen: the rows are drawn from this list, and only with it
        engine = getattr(getattr(self._panel, "app", None), "engine", None)
        if engine is None:
            return  # without a session the list is empty, so there is no row to act on
        rule = self._live_shown[index]
        wanted = not rule.enabled

        def switch() -> None:  # loop thread
            rows = None
            try:
                running = engine.live_rules(_SCRIPT_SOURCES)
                if any(live.handle is rule.handle for live in running):
                    engine.set_live_enabled(rule.handle, wanted)
                    outcome: object = "switched"
                else:
                    outcome = "gone"
                rows = engine.live_rules(_SCRIPT_SOURCES)
            except Exception as error:  # noqa: BLE001 - said on the UI thread, not lost
                outcome = error
            wx.CallAfter(self._live_rule_switched, rule, wanted, outcome, rows)

        self._live_switch_waiting = True
        try:
            self._panel._loop.call_soon_threadsafe(switch)
        except RuntimeError as error:  # the loop has closed: the session is gone
            self._live_switch_waiting = False
            self._announce(f"Script rule {_live_rule_name(rule)} was not switched: {error}.")

    def _live_rule_switched(self, rule, wanted: bool, outcome: object, rows) -> None:  # UI
        """Say what the switch did, then redraw the list from the session's answer."""
        self._live_switch_waiting = False
        name = _live_rule_name(rule)
        if outcome == "switched":
            self._announce(f"Script rule {name} {'enabled' if wanted else 'disabled'}.")
        elif outcome == "gone":
            self._announce(
                f"Script rule {name} was not switched: its script has removed it since the "
                "list was read."
            )
        else:
            self._announce(f"Script rule {name} was not switched: {outcome}")
        if not self or self._kind() != "live":
            return  # the manager has closed, or shows another category; nothing to redraw
        place = next(
            (spot for spot, live in enumerate(rows or []) if live.handle is rule.handle), None
        )
        self._refresh(place if place is not None else self._selected_index(), live_rules=rows)

    def _spent_one_shots(self) -> list[tuple[str, object]]:
        """The timers here that are one-shots the engine has seen fire, with their names.

        Asked by place, so only while the places still match the engine's: after a delete the
        names would point at whichever timer moved into the gap.
        """
        engine = getattr(getattr(self._panel, "app", None), "engine", None)
        if engine is None or self._timer_places_stale:
            return []
        spent = {
            name for name, origin in engine.spent_one_shots.items() if origin == user_rules.SOURCE
        }
        if not spent:
            return []
        return [
            (name, timer)
            for place, timer in enumerate(self._rules.timers)
            if timer.one_shot and (name := user_rules.timer_name(timer, place)) in spent
        ]

    def _save_and_reload_rules(
        self, select: int | None = None, *, just_edited=None, spent=None
    ) -> bool:
        """Save the rules and reload them live.

        ``just_edited`` is the rule the dialog has only now returned; every other rule takes its
        on/off state from the engine first, so saving one rule cannot switch a script's decision
        back on. ``spent`` is the fired one-shots as found before a delete moved the places.
        """
        if self._pack_dir is None:
            return False
        # The engine's word first, while the places still match: dropping a spent one-shot
        # below moves the timers after it.
        self._sync_from_engine(except_rule=just_edited)
        # A one-shot that has fired is not saved to run again, which is what MUSHclient does with
        # one: its timer list loses it when it fires, so its file loses it at the next save. Taken
        # out by identity, never by a place that a delete may have moved.
        found = spent if spent is not None else self._spent_one_shots()
        gone = [timer for _name, timer in found]
        kept = [timer for timer in self._rules.timers if not any(timer is g for g in gone)]
        if len(kept) != len(self._rules.timers):
            self._rules.timers = kept
            self._timer_places_stale = True
            app = getattr(self._panel, "app", None)
            if app is not None and app.diag is not None:
                app.diag.event("user_rules.one_shots_spent", names=[name for name, _ in found])
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
            self._timer_places_stale = False  # back to the file the engine is running
            self._refresh()
            return False
        if self._panel.app is not None:
            app = self._panel.app

            def reload() -> None:  # loop thread
                try:
                    app.reload_user_rules()
                finally:
                    wx.CallAfter(self._timer_places_caught_up)

            self._panel._loop.call_soon_threadsafe(reload)
        self._refresh(select, except_rule=just_edited)
        return True

    def _timer_places_caught_up(self) -> None:
        """The session has reloaded the saved rules, so a timer's place means the same again."""
        if self:  # the manager may have closed while the reload ran
            self._timer_places_stale = False

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
        elif kind == "timer":
            taken = {
                other.label.strip()
                for other in self._rules.timers
                if other is not rule and other.label.strip()
            }
            dialog = TimerEditorDialog(self, rule, labels=taken)
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
        if kind == "live":
            self._announce(_SCRIPT_RULE_REFUSALS["new"])
            return
        if kind == "script":
            self._new_script()
            return
        result = self._edit_rule(kind, self._RULE_TYPES[kind]())
        if result is None:
            return
        items = self._collection(kind)
        items.append(result)
        if self._save_and_reload_rules(len(items) - 1, just_edited=result):
            self._announce_rule_change(kind, result, f"{self._KIND_WORD[kind]} added.")

    def _on_edit(self, _event: wx.CommandEvent) -> None:
        if self._kind() == "live":
            self._announce(_SCRIPT_RULE_REFUSALS["edit"])
            return
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
            if self._save_and_reload_rules(index, just_edited=result):
                self._announce_rule_change(kind, result, f"{self._KIND_WORD[kind]} updated.")

    def _on_duplicate(self, _event: wx.CommandEvent) -> None:
        if self._kind() == "live":
            self._announce(_SCRIPT_RULE_REFUSALS["duplicate"])
            return
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
        if self._save_and_reload_rules(len(items) - 1, just_edited=result):
            self._announce_rule_change(kind, result, f"{self._KIND_WORD[kind]} duplicated.")

    def _on_toggle(self, _event: wx.CommandEvent) -> None:
        kind = self._kind()
        if kind == "script" or (index := self._selected_index()) is None:
            return
        if kind == "live":
            self._switch_live_rule(index)
            return
        items = self._collection(kind)
        # The opposite of what the list said, which is what the button said it would do. It is
        # the player's decision of this moment, so the engine's older state must not undo it,
        # either before the save or in the redraw after it.
        toggled = replace(items[index], enabled=not items[index].enabled)
        items[index] = toggled
        if self._save_and_reload_rules(index, just_edited=toggled):
            state = "enabled" if toggled.enabled else "disabled"
            self._announce_rule_change(kind, toggled, f"{self._KIND_WORD[kind]} {state}.")

    def _on_delete(self, _event: wx.CommandEvent) -> None:
        if self._kind() == "live":
            self._announce(_SCRIPT_RULE_REFUSALS["delete"])
            return
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
        # Ask the engine while the places still match it: once a timer is gone, the ones after
        # it move up, and the engine only learns their new places from the reload. The fired
        # one-shots are found now for the same reason.
        self._sync_from_engine()
        spent = self._spent_one_shots()
        del self._collection(kind)[index]
        if kind == "timer":
            self._timer_places_stale = True
        if self._save_and_reload_rules(index, spent=spent):
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
