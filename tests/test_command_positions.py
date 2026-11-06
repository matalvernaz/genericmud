"""Editable command offsets must land after whole native CRLF and UTF-16 characters."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("wx")
command_buffer = importlib.import_module("genericmud.ui.command_buffer")


@pytest.mark.parametrize("value", ["", "\n", "north\n", "north\nsouth\n", "🐉\né\n界"])
def test_windows_positions_do_not_split_native_newlines(monkeypatch, value):
    # MSW EDIT positions count UTF-16 units and both CRLF characters. GetRange
    # normalizes CR/CRLF afterward, so a range ending halfway through CRLF already
    # looks like a complete LF. Both positions match, but only the later one is valid.
    native = value.replace("\n", "\r\n").encode("utf-16-le")

    def native_range(start, end):
        text = native[2 * start:2 * end].decode("utf-16-le", errors="surrogatepass")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    control = SimpleNamespace(
        GetValue=lambda: value,
        GetLastPosition=lambda: len(native) // 2,
        GetRange=native_range,
        text_range=native_range,
    )
    monkeypatch.setattr(command_buffer, "sys", SimpleNamespace(platform="win32"))

    for offset in range(len(value) + 1):
        position = command_buffer.CommandEntryCtrl.position_from_offset(control, offset)
        expected = len(value[:offset].replace("\n", "\r\n").encode("utf-16-le")) // 2
        assert position == expected, (value, offset, position, expected)
