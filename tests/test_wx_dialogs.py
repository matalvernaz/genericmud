"""Build the native dialogs for real, instead of checking wx_app.py's source text.

The Linux CI jobs install the gui extra and run the suite under xvfb, so these construct
real controls there. They skip wherever wxPython is missing or can't open a display: a
dev host without wx, or a macOS runner, whose uv-provided Python isn't the framework
build wx needs to reach the window server. No dialog is shown modally, so nothing can
wait on a keypress.
"""

from __future__ import annotations

import sys

import pytest

wx = pytest.importorskip("wx")

from genericmud.config.worlds import World  # noqa: E402 - after the wx importorskip
from genericmud.protocol.charset import AUTO, ENCODING_CHOICES  # noqa: E402

_VALUES = [value for value, _label in ENCODING_CHOICES]


@pytest.fixture(scope="module")
def frame():
    if sys.platform == "darwin":
        pytest.skip("wx needs a framework-build Python on macOS")
    try:
        app = wx.App(False)
    except BaseException as error:  # noqa: BLE001 - wx raises SystemExit with no display
        pytest.skip(f"wx can't start here: {error}")
    parent = wx.Frame(None)
    yield parent
    parent.Destroy()
    del app


def _accept(dialog) -> None:
    """Run the dialog's OK handler without a modal loop to end."""
    dialog.EndModal = lambda _code: None
    dialog._on_ok(None)


def test_world_dialog_offers_every_encoding_and_starts_on_automatic(frame):
    from genericmud.ui.wx_app import WorldDialog

    dialog = WorldDialog(frame)
    try:
        assert dialog._encoding.GetCount() == len(ENCODING_CHOICES)
        assert dialog._encoding.GetSelection() == _VALUES.index(AUTO)
        assert dialog._encoding.GetName() == "Encoding"
    finally:
        dialog.Destroy()


def test_world_dialog_round_trips_the_encoding(frame):
    from genericmud.ui.wx_app import WorldDialog

    dialog = WorldDialog(frame, World("Bylins", "bylins.example", 4000, encoding="koi8-r"))
    try:
        assert _VALUES[dialog._encoding.GetSelection()] == "koi8-r"
        dialog._encoding.SetSelection(_VALUES.index("cp1251"))
        _accept(dialog)
        assert dialog.get_world().encoding == "cp1251"
    finally:
        dialog.Destroy()


def test_world_dialog_label_precedes_the_encoding_choice(frame):
    # NVDA names a control from the StaticText created immediately before it.
    from genericmud.ui.wx_app import WorldDialog

    dialog = WorldDialog(frame)
    try:
        children = list(dialog.GetChildren())
        index = children.index(dialog._encoding)
        assert isinstance(children[index - 1], wx.StaticText)
        assert children[index - 1].GetLabel() == "&Encoding:"
    finally:
        dialog.Destroy()


def test_connect_dialog_names_a_non_automatic_encoding(frame):
    from genericmud.ui.wx_app import ConnectDialog

    dialog = ConnectDialog(
        frame,
        [World("Aard", "aard.example", 4000), World("Bylins", "bylins.example", 4000,
                                                     encoding="cp1251")],
    )
    try:
        details = {}
        for index, world in enumerate(dialog._saved):
            dialog._choice.SetSelection(index)
            dialog._update_details()
            details[world.name] = dialog._details.GetValue()
        assert "Encoding: Cyrillic, Windows-1251" in details["Bylins"]
        assert "Encoding" not in details["Aard"]
    finally:
        dialog.Destroy()


def test_command_line_world_keeps_every_option():
    # `genericmud host port --sounds DIR` in the native UI used to drop the sounds folder,
    # so a MUD's sound cues had nowhere to resolve from.
    from genericmud.__main__ import _parse_args
    from genericmud.ui.wx_app import _world_from_args

    args = _parse_args(
        ["mud.example", "4000", "--tls", "--sounds", "/sounds", "--encoding", "koi8-r"]
    )
    assert _world_from_args(args) == World(
        "mud.example", "mud.example", 4000, tls=True, sounds="/sounds", encoding="koi8-r"
    )
    assert _world_from_args(_parse_args([])) is None


# --- MUD variables (issue #4) ---


def _rows():
    from genericmud.automation.variables import list_variables

    vitals = {"hp": 90, "mp": 30}
    # Stored the way the engine keeps MUD data: bare name plus a source-prefixed copy.
    return list_variables(
        {"Char.Vitals": vitals, "gmcp.Char.Vitals": vitals, "HEALTH": "500",
         "msdp.HEALTH": "500"},
        {"target": "orc"},
    )


def _labels(dialog) -> list[str]:
    return [child.GetLabel() for child in dialog.GetChildren() if isinstance(child, wx.Button)]


