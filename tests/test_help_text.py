"""The embedded Help-menu pages stay present and cover the advertised keys."""

from __future__ import annotations

import tomllib
from pathlib import Path

from genericmud import help_text

KEYMAP = Path("genericmud/config/keymaps/vipmud.toml")


def test_pages_are_substantial():
    assert len(help_text.GETTING_STARTED) > 500
    assert len(help_text.KEYBOARD_SHORTCUTS) > 500


def test_shortcuts_page_mentions_every_keymap_namespace_key():
    # Every action family bound in the default keymap has a spoken-name anchor in
    # the shortcuts page, so a rebind/feature landing without help text fails here.
    keys = tomllib.loads(KEYMAP.read_text(encoding="utf-8"))["keys"]
    anchors = {
        "recall:": "Recall the last nine",
        "review:": "Review line by line",
        "chan:": "channel",
        "nav:": "breadcrumb",
        "voice:follow_mode": "Follow mode",
        "voice:interrupt_mode": "Interrupt mode",
        "input:autoretype": "autoretype",
        "log:toggle": "Log this session",
        "diag:where": "diagnostic",
    }
    bound = set(keys.values())
    for prefix, anchor in anchors.items():
        if any(action.startswith(prefix) for action in bound):
            assert anchor in help_text.KEYBOARD_SHORTCUTS, f"help text lost: {anchor}"


def test_menu_access_keys_documented():
    for chunk in ("Alt+F File", "Alt+H Help"):
        assert chunk in help_text.KEYBOARD_SHORTCUTS
