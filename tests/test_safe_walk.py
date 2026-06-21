"""Safe-walk: step-by-step movement, GMCP/timeout advance, blocked halt, stop key."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.navigation import SafeWalk
from genericmud.protocol.telnet import OPT_GMCP, DataReceived, Subnegotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


class _Scheduler:
    """Manual scheduler: captures scheduled callbacks so tests fire them explicitly."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, _delay, callback) -> None:
        self.calls.append(callback)

    def fire_last(self) -> None:
        self.calls[-1]()


def _walk(steps, **kwargs):
    sent: list[str] = []
    announced: list[str] = []
    sched = _Scheduler()
    walk = SafeWalk(steps, send=sent.append, schedule=sched, announce=announced.append, **kwargs)
    return walk, sent, announced, sched


def test_safe_walk_advances_on_timeout_then_arrives():
    walk, sent, announced, sched = _walk(["n", "e"])
    walk.start()
    assert sent == ["n"]  # only the first step goes out
    sched.fire_last()  # per-step timeout -> next step
    assert sent == ["n", "e"]
    sched.fire_last()  # timeout past the last step -> arrived
    assert "arrived" in announced
    assert not walk.active


def test_safe_walk_advances_on_room_change_and_ignores_stale_timeout():
    walk, sent, announced, sched = _walk(["n", "e"])
    walk.start()  # sends n, schedules timeout (token 1)
    walk.on_room_change()  # confirmed move -> sends e, schedules timeout (later token)
    assert sent == ["n", "e"]
    sched.calls[0]()  # fire the STALE timeout from step 1 -> no-op
    assert sent == ["n", "e"]
    sched.calls[1]()  # fire the live timeout -> arrived
    assert "arrived" in announced


def test_safe_walk_halts_on_blocked_line():
    walk, sent, announced, _sched = _walk(["n", "e", "s"])
    walk.start()
    assert sent == ["n"]
    walk.on_line("You can't go that way.")
    assert not walk.active
    assert sent == ["n"]  # e and s abandoned
    assert any("blocked" in message for message in announced)


def _app():
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    sched = _Scheduler()
    app = EngineApp(
        voice, send=sent.append, post=[].append, schedule=sched, keymap=load_keymap("vipmud")
    )
    return app, sent, sched, backend


def test_app_double_dot_starts_a_safe_walk():
    app, sent, sched, _backend = _app()
    app.on_ws_message({"type": "input", "text": "..2n"})
    assert sent == ["n"]  # one step at a time, not both
    assert app.nav.trail == ["n"]  # recorded for breadcrumb too
    sched.fire_last()
    assert sent == ["n", "n"]


def test_app_safe_walk_advances_on_gmcp_room_change():
    app, sent, _sched, _backend = _app()
    app.on_ws_message({"type": "input", "text": "..2e"})
    assert sent == ["e"]
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'room.info {"num": 2}'))  # moved
    assert sent == ["e", "e"]


def test_app_safe_walk_halts_on_blocked_then_stop_key_is_idle():
    app, sent, _sched, backend = _app()
    app.on_ws_message({"type": "input", "text": "..3n"})
    app.on_telnet_event(DataReceived(b"You can't go that way.\r\n"))
    assert not app._walk.active
    assert sent == ["n"]
    app._handle_key("alt+s")  # nav:stop with nothing walking
    assert any("not walking" in spoken for spoken in backend.spoken)