def test_variables_dialog_reads_as_one_row_per_value_and_filters(frame):
    from genericmud.ui.wx_app import VariablesDialog

    dialog = VariablesDialog(frame, _rows, world_name="Aard")
    try:
        rows = [dialog._list.GetString(index) for index in range(dialog._list.GetCount())]
        assert rows == [
            "Char.Vitals.hp, 90, GMCP", "Char.Vitals.mp, 30, GMCP",
            "HEALTH, 500, MSDP", "target, orc, script",
        ]
        assert dialog._list_label.GetLabel() == "&Variables (4):"
        assert "Use it as: ${mud:Char.Vitals.hp}" in dialog._details.GetValue()
        dialog._filter.SetValue("VITALS")
        assert dialog._list.GetCount() == 2
        assert dialog._list_label.GetLabel() == "&Variables (2 of 4):"
        dialog._filter.SetValue("nothing matches")
        assert dialog._details.GetValue() == "No variable name contains that text."
        assert not dialog._action.IsEnabled()
    finally:
        dialog.Destroy()


def test_variables_dialog_says_why_it_is_empty(frame):
    from genericmud.ui.wx_app import VariablesDialog

    empty = VariablesDialog(frame, lambda: ([], False), world_name="Aard")
    unanswered = VariablesDialog(frame, lambda: None, world_name="Aard")
    try:
        assert "GMCP, MSDP or MSSP" in empty._details.GetValue()
        assert "didn't answer" in unanswered._details.GetValue()
        assert not empty._action.IsEnabled()
    finally:
        empty.Destroy()
        unanswered.Destroy()


def test_variables_dialog_copies_the_reference_and_keeps_the_row_on_refresh(frame):
    from genericmud.ui.wx_app import VariablesDialog

    spoken: list[str] = []
    dialog = VariablesDialog(frame, _rows, world_name="Aard", announce=spoken.append)
    try:
        dialog._list.SetSelection(2)
        dialog._show_details()
        dialog._on_activate(None)
        assert spoken[-1] in (
            "Copied the reference to HEALTH.", "The clipboard is busy; try again.",
        )
        if spoken[-1].startswith("Copied") and wx.TheClipboard.Open():
            data = wx.TextDataObject()
            wx.TheClipboard.GetData(data)
            wx.TheClipboard.Close()
            assert data.GetText() == "${mud:HEALTH}"
        dialog._refresh(announce=True)
        assert spoken[-1] == "4 variables."
        assert dialog._list.GetStringSelection() == "HEALTH, 500, MSDP"
    finally:
        dialog.Destroy()


def test_variables_picker_returns_the_chosen_row(frame):
    from genericmud.ui.wx_app import VariablesDialog

    dialog = VariablesDialog(frame, _rows, world_name="", pick=True)
    try:
        assert dialog.GetTitle() == "Insert a Variable"
        assert dialog._action.GetId() == wx.ID_OK
        dialog.EndModal = lambda _code: None
        dialog._list.SetSelection(3)
        dialog._on_activate(None)
        assert dialog.chosen is not None and dialog.chosen.reference == "${script:target}"
    finally:
        dialog.Destroy()


def test_rule_editors_offer_variables_only_with_a_live_session(frame, tmp_path):
    from genericmud.packs.user_rules import UserAlias, UserKey, UserTrigger
    from genericmud.ui.wx_app import AliasEditorDialog, KeyEditorDialog, TriggerEditorDialog

    makers = (
        lambda fetch: TriggerEditorDialog(frame, tmp_path, UserTrigger(), variables=fetch),
        lambda fetch: AliasEditorDialog(frame, UserAlias(), variables=fetch),
        lambda fetch: KeyEditorDialog(frame, tmp_path, UserKey(), variables=fetch),
    )
    for make in makers:
        with_session, without = make(_rows), make(None)
        try:
            assert "Insert a v&ariable into the speech..." in _labels(with_session)
            assert "Insert a variable into the co&mmands..." in _labels(with_session)
            assert not [label for label in _labels(without) if "variable" in label]
        finally:
            with_session.Destroy()
            without.Destroy()


def test_inserting_a_variable_puts_its_reference_at_the_caret(frame, tmp_path, monkeypatch):
    from genericmud.packs.user_rules import UserKey
    from genericmud.ui import wx_app

    def choose_first(picker):
        picker.chosen = picker._shown[0]
        return wx.ID_OK

    monkeypatch.setattr(wx_app.VariablesDialog, "ShowModal", choose_first)
    editor = wx_app.KeyEditorDialog(frame, tmp_path, UserKey(key="f2", speak="HP "),
                                    variables=_rows)
    try:
        editor._speak.SetInsertionPointEnd()
        editor._insert_variable(editor._speak)
        assert editor.result().speak == "HP ${mud:Char.Vitals.hp}"
    finally:
        editor.Destroy()


def test_focus_returns_to_the_field_only_after_the_picker_has_closed(frame, tmp_path,
                                                                        monkeypatch):
    from genericmud.packs.user_rules import UserKey
    from genericmud.ui import wx_app

    deferred: list = []
    monkeypatch.setattr(wx_app.VariablesDialog, "ShowModal", lambda _picker: wx.ID_CANCEL)
    monkeypatch.setattr(wx_app.wx, "CallAfter", lambda func, *args: deferred.append((func, args)))
    editor = wx_app.KeyEditorDialog(frame, tmp_path, UserKey(), variables=_rows)
    try:
        focused: list = []
        monkeypatch.setattr(editor._speak, "SetFocus", lambda: focused.append(True))
        editor._insert_variable(editor._speak)
        assert focused == []  # not while the picker is still being torn down
        func, args = deferred[-1]
        func(*args)
        assert focused == [True]
    finally:
        editor.Destroy()


