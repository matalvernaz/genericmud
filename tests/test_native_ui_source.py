"""Build-blind structural checks for the native wx world flows."""

from __future__ import annotations

import ast

from tests.ui_source import ui_class_source, ui_defines, ui_modules, ui_source


def test_the_view_menu_can_let_every_open_world_speak():
    # Nine worlds, one in front: by default only that one speaks, and the setting is the
    # tab-level twin of Background silence, in the same menu and saved the same way.
    menus = ui_source("GenericMudFrame.__init__")
    assert '"Silence &other worlds"' in menus
    assert "self._other_worlds_item.Check(self._prefs.silence_other_worlds)" in menus
    toggle = ui_source("GenericMudFrame._on_toggle_other_worlds")
    assert 'self._save_pref("silence_other_worlds"' in toggle
    assert "self.Bind(wx.EVT_MENU, self._on_toggle_other_worlds, self._other_worlds_item)" in menus

    # The setting has to reach the code that decides who speaks, and it has to be able to
    # say yes for a tab that isn't the selected one.
    handler = ui_source("GenericMudFrame._update_active")
    assert "silence_other_worlds" in handler
    assert "i == selected or every_world" in handler


def test_tab_keys_come_from_the_keymap_but_are_run_by_the_window():
    # The binding has to be editable like every other key, so it is read from the keymap; the
    # work has to happen in the window, because the engine serves one world and cannot see the
    # tabs. So the panel must divert `tab:` actions before the engine is handed the key.
    handler = ui_source("SessionPanel._dispatch_bound_combo")
    assert 'action.startswith("tab:")' in handler
    assert "handle_tab_action" in handler
    assert handler.index("handle_tab_action") < handler.index('{"type": "key"')


def test_both_ways_of_changing_tabs_say_where_you_landed():
    # Ctrl+Tab goes through _switch_session, and so does Alt+<number>; the announcement lives
    # there so the two cannot drift apart.
    switch = ui_source("GenericMudFrame._switch_session")
    assert "_announce_tab(index)" in switch
    select = ui_source("GenericMudFrame._select_tab")
    assert "_announce_tab(index)" in select


def test_new_world_and_saved_connect_use_distinct_dialogs_and_commands():
    for name in ("WorldDialog", "ConnectDialog"):
        assert ui_source(name).startswith(f"class {name}(")
    menus = ui_source("GenericMudFrame.__init__")
    assert '"&New World...\\tCtrl+N"' in menus
    assert '"&Connect to Saved World...\\tCtrl+O"' in menus


def test_the_world_dialog_offers_an_images_address():
    # Where an <IMAGE> with no address of its own looks; the field is per world, like the
    # sounds folder, because it belongs to one MUD's web space.
    dialog = ui_source("WorldDialog")
    assert '"&Images web address:"' in dialog
    assert "self._images_url.SetValue(initial.images_url)" in dialog
    assert "images_url=self._images_url.GetValue().strip()" in dialog


def test_find_shortcuts_remain_scoped_to_the_output_control():
    panel = ui_source("SessionPanel.__init__")
    assert "self.output.Bind(wx.EVT_KEY_DOWN, self._on_output_key)" in panel
    assert '"&Find in Output...\\tCtrl+F"' not in ui_class_source("GenericMudFrame")


def test_find_caret_mapping_stays_in_native_units():
    # Insertion-point units and GetValue() offsets disagree on Windows (a line break
    # is two native characters, one in the string), so the caret sync must convert
    # through the control's own line functions, never position by string offset.
    assert "find_offset" not in ui_class_source("SessionPanel")
    caret_sync = ui_source("SessionPanel._sync_find_caret")
    assert "self.output.PositionToXY(self.output.GetInsertionPoint())" in caret_sync
    assert "self.output.XYToPosition(0, line)" in caret_sync


def test_enter_in_the_output_follows_the_link_on_the_caret_line():
    # Plain Enter can't be a keymap combo: _key_combo keeps it as typing, or the command box
    # would stop sending. So the output control, where there is nothing to send, dispatches
    # the action itself, and names the line the caret is on rather than leaving the engine to
    # guess (its review cursor knows nothing of this caret).
    handler = ui_source("SessionPanel._on_output_key")
    assert "wx.WXK_RETURN" in handler
    assert '"type": protocol.LINK_AT' in handler
    assert "self.output.PositionToXY(" in handler


def test_the_panic_and_review_keys_still_reach_the_output_control():
    handler = ui_source("SessionPanel._on_output_key")
    assert "combo = _key_combo(event" in handler
    assert "self._dispatch_bound_combo(combo)" in handler


def test_output_caret_survives_appends_while_find_dialog_is_open():
    # While the modal Find dialog holds focus, arriving output must not yank the
    # underlying insertion point: _keep_caret_on_focus promises the search runs from
    # the reader's position, so the flush has to anchor whenever that flag is set,
    # not only while the output itself has focus.
    flush = ui_source("SessionPanel._flush_output")
    assert "preserve = self.output.HasFocus() or self._keep_caret_on_focus" in flush


