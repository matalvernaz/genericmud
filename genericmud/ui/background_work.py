"""Run slow work off the window's thread and hand its result back, so the window never freezes."""

from __future__ import annotations

import threading

import wx


def _run_async(work, on_done) -> None:
    """Run ``work()`` on a daemon thread; deliver its result (or exception) to
    ``on_done`` back on the wx main thread. Keeps network/IO off the UI thread."""

    def runner() -> None:
        try:
            outcome = work()
        except Exception as error:  # noqa: BLE001 - surfaced to the UI via on_done
            outcome = error
        wx.CallAfter(on_done, outcome)

    threading.Thread(target=runner, daemon=True).start()