def test_deferred_focus_on_a_closed_window_is_harmless(frame):
    from genericmud.ui.wx_app import _focus_if_alive

    field = wx.TextCtrl(frame)
    field.Destroy()
    _focus_if_alive(field)  # the editor closed before the deferred call ran


def test_a_password_typed_at_a_hidden_prompt_stays_out_of_the_command_history(frame,
                                                                             monkeypatch):
    import asyncio

    from genericmud.bridge import protocol
    from genericmud.ui import wx_app

    async def no_session(_panel):
        return None

    monkeypatch.setattr(wx_app.SessionPanel, "_start", no_session)
    loop = asyncio.new_event_loop()
    panel = wx_app.SessionPanel(frame, loop, {}, World("Test", "mud.example", 4000))
    try:
        panel._handle_message(protocol.input_masked(True))
        panel.input.SetValue("hunter2")
        panel._on_send(None)
        panel._handle_message(protocol.input_masked(False))
        panel.input.SetValue("look")
        panel._on_send(None)
        assert panel._history == ["look"]
    finally:
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))  # let the no-op session start finish
        loop.close()


def test_disconnecting_at_a_password_prompt_makes_typing_ordinary_again(frame, monkeypatch):
    import asyncio

    from genericmud.app import EngineApp
    from genericmud.bridge import protocol
    from genericmud.protocol import telnet as T
    from genericmud.ui import wx_app
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    async def no_session(_panel):
        return None

    monkeypatch.setattr(wx_app.SessionPanel, "_start", no_session)
    loop = asyncio.new_event_loop()
    panel = wx_app.SessionPanel(frame, loop, {}, World("Test", "mud.example", 4000))
    posted: list[dict] = []
    try:
        voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
        panel.app = EngineApp(voice, post=posted.append, keymap={})
        panel.app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
        panel._do_disconnect()  # the transport stays quiet; the panel reports it itself
        assert protocol.input_masked(False) in posted
    finally:
        panel.app = None
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()


@pytest.mark.parametrize(
    ("combo", "reason"),
    [
        ("alt+up", "Alt+Up is already genericMud's key for reviewing the output"),
        ("F11", "F11 is already genericMud's key for speech"),
        ("ctrl+c", "Ctrl+C is kept for the menus and text editing"),
        ("ctrl+space", "Ctrl+Space completes words in the command box"),
    ],
)
def test_a_hotkey_on_a_key_genericmud_keeps_says_why_it_would_never_run(frame, combo, reason):
    from genericmud.config.keymap import load_keymap
    from genericmud.ui.wx_app import _hotkey_taken

    message = _hotkey_taken(combo, load_keymap("vipmud"))
    assert message is not None and message.startswith(reason)
    assert _hotkey_taken("f2", load_keymap("vipmud")) is None


def test_the_automation_manager_refuses_a_hotkey_genericmud_already_uses(
    frame, tmp_path, monkeypatch
):
    # A hotkey saved on Ctrl+1, Alt+Up or F11 never fired (the keymap answers first) and
    # nothing said so. The editor now stays open with the reason instead of saving it.
    import types

    from genericmud.config.keymap import load_keymap
    from genericmud.packs.user_rules import UserKey
    from genericmud.ui import wx_app

    panel = types.SimpleNamespace(
        world=World("Test", "mud.example", 4000),
        app=types.SimpleNamespace(user_rules_dir=lambda: tmp_path),
        variable_snapshot=None,
        _keymap=load_keymap("vipmud"),
    )
    answers = iter([wx.ID_OK, wx.ID_CANCEL])  # press OK, read the refusal, then cancel
    monkeypatch.setattr(wx_app.KeyEditorDialog, "ShowModal", lambda _editor: next(answers))
    told: list[str] = []
    monkeypatch.setattr(wx_app.wx, "MessageBox", lambda text, *_args: told.append(text))
    manager = wx_app.AutomationManagerDialog(frame, panel)
    try:
        assert manager._edit_rule("key", UserKey(key="alt+up", send="score")) is None
        assert len(told) == 1 and "Alt+Up" in told[0]
        free = UserKey(key="f2", send="score")
        answers = iter([wx.ID_OK])
        assert manager._edit_rule("key", free) == free
    finally:
        manager.Destroy()


# --- soundpack questions (VIPMud #Pick/#Prompt, MUSHclient utils dialogs) ---


def test_a_pick_list_starts_on_the_packs_item_and_answers_its_index(frame):
    from genericmud.ui.wx_app import PackQuestionDialog

    request = {
        "kind": "choice", "title": "select monster", "prompt": "select monster",
        "items": ["rabbit", "fox", "badger"], "selected": 1,
    }
    dialog = PackQuestionDialog(frame, request, "select monster")
    try:
        assert dialog.GetTitle() == "select monster"
        assert dialog._control.GetName() == "select monster"
        assert [dialog._control.GetString(i) for i in range(3)] == ["rabbit", "fox", "badger"]
        assert dialog.answer() == 1
        dialog._control.SetSelection(2)
        assert dialog.answer() == 2
    finally:
        dialog.Destroy()


