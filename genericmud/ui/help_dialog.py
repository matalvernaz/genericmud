"""The help window: one page of plain text, read line by line, and a Close button."""

from __future__ import annotations

import wx


class HelpDialog(wx.Dialog):
    """One help page: a read-only text box NVDA reads line by line, plus Close.

    The same accessibility shape as the release-notes dialog: label precedes the
    control, SetName gives it a spoken name, and focus starts at the TOP of the
    text (a fresh TextCtrl caret sits at the end, which reads as an empty line).
    """

    def __init__(self, parent: wx.Window, title: str, text: str) -> None:
        super().__init__(
            parent, title=title, size=(640, 480),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=f"&{title}:"), 0, wx.LEFT | wx.TOP, 8)
        body = wx.TextCtrl(
            self, value=text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        body.SetName(title)
        body.SetInsertionPoint(0)
        sizer.Add(body, 1, wx.EXPAND | wx.ALL, 8)
        close = wx.Button(self, wx.ID_CANCEL, "Cl&ose")
        close.SetDefault()
        sizer.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(sizer)
        body.SetFocus()
