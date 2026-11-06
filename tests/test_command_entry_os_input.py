"""Opt-in OS input smoke test; ordinary CI never needs foreground access.

Run on an idle interactive desktop with GENERICMUD_TEST_OS_INPUT=1.
Unlike test_command_entry.py, this sends keys with wx.UIActionSimulator;
it never constructs KeyEvent objects or calls input handlers directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_command_entry_os_keyboard():
    # Fail closed before even importing wx: inherited opt-in flags must not
    # enable foreground input in CI, including empty or unexpected marker values.
    if "CI" in os.environ or "GITHUB_ACTIONS" in os.environ:
        pytest.skip("OS input smoke test is disabled in CI")
    if os.environ.get("GENERICMUD_TEST_OS_INPUT") != "1":
        pytest.skip("opt-in foreground test: set GENERICMUD_TEST_OS_INPUT=1")
    pytest.importorskip("wx")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("wx.UIActionSimulator needs an X11 display; Wayland alone is unsupported")
    if sys.platform == "darwin":
        import ctypes

        library = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
        )
        access = {}
        for name in ("AXIsProcessTrusted", "CGPreflightPostEventAccess"):
            function = getattr(library, name)
            function.restype = ctypes.c_bool
            function.argtypes = []
            access[name] = function()
        if not access["CGPreflightPostEventAccess"]:
            pytest.skip(f"macOS blocks OS event posting for this Python process: {access}")
    result = subprocess.run(
        [sys.executable, "-c", "from tests.test_command_entry_os_input import _probe; _probe()"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        encoding="utf-8", timeout=45,
    )
    if result.returncode == 77:
        pytest.skip(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OS keyboard scenarios passed" in result.stdout


@pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS"])
@pytest.mark.parametrize("value", ["", "false", "true"])
def test_os_input_cannot_start_in_ci(monkeypatch, marker, value):
    for name in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GENERICMUD_TEST_OS_INPUT", "1")
    monkeypatch.setenv(marker, value)

    def forbidden(*_args, **_kwargs):
        pytest.fail("CI must neither import wx nor start a GUI child process")

    monkeypatch.setattr(pytest, "importorskip", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(pytest.skip.Exception, match="disabled in CI"):
        test_command_entry_os_keyboard()


def test_os_input_requires_explicit_opt_in(monkeypatch):
    for name in ("CI", "GITHUB_ACTIONS", "GENERICMUD_TEST_OS_INPUT"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A normal test run must not import wx or start OS input")

    monkeypatch.setattr(pytest, "importorskip", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(pytest.skip.Exception, match="opt-in foreground test"):
        test_command_entry_os_keyboard()


def _probe():
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    import wx

    from genericmud.config.keymap import load_keymap
    from genericmud.config.ui_prefs import UiPrefs
    from genericmud.config.worlds import World
    from genericmud.ui.session_panel import SessionPanel

    app = wx.App(False)
    frame = wx.Frame(None, title="Isolated genericMud OS keyboard test", size=(700, 520))
    app.SetTopWindow(frame)
    book = wx.Simplebook(frame)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(book, 1, wx.EXPAND)
    frame.SetSizer(layout)
    canary_panel = wx.Panel(book)
    canary_layout = wx.BoxSizer(wx.VERTICAL)
    canary_layout.Add(wx.StaticText(canary_panel, label="&Input delivery check:"))
    canary = wx.TextCtrl(canary_panel)
    canary.SetName("Input delivery check")
    canary_layout.Add(canary, 0, wx.EXPAND)
    canary_panel.SetSizer(canary_layout)
    book.AddPage(canary_panel, "Delivery check")
    loop = SimpleNamespace(call_soon_threadsafe=lambda function, *args: function(*args))
    sent, panels = [], []

    def no_start(coroutine, _loop):
        coroutine.close()  # no connection, DNS lookup or engine startup

    for mode in ("editable", "standard"):
        with patch("asyncio.run_coroutine_threadsafe", no_start):
            panel = SessionPanel(book, loop, load_keymap("vipmud"),
                                 World(mode, "invalid.local", 1),
                                 prefs=UiPrefs(command_input_mode=mode, numpad_compass=False))
        panel.app = SimpleNamespace(on_ws_message=sent.append,
                                    engine=SimpleNamespace(has_key=lambda _combo: False))
        panel._speak_system = lambda _text: None
        panel.output.SetValue("Local fake engine only")
        book.AddPage(panel, mode)
        panels.append(panel)
    editable, standard = panels
    entry = editable.input
    command = standard.input
    controls = [canary, entry, editable.output, command, standard.output]
    keyups = 0
    last_event = time.monotonic()

    def observe(event):
        nonlocal keyups, last_event
        last_event = time.monotonic()
        if event.GetEventType() == wx.wxEVT_KEY_UP:
            keyups += 1
        event.Skip()

    for control in controls:
        observer = wx.EvtHandler()
        for kind in (wx.EVT_KEY_DOWN, wx.EVT_KEY_UP, wx.EVT_CHAR):
            observer.Bind(kind, observe)
        control.PushEventHandler(observer)

    def fixture(panel, text, start, end=None, *, output=False):
        book.SetSelection(1 if panel is editable else 2)
        panel.input.SetValue(text)
        panel.input.EmptyUndoBuffer()
        panel.input.SetSelection(start, start if end is None else end)
        sent.clear()
        (panel.output if output else panel.input).SetFocus()

    def submissions(*text):
        assert sent == [{"type": "input", "text": line} for line in text], sent

    def scenarios():
        yield canary, ord("Q"), 0
        assert canary.GetValue() == "q"
        fixture(editable, "old\nlook", 8)
        yield entry, wx.WXK_RETURN, 0
        assert entry.GetValue() == "old\nlook\n"
        submissions("look")
        fixture(editable, "old\nlook", 4, 8)
        yield entry, wx.WXK_RETURN, wx.MOD_SHIFT
        assert entry.GetValue() == "old\nlook" and entry.GetSelection() == (4, 8)
        submissions("look")
        fixture(editable, "old\ndraft", 9)
        for _ in range(3):
            yield entry, wx.WXK_DOWN, 0
        assert entry.GetValue() == "old\ndraft\n"
        submissions()
        fixture(editable, "alpha\nalpha\nalpha", 17)
        yield entry, wx.WXK_UP, 0
        assert entry.GetSelection() == (11, 11)
        yield entry, wx.WXK_DOWN, 0
        assert entry.GetSelection() == (17, 17)
        yield entry, wx.WXK_UP, wx.MOD_SHIFT
        assert entry.GetSelection() == (11, 17)
        yield entry, wx.WXK_DOWN, wx.MOD_SHIFT
        assert entry.GetSelection() == (17, 17)
        submissions()
        fixture(editable, "old\ndraft", 5, 8)
        yield entry, wx.WXK_ESCAPE, 0
        assert entry.GetValue() == "old\n\ndraft"
        submissions()
        fixture(editable, "north\nsouth", 6)
        yield entry, wx.WXK_BACK, 0
        assert entry.GetValue() == "north\nsouth"
        yield entry, wx.WXK_LEFT, wx.MOD_SHIFT
        yield entry, wx.WXK_BACK, 0
        assert entry.GetValue() == "northsouth"
        submissions()
        fixture(editable, "north\nsouth", 5)
        yield entry, wx.WXK_RIGHT, wx.MOD_SHIFT
        yield entry, wx.WXK_DELETE, 0
        assert entry.GetValue() == "northsouth"
        submissions()
        fixture(editable, "old\ndraft", 0, 3, output=True)
        yield editable.output, wx.WXK_TAB, 0
        assert entry.HasFocus() and entry.GetSelection() == (9, 9)
        fixture(editable, "old\ndraft", 0, 3, output=True)
        yield editable.output, ord("X"), 0
        assert entry.GetValue() == "old\ndraftx" and entry.HasFocus()
        submissions()
        fixture(editable, "look dragon\nlook goblin\nlo", 26)
        yield entry, ord("R"), wx.MOD_CONTROL
        assert entry.GetValue().endswith("\nlook goblin")
        yield entry, ord("R"), wx.MOD_CONTROL
        assert entry.GetValue().endswith("\nlook dragon")
        submissions()
        fixture(editable, "old\ndraft", 1, output=True)
        yield editable.output, wx.WXK_RETURN, wx.MOD_CONTROL
        assert entry.HasFocus() and entry.GetSelection() == (9, 9)
        submissions()
        fixture(standard, "look", 4)
        yield command, wx.WXK_RETURN, 0
        assert command.GetValue() == ""
        submissions("look")
        yield command, wx.WXK_UP, 0
        assert command.GetValue() == "look"
        yield command, wx.WXK_DOWN, 0
        assert command.GetValue() == ""
        fixture(standard, "look", 4)
        yield command, wx.WXK_RETURN, wx.MOD_SHIFT
        assert command.GetValue() == "look\n"
        submissions()
        yield command, wx.WXK_RETURN, 0
        assert command.GetValue() == ""
        submissions("look")

    simulator = wx.UIActionSimulator()
    sequence = scenarios()
    pending = None
    awaiting_keyup = None
    deadline = time.monotonic() + 5
    completed = 0
    finished = False
    failures = []

    def poll():
        nonlocal pending, awaiting_keyup, deadline, completed, finished
        try:
            now = time.monotonic()
            if now > deadline:
                raise TimeoutError(f"OS delivery/focus timeout after action {completed}")
            if awaiting_keyup is not None:
                # Wait for actual release and a quiet event stream, including
                # Cocoa modifier-release flagsChanged events after the key-up.
                if keyups <= awaiting_keyup or now - last_event < 0.05:
                    return
                awaiting_keyup = None
                completed += 1
            if pending is None:
                pending = next(sequence)
                deadline = now + 5
            control, key, modifiers = pending
            if not frame.IsActive() or not control.HasFocus():
                return
            awaiting_keyup = keyups
            assert simulator.Char(key, modifiers), "OS simulator rejected the key"
            pending = None
        except StopIteration:
            finished = True
            timer.Stop()
            app.ExitMainLoop()
        except BaseException as error:
            failures.append(error)
            timer.Stop()
            app.ExitMainLoop()

    frame.Show()
    frame.Raise()
    canary.SetFocus()
    timer = wx.PyTimer(poll)
    timer.Start(10)
    try:
        app.MainLoop()
    finally:
        timer.Stop()
        for panel in panels:
            panel._alive = False
        for control in controls:
            control.PopEventHandler(True)
        frame.Destroy()
    if failures:
        if completed == 0 and isinstance(failures[0], TimeoutError):
            print(f"OS input unavailable: plain-control canary received no input: {failures[0]}")
            sys.exit(77)
        raise failures[0]
    assert finished, "Native event loop exited before all OS input scenarios completed"
    print("OS keyboard scenarios passed")