def test_a_list_with_no_item_named_starts_with_nothing_selected(frame):
    # MUSHclient's listbox selects nothing without a default, and OK alone answers nil:
    # the mapper's "Choose exit to delete" deletes whatever comes back.
    from genericmud.ui.wx_app import PackQuestionDialog

    request = {"kind": "choice", "title": "", "prompt": "", "items": ["a", "b"], "selected": None}
    dialog = PackQuestionDialog(frame, request, "Miriani")
    try:
        assert dialog.answer() is None
        assert dialog._control.GetName() == "Miriani"  # no prompt: named after the title
        dialog._control.SetSelection(1)
        assert dialog.answer(wx.ID_CANCEL) is None
    finally:
        dialog.Destroy()


def test_a_several_item_list_ticks_the_defaults_and_answers_every_selected_index(frame):
    from genericmud.ui.wx_app import PackQuestionDialog

    request = {
        "kind": "choices", "title": "Food", "prompt": "Pick",
        "items": ["apple", "pepper", "potato"], "selected": [0],
    }
    dialog = PackQuestionDialog(frame, request, "Food")
    try:
        assert dialog.answer() == [0]
        dialog._control.SetSelection(2)
        assert dialog.answer() == [0, 2]
    finally:
        dialog.Destroy()


def test_a_text_question_fills_in_the_default_and_answers_what_was_typed(frame):
    from genericmud.ui.wx_app import PackQuestionDialog

    request = {"kind": "text", "title": "Who", "prompt": "Name & rank?", "default": "Bob",
               "multiline": False}
    dialog = PackQuestionDialog(frame, request, "Who")
    try:
        assert dialog._control.GetValue() == "Bob"
        assert not dialog._control.IsMultiLine()
        assert dialog._control.GetName() == "Name & rank?"
        dialog._control.SetValue("Alice")
        assert dialog.answer() == "Alice"
    finally:
        dialog.Destroy()
    request = {**request, "multiline": True}
    dialog = PackQuestionDialog(frame, request, "Notes")
    try:
        assert dialog._control.IsMultiLine()
    finally:
        dialog.Destroy()


@pytest.mark.parametrize(
    ("buttons", "default", "labels", "escape"),
    [
        ("ok", 1, ["OK"], "ID_OK"),
        ("yesno", 2, ["Yes", "No"], "ID_NONE"),
        ("okcancel", 2, ["OK", "Cancel"], "ID_CANCEL"),
        ("yesnocancel", 1, ["Yes", "No", "Cancel"], "ID_CANCEL"),
        ("retrycancel", 1, ["Retry", "Cancel"], "ID_CANCEL"),
        ("abortretryignore", 3, ["Abort", "Retry", "Ignore"], "ID_NONE"),
    ],
)
def test_a_msgbox_has_mushclients_buttons_default_and_escape(
    frame, buttons, default, labels, escape
):
    # Escape is Cancel where there is one and OK on a lone OK; on Yes/No and
    # Abort/Retry/Ignore it does nothing, as on Windows' own message boxes.
    from genericmud.scripting.api import MESSAGE_BUTTONS
    from genericmud.ui.wx_app import PackMessageDialog

    request = {"kind": "message", "title": "Q", "prompt": "Sure & certain?", "buttons": buttons,
               "icon": "?", "default": default}
    dialog = PackMessageDialog(frame, request, "Q")
    try:
        assert [button.GetLabel().replace("&", "") for button in dialog._buttons] == labels
        assert dialog.GetDefaultItem() is dialog._buttons[default - 1]
        assert dialog.GetEscapeId() == getattr(wx, escape)
        for button, name in zip(dialog._buttons, MESSAGE_BUTTONS[buttons], strict=True):
            assert dialog.answer(button.GetId()) == name
        assert dialog.answer(wx.ID_HELP) is None  # closed some other way
    finally:
        dialog.Destroy()


def _question_panel(frame, monkeypatch, parent=None):
    import asyncio

    from genericmud.ui import wx_app

    async def no_session(_panel):
        return None

    monkeypatch.setattr(wx_app.SessionPanel, "_start", no_session)
    loop = asyncio.new_event_loop()
    panel = wx_app.SessionPanel(parent or frame, loop, {}, World("Test", "mud.example", 4000))
    return panel, loop


def _close_question_panel(panel, loop) -> None:
    import asyncio

    panel.Destroy()
    loop.run_until_complete(asyncio.sleep(0.01))
    loop.close()


def _ask_on_another_thread(panel, request, while_waiting=None):
    import threading

    outcome: dict = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault(
            "answer", panel._ask_from_pack(request, while_waiting)
        ),
        daemon=True,
    )
    worker.start()
    return worker, outcome


def _pump_until_done(worker, seconds=10.0) -> None:
    import time

    deadline = time.monotonic() + seconds
    while worker.is_alive() and time.monotonic() < deadline:
        wx.GetApp().ProcessPendingEvents()
        worker.join(0.01)


