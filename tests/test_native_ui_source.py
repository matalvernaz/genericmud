"""Build-blind structural checks for the native wx world flows."""

from __future__ import annotations

import ast
from pathlib import Path


def test_tab_keys_come_from_the_keymap_but_are_run_by_the_window():
    # The binding has to be editable like every other key, so it is read from the keymap; the
    # work has to happen in the window, because the engine serves one world and cannot see the
    # tabs. So the panel must divert `tab:` actions before the engine is handed the key.
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    handler = source.split("def _dispatch_bound_combo", 1)[1].split("def _send_command", 1)[0]
    assert 'action.startswith("tab:")' in handler
    assert "handle_tab_action" in handler
    assert handler.index("handle_tab_action") < handler.index('{"type": "key"')


def test_both_ways_of_changing_tabs_say_where_you_landed():
    # Ctrl+Tab goes through _switch_session, and so does Alt+<number>; the announcement lives
    # there so the two cannot drift apart.
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    switch = source.split("def _switch_session", 1)[1].split("def handle_tab_action", 1)[0]
    assert "_announce_tab(index)" in switch
    select = source.split("def _select_tab", 1)[1].split("def _announce_tab", 1)[0]
    assert "_announce_tab(index)" in select


def test_new_world_and_saved_connect_use_distinct_dialogs_and_commands():
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    classes = {
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef)
    }

    assert {"WorldDialog", "ConnectDialog"} <= classes
    assert '"&New World...\\tCtrl+N"' in source
    assert '"&Connect to Saved World...\\tCtrl+O"' in source


def test_find_shortcuts_remain_scoped_to_the_output_control():
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    assert "self.output.Bind(wx.EVT_KEY_DOWN, self._on_output_key)" in source
    assert '"&Find in Output...\\tCtrl+F"' not in source


def test_find_caret_mapping_stays_in_native_units():
    # Insertion-point units and GetValue() offsets disagree on Windows (a line break
    # is two native characters, one in the string), so the caret sync must convert
    # through the control's own line functions, never position by string offset.
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    assert "find_offset" not in source
    assert "self.output.PositionToXY(self.output.GetInsertionPoint())" in source
    assert "self.output.XYToPosition(0, line)" in source


def test_output_caret_survives_appends_while_find_dialog_is_open():
    # While the modal Find dialog holds focus, arriving output must not yank the
    # underlying insertion point: _keep_caret_on_focus promises the search runs from
    # the reader's position, so the flush has to anchor whenever that flag is set,
    # not only while the output itself has focus.
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    assert "preserve = self.output.HasFocus() or self._keep_caret_on_focus" in source


def test_history_recall_parks_and_restores_the_unsent_draft():
    # Up parks the live edit line; Down past the newest entry restores it instead of
    # blanking the field (silent data loss for a blind typist).
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    assert "self._history_draft = self.input.GetValue()" in source
    assert "else self._history_draft" in source


