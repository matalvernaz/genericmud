"""Real native controls; discovery/playback are injected so no test needs sound hardware."""

from __future__ import annotations

import sys
import threading

import pytest

wx = pytest.importorskip("wx")

from genericmud.config import audio_routes  # noqa: E402
from genericmud.packs.user_rules import UserTrigger  # noqa: E402
from genericmud.ui.audio_routes_dialog import AudioRouteEditor, AudioRoutesDialog  # noqa: E402
from genericmud.ui.rule_editors import TriggerEditorDialog  # noqa: E402
from tests.test_audio_routes import HEADPHONES, SPEAKERS  # noqa: E402
from tests.test_automation_dialogs import _shortcut_clashes, _tab_stops  # noqa: E402


@pytest.fixture
def frame(tmp_path, monkeypatch):
    if sys.platform == "darwin":
        pytest.skip("wx needs a framework-build Python on macOS")
    app = wx.GetApp() or wx.App(False)
    parent = wx.Frame(None)
    monkeypatch.setattr(audio_routes, "routes_path", lambda: tmp_path / "audio-routes.json")
    yield parent
    parent.Destroy()
    del app


@pytest.fixture
def dialogs(frame, monkeypatch):
    # Exercise the same callbacks as the worker completion, without a driver or timing wait.
    pending = []
    monkeypatch.setattr(AudioRouteEditor, "_run", lambda self, work, done:
                        pending.append((work, done)))
    manager = AudioRoutesDialog(frame)
    editor = AudioRouteEditor(manager, {}, "alerts", HEADPHONES)
    yield manager, editor, pending
    editor.Destroy()
    manager.Destroy()


def test_route_editor_labels_shortcuts_and_preserves_unavailable_identity(dialogs):
    _manager, editor, _pending = dialogs
    editor._show_outputs([SPEAKERS])
    assert editor._device.GetStringSelection() == "Unavailable: Наушники"
    assert editor.result() == ("alerts", HEADPHONES)
    assert _shortcut_clashes(editor) == {}
    for child, label, before in _tab_stops(editor):
        if isinstance(child, (wx.TextCtrl, wx.Choice)):
            assert before is not None and "&" in label
            assert child.GetName() in ("Route name", "Output device")


def test_device_refresh_restores_stable_identity_after_reordering(dialogs):
    _manager, editor, _pending = dialogs
    editor._show_outputs([SPEAKERS, HEADPHONES])
    assert editor.result()[1] == HEADPHONES
    editor._show_outputs([HEADPHONES, SPEAKERS])
    assert editor.result()[1] == HEADPHONES


def test_selected_output_test_uses_selected_hardware_not_route_name(dialogs):
    _manager, editor, pending = dialogs
    editor._show_outputs([HEADPHONES, SPEAKERS])
    editor._device.SetSelection(1)
    played = []

    class Preview:
        def play(self, output):
            played.append(output)

        def close(self):
            pass

    editor._preview = Preview()
    editor._on_test(None)
    work, done = pending[-1]
    done(work())
    assert played == [SPEAKERS]
    assert editor._status.GetLabel() == "Test sound played."


def test_route_settings_save_and_cancel_do_not_share_mutable_state(frame, monkeypatch):
    audio_routes.save_audio_routes({"alerts": HEADPHONES})
    manager = AudioRoutesDialog(frame)
    try:
        manager._routes["music"] = SPEAKERS
        assert audio_routes.load_audio_routes() == {"alerts": HEADPHONES}
        closed = []
        monkeypatch.setattr(manager, "EndModal", closed.append)
        manager._on_ok(None)
        assert audio_routes.load_audio_routes() == {"alerts": HEADPHONES, "music": SPEAKERS}
        assert closed == [wx.ID_OK]
    finally:
        manager.Destroy()


def test_trigger_editor_default_and_unknown_route_round_trip(frame, tmp_path):
    audio_routes.save_audio_routes({"alerts": HEADPHONES})
    for route in ("", "alerts", "route from another computer"):
        dialog = TriggerEditorDialog(frame, tmp_path, UserTrigger(audio_route=route))
        try:
            assert dialog.result().audio_route == route
            assert dialog._audio_route.GetName() == "Audio output route"
            assert dialog._audio_route.GetString(0) == "System default"
        finally:
            dialog.Destroy()


def test_invalid_name_or_missing_device_keeps_entered_text(dialogs, monkeypatch):
    _manager, editor, _pending = dialogs
    messages, closed = [], []
    monkeypatch.setattr(wx, "MessageBox", lambda text, *_args: messages.append(text))
    monkeypatch.setattr(editor, "EndModal", closed.append)
    editor._name.SetValue("my typed name")
    editor._on_ok(None)
    assert messages == ["Choose an output device first."] and not closed
    assert editor._name.GetValue() == "my typed name"


def test_worker_completion_is_marshaled_to_wx_and_cleanup_is_queued(frame, monkeypatch):
    from genericmud.ui import audio_routes_dialog as module

    ready, closed = threading.Event(), threading.Event()
    callbacks = []
    main_thread = threading.get_ident()
    threads = []
    app = wx.GetApp()
    call_after = wx.CallAfter

    def enumerate_outputs():
        threads.append(threading.get_ident())
        return [HEADPHONES]

    def post(callback, *args):
        callbacks.append((callback, args))
        ready.set()

    class Preview:
        def close(self):
            closed.set()
            call_after(app.ExitMainLoop)

    monkeypatch.setattr(module, "available_outputs", enumerate_outputs)
    monkeypatch.setattr(wx, "CallAfter", post)
    manager = AudioRoutesDialog(frame)
    editor = AudioRouteEditor(manager, {})
    try:
        assert ready.wait(2), "enumeration worker did not finish"
        assert threads and all(thread != main_thread for thread in threads)
        callback, args = callbacks.pop()
        callback(*args)
        assert editor._device.GetStringSelection() == "Наушники (1)"
        editor._preview = Preview()
    finally:
        monkeypatch.setattr(wx, "CallAfter", call_after)
        editor.Destroy()
        manager.Destroy()
    # Destroy is deferred until wx processes idle events. YieldIfNeeded alone does not
    # reliably process pending deletions; run the actual bounded loop used by the app.
    timeout = wx.CallLater(2000, app.ExitMainLoop)
    try:
        app.MainLoop()
    finally:
        timeout.Stop()
    assert closed.is_set(), "closing the dialog did not clean up its preview"
