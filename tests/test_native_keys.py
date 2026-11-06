"""Native key routing with wx keycodes and real automation, without opening windows.

Synthetic events do not verify physical keypad translation, NumLock, or screen-reader
interception. These tests need wxPython, but no display or foreground keyboard focus.
"""

from __future__ import annotations

import importlib
import sys
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
automation_manager = importlib.import_module("genericmud.ui.automation_manager")
keyboard = importlib.import_module("genericmud.ui.keyboard")
rule_editors = importlib.import_module("genericmud.ui.rule_editors")
session_panel = importlib.import_module("genericmud.ui.session_panel")

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
    _on_input_key = session_panel.SessionPanel._on_input_key
    _on_output_key = session_panel.SessionPanel._on_output_key
    _dispatch_bound_combo = session_panel.SessionPanel._dispatch_bound_combo
    _send_command = session_panel.SessionPanel._send_command

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
    assert keyboard._key_combo(KeyEvent(code)) == name
    assert keyboard._key_combo(KeyEvent(code), allow_numpad=False) is None


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
    rule_editors._KeyCaptureCtrl._on_key(SimpleNamespace(SetValue=values.append), KeyEvent(code))
    assert values == [name]


@pytest.mark.parametrize("code,modifiers", [
    (wx.WXK_TAB, {}), (wx.WXK_TAB, {"shift": True}), (wx.WXK_ESCAPE, {}),
])
def test_key_capture_preserves_dialog_navigation(code, modifiers):
    values = []
    event = KeyEvent(code, **modifiers)
    rule_editors._KeyCaptureCtrl._on_key(SimpleNamespace(SetValue=values.append), event)
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
    rule_editors._KeyCaptureCtrl._on_key(SimpleNamespace(SetValue=captured.append), event)
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
    manager_type = automation_manager.AutomationManagerDialog
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


# --- AltGr ---------------------------------------------------------------------


def test_altgr_typing_a_character_is_typing_not_a_ctrl_alt_combo(monkeypatch):
    # Windows reports AltGr as Ctrl+Alt: on a French keyboard AltGr+0 is @, on a German one
    # it is }. When the layout says the key types something, it must reach the input box.
    monkeypatch.setattr(keyboard, "_altgr_types_a_character", lambda _event: True)
    assert keyboard._key_combo(KeyEvent(ord("0"), ctrl=True, alt=True)) is None


def test_ctrl_alt_on_a_key_that_types_nothing_stays_a_combo(monkeypatch):
    monkeypatch.setattr(keyboard, "_altgr_types_a_character", lambda _event: False)
    assert keyboard._key_combo(KeyEvent(ord("0"), ctrl=True, alt=True)) == "ctrl+alt+0"


def test_altgr_is_only_asked_about_with_both_ctrl_and_alt(monkeypatch):
    asked = []
    monkeypatch.setattr(keyboard, "_altgr_types_a_character", lambda event: asked.append(event))
    assert keyboard._key_combo(KeyEvent(ord("1"), ctrl=True)) == "ctrl+1"
    assert keyboard._key_combo(KeyEvent(ord("1"), alt=True)) == "alt+1"
    assert asked == []


def test_altgr_typing_reaches_the_input_box_past_the_channel_keys(monkeypatch):
    # The shipped keymap binds Ctrl+Alt+0 to the tenth channel message. AltGr+0 must still type.
    monkeypatch.setattr(keyboard, "_altgr_types_a_character", lambda _event: True)
    panel = PanelHarness(compass=True, keymap=load_keymap("vipmud"))
    event = KeyEvent(ord("0"), ctrl=True, alt=True)
    panel._on_input_key(event)
    assert event.skipped is True
    assert panel.sent == []