def test_a_packs_question_is_shown_on_the_ui_thread_and_answered_to_the_loop_thread(
    frame, monkeypatch
):
    import time

    from genericmud.ui import wx_app

    shown: list[str] = []

    def answer_fox(dialog):
        shown.append(dialog.GetTitle())
        dialog._control.SetSelection(1)
        return wx.ID_OK

    monkeypatch.setattr(wx_app.PackQuestionDialog, "ShowModal", answer_fox)
    panel, loop = _question_panel(frame, monkeypatch)
    try:
        request = {"kind": "choice", "title": "", "prompt": "Choose an item",
                   "items": ["rabbit", "fox"], "selected": None}
        worker, outcome = _ask_on_another_thread(panel, request)
        deadline = time.monotonic() + 5
        while worker.is_alive() and time.monotonic() < deadline:
            wx.GetApp().ProcessPendingEvents()
            worker.join(0.01)
        assert outcome == {"answer": 1}
        assert shown == ["Test"]  # no title from the pack: the world's name
        assert panel._questions == []
    finally:
        _close_question_panel(panel, loop)


def test_an_unanswered_question_gives_up_as_cancelled(frame, monkeypatch):
    from genericmud.ui import wx_app

    monkeypatch.setattr(wx_app, "_PACK_QUESTION_SHOW_TIMEOUT_S", 0.05)
    panel, loop = _question_panel(frame, monkeypatch)
    try:
        request = {"kind": "text", "title": "Name", "prompt": "Name?", "default": "",
                   "multiline": False}
        worker, outcome = _ask_on_another_thread(panel, request)
        worker.join(5)  # nothing pumps the UI, so the dialog never comes up
        assert outcome == {"answer": None}
        wx.GetApp().ProcessPendingEvents()  # the late show finds it already over
    finally:
        _close_question_panel(panel, loop)


def test_closing_the_tab_frees_a_script_waiting_on_its_question(frame, monkeypatch):
    import types

    panel, loop = _question_panel(frame, monkeypatch)
    try:
        request = {"kind": "message", "title": "Q", "prompt": "Sure?", "buttons": "yesno",
                   "icon": "?", "default": 1}
        worker, outcome = _ask_on_another_thread(panel, request)
        while not panel._questions and worker.is_alive():
            worker.join(0.01)
        # The variables window doesn't wait on a loop that's busy waiting on the player.
        panel.app = types.SimpleNamespace()
        assert panel.variable_snapshot() is None
        panel.app = None
        panel.close()
        worker.join(5)
        assert outcome == {"answer": None}
    finally:
        _close_question_panel(panel, loop)


def test_the_world_is_named_on_a_question_when_several_are_open(frame, monkeypatch):
    book = wx.Simplebook(frame)
    panel, loop = _question_panel(frame, monkeypatch, parent=book)
    try:
        book.AddPage(panel, "Test")
        assert panel._question_title({"title": "Are you sure?"}) == "Are you sure?"
        book.AddPage(wx.Panel(book), "Other")
        assert panel._question_title({"title": "Are you sure?"}) == "Are you sure? - Test"
        assert panel._question_title({"title": ""}) == "Test"
    finally:
        book.RemovePage(0)
        _close_question_panel(panel, loop)
        book.Destroy()


def test_the_asking_packs_timers_get_their_turns_while_it_waits(frame, monkeypatch):
    from genericmud.ui import wx_app

    monkeypatch.setattr(wx_app, "_PACK_QUESTION_SHOW_TIMEOUT_S", 0.3)
    panel, loop = _question_panel(frame, monkeypatch)
    try:
        turns: list[int] = []
        request = {"kind": "text", "title": "Name", "prompt": "Name?", "default": "",
                   "multiline": False}
        worker, outcome = _ask_on_another_thread(panel, request, lambda: turns.append(1))
        worker.join(5)
        assert outcome == {"answer": None}
        assert len(turns) >= 2
    finally:
        _close_question_panel(panel, loop)


def test_a_question_on_screen_isnt_given_up_on_like_one_that_never_appeared(
    frame, monkeypatch
):
    import time

    from genericmud.ui import wx_app

    monkeypatch.setattr(wx_app, "_PACK_QUESTION_SHOW_TIMEOUT_S", 0.2)

    def slow_reader(dialog):
        time.sleep(0.6)  # reading Cosmic Rage's paragraphs
        dialog._control.SetSelection(0)
        return wx.ID_OK

    monkeypatch.setattr(wx_app.PackQuestionDialog, "ShowModal", slow_reader)
    panel, loop = _question_panel(frame, monkeypatch)
    try:
        request = {"kind": "choice", "title": "Gags", "prompt": "Gags",
                   "items": ["Create a new gag"], "selected": 0}
        worker, outcome = _ask_on_another_thread(panel, request)
        _pump_until_done(worker)
        assert outcome == {"answer": 0}
    finally:
        _close_question_panel(panel, loop)


def test_closing_the_tab_takes_down_a_message_box_on_screen(frame, monkeypatch):
    import asyncio

    panel, loop = _question_panel(frame, monkeypatch)
    torn_down: list[bool] = []
    monkeypatch.setattr(panel, "_teardown", lambda: torn_down.append(True))

    def end_any_dialog_left() -> None:  # so a failure can't hang the suite
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    try:
        request = {"kind": "message", "title": "Q", "prompt": "Sure?", "buttons": "yesno",
                   "icon": "?", "default": 1}
        worker, outcome = _ask_on_another_thread(panel, request)
        wx.CallLater(300, panel.close)  # fires inside the dialog's own modal loop
        wx.CallLater(5000, end_any_dialog_left)
        _pump_until_done(worker)
        assert outcome == {"answer": None}
        loop.run_until_complete(asyncio.sleep(0.01))
        assert torn_down == [True]
    finally:
        _close_question_panel(panel, loop)


