"""Start the native window: the wx app, the main window, and the loop thread the engines run on.

The window follows the VIPMud model on native controls: a read-only output box a screen reader
reads like Notepad, a separate command box, Tab and Shift+Tab between them, typing in the output
jumps to the command box, and one wx.Simplebook page per world with no visible tab strip, so
nothing extra sits in the Tab order (Ctrl+Tab and Ctrl+Shift+Tab switch worlds).

Threading: wx runs on the main thread, and an asyncio loop runs in a background thread for the
connections. Engine output reaches the window through wx.CallAfter; input and keys go to the
loop with call_soon_threadsafe. Each tab's engine objects (connection, voice, EngineApp) are
created on the loop thread, so the SAPI voice's COM apartment is right.

The window itself is main_window (menus and tabs) and session_panel (one world's tab, with
session_connection, command_box, output_box and pack_window), plus a module for each dialog.
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


