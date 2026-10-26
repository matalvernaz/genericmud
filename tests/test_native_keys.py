"""Native key routing with wx keycodes and real automation, without opening windows.

Synthetic events do not verify physical keypad translation, NumLock, or screen-reader
interception. These tests need wxPython, but no display or foreground keyboard focus.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.config.ui_prefs import UiPrefs
from genericmud.packs.user_rules import SOURCE, UserKey, UserRules, load_rules, register_rules
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

wx = pytest.importorskip("wx", reason="native key routing requires wxPython")
wx_app = importlib.import_module("genericmud.ui.wx_app")

NUMPAD_CASES = [
    *((getattr(wx, f"WXK_NUMPAD{n}"), f"numpad{n}") for n in range(10)),
    (wx.WXK_NUMPAD_ADD, "numpad+"),
    (wx.WXK_NUMPAD_SUBTRACT, "numpad-"),
    (wx.WXK_NUMPAD_DECIMAL, "numpad."),
    (wx.WXK_NUMPAD_MULTIPLY, "numpad*"),
    (wx.WXK_NUMPAD_DIVIDE, "numpad/"),
]
HANDLERS = ("_on_input_key", "_on_output_key")


@dataclass
class KeyEvent:
    code: int
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    skipped: bool = False

    def GetKeyCode(self):
        return self.code

    def ControlDown(self):
        return self.ctrl

    def AltDown(self):
        return self.alt

    def ShiftDown(self):
        return self.shift

    def Skip(self):
        self.skipped = True


class PanelHarness:
    _on_input_key = wx_app.SessionPanel._on_input_key
    _on_output_key = wx_app.SessionPanel._on_output_key
    _dispatch_bound_combo = wx_app.SessionPanel._dispatch_bound_combo
    _send_command = wx_app.SessionPanel._send_command

    def __init__(self, *, compass: bool, bindings=(), keymap=None):
        self.sent = []
        self._prefs = UiPrefs(numpad_compass=compass)
        self._keymap = keymap or {}
        self._completion = SimpleNamespace(reset=lambda: None)
        self._loop = SimpleNamespace(
            call_soon_threadsafe=lambda callback, *args: callback(*args),
        )
        self.backend = RecordingBackend()
        self.app = EngineApp(
            VoiceRouter(self.backend, clock=lambda: 0.0),
            send=self.sent.append,
            keymap=self._keymap,
        )
        register_rules(
            ScriptApi(self.app.engine, source=SOURCE),
            UserRules(keys=[UserKey(key=key, send=command) for key, command in bindings]),
        )


@pytest.mark.parametrize("code,name", NUMPAD_CASES)
def test_numpad_canonical_names(code, name):
    assert wx_app._key_combo(KeyEvent(code)) == name
    assert wx_app._key_combo(KeyEvent(code), allow_numpad=False) is None


@pytest.mark.parametrize("digit,command", [
    (0, "look"), (1, "sw"), (2, "s"), (3, "se"), (4, "w"),
    (5, "look"), (6, "e"), (7, "nw"), (8, "n"), (9, "ne"),
    ("_DECIMAL", "scan"), ("_SUBTRACT", "u"), ("_ADD", "d"),
])
def test_compass_on_keeps_mapping_despite_conflicting_hotkey(digit, command):
    code = getattr(wx, f"WXK_NUMPAD{digit}")
    name = next(name for key, name in NUMPAD_CASES if key == code)
    panel = PanelHarness(compass=True, bindings=[(name, "conflicting macro")])
    event = KeyEvent(code)

    panel._on_input_key(event)

    assert panel.sent == [command]
    assert not event.skipped


@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize("code,name", NUMPAD_CASES)
def test_compass_off_dispatches_only_bound_numpad_keys(handler, code, name):
    panel = PanelHarness(compass=False)
    unbound = KeyEvent(code)
    getattr(panel, handler)(unbound)
    assert unbound.skipped
    assert panel.sent == []

    register_rules(
        ScriptApi(panel.app.engine, source=SOURCE),
        UserRules(keys=[UserKey(key=name, send="look\nscore")]),
    )
    bound = KeyEvent(code)
    getattr(panel, handler)(bound)
    assert not bound.skipped
    assert panel.sent == ["look", "score"]


@pytest.mark.parametrize("code,name", NUMPAD_CASES)
def test_output_with_compass_on_does_not_dispatch_numpad_hotkeys(code, name):
    panel = PanelHarness(compass=True, bindings=[(name, "conflicting macro")])
    event = KeyEvent(code)
    panel._on_output_key(event)
    assert event.skipped
    assert panel.sent == []


@pytest.mark.parametrize("handler", HANDLERS)
def test_compass_toggle_takes_effect_immediately(handler):
    panel = PanelHarness(compass=True, bindings=[("numpad5", "custom command")])
    for compass in (True, False, True):
        panel._prefs.numpad_compass = compass
        getattr(panel, handler)(KeyEvent(wx.WXK_NUMPAD5))
    expected = ["look", "custom command", "look"] if handler == "_on_input_key" else [
        "custom command",
    ]
    assert panel.sent == expected


@pytest.mark.parametrize("code,name", NUMPAD_CASES)
def test_key_capture_names_numpad_without_consulting_compass(code, name):
    values = []
    wx_app._KeyCaptureCtrl._on_key(SimpleNamespace(SetValue=values.append), KeyEvent(code))
    assert values == [name]


@pytest.mark.parametrize("code,modifiers", [
    (wx.WXK_TAB, {}), (wx.WXK_TAB, {"shift": True}), (wx.WXK_ESCAPE, {}),
])
def test_key_capture_preserves_dialog_navigation(code, modifiers):
    values = []
    event = KeyEvent(code, **modifiers)
    wx_app._KeyCaptureCtrl._on_key(SimpleNamespace(SetValue=values.append), event)
    assert event.skipped
    assert values == []


@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize("compass", [False, True])
@pytest.mark.parametrize("modifiers,prefix", [
    ({"ctrl": True}, "ctrl+"), ({"alt": True}, "alt+"),
    ({"ctrl": True, "alt": True, "shift": True}, "ctrl+alt+shift+"),
    ({"shift": True}, None),
])
def test_modified_numpad_matches_capture_and_dispatch_conventions(
    handler, compass, modifiers, prefix,
):
    combo = f"{prefix}numpad+" if prefix else "shift+numpad+"
    panel = PanelHarness(compass=compass, bindings=[(combo, "custom command")])
    event = KeyEvent(wx.WXK_NUMPAD_ADD, **modifiers)
    captured = []
    wx_app._KeyCaptureCtrl._on_key(SimpleNamespace(SetValue=captured.append), event)
    assert captured == ([combo] if prefix else [])

    getattr(panel, handler)(event)
    consumed = prefix is not None and not compass
    assert event.skipped is not consumed
    assert panel.sent == (["custom command"] if consumed else [])


@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize("compass", [False, True])
@pytest.mark.parametrize("code,modifiers,binding,consumed", [
    (ord("A"), {}, "a", False), (ord("5"), {}, "5", False),
    (ord("+"), {}, "+", False), (ord("A"), {"shift": True}, "shift+a", False),
    (wx.WXK_TAB, {}, "tab", False),
    (wx.WXK_TAB, {"shift": True}, "shift+tab", False),
    (wx.WXK_LEFT, {"shift": True}, "shift+left", False),
    (wx.WXK_F12, {}, "f12", True), (ord("H"), {"ctrl": True}, "ctrl+h", True),
    (ord("C"), {"ctrl": True}, "ctrl+c", False),
    (ord("V"), {"ctrl": True}, "ctrl+v", False),
    (wx.WXK_LEFT, {"ctrl": True}, "ctrl+left", False),
    (wx.WXK_F4, {"alt": True}, "alt+f4", False),
    (ord("V"), {"alt": True}, "alt+v", False),
])
def test_typing_and_existing_hotkey_passthrough(
    handler, compass, code, modifiers, binding, consumed,
):
    panel = PanelHarness(compass=compass, bindings=[(binding, "custom command")])
    event = KeyEvent(code, **modifiers)
    getattr(panel, handler)(event)
    assert panel.sent == (["custom command"] if consumed else [])
    assert event.skipped is not consumed


@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize("compass", [False, True])
def test_speech_stop_key_still_reaches_the_keymap(handler, compass):
    panel = PanelHarness(compass=compass, keymap=load_keymap("vipmud"))
    event = KeyEvent(wx.WXK_F11)
    getattr(panel, handler)(event)
    assert panel.backend.stops == 1
    assert not event.skipped
    assert panel.sent == []


@pytest.mark.parametrize("action", ["new", "edit", "duplicate", "toggle"])
@pytest.mark.parametrize("combo,compass,enabled,hint", [
    ("numpad5", True, True, True),
    ("ctrl+numpad+", True, True, True),
    ("alt+shift+numpad/", True, True, True),
    ("numpad*", True, True, True),
    ("numpad5", False, True, False),
    ("numpad5", True, False, False),
    ("f2", True, True, False),
    ("shift+numpad5", True, True, False),
])
def test_saved_hotkey_explains_compass_only_when_it_blocks_use(
    tmp_path, action, combo, compass, enabled, hint,
):
    # Use the real manager handlers, validation and persistence without opening a dialog.
    manager_type = wx_app.AutomationManagerDialog
    rule = UserKey(key=combo, send="look", enabled=enabled)
    original = replace(rule, enabled=not enabled) if action == "toggle" else rule
    spoken = []
    manager = SimpleNamespace(
        _panel=SimpleNamespace(app=None, _prefs=UiPrefs(numpad_compass=compass)),
        _rules=UserRules(keys=[] if action == "new" else [original]),
        _pack_dir=tmp_path,
        _KIND_WORD=manager_type._KIND_WORD,
        _RULE_TYPES=manager_type._RULE_TYPES,
        _kind=lambda: "key",
        _selected_index=lambda: 0,
        _edit_rule=lambda _kind, _rule: rule,
        _refresh=lambda _select: None,
        _announce=spoken.append,
    )
    manager._collection = lambda _kind: manager._rules.keys
    manager._save_and_reload_rules = lambda select: manager_type._save_and_reload_rules(
        manager, select,
    )
    manager._announce_rule_change = lambda kind, saved, verb: manager_type._announce_rule_change(
        manager, kind, saved, verb,
    )

    getattr(manager_type, f"_on_{action}")(manager, None)

    assert load_rules(tmp_path).keys[-1] == rule
    verb = {
        "new": "added", "edit": "updated", "duplicate": "duplicated",
        "toggle": "enabled" if enabled else "disabled",
    }[action]
    expected = f"Hotkey {verb}."
    if hint:
        expected += " Turn off Numpad compass walking under View to use it."
    assert spoken == [expected]
