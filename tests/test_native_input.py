"""Real wx focus transitions; Linux CI needs its existing xvfb-run wrapper.

The child process isolates wx.App's native main loop from the other dialog tests.
Only EVT_CHAR is synthetic: SetFocus, selection changes and focus events are native.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_typing_from_output_preserves_command_selection():
    pytest.importorskip("wx")
    if sys.platform == "darwin" and os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip("GitHub-hosted macOS does not grant native foreground/key-window focus")
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        pytest.skip("native wx focus needs a display; run under xvfb-run on Linux")
    result = subprocess.run(
        [sys.executable, "-c", "from tests.test_native_input import _probe; _probe()"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        encoding="utf-8", timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cases = json.loads(result.stdout)
    assert len(cases) == 6
    failures = [case for case in cases if case["observed"] != case["expected"]
                or case["caret"] != case["expected_caret"]]
    assert not failures, json.dumps(failures, ensure_ascii=False, indent=2)


def _probe():
    import wx

    from genericmud.ui.session_panel import SessionPanel

    cases = [
        ("caret", "attack goblin", 3, 3, "X"),
        ("substring", "attack goblin", 2, 6, "X"),
        ("empty", "", 0, 0, "X"),
        ("unicode_character", "attack goblin", 3, 3, "я"),
        ("space", "attack goblin", 3, 3, " "),
        ("unicode_command", "атака гоблина", 3, 3, "X"),
    ]
    app = wx.App(False)
    app.SetExitOnFrameDelete(False)
    frame = wx.Frame(None, title="Command input regression", size=(600, 350))
    app.SetTopWindow(frame)
    panel = wx.Panel(frame)
    controls = SimpleNamespace(
        output=wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP),
        input=wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER),
    )
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(controls.output, 1, wx.EXPAND)
    layout.Add(controls.input, 0, wx.EXPAND)
    panel.SetSizer(layout)
    controls.output.Bind(wx.EVT_CHAR, lambda event: SessionPanel._on_output_char(controls, event))
    results, errors, focus_events = [], [], []
    stage = "input"
    deadline = time.monotonic() + 5
    settle_at = 0
    inside_handler = False

    def focused(control):
        return control.HasFocus()

    def on_focus(event):
        focus_events.append({"selection": controls.input.GetSelection(),
                             "during_handler": inside_handler})
        event.Skip()

    controls.input.Bind(wx.EVT_SET_FOCUS, on_focus)

    def finish():
        timer.Stop()
        app.ExitMainLoop()

    def poll():
        nonlocal stage, deadline, settle_at, inside_handler
        try:
            assert time.monotonic() < deadline, (
                f"Native focus timeout: stage={stage}, active={frame.IsActive()}, "
                f"input={controls.input.HasFocus()}, output={controls.output.HasFocus()}, "
                f"FindFocus={wx.Window.FindFocus()!r}, events={focus_events!r}"
            )
            name, text, start, end, char = cases[len(results)]
            if stage == "input" and focused(controls.input):
                controls.input.SetValue(text)
                controls.input.SetSelection(start, end)
                focus_events.clear()
                stage = "output"
                controls.output.SetFocus()
            elif stage == "output" and focused(controls.output):
                assert controls.input.GetSelection() == (start, end)
                event = wx.KeyEvent(wx.wxEVT_CHAR)
                event.SetUnicodeKey(ord(char))
                event.SetKeyCode(ord(char))
                event.SetId(controls.output.GetId())
                event.SetEventObject(controls.output)
                inside_handler = True
                controls.output.GetEventHandler().ProcessEvent(event)
                inside_handler = False
                settle_at = time.monotonic() + 0.1
                stage = "settle"
            elif stage == "settle" and focused(controls.input) and time.monotonic() >= settle_at:
                assert focus_events, "No native Command EVT_SET_FOCUS received"
                results.append({
                    "name": name, "expected": text[:start] + char + text[end:],
                    "observed": controls.input.GetValue(),
                    "expected_caret": start + len(char),
                    "caret": controls.input.GetInsertionPoint(),
                    "focus_events": list(focus_events),
                })
                if len(results) == len(cases):
                    finish()
                    return
                stage = "input"
                deadline = time.monotonic() + 5
        except BaseException as error:
            errors.append(error)
            finish()

    frame.Show()
    frame.Raise()
    timer = wx.PyTimer(poll)
    timer.Start(10)
    wx.CallAfter(controls.input.SetFocus)
    try:
        app.MainLoop()
    finally:
        timer.Stop()
        if frame:
            frame.Destroy()
    if errors:
        raise errors[0]
    assert len(results) == len(cases), "MainLoop exited before native focus scenarios completed"
    # ASCII escapes: a Windows pipe uses the ANSI code page, which can't encode the Cyrillic cases.
    print(json.dumps(results))
