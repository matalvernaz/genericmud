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