def test_history_moves_only_from_an_empty_box_or_a_message_already_in_history():
    # The draft stays put: an arrow that replaced typed-but-unsent text would be silent data
    # loss for a blind typist. History moves only from an empty box or from a message that is
    # itself in history, which is why no draft needs parking.
    step = ui_source("SessionPanel._history_step")
    assert step.startswith("def _history_step(")
    assert "if value not in self._history:" in step
    assert "_history_draft" not in ui_class_source("SessionPanel")


def test_bound_combos_dispatch_from_the_output_box_too():
    # The panic keys (Escape/F11 stop speech, Shift+F11 stop sound) and other macros must
    # work while the reader is focused in the output, not only in the command box.
    dispatch = ui_source("SessionPanel._dispatch_bound_combo")
    assert dispatch.startswith("def _dispatch_bound_combo(")
    # Input AND output handlers.
    assert "self._dispatch_bound_combo(" in ui_source("SessionPanel._on_input_key")
    assert "self._dispatch_bound_combo(" in ui_source("SessionPanel._on_output_key")


def test_soundpacks_and_unified_automation_have_distinct_menus_clear_of_retrace_key():
    # Installed packs and user-authored automation are different jobs. Alt+R stays free for
    # nav:retrace; Automation uses one manager instead of separate basic/script paths.
    menus = ui_source("GenericMudFrame.__init__")
    frame = ui_class_source("GenericMudFrame")
    assert '"Sound&packs"' in menus
    assert '"&Automation"' in menus
    assert '"R&ules"' not in frame and '"&Rules"' not in frame
    assert '"&Manage automation...\\tCtrl+B"' in menus
    assert '"Automation &help..."' in menus
    assert "AutomationManagerDialog" in frame
    assert "RulesBuilderDialog" not in frame and not ui_defines("RulesBuilderDialog")
    assert "AutomationScriptsDialog" not in frame and not ui_defines("AutomationScriptsDialog")
    assert '"Browse soundpacks &online...\\tCtrl+Shift+B"' in menus
    assert '"alt+f", "alt+p", "alt+a", "alt+v", "alt+h"' in ui_source("_PASSTHROUGH_COMBOS")


def test_automation_manager_has_direct_categories_and_contextual_script_actions():
    manager = ui_source("AutomationManagerDialog")
    for category in (
        "Triggers — when the MUD sends text",
        "Aliases — when I type a shortcut",
        "Hotkeys — when I press a key",
        "Channels — how matching lines are handled",
        "Scripts — reusable Lua automation",
        "MUD data — when the MUD sends a value",
    ):
        assert category in manager
    assert 'self._buttons["rename"].Enable(has_item and is_script)' in manager
    assert 'self._buttons["reload"].Enable(is_script)' in manager
    assert 'self._buttons["folder"].Enable(is_script)' in manager
    assert 'self._buttons["new"].SetLabel(f"&New {singular}...")' in manager
    assert "wx.EVT_LISTBOX_DCLICK, self._on_edit" in manager
    # Ctrl+1 to Ctrl+6: one per category, counted from the list so a new one gets its key.
    assert 'if ord("1") <= code < ord("1") + len(self._CATEGORIES)' in manager
    assert 'if code == ord("N")' in manager


def test_automation_manager_validates_required_fields_instead_of_silently_dropping():
    manager = ui_source("AutomationManagerDialog")
    assert "def _complete(self, kind" in manager
    assert "self._complete(kind, result) and self._valid_rule(kind, result)" in manager


def test_rule_editors_accept_command_stacks_and_explain_shared_variables():
    commands = ui_source("_RuleEditorBase._command_text")
    assert "wx.TE_MULTILINE | wx.TE_DONTWRAP" in commands
    assert "Commands to send, one per line" in commands
    assert "${script:name}" in commands
    assert "${mud:HEALTH}" in commands


def test_self_voice_toggle_announces():
    assert '"Self-voice on."' in ui_source("GenericMudFrame._on_toggle_self_voice")


def test_closing_last_tab_places_focus_and_announces():
    assert "All sessions closed" in ui_source("GenericMudFrame._on_close_tab")


def test_uninstall_guards_against_file_lock_and_confirms():
    assert "Couldn't uninstall" in ui_source("PackManagerDialog._on_uninstall")


def test_vault_browser_hides_other_client_packs_behind_a_toggle():
    # Unsupported (other-client) packs are filtered out of the list by default, with a
    # checkbox to reveal them; _populate_list keeps _packs parallel to the visible rows.
    browser = ui_source("VaultBrowserDialog")
    assert "show or pack.supported" in browser.replace("show_all", "show")
    assert "self._show_all" in browser and "wx.CheckBox" in browser


def test_vault_browser_reports_an_unavailable_source_without_calling_setup_failed():
    browser = ui_source("VaultBrowserDialog")
    assert "isinstance(outcome, vault.SourceUnavailable)" in browser
    assert 'self._status(f"Source unavailable: {outcome}")' in browser


