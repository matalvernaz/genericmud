"""Read and write the system clipboard, from the window's thread."""

from __future__ import annotations

import wx


def _clipboard_text() -> str:  # UI thread
    """The clipboard's text, or "" when it holds none or can't be opened."""
    clipboard = wx.TheClipboard
    if not clipboard.Open():
        return ""
    try:
        data = wx.TextDataObject()
        return data.GetText() if clipboard.GetData(data) else ""
    finally:
        clipboard.Close()


def _set_clipboard_text(text: str) -> bool:  # UI thread
    """Put ``text`` on the clipboard; False when another program is holding it open."""
    clipboard = wx.TheClipboard
    if not clipboard.Open():
        return False
    try:
        clipboard.SetData(wx.TextDataObject(text))
        clipboard.Flush()  # Windows keeps it after genericMud closes
    finally:
        clipboard.Close()
    return True
