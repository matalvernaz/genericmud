"""CRLF positions can alias in native GetRange without being valid caret boundaries."""

from types import SimpleNamespace

import pytest

pytest.importorskip("wx")
from genericmud.ui import command_entry


class WindowsRanges:
    """The Windows trace: both a trailing CR and CRLF read back as one LF."""

    text_range = command_entry.CommandEntryCtrl.text_range
    position_from_offset = command_entry.CommandEntryCtrl.position_from_offset

    def __init__(self, value):
        self.value = value
        self.raw = value.replace("\n", "\r\n").encode("utf-16-le")

    def GetValue(self):
        return self.value

    def GetLastPosition(self):
        return len(self.raw) // 2

    def GetRange(self, start, end):
        value = self.raw[2 * start:2 * end].decode("utf-16-le", errors="replace")
        return value.replace("\r\n", "\n").replace("\r", "\n")


@pytest.mark.parametrize("value", ["north\n", "north\nsouth\n", "a\n\nb", "🐉\ne\u0301界\n"])
def test_caret_mapping_uses_complete_native_character_boundaries(monkeypatch, value):
    monkeypatch.setattr(command_entry, "sys", SimpleNamespace(platform="win32"))
    control = WindowsRanges(value)
    for offset in range(len(value) + 1):
        position = control.position_from_offset(offset)
        expected = len(value[:offset].replace("\n", "\r\n").encode("utf-16-le")) // 2
        assert position == expected, (value, offset, position, expected)
        assert control.text_range(0, position) == value[:offset]