def test_off_windows_the_layout_is_never_asked(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert keyboard._altgr_types_a_character(KeyEvent(ord("0"), ctrl=True, alt=True)) is False


def test_a_layout_that_cannot_be_asked_leaves_the_shortcut_working(monkeypatch):
    # Pretend to be on Windows: the fake event has no raw key code, so asking fails, and the
    # key must stay the shortcut it always was rather than vanish.
    monkeypatch.setattr(sys, "platform", "win32")
    assert keyboard._altgr_types_a_character(KeyEvent(ord("0"), ctrl=True, alt=True)) is False


@pytest.mark.skipif(sys.platform != "win32", reason="asks real Windows keyboard layouts")
def test_real_windows_layouts_answer_the_altgr_question():
    # The Windows build runs this on a real machine: load the layouts and ask them, so the
    # ctypes call itself is proven, not just the decision built on it.
    #
    # Loading a layout is not the harmless thing it looks like: it puts it in the session's
    # layout list, where Windows' own Left Alt+Shift can then switch to it, and nothing on
    # screen says where it came from. So whatever this test adds, it takes away again -- and
    # only what it added, since a layout the developer already had is not ours to unload.
    import ctypes

    user32 = ctypes.WinDLL("user32")
    user32.LoadKeyboardLayoutW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    user32.LoadKeyboardLayoutW.restype = ctypes.c_void_p
    user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    user32.GetKeyboardLayoutList.restype = ctypes.c_int
    user32.UnloadKeyboardLayout.argtypes = [ctypes.c_void_p]
    user32.UnloadKeyboardLayout.restype = ctypes.c_int

    already = _loaded_layouts(user32)
    loaded: dict[str, int] = {}
    try:
        for layout_id in ("00000407", "0000040C", "00000409"):
            loaded[layout_id] = user32.LoadKeyboardLayoutW(layout_id, 0)
        german = loaded["00000407"]
        french = loaded["0000040C"]
        english = loaded["00000409"]
        zero_key, zero_scan = 0x30, 0x0B
        e_key, e_scan = 0x45, 0x12
        # AltGr+0 types } on a German layout and @ on a French one.
        assert keyboard._ctrl_alt_types(zero_key, zero_scan, shift=False, layout=german) is True
        assert keyboard._ctrl_alt_types(zero_key, zero_scan, shift=False, layout=french) is True
        assert keyboard._ctrl_alt_types(e_key, e_scan, shift=False, layout=german) is True  # euro
        # US English has no AltGr: Ctrl+Alt+0 and Ctrl+Alt+E type nothing, so they stay
        # shortcuts.
        assert keyboard._ctrl_alt_types(zero_key, zero_scan, shift=False, layout=english) is False
        assert keyboard._ctrl_alt_types(e_key, e_scan, shift=False, layout=english) is False
    finally:
        _unload_layouts(user32, loaded, already)


def _loaded_layouts(user32) -> set[int]:
    """The layouts this session already has, by identifier (the low half of each handle)."""
    import ctypes

    count = user32.GetKeyboardLayoutList(0, None)
    if count <= 0:
        return set()
    handles = (ctypes.c_void_p * count)()
    user32.GetKeyboardLayoutList(count, handles)
    return {int(handle) & 0xFFFF for handle in handles if handle}


def _layouts_to_unload(loaded: dict[str, int], already: set[int]) -> list[str]:
    """Which of the layouts a test loaded are its own to remove.

    A layout that was loaded before the test ran belongs to whoever put it there -- the
    developer's own German keyboard, say -- so it is left exactly as it was found.
    """
    return [layout_id for layout_id in loaded if int(layout_id, 16) not in already]


def _unload_layouts(user32, loaded: dict[str, int], already: set[int]) -> None:
    """Put the session back as it was found: remove only the layouts this test added.

    Unloading is best-effort: Windows refuses to unload a layout that is in use, and a test is
    not the place to insist. Nothing here may fail the run.
    """
    for layout_id in _layouts_to_unload(loaded, already):
        handle = loaded.get(layout_id)
        if handle:
            user32.UnloadKeyboardLayout(handle)

def test_only_the_layouts_a_test_added_are_its_to_remove():
    # The rule that matters: a layout that was already loaded belongs to whoever put it there.
    loaded = {"00000407": 0x407, "0000040C": 0x40C, "00000409": 0x409}
    assert _layouts_to_unload(loaded, {0x409}) == ["00000407", "0000040C"]
    assert _layouts_to_unload(loaded, {0x407, 0x40C, 0x409}) == []
    assert _layouts_to_unload({}, {0x409}) == []


def test_a_layout_identifier_is_read_as_the_hex_handle_it_is():
    # 0x0409 is 1033 in decimal; comparing the two as plain numbers would unload nothing (or
    # everything), so the identifier is parsed as what it is.
    assert _layouts_to_unload({"00000409": 0x409}, {1033}) == []
    assert _layouts_to_unload({"00000409": 0x409}, {0x409}) == []
    assert _layouts_to_unload({"00000409": 0x409}, {0x407}) == ["00000409"]
