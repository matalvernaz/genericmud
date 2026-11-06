"""How a key press becomes a keymap name such as "ctrl+1", "alt+up" or "f11".

Also the Windows AltGr check, the keys the window keeps for itself (menus, clipboard and word
moves), and why a hotkey can't have one of those keys.
"""

from __future__ import annotations

import sys

import wx

_NAMED_KEYS = {
    wx.WXK_UP: "up",
    wx.WXK_DOWN: "down",
    wx.WXK_LEFT: "left",
    wx.WXK_RIGHT: "right",
    wx.WXK_HOME: "home",
    wx.WXK_END: "end",
    wx.WXK_ESCAPE: "escape",
    # Named so pack accelerators can bind them (Erion's history browser uses
    # alt+pageup/pagedown, alt+space, alt+shift+delete, alt+enter...). Only modified
    # combos reach the keymap -- plain enter/space/tab stay ordinary typing below.
    wx.WXK_PAGEUP: "pageup",
    wx.WXK_PAGEDOWN: "pagedown",
    wx.WXK_DELETE: "delete",
    wx.WXK_INSERT: "insert",
    wx.WXK_RETURN: "enter",
    wx.WXK_TAB: "tab",
    wx.WXK_SPACE: "space",
}


_NUMPAD_KEYS = {
    wx.WXK_NUMPAD0: "numpad0", wx.WXK_NUMPAD1: "numpad1", wx.WXK_NUMPAD2: "numpad2",
    wx.WXK_NUMPAD3: "numpad3", wx.WXK_NUMPAD4: "numpad4", wx.WXK_NUMPAD5: "numpad5",
    wx.WXK_NUMPAD6: "numpad6", wx.WXK_NUMPAD7: "numpad7", wx.WXK_NUMPAD8: "numpad8",
    wx.WXK_NUMPAD9: "numpad9", wx.WXK_NUMPAD_ADD: "numpad+",
    wx.WXK_NUMPAD_SUBTRACT: "numpad-", wx.WXK_NUMPAD_DECIMAL: "numpad.",
    wx.WXK_NUMPAD_MULTIPLY: "numpad*", wx.WXK_NUMPAD_DIVIDE: "numpad/",
}


# Windows virtual-key codes and the key-state flag ToUnicodeEx reads, for the AltGr check.
_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_LCONTROL = 0xA2
_VK_RMENU = 0xA5
_KEY_IS_DOWN = 0x80
# Work the character out without disturbing a dead key the player is part-way through.
_TO_UNICODE_KEEP_STATE = 0x4