def test_closing_the_tab_tears_the_session_down_even_if_a_question_wont_close(
    frame, monkeypatch
):
    import asyncio

    from genericmud.ui import wx_app

    panel, loop = _question_panel(frame, monkeypatch)
    torn_down: list[bool] = []
    monkeypatch.setattr(panel, "_teardown", lambda: torn_down.append(True))

    def broken(_question):
        raise RuntimeError("the dialog wouldn't close")

    monkeypatch.setattr(panel, "_withdraw_question", broken)
    try:
        panel._questions.append(wx_app._PackQuestion({"kind": "text"}))
        panel.close()
        loop.run_until_complete(asyncio.sleep(0.01))
        assert torn_down == [True]
    finally:
        panel._questions.clear()
        _close_question_panel(panel, loop)


def test_world_dialog_round_trips_command_stacking_and_logging(frame):
    from genericmud.ui.wx_app import WorldDialog

    world = World(
        "Chat", "chatmud.com", 7777, command_stacking=False, command_separator="|",
        log_automatically=True, log_files="world", log_commands=False,
    )
    dialog = WorldDialog(frame, world)
    try:
        assert dialog._separator.GetName() == "Command separator"
        assert dialog._log_files.GetName() == "Log files"
        assert dialog._log_files.GetStringSelection() == "One file for this world"
        _accept(dialog)
        assert dialog.get_world() == world
    finally:
        dialog.Destroy()


def test_world_dialog_refuses_a_separator_that_cant_work(frame, monkeypatch):
    from genericmud.ui import wx_app

    told: list[str] = []
    monkeypatch.setattr(wx_app.wx, "MessageBox", lambda text, *_args: told.append(text))
    dialog = wx_app.WorldDialog(frame, World("A", "a.example", 23))
    try:
        dialog._separator.SetValue("/")
        _accept(dialog)
        assert told == ["The command separator can't be /, which starts genericMud's own commands."]
        with pytest.raises(RuntimeError):
            dialog.get_world()  # not accepted
        dialog._stacking.SetValue(False)  # with stacking off it doesn't matter
        _accept(dialog)
        assert dialog.get_world().command_separator == ";"
    finally:
        dialog.Destroy()


def test_editing_a_world_changes_its_open_tab_at_once(frame, monkeypatch):
    import asyncio

    from genericmud.app import EngineApp
    from genericmud.ui import wx_app
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    async def no_session(_panel):
        return None

    monkeypatch.setattr(wx_app.SessionPanel, "_start", no_session)
    loop = asyncio.new_event_loop()
    panel = wx_app.SessionPanel(frame, loop, {}, World("W", "w.example", 23))
    try:
        panel.app = EngineApp(VoiceRouter(RecordingBackend(), clock=lambda: 0.0), keymap={})
        edited = World(
            "W", "new.example", 4000, encoding="cp1251", sounds="C:/sounds",
            command_separator="|", log_commands=False,
        )
        panel.apply_world_settings(edited)
        loop.run_until_complete(asyncio.sleep(0.01))
        assert panel.app.command_separator == "|"
        assert panel.app.log_commands is False
        # How the tab connects stays as it opened until the world is opened again, so a
        # reconnect (Ctrl+D) can't mix a new host with the old encoding and sounds.
        assert (panel.world.host, panel.world.port) == ("w.example", 23)
        assert (panel.world.encoding, panel.world.sounds) == ("auto", None)
        assert panel.world.command_separator == "|" and not panel.world.log_commands
    finally:
        panel.app = None
        panel.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()


def test_the_connection_menu_item_names_what_it_will_do():
    # One item, relabelled: Ctrl+D has to mean "drop it" while connected and "bring it
    # back" once it is down, so the label has to follow the tab, not the menu.
    from genericmud.ui.wx_app import _connection_menu_label

    assert _connection_menu_label(True) == "&Disconnect\tCtrl+D"
    assert _connection_menu_label(False) == "&Reconnect\tCtrl+D"


class _FakeMudReader:
    """The first link the server drops at once; every later one stays up, saying nothing."""

    def __init__(self, ended: bool) -> None:
        self._ended = ended

    async def read(self, _n):
        import asyncio

        if not self._ended:
            await asyncio.Event().wait()
        return b""


class _FakeMudWriter:
    def __init__(self) -> None:
        self.closed = False

    def write(self, _data) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


