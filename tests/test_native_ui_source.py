"""Build-blind structural checks for the native wx world flows."""

from __future__ import annotations

import ast
from pathlib import Path


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


def test_the_world_dialog_offers_an_images_address():
    # Where an <IMAGE> with no address of its own looks; the field is per world, like the
    # sounds folder, because it belongs to one MUD's web space.
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    assert '"&Images web address:"' in source
    assert "self._images_url.SetValue(initial.images_url)" in source
    assert "images_url=self._images_url.GetValue().strip()" in source


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


def test_enter_in_the_output_follows_the_link_under_the_review_cursor():
    # Plain Enter can't be a keymap combo: _key_combo keeps it as typing, or the command box
    # would stop sending. So the output control, where there is nothing to send, dispatches
    # the action itself.
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    handler = source.split("def _on_output_key", 1)[1].split("def _open_find", 1)[0]
    assert "wx.WXK_RETURN" in handler
    assert '"key": "review:link"' in handler


def test_the_panic_and_review_keys_still_reach_the_output_control():
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    handler = source.split("def _on_output_key", 1)[1].split("def _open_find", 1)[0]
    assert "self._dispatch_bound_combo(_key_combo(event))" in handler


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


def test_the_window_tells_the_session_when_the_socket_opens():
    # on_connect runs before the transport connects, so the login waits in a queue; only the
    # window knows when the socket is really up. Without this call a MUSH/MOO or Diku world
    # has its login typed into a socket that does not exist, and the MUD never sees it.
    source = _wx_source()
    connected_at = source.index("await self._connection.connect(self.world.host")
    opened_at = source.index("self.app.after_connect()")
    assert opened_at > connected_at


def test_no_button_label_doubles_an_ampersand():
    # On Windows a button whose label has a shortcut letter keeps "&&" doubled in the name a
    # screen reader gets, so "&Download && Set Up" was read "Download and and Set Up".
    # Static text and checkboxes come through as one "&"; buttons have to say "and".
    source = (
        Path(__file__).resolve().parents[1] / "genericmud/ui/wx_app.py"
    ).read_text(encoding="utf-8")
    buttons = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Button"
    ]
    labels = [
        keyword.value.value for node in buttons for keyword in node.keywords
        if keyword.arg == "label" and isinstance(keyword.value, ast.Constant)
    ]
    assert labels, "found no button labels: the check looks in the wrong place"
    assert not [label for label in labels if "&&" in label]