def _ctrl_alt_types(
    virtual_key: int, scan_code: int, *, shift: bool, layout: int | None = None
) -> bool:
    """Whether a Windows keyboard layout types a character for this key with Ctrl+Alt held.

    Windows reports AltGr as Ctrl+Alt, so this is the AltGr question, asked of the layout the
    way a browser decides AltGraph. ``layout`` defaults to the one this thread is typing with.
    Windows only; raises if the layout can't be asked.
    """
    import ctypes

    # A private handle on user32, so the argument types set here can't affect anyone else.
    user32 = ctypes.WinDLL("user32")
    user32.GetKeyboardLayout.argtypes = [ctypes.c_uint32]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.ToUnicodeEx.argtypes = [
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    user32.ToUnicodeEx.restype = ctypes.c_int
    key_state = (ctypes.c_ubyte * 256)()
    for held in (_VK_CONTROL, _VK_MENU, _VK_LCONTROL, _VK_RMENU):
        key_state[held] = _KEY_IS_DOWN
    if shift:
        key_state[_VK_SHIFT] = _KEY_IS_DOWN
    typed = ctypes.create_unicode_buffer(8)
    if layout is None:
        layout = user32.GetKeyboardLayout(0)
    count = user32.ToUnicodeEx(
        virtual_key, scan_code, key_state, typed, len(typed), _TO_UNICODE_KEEP_STATE, layout
    )
    if count < 0:
        return True  # a dead key: an accent waiting for the letter it goes on
    # Ctrl can also make control characters (Ctrl+[ is Escape), and those aren't typing.
    return count > 0 and typed.value[:1].isprintable()


def _altgr_types_a_character(event: wx.KeyEvent) -> bool:
    """Whether a Ctrl+Alt key press is really AltGr typing a character, on Windows.

    On a German, French, Nordic or Polish keyboard Ctrl+Alt+0 is how } or @ gets typed, and a
    Ctrl+Alt binding on that key swallowed it. Off Windows, or if the layout can't be asked,
    the key stays a shortcut as it always was.
    """
    if sys.platform != "win32":
        return False
    try:
        scan_code = (event.GetRawKeyFlags() >> 16) & 0xFF
        return _ctrl_alt_types(event.GetRawKeyCode(), scan_code, shift=event.ShiftDown())
    except Exception:  # noqa: BLE001 - a layout that can't be asked leaves the shortcut working
        return False


def _key_combo(event: wx.KeyEvent, *, allow_numpad: bool = True) -> str | None:
    """Build a keymap combo ("ctrl+1", "alt+up", "f11") or None for plain typing."""
    code = event.GetKeyCode()
    mods = []
    if event.ControlDown():
        mods.append("ctrl")
    if event.AltDown():
        mods.append("alt")
    if event.ShiftDown():
        mods.append("shift")

    if code in _NUMPAD_KEYS:
        if not allow_numpad:
            return None  # compass owns these keys; key capture can still name them
        name = _NUMPAD_KEYS[code]
    elif wx.WXK_F1 <= code <= wx.WXK_F24:
        name = f"f{code - wx.WXK_F1 + 1}"
    elif code in _NAMED_KEYS:
        name = _NAMED_KEYS[code]
    elif 33 <= code < 127:
        name = chr(code).lower()
        if "ctrl" in mods and "alt" in mods and _altgr_types_a_character(event):
            return None  # AltGr typing a character, which Windows reports as Ctrl+Alt
    else:
        return None

    is_fkey = name.startswith("f") and name[1:].isdigit()
    is_special = is_fkey or name == "escape" or code in _NUMPAD_KEYS
    if not mods and not is_special:
        return None  # ordinary typing
    if mods == ["shift"] and not is_fkey:
        # Shift+<key> is typing and editing, not a macro chord: capitals, punctuation,
        # shift+arrow/home/end selection, shift+insert paste, shift+delete cut, shift+tab.
        # Consuming these (as combos) made every shifted character vanish from the input
        # box. Shift+F-keys stay combos -- packs bind them (Erion: shift+f1 -> full hp).
        return None
    return "+".join(mods + [name])


# Window/OS commands the input box must NOT swallow -- they have to reach the platform's
# default handler (Alt+F4 -> WM_CLOSE -> our EVT_CLOSE), or the window can't be closed.
# Combos the input box must keep for native editing/system behaviour rather than
# treat as macros: window close, clipboard, undo/redo, select-all, and word-wise
# caret movement/selection -- these matter doubly under a screen reader.
_PASSTHROUGH_COMBOS = frozenset({
    "alt+f4",
    # Top-level menu mnemonics remain reachable even if a user script tries to bind them.
    "alt+f", "alt+p", "alt+a", "alt+v", "alt+h",
    "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+a", "ctrl+z", "ctrl+y",
    "ctrl+left", "ctrl+right", "ctrl+home", "ctrl+end", "ctrl+delete",
    "ctrl+shift+left", "ctrl+shift+right", "ctrl+shift+home", "ctrl+shift+end",
})


_COMPLETE_FORWARD = "ctrl+space"  # cycle a completion of the current input word
_COMPLETE_BACKWARD = "ctrl+shift+space"
# What each keymap namespace is for, so a refused hotkey can say what has the key.
_KEYMAP_AREAS = {
    "recall": "reading back recent messages",
    "review": "reviewing the output",
    "voice": "speech",
    "sound": "stopping sounds",
    "input": "auto-repeat",
    "chan": "browsing channels",
    "nav": "walking and breadcrumbs",
    "map": "the map",
    "log": "logging",
}


def _hotkey_taken(combo: str, keymap: dict[str, str]) -> str | None:
    """Why a hotkey on ``combo`` would never run, or None when the key is free.

    genericMud's own keymap answers a key before any hotkey does, the menu and editing
    keys in _PASSTHROUGH_COMBOS never reach the engine, and the command box keeps the
    completion keys, so a hotkey saved on one of them sat in the list doing nothing.
    """
    combo = combo.strip().lower()
    name = "+".join(part.capitalize() for part in combo.split("+"))
    if combo in keymap:
        area = _KEYMAP_AREAS.get(keymap[combo].partition(":")[0], "one of its own commands")
        reason = f"{name} is already genericMud's key for {area}"
    elif combo in _PASSTHROUGH_COMBOS:
        reason = f"{name} is kept for the menus and text editing"
    elif combo in (_COMPLETE_FORWARD, _COMPLETE_BACKWARD):
        reason = f"{name} completes words in the command box"
    else:
        return None
    return f"{reason}, so a hotkey on it would never run. Choose another key."