def test_pack_updates_route_curated_sources_through_their_native_sync():
    update = ui_source("PackManagerDialog._on_update")
    assert "manifest_sources.by_id(pack_id)" in update
    assert "setup_pack_from_manifest(" in update
    # A pack whose upstream is a git repository updates file by file from it.
    assert "git_sources.update_source(pack_id)" in update
    assert "update_pack_from_git(" in update


def test_pack_compatibility_dialog_accounts_for_skipped_plugins_and_rules():
    manager = ui_source("PackManagerDialog")
    assert '"Check &compatibility"' in manager
    assert "result.skipped_plugins.items()" in manager
    assert "result.skipped_rules.items()" in manager
    assert "result.module_errors.items()" in manager


def test_pack_manager_and_builder_speak_the_result_of_actions():
    # Enable/trust/uninstall and add/edit/delete used to be silent; a blind user needs to
    # hear that the action happened.
    packs = ui_source("PackManagerDialog")
    assert "enabled for" in packs and "disabled for" in packs  # enable/disable toggle
    assert "trusted. Its sounds will play" in packs
    builder = ui_source("AutomationManagerDialog")
    assert "added." in builder and "updated." in builder and "deleted." in builder  # builder
    assert 'wx.MessageBox(\n            f"Delete this' in builder or "Delete this" in builder


def test_connection_success_is_spoken_not_just_echoed():
    assert 'self.app.voice.speak(f"Connected to' in ui_source("SessionPanel._start_inner")


def test_blank_launch_announces_the_way_in():
    assert "Welcome to genericMud" in ui_source("run")


def test_a_dead_tab_asks_before_swallowing_a_command():
    # The transport drops commands typed while disconnected and says nothing, which is
    # indistinguishable from a MUD ignoring the line. The command box must notice, ask,
    # and leave the typed text in place while it asks.
    send = ui_source("SessionPanel._on_send")
    assert "if self._connect_finished and not self.is_connected() and not offline_ok:" in send
    assert "self._prompt_reconnect()" in send
    prompt = ui_source("SessionPanel._prompt_reconnect")
    assert 'dialog.SetYesNoLabels("&Reconnect", "&Cancel")' in prompt


def test_the_connection_menu_item_is_refreshed_as_the_menu_opens():
    # The label is only read aloud when the menu opens, so that is the moment it has to be
    # true; pushing every loop-thread status change onto the wx thread would be worse.
    menus = ui_source("GenericMudFrame.__init__")
    assert "self.Bind(wx.EVT_MENU_OPEN, self._on_menu_open)" in menus
    refresh = ui_source("GenericMudFrame._refresh_connection_item")
    assert "self._connection_item.SetItemLabel(label)" in refresh


def test_a_disconnected_tab_can_reconnect_without_opening_a_second_one():
    # Ctrl+O builds a new tab, so it cannot stand in for reviving this one.
    reconnect = ui_source("SessionPanel._reconnect")
    assert reconnect.startswith("async def _reconnect(self)")
    # Reattached through the path the backoff loop reports on (after_reconnect and the
    # VIPMud packs' OnConnect come with it).
    assert 'self.app.on_connection_status("reconnected", manual=True)' in reconnect
    assert "panel.reconnect()" in ui_source("GenericMudFrame._on_disconnect")


def test_a_deliberate_disconnect_reports_through_the_transport_path():
    # close() sets _closing, so the read loop never emits its own "disconnected". The panel
    # therefore has to report the end of the session the same way a dropped link does, or
    # the wording, the sound flush and the timer cancel all get a second copy that drifts.
    disconnect = ui_source("SessionPanel._do_disconnect")
    assert 'self.app.on_connection_status("disconnected")' in disconnect


def test_no_button_label_doubles_an_ampersand():
    # On Windows a button whose label has a shortcut letter keeps "&&" doubled in the name a
    # screen reader gets, so "&Download && Set Up" was read "Download and and Set Up".
    # Static text and checkboxes come through as one "&"; buttons have to say "and".
    # Every module of the window is checked, each parsed on its own.
    buttons = [
        node for _path, _text, tree in ui_modules() for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Button"
    ]
    labels = [
        keyword.value.value for node in buttons for keyword in node.keywords
        if keyword.arg == "label" and isinstance(keyword.value, ast.Constant)
    ]
    assert labels, "found no button labels: the check looks in the wrong place"
    assert not [label for label in labels if "&&" in label]


def test_the_world_dialog_has_an_echo_input_switch():
    # Separate from logging on purpose: showing a command and writing it to a file are two
    # different wants, so each has its own box.
    dialog = ui_source("WorldDialog")
    assert '"Echo &input in the output"' in dialog
    assert "self._echo_input.SetValue(initial.echo_input)" in dialog
    assert "echo_input=self._echo_input.GetValue()" in dialog


def test_the_window_tells_the_session_when_the_socket_opens():
    # on_connect runs before the transport connects, so the login waits in a queue; only the
    # window knows when the socket is really up. Without this call a MUSH/MOO or Diku world
    # has its login typed into a socket that does not exist, and the MUD never sees it.
    start = ui_source("SessionPanel._start_inner")
    connected_at = start.index("await self._connection.connect(self.world.host")
    opened_at = start.index("self.app.after_connect()")
    assert opened_at > connected_at