def test_a_reconnect_waits_for_a_connect_already_under_way(frame, monkeypatch):
    # Ctrl+D and "Reconnect now?" are offered whenever the tab isn't connected, which
    # includes the transport's own backoff between tries. A second connect() there opened
    # a second socket: the two read loops tore down each other's and restarted the backoff
    # without end, saying "reconnected" twice a second.
    import asyncio

    from genericmud.app import EngineApp
    from genericmud.transport.connection import MudConnection, ReconnectPolicy
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    opened: list[str] = []

    async def fake_open_connection(host, port, ssl=None):
        opened.append(host)
        return _FakeMudReader(ended=len(opened) == 1), _FakeMudWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    panel, loop = _question_panel(frame, monkeypatch)
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    connection = MudConnection()
    connection.auto_reconnect = True
    connection.reconnect_policy = ReconnectPolicy(base_delay=0.2, max_attempts=1)

    async def scenario() -> None:
        panel._voice = voice
        panel.app = EngineApp(voice, post=lambda _m: None, keymap={}, name="Test")
        connection.on_status = panel.app.on_connection_status
        panel._connection = connection
        await connection.connect("mud.example", 4000)
        await asyncio.sleep(0.05)  # the link drops; the backoff waits before its first try
        await panel._reconnect()  # Ctrl+D, or "Reconnect now?" answered
        await asyncio.sleep(0.4)  # the backoff's own try
        await connection.close()

    try:
        loop.run_until_complete(scenario())
        assert opened == ["mud.example", "mud.example"]  # the first link, then the backoff's
        assert "Still connecting to Test." in backend.spoken
    finally:
        panel.app = None
        _close_question_panel(panel, loop)


def test_a_vipmud_pack_hears_a_reconnect_made_from_the_window(frame, monkeypatch, tmp_path):
    # VIPMud packs load their settings and switch their sounds on in OnConnect, and save
    # them in OnDisconnect. The window's Reconnect skipped OnConnect, and the state that
    # left made the next Disconnect skip OnDisconnect as well, so nothing was saved.
    import asyncio

    from genericmud.app import EngineApp
    from genericmud.packs import PackStore
    from genericmud.transport.connection import MudConnection
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    async def fake_open_connection(host, port, ssl=None):
        return _FakeMudReader(ended=False), _FakeMudWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    store = PackStore(tmp_path / "store")
    pack_source = tmp_path / "pack.set"
    pack_source.write_text(
        "#alias OnConnect {#say ready}\n#alias OnDisconnect {#say gone}\n", encoding="latin-1"
    )
    store.install(pack_source, world="Test", trust=True)
    panel, loop = _question_panel(frame, monkeypatch)
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)

    async def scenario() -> list[list[str]]:
        panel._voice = voice
        panel.app = EngineApp(voice, post=lambda _m: None, keymap={}, packs=store, name="Test")
        panel._connection = MudConnection(on_event=panel.app.on_telnet_event)
        panel._connection.on_status = panel.app.on_connection_status
        panel.app.on_connect("Test")
        await panel._connection.connect("mud.example", 4000)
        heard: list[list[str]] = []
        for step in ("disconnect", "reconnect", "disconnect"):
            backend.spoken.clear()
            if step == "disconnect":
                panel._do_disconnect()  # Ctrl+D while connected
            else:
                await panel._reconnect()  # Ctrl+D once it isn't
            await asyncio.sleep(0.01)
            heard.append(list(backend.spoken))
        return heard

    try:
        disconnected, reconnected, disconnected_again = loop.run_until_complete(scenario())
        assert "gone" in disconnected
        # OnConnect runs once, after the reconnect is said, so the pack's welcome stands.
        assert reconnected.count("ready") == 1
        assert reconnected.index("Reconnected to Test.") < reconnected.index("ready")
        assert "gone" in disconnected_again
    finally:
        panel.app = None
        _close_question_panel(panel, loop)


def _closing_panel_session(panel):
    """A disconnected session on ``panel``, speaking into the returned recording backend."""
    from genericmud.app import EngineApp
    from genericmud.transport.connection import MudConnection
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    panel._voice = voice
    panel.app = EngineApp(voice, post=lambda _m: None, keymap={}, name="Test")
    panel.app.engine.connected = False  # the tab's link is down
    panel._connection = MudConnection()
    return backend


@pytest.mark.parametrize(
    ("closed_by", "settled_first", "refused"),
    [("tab", True, False), ("tab", False, False), ("connection", True, False),
     ("tab", True, True)],
    ids=["tab-closed", "tab-closing", "connection-closed", "tab-closed-refused"],
)
def test_a_reconnect_closed_during_its_handshake_says_nothing_untrue(
    frame, monkeypatch, closed_by, settled_first, refused
):
    # Ctrl+D on a dead tab, then Ctrl+W while the MUD hasn't answered yet: the closed tab
    # still said "Reconnected to Test." with no socket live, and marked its shut-down
    # engine connected. The same goes for the connection being closed under it, which
    # connect() answers by dropping the late socket and returning quietly. "tab-closing" is
    # the MUD answering before the closed tab's teardown has had its turn on the loop;
    # "tab-closed-refused" is the MUD refusing, which a closed tab has no one to tell.
    import asyncio

    handshake_started = asyncio.Event()
    release_handshake = asyncio.Event()
    writers: list[_FakeMudWriter] = []

    async def held_open_connection(host, port, ssl=None):
        handshake_started.set()
        await release_handshake.wait()
        if refused:
            raise ConnectionRefusedError("Connection refused")
        writers.append(_FakeMudWriter())
        return _FakeMudReader(ended=False), writers[-1]

    monkeypatch.setattr(asyncio, "open_connection", held_open_connection)
    panel, loop = _question_panel(frame, monkeypatch)

    async def scenario():
        backend = _closing_panel_session(panel)
        reconnecting = asyncio.ensure_future(panel._reconnect())  # Ctrl+D
        await handshake_started.wait()
        if closed_by == "tab":
            panel.close()  # Ctrl+W before the MUD answers
        else:
            await panel._connection.close()
        if settled_first:
            await asyncio.sleep(0.01)  # the teardown runs before the MUD answers
        release_handshake.set()
        await reconnecting
        await asyncio.sleep(0.01)  # a teardown still queued runs now
        return backend

    try:
        backend = loop.run_until_complete(scenario())
        assert backend.spoken == ["Reconnecting to Test."]  # said before the close only
        assert panel.app.engine.connected is False
        assert all(writer.closed for writer in writers)  # a late socket was dropped
        assert len(writers) == (0 if refused else 1)
    finally:
        panel.app = None
        _close_question_panel(panel, loop)


