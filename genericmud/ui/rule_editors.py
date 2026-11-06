"""The editor dialogs for each kind of automation rule.

Triggers, aliases, hotkeys, MUD data rules and channels, and "Watch this" from the variables
window, which makes a MUD data rule for one value.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import wx

from genericmud.automation.engine import AutomationEngine
from genericmud.automation.variables import SCRIPT, VariableEntry
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
from genericmud.scripting.api import ScriptApi
from genericmud.ui.keyboard import _key_combo
from genericmud.ui.session_panel import SessionPanel
from genericmud.ui.variables_dialog import VariablesDialog, _focus_if_alive


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
