from __future__ import annotations

import threading

import wx

from genericmud.update import self_update

_ID_UPDATE_NOW = wx.ID_HIGHEST + 101
_ID_RELEASE_PAGE = wx.ID_HIGHEST + 102
_ID_SNOOZE = wx.ID_HIGHEST + 103
_ID_SKIP = wx.ID_HIGHEST + 104


class UpdateNotificationDialog(wx.Dialog):
    """Announce a newer genericMud and offer what to do about it.

    Follows WorldDialog's accessibility pattern: a StaticText precedes each control and
    every control gets SetName so NVDA reads it. Release notes sit in a focusable read-only
    text box the user can review line by line. ShowModal returns one of the module ``_ID_*``
    actions, or wx.ID_CANCEL if the dialog is closed. "Update Now" only appears on a build
    that can self-replace; elsewhere the release page is the only install route.
    """

    def __init__(self, parent: wx.Window, info: dict, current: str) -> None:
        super().__init__(parent, title="genericMud update available")
        heading = f"genericMud {info['tag']} is available. You have {current}."
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=heading), 0, wx.ALL, 8)

        sizer.Add(wx.StaticText(self, label="Release &notes:"), 0, wx.LEFT, 8)
        notes = wx.TextCtrl(
            self, value=info.get("notes") or "(no release notes)",
            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(460, 180),
        )
        notes.SetName("Release notes")
        sizer.Add(notes, 1, wx.EXPAND | wx.ALL, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        default_button = None
        if self_update.can_self_replace():
            default_button = self._button(buttons, "&Update Now", _ID_UPDATE_NOW)
        page_button = self._button(buttons, "Open Release &Page", _ID_RELEASE_PAGE)
        self._button(buttons, "&Remind Me Later", _ID_SNOOZE)
        self._button(buttons, "&Skip This Version", _ID_SKIP)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "&Close"), 0)
        sizer.Add(buttons, 0, wx.ALL, 8)

        (default_button or page_button).SetDefault()
        self.SetSizerAndFit(sizer)

    def _button(self, sizer: wx.BoxSizer, label: str, action_id: int) -> wx.Button:
        button = wx.Button(self, action_id, label)
        button.Bind(wx.EVT_BUTTON, lambda _event, a=action_id: self.EndModal(a))
        sizer.Add(button, 0, wx.RIGHT, 4)
        return button


class UpdateProgressDialog(wx.Dialog):
    """Self-update progress: a status log, a gauge, spoken phase changes, and Cancel.

    Division of labour between the two channels, which is easy to get wrong in both
    directions: the gauge carries *how far along*, speech carries *which phase*.

    Don't speak percentages. A wx.Gauge is a native msctls_progress32 reporting
    ROLE_SYSTEM_PROGRESSBAR, so NVDA already tracks it -- beeping at every 1% by
    default, or speaking every 10% if the user chose speech instead. Announcing our
    own milestones on top of that is both redundant and coarser than what the user
    asked for, and it talks over a deliberate "progress bar output: off".

    Do speak phase changes. _status_log is an unfocused read-only TextCtrl, and wx has
    no live-region equivalent on MSW, so appended text reaches no screen reader. Speech
    is the only channel for "download finished, now extracting" -- which no
    accessibility API infers from a gauge sitting at 100%.

    Deliberately a plain owned dialog, NOT wx.ProgressDialog: on MSW that runs a native
    task dialog on its own thread and PD_AUTO_HIDE dismisses it the instant Update()
    reaches the maximum. Tearing that window down while the screen reader still has COM
    calls in flight against it faulted the whole process (RPC_E_SERVER_DIED_DNE /
    RPC_E_DISCONNECTED, then an access violation) right as extraction began. Keeping the
    dialog on the main thread and destroying it only from _on_update_finished removes the
    race entirely; VaultBrowserDialog survives far larger downloads with this same shape.

    Cancel sets the shared event; the download worker notices at its next progress
    callback. After the last callback (download done, extraction running) cancellation no
    longer takes effect -- the old dialog had the same window, now stated instead of implied.
    """

    def __init__(self, parent: wx.Window, tag: str, announce, cancelled: threading.Event) -> None:
        super().__init__(parent, title="Updating genericMud")
        self._announce = announce
        self._cancelled = cancelled
        self._setup_announced = False  # one-time "setting up" line after the last callback

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label="S&tatus:"), 0, wx.LEFT | wx.TOP, 8)
        self._status_log = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(460, 90)
        )
        self._status_log.SetName("Status")
        sizer.Add(self._status_log, 1, wx.EXPAND | wx.ALL, 8)

        self._gauge = wx.Gauge(self, range=100)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.ALL, 8)

        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, "&Cancel")
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        sizer.Add(self._cancel_btn, 0, wx.ALL, 8)
        self.Bind(wx.EVT_CLOSE, self._on_cancel)
        self.SetSizerAndFit(sizer)

        self._status(f"Downloading genericMud {tag}.")

    def _status(self, message: str) -> None:  # main thread only
        self._status_log.AppendText(message + "\n")
        self._announce(message)

    def pump(self, done: int, total: int) -> None:  # main thread, via wx.CallAfter
        """Reflect download progress; total may be 0 when no size was advertised."""
        if total <= 0:
            self._gauge.Pulse()
            return
        pct = min(done * 100 // total, 100)
        self._gauge.SetValue(pct)  # NVDA reports this itself; see the class docstring
        if pct >= 100 and not self._setup_announced:
            self._setup_announced = True
            self._status("Download finished. Setting up the update.")

    def _on_cancel(self, event) -> None:
        if isinstance(event, wx.CloseEvent) and event.CanVeto():
            event.Veto()  # the frame destroys us once the worker unwinds; don't die early
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        self._cancel_btn.Disable()
        self._status("Cancelling. This takes effect at the next download step.")
