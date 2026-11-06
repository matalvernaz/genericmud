"""The session window's side of soundpacks' MUSHclient window calls.

Packs call these from the loop thread; the window does them on its own thread and answers.
Channel history plugins paste a picked message into the command box and copy messages to
the clipboard, Mush-Z keeps its channel log in a notepad, and packs open help pages.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser

import pytest

wx = pytest.importorskip("wx")

from genericmud.config.worlds import World  # noqa: E402 - after the wx importorskip


@pytest.fixture(scope="module")
def frame():
    if sys.platform == "darwin":
        pytest.skip("wx needs a framework-build Python on macOS")
    try:
        app = wx.App(False)
    except BaseException as error:  # noqa: BLE001 - wx raises SystemExit with no display
        pytest.skip(f"wx can't start here: {error}")
    parent = wx.Frame(None)
    yield parent
    parent.Destroy()
    del app


@pytest.fixture
def panel(frame, monkeypatch):
    import asyncio

    from genericmud.ui import session_panel

    async def no_session(_panel):
        return None

    monkeypatch.setattr(session_panel.SessionPanel, "_start", no_session)
    loop = asyncio.new_event_loop()
    session = session_panel.SessionPanel(frame, loop, {}, World("Test", "mud.example", 4000))
    yield session
    session.close_notepads()
    session.Destroy()
    loop.run_until_complete(asyncio.sleep(0.01))
    loop.close()


def _on_loop_thread(work) -> dict:
    """Run ``work`` on another thread, as the loop thread would, pumping the UI meanwhile."""
    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.update(work()), daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while worker.is_alive() and time.monotonic() < deadline:
        wx.GetApp().ProcessPendingEvents()
        worker.join(0.01)
    assert not worker.is_alive()
    return outcome


def _pump(seconds: float = 0.2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        wx.GetApp().ProcessPendingEvents()
        wx.MilliSleep(10)


def test_the_command_box_does_what_channel_histories_ask(panel):
    panel.input.SetValue("old words")

    def work() -> dict:
        refused = panel.set_command("ignored")
        panel.select_command(1, -1)
        replaced = panel.paste_command("Bob says hi")
        now = panel.get_command()
        panel.select_command(1, len(now))
        panel.paste_command("")
        filled = panel.set_command("reply")
        return {
            "refused": refused, "replaced": replaced, "now": now, "filled": filled,
            "final": panel.get_command(),
        }

    assert _on_loop_thread(work) == {
        "refused": False, "replaced": "old words", "now": "Bob says hi", "filled": True,
        "final": "reply",
    }


def test_the_clipboard_takes_and_gives_text(panel):
    def work() -> dict:
        panel.set_clipboard("a channel message")
        time.sleep(0.2)  # the window sets it on its own thread
        return {"read": panel.get_clipboard()}

    outcome = _on_loop_thread(work)
    if not wx.TheClipboard.Open():
        pytest.skip("no clipboard on this display")
    wx.TheClipboard.Close()
    assert outcome == {"read": "a channel message"}


def test_a_notepad_opens_beside_the_game_and_reopens_in_place(panel):
    _on_loop_thread(lambda: {"shown": panel.show_notepad("comlog", "Bob: hi\r\n")})
    _pump()
    window = panel._notepad_windows["comlog"]
    assert window.IsShown()
    assert window.text.GetValue().replace("\r", "") == "Bob: hi\n"
    assert window.text.HasFlag(wx.TE_READONLY)
    assert "comlog" in window.GetTitle()
    _on_loop_thread(lambda: {"shown": panel.show_notepad("comlog", "Bob: hi\r\nAnn: yo\r\n")})
    _pump()
    assert panel._notepad_windows["comlog"] is window
    assert window.text.GetValue().replace("\r", "") == "Bob: hi\nAnn: yo\n"
    window.Close()
    _pump()
    assert "comlog" not in panel._notepad_windows


def test_a_link_opens_in_the_browser_and_says_so(panel, monkeypatch):

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    posted: list[dict] = []
    monkeypatch.setattr(panel, "_post", posted.append)
    outcome = _on_loop_thread(lambda: {"ok": panel.open_url("https://example.com/help")})
    _pump()
    assert outcome == {"ok": True}
    assert opened == ["https://example.com/help"]
    assert any("example.com/help" in message.get("text", "") for message in posted)


def test_the_notepad_list_is_read_from_the_session(panel):
    import threading as _threading

    class _App:
        def pack_notepads(self):
            return {"comlog": "Bob: hi\r\n"}

    panel.app = _App()
    loop = panel._loop
    runner = _threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        assert panel.notepad_snapshot() == {"comlog": "Bob: hi\r\n"}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(2)
        panel.app = None