def test_a_reconnect_whose_turn_comes_after_the_tab_closed_does_nothing(frame, monkeypatch):
    # Ctrl+D then Ctrl+W at once: the reconnect queued on the loop runs after the close, and
    # opened a socket for a tab that no longer exists, announcing it as it went.
    import asyncio

    opened: list[str] = []

    async def fake_open_connection(host, port, ssl=None):
        opened.append(host)
        return _FakeMudReader(ended=False), _FakeMudWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    panel, loop = _question_panel(frame, monkeypatch)

    async def scenario():
        backend = _closing_panel_session(panel)
        panel.close()
        await panel._reconnect()
        await asyncio.sleep(0.01)
        return backend

    try:
        backend = loop.run_until_complete(scenario())
        assert opened == []
        assert backend.spoken == []
    finally:
        panel.app = None
        _close_question_panel(panel, loop)


def test_a_disconnected_tab_still_runs_genericmuds_own_commands(frame, monkeypatch):
    # "/to Bob say hi" goes out through Bob's session and "/aliases" only reads this
    # world's aliases; neither needs this tab's connection. The "Reconnect now?" question
    # stopped both, and Cancel keeps the line in the box, so they could not run at all.
    import asyncio

    from genericmud.app import EngineApp
    from genericmud.session.hub import SessionHub
    from genericmud.transport.connection import MudConnection
    from genericmud.ui import wx_app
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    asked: list[str] = []
    monkeypatch.setattr(
        wx_app.SessionPanel, "_prompt_reconnect",
        lambda panel: asked.append(panel.input.GetValue()),
    )
    panel, loop = _question_panel(frame, monkeypatch)
    hub = SessionHub()
    bob_heard: list[str] = []
    hub.register("Bob", bob_heard.append)  # another tab, still connected
    backend = RecordingBackend()
    panel.app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0), post=lambda _m: None, keymap={},
        name="Test", hub=hub,
    )
    panel._connection = MudConnection()  # this tab's link is down
    panel._connect_finished = True
    try:
        for typed in ("/to Bob say hi", "/aliases", "look"):
            panel.input.SetValue(typed)
            panel._on_send(None)
            loop.run_until_complete(asyncio.sleep(0.01))  # the engine's turn on the loop
        assert bob_heard == ["say hi"]
        assert "no aliases" in backend.spoken
        assert asked == ["look"]  # a line for the MUD still asks first
        assert panel.input.GetValue() == "look"  # and stays in the box meanwhile
    finally:
        panel.app = None
        _close_question_panel(panel, loop)


def test_ctrl_d_works_in_the_first_tab_after_a_menu_was_opened_with_none(
    frame, monkeypatch, tmp_path
):
    # At a blank launch, Alt+F to find Connect is a natural first move. Opening any menu
    # with no tab open greys the File item out, a greyed item's Ctrl+D never fires, and
    # opening a tab didn't bring it back: Ctrl+D did nothing until a menu was opened again.
    import asyncio

    from genericmud.ui import wx_app
    from tests.helpers import RecordingBackend

    monkeypatch.setenv("HOME", str(tmp_path))  # the frame reads and writes its config
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(wx_app, "make_voice_backend", RecordingBackend)
    monkeypatch.setattr(wx_app, "make_diagnostic_log", lambda: None)

    async def no_session(_panel):
        return None

    monkeypatch.setattr(wx_app.SessionPanel, "_start", no_session)
    pressed: list[str] = []
    monkeypatch.setattr(
        wx_app.SessionPanel, "reconnect", lambda panel: pressed.append(panel.world.name)
    )
    loop = asyncio.new_event_loop()
    main = wx_app.GenericMudFrame(loop, {})
    try:
        item = main._connection_item
        menu_open = wx.MenuEvent(wx.wxEVT_MENU_OPEN, 0, main.GetMenuBar().GetMenu(0))
        main._on_menu_open(menu_open)
        assert not item.IsEnabled()  # no tab: nothing to disconnect or reconnect
        main.open_session(World("Test", "mud.example", 4000))
        assert item.IsEnabled()
        main.ProcessCommand(item.GetId())  # the route wx gives the Ctrl+D accelerator
        assert pressed == ["Test"]  # the tab isn't connected, so Ctrl+D reconnects it
        main._on_close_tab(None)
        assert not item.IsEnabled()  # the last tab closed: greyed out again
    finally:
        main.Destroy()
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()