def _wx_source() -> str:
    return (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")


def test_bound_combos_dispatch_from_the_output_box_too():
    # The panic keys (Escape/F11 stop speech, Shift+F11 stop sound) and other macros must
    # work while the reader is focused in the output, not only in the command box.
    source = _wx_source()
    assert "def _dispatch_bound_combo" in source
    assert source.count("self._dispatch_bound_combo(") >= 2  # input AND output handlers


def test_soundpacks_and_unified_automation_have_distinct_menus_clear_of_retrace_key():
    # Installed packs and user-authored automation are different jobs. Alt+R stays free for
    # nav:retrace; Automation uses one manager instead of separate basic/script paths.
    source = _wx_source()
    assert '"Sound&packs"' in source
    assert '"&Automation"' in source
    assert '"R&ules"' not in source and '"&Rules"' not in source
    assert '"&Manage automation...\\tCtrl+B"' in source
    assert '"Automation &help..."' in source
    assert "AutomationManagerDialog" in source
    assert "RulesBuilderDialog" not in source
    assert "AutomationScriptsDialog" not in source
    assert '"Browse soundpacks &online...\\tCtrl+Shift+B"' in source
    assert '"alt+f", "alt+p", "alt+a", "alt+v", "alt+h"' in source


def test_automation_manager_has_direct_categories_and_contextual_script_actions():
    source = _wx_source()
    for category in (
        "Triggers — when the MUD sends text",
        "Aliases — when I type a shortcut",
        "Hotkeys — when I press a key",
        "Channels — how matching lines are handled",
        "Scripts — reusable Lua automation",
    ):
        assert category in source
    assert 'self._buttons["rename"].Enable(has_item and is_script)' in source
    assert 'self._buttons["reload"].Enable(is_script)' in source
    assert 'self._buttons["folder"].Enable(is_script)' in source
    assert 'self._buttons["new"].SetLabel(f"&New {singular}...")' in source
    assert "wx.EVT_LISTBOX_DCLICK, self._on_edit" in source
    assert 'if ord("1") <= code <= ord("5")' in source
    assert 'if code == ord("N")' in source


def test_automation_manager_validates_required_fields_instead_of_silently_dropping():
    source = _wx_source()
    assert "def _complete(self, kind" in source
    assert "self._complete(kind, result) and self._valid_rule(kind, result)" in source


def test_rule_editors_accept_command_stacks_and_explain_shared_variables():
    source = _wx_source()
    assert "wx.TE_MULTILINE | wx.TE_DONTWRAP" in source
    assert "Commands to send, one per line" in source
    assert "${script:name}" in source
    assert "${mud:HEALTH}" in source


def test_self_voice_toggle_announces():
    source = _wx_source()
    assert '"Self-voice on."' in source


def test_closing_last_tab_places_focus_and_announces():
    source = _wx_source()
    assert "All sessions closed" in source


def test_uninstall_guards_against_file_lock_and_confirms():
    source = _wx_source()
    assert "Couldn't uninstall" in source


def test_vault_browser_hides_other_client_packs_behind_a_toggle():
    # Unsupported (other-client) packs are filtered out of the list by default, with a
    # checkbox to reveal them; _populate_list keeps _packs parallel to the visible rows.
    source = _wx_source()
    assert "show or pack.supported" in source.replace("show_all", "show")
    assert "self._show_all" in source and "wx.CheckBox" in source


def test_vault_browser_reports_an_unavailable_source_without_calling_setup_failed():
    source = _wx_source()
    assert "isinstance(outcome, vault.SourceUnavailable)" in source
    assert 'self._status(f"Source unavailable: {outcome}")' in source


def test_pack_updates_route_curated_sources_through_their_native_sync():
    source = _wx_source()
    assert "manifest_sources.by_id(pack_id)" in source
    assert "setup_pack_from_manifest(" in source
    # A pack whose upstream is a git repository updates file by file from it.
    assert "git_sources.update_source(pack_id)" in source
    assert "update_pack_from_git(" in source


def test_pack_compatibility_dialog_accounts_for_skipped_plugins_and_rules():
    source = _wx_source()
    assert '"Check &compatibility"' in source
    assert "result.skipped_plugins.items()" in source
    assert "result.skipped_rules.items()" in source
    assert "result.module_errors.items()" in source


def test_pack_manager_and_builder_speak_the_result_of_actions():
    # Enable/trust/uninstall and add/edit/delete used to be silent; a blind user needs to
    # hear that the action happened.
    source = _wx_source()
    assert "enabled for" in source and "disabled for" in source  # enable/disable toggle
    assert "trusted. Its sounds will play" in source
    assert "added." in source and "updated." in source and "deleted." in source  # builder
    assert 'wx.MessageBox(\n            f"Delete this' in source or "Delete this" in source


def test_connection_success_is_spoken_not_just_echoed():
    source = _wx_source()
    assert 'self.app.voice.speak(f"Connected to' in source


def test_blank_launch_announces_the_way_in():
    source = _wx_source()
    assert "Welcome to genericMud" in source


def test_a_dead_tab_asks_before_swallowing_a_command():
    # The transport drops commands typed while disconnected and says nothing, which is
    # indistinguishable from a MUD ignoring the line. The command box must notice, ask,
    # and leave the typed text in place while it asks.
    source = _wx_source()
    assert "if self._connect_finished and not self.is_connected() and not offline_ok:" in source
    assert "self._prompt_reconnect()" in source
    assert 'dialog.SetYesNoLabels("&Reconnect", "&Cancel")' in source


def test_the_connection_menu_item_is_refreshed_as_the_menu_opens():
    # The label is only read aloud when the menu opens, so that is the moment it has to be
    # true; pushing every loop-thread status change onto the wx thread would be worse.
    source = _wx_source()
    assert "self.Bind(wx.EVT_MENU_OPEN, self._on_menu_open)" in source
    assert "self._connection_item.SetItemLabel(label)" in source


def test_a_disconnected_tab_can_reconnect_without_opening_a_second_one():
    # Ctrl+O builds a new tab, so it cannot stand in for reviving this one.
    source = _wx_source()
    assert "async def _reconnect(self)" in source
    # Reattached through the path the backoff loop reports on (after_reconnect and the
    # VIPMud packs' OnConnect come with it).
    assert 'self.app.on_connection_status("reconnected")' in source
    assert "panel.reconnect()" in source


def test_a_deliberate_disconnect_reports_through_the_transport_path():
    # close() sets _closing, so the read loop never emits its own "disconnected". The panel
    # therefore has to report the end of the session the same way a dropped link does, or
    # the wording, the sound flush and the timer cancel all get a second copy that drifts.
    source = _wx_source()
    assert 'self.app.on_connection_status("disconnected")' in source
