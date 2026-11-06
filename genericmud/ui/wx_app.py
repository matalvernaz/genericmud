"""Native wxPython UI (Windows-first).

The VIPMud-class interaction model on native controls: a read-only multiline
output box NVDA reads like Notepad (Tab to it, arrow/say-line), a separate command
box, Tab/Shift+Tab between them, type-on-output jumps to the command box, and one
wx.Simplebook page per MUD (no visible tab strip, so nothing sits in the keyboard Tab
order; Ctrl+Tab / Ctrl+Shift+Tab switch sessions).

Threading: wx runs on the main thread; an asyncio loop runs in a background thread
for the connections. Engine output is marshaled to the UI with wx.CallAfter; input
and keys are pushed to the loop with call_soon_threadsafe. Each session's engine
objects (connection, voice, EngineApp) are created ON the loop thread so the SAPI
voice's COM apartment is correct.

Build-blind: this module isn't exercised by the test suite (wxPython needs a display
and isn't installed on the dev host); the reused engine is what the tests cover.
"""

from __future__ import annotations

import asyncio
import threading

import wx

from genericmud.config.keymap import load_keymap
from genericmud.config.update_prefs import load_prefs
from genericmud.config.worlds import World
from genericmud.protocol.charset import AUTO
from genericmud.session.crashlog import install_loop_exception_handler
from genericmud.ui.main_window import GenericMudFrame
from genericmud.update import self_update


def _world_from_args(args) -> World | None:
    """The world to auto-connect for ``genericmud host port [--tls] [--sounds] [--encoding]``."""
    if not args.host:
        return None
    return World(
        name=args.host, host=args.host, port=args.port, tls=args.tls,
        sounds=args.sounds, encoding=getattr(args, "encoding", AUTO),
        images_url=getattr(args, "images", "") or "",
    )


def run(args, recovery=None) -> None:
    loop = asyncio.new_event_loop()
    install_loop_exception_handler(loop)  # capture engine-thread coroutine crashes
    threading.Thread(target=_run_loop, args=(loop,), daemon=True).start()

    wx_app = wx.App(False)
    frame = GenericMudFrame(loop, load_keymap("vipmud"))
    frame.Show()
    if (world := _world_from_args(args)) is not None:
        frame.open_session(world)
    else:
        # A blank first launch was silent; give a blind user the way in. Deferred so it
        # speaks after the window is up, not over the screen reader announcing the window.
        wx.CallAfter(
            frame.announce,
            "Welcome to genericMud. Press Control N for a new world, or Control O for a "
            "saved one. The Help menu has Getting Started.",
        )
    if recovery is not None:  # a prior in-app update was rolled back at startup; tell the user
        wx.CallAfter(frame.show_recovery, recovery)
    prefs = load_prefs()
    # Always run the check when enabled; a snooze no longer blocks it (it would hide a newer
    # release too). Suppression is applied per-version at the offer stage in _on_update_checked.
    if self_update.is_frozen() and prefs.check_enabled:
        frame.check_for_updates(manual=False)
    wx_app.MainLoop()
    loop.call_soon_threadsafe(loop.stop)


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


