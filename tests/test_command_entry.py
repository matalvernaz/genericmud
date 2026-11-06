"""Native controls/focus with synthetic events; no assistive-technology claim."""

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
        encoding="utf-8", timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native scenarios passed" in result.stdout


def _probe():
    import threading
    import time
    import unicodedata
    from types import SimpleNamespace
    from unittest.mock import patch

    import wx

    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.config.ui_prefs import UiPrefs
    from genericmud.config.worlds import World
    from genericmud.ui.command_entry import CommandEntryCtrl
    from genericmud.ui.main_window import GenericMudFrame
    from genericmud.ui.session_panel import SessionPanel
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    app = wx.App(False)
    app.SetExitOnFrameDelete(False)
    frame = wx.Frame(None, title="Command input regression probe", size=(700, 550))
    app.SetTopWindow(frame)
    book = wx.Simplebook(frame)
    frame.SetSizer(wx.BoxSizer(wx.VERTICAL))
    frame.GetSizer().Add(book, 1, wx.EXPAND)
    sent, spoken = [], []
    loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))
    prefs = UiPrefs(command_input_mode="editable", numpad_compass=False)

    def no_start(coro, _loop):
        coro.close()

    with patch("asyncio.run_coroutine_threadsafe", no_start):
        alpha = SessionPanel(book, loop, load_keymap("vipmud"), World("Alpha", "localhost", 4000),
                             prefs=prefs)
        beta = SessionPanel(book, loop, {}, World("Beta", "localhost", 4000), prefs=prefs)
        prefs.command_input_mode = "standard"  # existing sessions retain their original controls
        normal = SessionPanel(book, loop, {}, World("Normal", "localhost", 4000), prefs=prefs)
    panels = [alpha, beta, normal]
    for panel in panels:
        book.AddPage(panel, panel.world.name)
        panel.app = SimpleNamespace(on_ws_message=lambda msg, p=panel: sent.append((p, msg)))
        panel._speak_system = spoken.append
    entry = alpha.input
    assert isinstance(entry, CommandEntryCtrl) and isinstance(beta.input, CommandEntryCtrl)
    assert not isinstance(normal.input, CommandEntryCtrl)
    assert normal.input.HasFlag(wx.TE_MULTILINE)

    def position(offset):
        entry.SetInsertionPoint(entry.position_from_offset(offset))

    def select(start, end):
        entry.SetSelection(entry.position_from_offset(start), entry.position_from_offset(end))

    def key(control, code, *, shift=False, ctrl=False, char=False):
        event = wx.KeyEvent(wx.wxEVT_CHAR if char else wx.wxEVT_KEY_DOWN)
        event.SetKeyCode(code)
        event.SetShiftDown(shift)
        event.SetControlDown(ctrl)
        control.GetEventHandler().ProcessEvent(event)
        return event

    def enter(panel=alpha, *, shift=False):
        if shift:
            event = key(panel.input, wx.WXK_RETURN, shift=True)
            assert event.GetSkipped()
            key(panel.input, wx.WXK_RETURN, shift=True, char=True)
        elif sys.platform == "darwin":
            event = key(panel.input, wx.WXK_RETURN)
            if event.GetSkipped():
                key(panel.input, wx.WXK_RETURN, char=True)
        else:
            # Exercise the bound native submission event; physical platform key
            # translation and screen-reader interception need manual validation.
            event = wx.CommandEvent(wx.wxEVT_TEXT_ENTER, panel.input.GetId())
            event.SetEventObject(panel.input)
            panel.input.GetEventHandler().ProcessEvent(event)

    def check_document():
        entry.WriteText("north")
        enter()
        assert entry.GetValue() == "north\n"
        assert entry.GetSelection() == (entry.GetLastPosition(),) * 2
        entry.WriteText("look target")
        key(entry, wx.WXK_DOWN)
        entry.WriteText("south")
        enter()
        entry.WriteText("east")
        enter()
        assert entry.GetValue() == "north\nlook target\nsouth\neast\n"
        assert [m["text"] for p, m in sent if p is alpha] == ["north", "south", "east"]
        before = entry.GetValue()
        assert key(entry, wx.WXK_UP).GetSkipped()
        position(before.index("look target"))
        entry.replace_command("look dragon")
        assert entry.GetValue() == before.replace("look target", "look dragon")
        enter()
        assert entry.GetValue() == "north\nlook dragon\nsouth\neast\nlook dragon\n"
        position(entry.GetValue().rfind("look dragon"))
        count, before = len(sent), entry.GetValue()
        enter()
        assert len(sent) == count + 1 and entry.GetValue() == before
        entry.WriteText("look dragon")
        enter()
        assert entry.GetValue() == before  # manual adjacent duplicate is removed too
        entry.SetValue("north\n")
        position(1)
        enter()
        assert entry.GetValue() == "north\nnorth\n"  # short-buffer exception
        entry.SetValue("al\nalpha")
        position(len("al\nal"))
        enter()
        assert sent[-1][1]["text"] == "alpha"
        entry.SetValue("p\na\n")
        entry.move_to_tail()
        enter()
        assert entry.GetValue() == "p\na\n\n"  # empty Enter updates the document
        # Shift+Down remains selection; plain Down creates just one missing tail.
        entry.SetValue("draft")
        position(2)
        assert key(entry, wx.WXK_DOWN, shift=True).GetSkipped()
        assert entry.GetValue() == "draft"
        key(entry, wx.WXK_DOWN)
        assert entry.GetValue() == "draft\n"
        key(entry, wx.WXK_DOWN)
        assert entry.GetValue() == "draft\n"
        # Shift+Enter executes in place, including an explicit cross-line selection.
        entry.SetValue("north\nsouth\ndraft")
        position(2)
        before, selection, count = entry.GetValue(), entry.GetSelection(), len(sent)
        enter(shift=True)
        assert sent[-1][1]["text"] == "north" and len(sent) == count + 1
        assert entry.GetValue() == before and entry.GetSelection() == selection
        select(1, 8)
        before, selection, count = entry.GetValue(), entry.GetSelection(), len(sent)
        enter(shift=True)
        assert [m["text"] for _p, m in sent[count:]] == ["orth", "so"]
        assert entry.GetValue() == before and entry.GetSelection() == selection
        # Ordinary Enter with the same selection executes the first logical row only.
        enter()
        assert sent[-1][1]["text"] == "north"
        assert entry.GetValue() == before + "\nnorth\n"
        # Escape drops the selection but preserves its text, including the first-row case.
        entry.SetValue("p\nabcd\ntail")
        select(3, 8)
        key(entry, wx.WXK_ESCAPE)
        assert entry.GetValue() == "p\n\nabcd\ntail"
        assert entry.GetSelection() == (entry.position_from_offset(2),) * 2
        entry.SetValue("abcdef")
        select(2, 4)
        key(entry, wx.WXK_ESCAPE)
        assert entry.GetValue() == "ab\ncdef" and entry.GetSelection() == (1, 1)
        # Boundary Backspace is protected in both event paths; selections are never blocked.
        entry.SetValue("a\nbc\nd")
        position(2)
        assert not key(entry, wx.WXK_BACK).GetSkipped()
        key(entry, wx.WXK_BACK, char=True)
        assert entry.GetValue() == "a\nbc\nd"
        assert key(entry, wx.WXK_BACK, shift=True).GetSkipped()
        select(2, 6)
        assert key(entry, wx.WXK_BACK).GetSkipped()
        assert key(entry, wx.WXK_DELETE).GetSkipped()
        entry.Remove(*entry.GetSelection())
        assert entry.GetValue() == "a\n"  # explicit cross-line deletion is allowed
        # Ctrl+R sees drafts and earlier commands, and repeated presses cycle older matches.
        entry.SetValue("LOOK first\nlook draft\nlo")
        entry.move_to_tail()
        key(entry, ord("R"), ctrl=True)
        assert entry.GetValue() == "LOOK first\nlook draft\nlook draft"
        # Actual Cocoa flagsChanged delivery uses KEY_DOWN with key code zero
        # for modifier transitions; other ports report the modifier key itself.
        # Releasing/pressing Control must not restart prefix completion.
        for modifier in (0, wx.WXK_CONTROL, wx.WXK_RAW_CONTROL, wx.WXK_SHIFT, wx.WXK_ALT):
            assert key(entry, modifier).GetSkipped()
            assert key(entry, modifier, ctrl=True).GetSkipped()
        key(entry, ord("R"), ctrl=True)
        assert entry.GetValue().endswith("\nloOK first")  # preserve the typed prefix's case
        key(entry, wx.WXK_RETURN, ctrl=True)
        assert entry.GetSelection() == (entry.GetLastPosition(),) * 2
        assert alpha._prefs.autoretype is False  # no conflicting default action
        # Insertion after each logical boundary must not split a native CRLF
        # or surrogate pair. Check visible text, not just GetRange round trips:
        # on Windows both halves of CRLF can read as the same normalized prefix.
        document = "north\n🐉 e\u0301界\n\nunsent"
        for offset in range(len(document) + 1):
            # Cocoa snaps an insertion inside e + combining accent to the
            # grapheme's edge. Exercise legal native caret positions here;
            # the range-conversion unit test still covers every code point.
            if offset < len(document) and unicodedata.combining(document[offset]):
                continue
            entry.SetValue(document)
            position(offset)
            entry.WriteText("X")
            assert entry.GetValue() == document[:offset] + "X" + document[offset:], (
                offset, entry.GetSelection(), entry.GetValue(),
                document[:offset] + "X" + document[offset:],
            )
        entry.SetValue("north\n")
        position(len("north\n"))
        assert entry.GetSelection() == (entry.GetLastPosition(),) * 2
        # Unicode mapping after non-BMP, combining marks and multiple line separators.
        entry.SetValue("north\nатака 🐉 e\u0301界\nunsent")
        position(len("north\nата"))
        assert entry.current_command() == "атака 🐉 e\u0301界"
        enter()
        assert sent[-1][1]["text"] == "атака 🐉 e\u0301界"
        position(entry.GetValue().index("unsent"))
        assert alpha.get_command() == "unsent"
        assert not alpha.set_command("never overwrite")
        alpha._fill_command("MXP draft")
        alpha.select_command(1, 3)
        assert alpha.paste_command("new") == "MXP"
        assert alpha.get_command() == "new draft"
        entry.replace_command("🐉界 draft")
        alpha.select_command(1, 1)
        assert alpha.paste_command("X") == "🐉"
        assert alpha.get_command() == "X界 draft"
        entry.move_to_tail()
        assert alpha.set_command("plugin")
        # Plugin Paste must not erase an old cross-line UI selection or execute inserted rows.
        select(0, len("north\nатака"))
        before, count = entry.GetValue(), len(sent)
        assert alpha.paste_command("bad") == ""
        assert entry.GetValue() == before and len(sent) == count
        assert "selection within one line" in spoken[-1]
        entry.move_to_tail()
        alpha.select_command(1, -1)
        alpha.paste_command("draft1\ndraft2")
        assert len(sent) == count
        enter()
        assert sent[-1][1]["text"] == "draft2"  # never implicitly execute the whole document
        entry.move_to_tail()
        alpha.paste_command("one\r\ntwo\rthree")
        assert entry.GetValue().endswith("one\ntwo\nthree")
        count = len(sent)
        entry.SetValue("say a\u2028b\nlook")
        select(0, len(entry.GetValue()))
        enter(shift=True)
        assert [m["text"] for _p, m in sent[count:]] == ["say a\u2028b", "look"]
        # Completion preserves other rows, including Unicode around the completed word.
        entry.move_to_tail()
        entry.WriteText("dr suffix")
        position(len(entry.GetValue()) - len(" suffix"))
        alpha.app.word_index = SimpleNamespace(complete=lambda prefix: ["dragon🐉"])
        alpha._cycle_completion(False)
        assert entry.current_command() == "dragon🐉 suffix"
        start, _end = entry.line_bounds()
        assert entry.text_range(start, entry.GetInsertionPoint()) == "dragon🐉"
        # Offline handling preserves text and selection for both submission actions.
        alpha._connect_finished = True
        alpha.is_connected = lambda: False
        alpha.is_connecting = lambda: False
        prompted = []
        alpha._prompt_reconnect = lambda: prompted.append(True)
        before, selection = entry.GetValue(), entry.GetSelection()
        enter()
        enter(shift=True)
        assert len(prompted) == 2 and entry.GetValue() == before
        assert entry.GetSelection() == selection
        entry.replace_command("/aliases")
        count = len(sent)
        enter()
        assert len(sent) == count + 1  # client commands work offline
        alpha._connect_finished = False
        # Session switching cannot exchange documents; live preference changes cannot convert them.
        beta.input.WriteText("Beta draft")
        harness = SimpleNamespace(book=book, _update_active=lambda: None,
                                  _update_focus=lambda: None, _announce_tab=lambda index: None)
        before = entry.GetValue()
        GenericMudFrame._switch_session(harness, 1)
        GenericMudFrame._switch_session(harness, 0)
        assert beta.input.GetValue() == "Beta draft" and entry.GetValue() == before
        # Standard uses upstream compose/history behavior: retain drafts and send explicit blocks.
        normal.input.WriteText("look")
        enter(normal)
        normal.input.WriteText("normal draft")
        assert not normal._history_step(-1)
        assert normal.input.GetValue() == "normal draft"
        normal.input.SetValue("look\nscore")
        enter(normal)
        assert normal.input.GetValue() == "" and normal._history == ["look", "look", "score"]
        # Optional hidden input suspends the document and never puts secrets into its history/undo.
        before, selection, history = entry.GetValue(), entry.GetSelection(), list(alpha._history)
        alpha._set_input_masked(True)
        secret = alpha.input
        assert secret.HasFlag(wx.TE_PASSWORD) and not entry.IsShown()
        secret.WriteText(" pass;word ")
        enter()
        assert sent[-1][1]["text"] == " pass;word " and alpha._history == history
        assert entry.GetValue() == before
        secret.WriteText("unsubmitted")
        alpha._set_input_masked(False)
        assert alpha.input is entry and entry.GetValue() == before
        assert entry.GetSelection() == selection
        assert spoken[-1] == "Unsent password cleared"
        # Real shared engine path: aliases, stacking, autoretype and secret bypass stay intact.
        network = []
        alpha.app = EngineApp(
            VoiceRouter(RecordingBackend(), clock=lambda: 0.0), send=network.append,
            post=alpha._handle_message, keymap=load_keymap("vipmud"), schedule=lambda *args: None,
        )
        alpha.app.engine.add_alias("x", lambda ctx: ctx.engine.sink.send("look"))
        alpha.app.autoretype = True
        entry.SetValue("x;south")
        entry.move_to_tail()
        enter()
        enter()
        assert network == ["look", "south", "look", "south"]
        alpha.app._set_input_masked(True)
        alpha.input.WriteText("abc;def")
        enter()
        assert network[-1] == "abc;def" and alpha.app._last_input == "x;south"
        assert "abc;def" not in alpha._history and "abc;def" not in entry.GetValue()
        alpha.app._set_input_masked(False)
        # Large documents keep early drafts; the old client's character trim is not copied.
        entry.SetValue("pending draft\n" * 5000 + "look")
        entry.move_to_tail()
        enter()
        assert entry.GetValue().startswith("pending draft\npending draft\n")
        assert entry.GetValue().endswith("look\n")
        # Pack/user bindings win over the new default Ctrl+R / Ctrl+Enter / Escape actions.
        for combo, code, ctrl in (("ctrl+r", ord("R"), True),
                                  ("ctrl+enter", wx.WXK_RETURN, True),
                                  ("escape", wx.WXK_ESCAPE, False)):
            alpha.app.engine.add_key(combo, lambda ctx: ctx.engine.sink.send("bound"))
            count = len(network)
            key(entry, code, ctrl=ctrl)
            assert network[count:] == ["bound"]
        assert not any("abc;def" in text for text in spoken)

    errors = []
    stages = iter((("north\nold\n", "", False),
                   ("north\nold\nfinal draft", "", False),
                   ("north\nold\n", "я", True),
                   ("north\nold\nfinal draft", "🐉", True)))
    current, stage = None, "start"
    deadline, settle_at = time.monotonic() + 20, 0.0
    focus_events, worker_result = [], {}
    worker = None

    def record_focus(event):
        focus_events.append(True)
        event.Skip()

    entry.Bind(wx.EVT_SET_FOCUS, record_focus)

    def finish():
        timer.Stop()
        app.ExitMainLoop()

    def plugin_work():
        try:
            worker_result["read"] = alpha.get_command()
            alpha.select_command(1, -1)
            worker_result["replaced"] = alpha.paste_command("plugin result")
        except BaseException as error:
            worker_result["error"] = error

    def poll():
        nonlocal current, stage, settle_at, worker
        try:
            assert time.monotonic() < deadline, (stage, wx.Window.FindFocus())
            if stage == "start" and entry.HasFocus():
                check_document()
                stage = "next"
            if stage == "next":
                current = next(stages, None)
                if current is None:
                    entry.SetValue("old\nworker draft")
                    entry.move_to_tail()
                    worker = threading.Thread(target=plugin_work, daemon=True)
                    worker.start()
                    stage = "worker"
                    return
                text, _char, _typing = current
                entry.SetValue(text)
                select(6, 9)
                focus_events.clear()
                stage = "output"
                alpha.output.SetFocus()
            elif stage == "output" and alpha.output.HasFocus():
                _text, char, typing = current
                if typing:
                    event = wx.KeyEvent(wx.wxEVT_CHAR)
                    event.SetUnicodeKey(ord(char))
                    event.SetKeyCode(ord(char))
                    alpha.output.GetEventHandler().ProcessEvent(event)
                elif sys.platform == "darwin":
                    key(alpha.output, wx.WXK_TAB, char=True)
                else:
                    entry.SetFocus()
                settle_at = time.monotonic() + 0.1
                stage = "settle"
            elif stage == "settle" and entry.HasFocus() and time.monotonic() >= settle_at:
                text, char, _typing = current
                assert focus_events and entry.GetValue() == text + char
                assert entry.GetSelection() == (entry.GetLastPosition(),) * 2
                stage = "next"
            elif stage == "worker" and not worker.is_alive():
                assert worker_result == {"read": "worker draft", "replaced": "worker draft"}
                assert entry.GetValue() == "old\nplugin result"
                finish()
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
        for panel in panels:
            panel._alive = False
        frame.Destroy()
    if errors:
        raise errors[0]
    print("native scenarios passed")
