"""Real native controls and focus, synthetic key events; no screen-reader claim."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_native_command_entry_scenarios():
    pytest.importorskip("wx")
    if sys.platform == "darwin" and os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip("hosted macOS does not grant native foreground focus")
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        pytest.skip("native wx requires a display; use xvfb-run")
    result = subprocess.run(
        [sys.executable, "-c", "from tests.test_command_entry import _probe; _probe()"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        encoding="utf-8", timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native scenarios passed" in result.stdout


def _probe():
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    import wx

    from genericmud.app import EngineApp
    from genericmud.completion import CompletionCycler
    from genericmud.config.ui_prefs import UiPrefs
    from genericmud.config.worlds import World
    from genericmud.ui.command_entry import CommandEntryCtrl
    from genericmud.ui.main_window import GenericMudFrame
    from genericmud.ui.session_panel import SessionPanel
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    app = wx.App(False)
    app.SetExitOnFrameDelete(False)
    frame = wx.Frame(None, title="Editable command entry probe", size=(700, 550))
    app.SetTopWindow(frame)
    book = wx.Simplebook(frame)
    frame.SetSizer(wx.BoxSizer(wx.VERTICAL))
    frame.GetSizer().Add(book, 1, wx.EXPAND)
    sent = []
    loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))

    def no_start(coro, _loop):
        coro.close()

    with patch("asyncio.run_coroutine_threadsafe", no_start):
        panels = [SessionPanel(
            book, loop, {}, World(name=name, host="localhost", port=4000),
            prefs=UiPrefs(editable_command_buffer=editable, numpad_compass=False),
        ) for name, editable in (("Alpha", True), ("Beta", True), ("Normal", False))]
    for panel in panels:
        book.AddPage(panel, panel.world.name)
        panel.app = SimpleNamespace(on_ws_message=lambda msg, p=panel: sent.append((p, msg)))
    panel, other, normal = panels
    entry = panel.input
    assert isinstance(entry, CommandEntryCtrl)

    def key(control, code, *, shift=False, char=False):
        event = wx.KeyEvent(wx.wxEVT_CHAR if char else wx.wxEVT_KEY_DOWN)
        event.SetKeyCode(code)
        event.SetShiftDown(shift)
        control.GetEventHandler().ProcessEvent(event)
        return event

    def enter(p=panel):
        if sys.platform == "darwin":
            # Exercise Cocoa's actual CHAR -> TEXT_ENTER translation as well.
            key(p.input, wx.WXK_RETURN, char=True)
            return
        event = wx.CommandEvent(wx.wxEVT_TEXT_ENTER, p.input.GetId())
        event.SetEventObject(p.input)
        p.input.GetEventHandler().ProcessEvent(event)

    def line(row, col=0):
        lines = entry.GetValue().split("\n")
        offset = sum(len(text) + 1 for text in lines[:row]) + col
        entry.SetInsertionPoint(entry.position_from_offset(offset))

    def check_document_scenarios():
        # 1: drafts retain their place while later commands execute.
        entry.WriteText("north")
        enter()
        entry.WriteText("look target")
        key(entry, wx.WXK_RETURN, shift=True, char=True)
        entry.WriteText("south")
        enter()
        entry.WriteText("east")
        enter()
        expected = "north\nlook target\nsouth\neast\n"
        assert entry.GetValue() == expected
        assert [msg["text"] for p, msg in sent if p is panel] == ["north", "south", "east"]
        # 2: Up is left to the native editor, including unsent lines.
        event = key(entry, wx.WXK_UP)
        assert event.GetSkipped()
        # The handler passes Up through. Set the resulting native caret location
        # deterministically; physical key delivery is a separate platform check.
        line(1)
        assert entry.line_number() == 1
        entry.replace_command("look dragon")
        # 5/6: editing is in place, and Enter appends its executed copy.
        assert entry.GetValue() == expected.replace("look target", "look dragon")
        enter()
        assert entry.GetValue() == "north\nlook dragon\nsouth\neast\nlook dragon\n"
        # 7: unchanged newest historical command executes again without a copy.
        line(4)
        before = entry.GetValue()
        count = len(sent)
        enter()
        assert entry.GetValue() == before
        assert len(sent) == count + 1
        # 8: manually entering that same command retains another line.
        entry.WriteText("look dragon")
        enter()
        assert entry.GetValue() == before + "look dragon\n"
        # Shift-selection and horizontal/deletion gestures must stay native.
        for code in (wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_UP, wx.WXK_DOWN):
            assert key(entry, code, shift=True).GetSkipped()
        for code in (wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_BACK, wx.WXK_DELETE):
            assert key(entry, code).GetSkipped()
        # 9: native selection deletion crosses line boundaries and joins fragments.
        entry.SetSelection(entry.XYToPosition(2, 0), entry.XYToPosition(4, 2))
        assert entry.selected_text() == "rth\nlook dragon\nsout"
        entry.Remove(*entry.GetSelection())
        assert entry.GetValue().startswith("noh\neast\n")
        # Logical lines and caret units: BMP, non-BMP, combining marks, CJK.
        entry.SetValue("north\nатака 🐉 e\u0301界\nunsent")
        line(1)
        assert entry.current_command() == "атака 🐉 e\u0301界"
        enter()
        assert sent[-1][1]["text"] == "атака 🐉 e\u0301界"
        assert entry.GetValue() == "north\nатака 🐉 e\u0301界\nunsent\nатака 🐉 e\u0301界\n"
        # Plugin APIs replace only the current line, not the document.
        line(2)
        assert panel.get_command() == "unsent"
        assert not panel.set_command("do not overwrite")
        panel._fill_command("MXP draft")
        assert entry.GetValue().split("\n")[2] == "MXP draft"
        panel.select_command(1, 3)
        assert panel.paste_command("new") == "MXP"
        assert panel.get_command() == "new draft"
        entry.move_to_tail()
        assert panel.set_command("plugin")
        assert entry.GetValue().endswith("plugin")
        # Completion at a Unicode word, with a suffix, preserves every other row.
        panel._completion = CompletionCycler()
        panel.app.word_index = SimpleNamespace(complete=lambda prefix: ["dragon🐉"])
        panel._speak_system = lambda word: None
        entry.replace_command("dr suffix", caret=2)
        panel._cycle_completion(False)
        assert entry.current_command() == "dragon🐉 suffix"
        a, _b = entry.line_bounds()
        assert entry.text_range(a, entry.GetInsertionPoint()) == "dragon🐉"
        # Disconnected Enter must leave text/selection/provenance untouched.
        panel._connect_finished = True
        panel.is_connected = lambda: False
        prompted = []
        panel._prompt_reconnect = lambda: prompted.append(True)
        before, selection = entry.GetValue(), entry.GetSelection()
        enter()
        assert prompted and entry.GetValue() == before and entry.GetSelection() == selection
        panel._connect_finished = False
        # 10: real Simplebook session switching cannot share command buffers.
        other.input.WriteText("Beta draft")
        harness = SimpleNamespace(
            book=book, _update_active=lambda: None, _update_focus=lambda: None,
            _announce_tab=lambda index: None,
        )
        GenericMudFrame._switch_session(harness, 1)
        assert other.input.GetValue() == "Beta draft"
        GenericMudFrame._switch_session(harness, 0)
        assert panel.input.GetValue() == before
        # 11: normal mode still clears/sends and parks/restores one draft.
        normal.input.WriteText("normal")
        enter(normal)
        assert normal.input.GetValue() == "" and normal._history == ["normal"]
        normal.input.WriteText("normal draft")
        normal._recall_history(-1)
        assert normal.input.GetValue() == "normal"
        normal._recall_history(1)
        assert normal.input.GetValue() == "normal draft"
        normal._handle_message({"type": "input_masked", "masked": True})
        normal.input.SetValue("normal secret")
        enter(normal)
        assert normal._history == ["normal"]
        # Optional password mode hides/suspends the document and never records secrets.
        saved_history = list(panel._history)
        panel._handle_message({"type": "input_masked", "masked": True})
        secret = panel.input
        assert secret is not entry and secret.HasFlag(wx.TE_PASSWORD) and not entry.IsShown()
        secret.WriteText("sensitive")
        enter()
        assert panel._history == saved_history and entry.GetValue() == before
        assert sent[-1][1]["text"] == "sensitive"
        secret.WriteText("unsubmitted secret")
        panel._handle_message({"type": "input_masked", "masked": False})
        assert panel.input is entry and entry.GetValue() == before
        assert "secret" not in entry.GetValue()
        panel._set_input_masked(False)  # repeated unhide leaves the document alone
        assert panel.input is entry
        # A cross-line Enter selection submits one logical line, never the selection.
        entry.SetValue("north\nsouth\ndraft")
        entry.SetSelection(entry.position_from_offset(2), entry.position_from_offset(14))
        count = len(sent)
        enter()
        assert len(sent) == count + 1 and sent[-1][1]["text"] == "north"
        assert entry.GetValue() == "north\nsouth\ndraft\nnorth\n"
        # Exercise the unchanged engine path with an in-memory alias, stacking,
        # client command, auto-repeat, and secret dispatch; no server or config writes.
        network = []
        panel.app = EngineApp(
            VoiceRouter(RecordingBackend(), clock=lambda: 0.0), send=network.append,
            post=panel._handle_message, keymap={}, schedule=lambda *args: None,
        )
        panel.app.engine.add_alias("x", lambda ctx: ctx.engine.sink.send("look"))
        panel.app.autoretype = True
        entry.SetValue("x;south")
        enter()
        assert network == ["look", "south"]
        document = entry.GetValue()
        enter()  # empty tail still reaches engine autoretype
        assert network == ["look", "south", "look", "south"]
        assert entry.GetValue() == document
        entry.WriteText("/aliases")
        enter()
        assert len(network) == 4
        panel.app._set_input_masked(True)
        panel.input.WriteText("abc;def")
        enter()
        assert network[-1] == "abc;def"  # neither split nor treated as an alias
        assert panel.app._last_input == "/aliases"
        assert "abc;def" not in panel._history and "abc;def" not in entry.GetValue()
        panel.app._set_input_masked(False)

    errors = []
    stages = iter((
        ("north\nold\n", "", False),
        ("north\nold\nfinal draft", "", False),
        ("north\nold\n", "я", True),
        ("north\nold\nfinal draft", "X", True),
    ))
    current = None
    stage = "start"
    deadline = time.monotonic() + 15
    settle_at = 0.0
    focus_events = []

    def record_focus(event):
        focus_events.append(True)
        event.Skip()

    entry.Bind(wx.EVT_SET_FOCUS, record_focus)

    def finish():
        timer.Stop()
        app.ExitMainLoop()

    def poll():
        nonlocal current, stage, settle_at
        try:
            assert time.monotonic() < deadline, (stage, wx.Window.FindFocus())
            if stage == "start" and entry.HasFocus():
                check_document_scenarios()
                stage = "next"
            if stage == "next":
                current = next(stages, None)
                if current is None:
                    finish()
                    return
                text, _char, _typing = current
                entry.SetValue(text)
                entry.SetSelection(entry.XYToPosition(0, 1), entry.XYToPosition(3, 1))
                focus_events.clear()
                stage = "output"
                panel.output.SetFocus()
            elif stage == "output" and panel.output.HasFocus():
                text, char, typing = current
                if typing:
                    event = wx.KeyEvent(wx.wxEVT_CHAR)
                    event.SetUnicodeKey(ord(char))
                    event.SetKeyCode(ord(char))
                    panel.output.GetEventHandler().ProcessEvent(event)
                else:
                    if sys.platform == "darwin":
                        key(panel.output, wx.WXK_TAB, char=True)
                    else:
                        entry.SetFocus()
                settle_at = time.monotonic() + 0.1
                stage = "settle"
            elif stage == "settle" and entry.HasFocus() and time.monotonic() >= settle_at:
                text, char, _typing = current
                # 3/4/12: real Output return chooses empty/draft tail, and typing
                # cannot replace the old selection. No new line or command is sent.
                assert focus_events
                assert entry.GetValue() == text + char
                assert entry.GetSelection() == (entry.GetLastPosition(),) * 2
                stage = "next"
        except BaseException as error:
            errors.append(error)
            finish()

    frame.Show()
    frame.Raise()
    timer = wx.PyTimer(poll)
    timer.Start(10)
    wx.CallAfter(entry.SetFocus)
    try:
        app.MainLoop()
    finally:
        timer.Stop()
        for p in panels:
            p._alive = False
        frame.Destroy()
    if errors:
        raise errors[0]
    print("native scenarios passed")
