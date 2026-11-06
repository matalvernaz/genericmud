"""MSSP's SSL/TLS variable: the client offers the port the MUD keeps quiet about.

Both MUDs checked by hand hid their encrypted port there -- one behind an in-game FAQ, one
behind nothing at all -- and it is the one setting a player cannot guess. The offer says the
port once, in passing, and /secure takes it; nothing changes on the strength of a MUD's own
claim.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.config.worlds import World
from genericmud.protocol.telnet import OPT_MSSP, WILL, Negotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"


def _status(*pairs: tuple[str, str]) -> bytes:
    """One MSSP payload: VAR/VALUE pairs, which is what the protocol sends."""
    payload = b""
    for name, value in pairs:
        payload += bytes([1]) + name.encode() + bytes([2]) + value.encode()
    return payload


def _app(*, port: int = 9500, tls: bool = False, window=None):
    backend = RecordingBackend()
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="gw",
    )
    app.apply_world_settings(World("gw", "gw.example", port, tls=tls))
    app.pack_window = window
    app.on_telnet_event(Negotiation(WILL, OPT_MSSP))
    return app, backend


def _mssp(app: EngineApp, *pairs: tuple[str, str]) -> None:
    from genericmud.protocol.telnet import Subnegotiation

    app.on_telnet_event(Subnegotiation(OPT_MSSP, _status(*pairs)))


def _said(backend: RecordingBackend) -> list[str]:
    return [line for line in backend.spoken if "encrypted" in line or "port" in line]


def test_an_advertised_encrypted_port_is_offered_once():
    app, backend = _app()
    _mssp(app, ("NAME", "Penultimate Destination"), ("PORT", "9500"), ("SSL", "9501"))
    said = _said(backend)
    assert len(said) == 1
    assert "9501" in said[0] and "/secure" in said[0]


def test_it_is_offered_only_once_however_often_mssp_arrives():
    app, backend = _app()
    for _ in range(3):
        _mssp(app, ("SSL", "9501"))
    assert len(_said(backend)) == 1


def test_a_link_that_is_already_encrypted_is_left_alone():
    app, backend = _app(port=9501, tls=True)
    _mssp(app, ("SSL", "9501"))
    assert _said(backend) == []


def test_a_port_we_are_already_on_is_not_an_offer():
    app, backend = _app(port=9501, tls=False)
    _mssp(app, ("SSL", "9501"))
    assert _said(backend) == []


def test_mssp_without_an_encrypted_port_says_nothing():
    app, backend = _app()
    _mssp(app, ("NAME", "Some Game"), ("PORT", "9500"), ("PLAYERS", "12"))
    assert _said(backend) == []


def test_a_port_that_is_not_a_number_is_ignored():
    app, backend = _app()
    _mssp(app, ("SSL", "yes"))
    assert _said(backend) == []


def test_the_tls_name_works_as_well_as_ssl():
    # MSSP's own list says SSL; a game that calls it TLS should not be missed.
    app, backend = _app()
    _mssp(app, ("TLS", "9501"))
    assert _said(backend) and "9501" in _said(backend)[0]


def test_secure_asks_the_window_and_says_what_it_did():
    class Window:
        def __init__(self) -> None:
            self.asked: list[int] = []

        def use_secure_port(self, port: int) -> bool:
            self.asked.append(port)
            return True

    window = Window()
    app, backend = _app(window=window)
    _mssp(app, ("SSL", "9501"))
    assert app._client_command("/secure") is True
    assert window.asked == [9501]
    assert "switching this world to port 9501" in backend.spoken[-1]


def test_secure_says_what_to_change_by_hand_when_there_is_no_window():
    # The web UI has no world file to edit, so the instruction is the honest answer.
    app, backend = _app(window=None)
    _mssp(app, ("SSL", "9501"))
    app._client_command("/secure")
    assert "tick Use TLS" in backend.spoken[-1]


def test_secure_with_no_offer_says_so():
    app, backend = _app()
    app._client_command("/secure")
    assert "has not offered" in backend.spoken[-1]


def test_the_window_saves_the_new_port_and_reconnects(monkeypatch):
    import pytest

    wx = pytest.importorskip("wx")
    from genericmud.ui import session_connection, session_panel

    saved: list[tuple] = []

    class Frame:
        def _save_world(self, world, *, replacing=None):
            saved.append((world, replacing))
            return True

    scheduled: list = []

    class Loop:
        def call_soon_threadsafe(self, callback):
            scheduled.append(callback)

    frame = Frame()
    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel.world = World("gw", "gw.example", 9500, tls=False)
    panel.app = None  # no engine here: the switch only has to schedule its work
    panel._loop = Loop()
    panel._alive = True
    monkeypatch.setattr(wx, "GetTopLevelParent", lambda _window: frame)
    monkeypatch.setattr(wx, "CallAfter", lambda function, *args: function(*args))
    monkeypatch.setattr(
        session_connection, "load_worlds", lambda: [World("GW", "gw.example", 9500)]
    )

    assert panel.use_secure_port(9501) is True
    assert panel.world.port == 9501 and panel.world.tls is True
    # Saved under its own name, so it edits the world rather than offering to replace it.
    assert saved == [(panel.world, "gw")]
    # The switch runs on the loop thread, because the live link has to be dropped first.
    assert len(scheduled) == 1


def test_the_switch_closes_the_live_link_before_reconnecting():
    import asyncio

    import pytest

    pytest.importorskip("wx")
    from genericmud.ui import session_panel

    order: list[str] = []

    class Connection:
        connected = True
        connecting = False

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel._alive = True
    panel._reconnecting = True  # as a live session's own reconnect guard would leave it
    panel._connection = Connection()
    panel.app = object()

    def do_disconnect():
        order.append("closed")
        Connection.connected = False

    async def reconnect():
        order.append("reconnected")

    panel._do_disconnect = do_disconnect
    panel._reconnect = reconnect
    asyncio.run(panel._switch_secure_port())
    assert order == ["closed", "reconnected"]
    assert panel._reconnecting is False


def test_the_window_does_not_reconnect_a_world_already_there(monkeypatch):
    import pytest

    wx = pytest.importorskip("wx")
    from genericmud.ui import session_panel

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel.world = World("gw", "gw.example", 9501, tls=True)
    panel._alive = True
    panel.reconnect = lambda: (_ for _ in ()).throw(AssertionError("reconnected anyway"))
    monkeypatch.setattr(wx, "GetTopLevelParent", lambda _window: None)
    assert panel.use_secure_port(9501) is False

def test_the_offer_is_made_again_after_a_reconnect():
    # A game can move its port, so an offer belongs to the connection that made it.
    app, backend = _app()
    _mssp(app, ("SSL", "9501"))
    app.after_reconnect()
    _mssp(app, ("SSL", "9501"))
    assert len(_said(backend)) == 2


def test_a_privileged_port_is_not_offered():
    # Dialling whatever number a status reply names would write it into the world file, so only
    # ports a game could plausibly serve TLS on are taken.
    app, backend = _app()
    for value in ("22", "80", "1023", "70000", "gopher"):
        _mssp(app, ("SSL", value))
    assert _said(backend) == []


def test_https_is_allowed_as_a_secure_port():
    app, backend = _app()
    _mssp(app, ("SSL", "443"))
    assert _said(backend) and "443" in _said(backend)[0]


def test_secure_says_so_when_the_world_is_already_on_the_offered_port():
    # The offer stood before the world was moved onto it; taking it again is not a reason to
    # read out instructions for changing what is already right.
    app, backend = _app()
    _mssp(app, ("SSL", "9501"))
    app.apply_world_settings(World("gw", "gw.example", 9501, tls=True))
    app._client_command("/secure")
    assert "already using its encrypted port" in backend.spoken[-1]
    assert "tick Use TLS" not in backend.spoken[-1]

def test_the_window_tells_the_engine_what_it_switched_to(monkeypatch):
    # Otherwise the next connect offers the same port again, and /secure then answers "set it by
    # hand" about a world that is already on it.
    import pytest

    wx = pytest.importorskip("wx")
    from genericmud.ui import session_connection, session_panel

    told: list[World] = []
    scheduled: list = []

    class Frame:
        def _save_world(self, world, *, replacing=None):
            return True

    class App:
        def apply_world_settings(self, world):
            told.append(world)

    class Loop:
        def call_soon_threadsafe(self, callback, *args):
            scheduled.append((callback, args))

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel.world = World("Pen", "pen.example", 9500, tls=False)
    panel.app = App()
    panel._loop = Loop()
    panel._alive = True
    monkeypatch.setattr(wx, "GetTopLevelParent", lambda _window: Frame())
    monkeypatch.setattr(wx, "CallAfter", lambda function, *args: function(*args))
    monkeypatch.setattr(
        session_connection, "load_worlds", lambda: [World("Pen", "pen.example", 9500)]
    )

    assert panel.use_secure_port(9501) is True
    assert scheduled and scheduled[0][0] == panel.app.apply_world_settings
    assert scheduled[0][1] == (panel.world,)
    assert panel.world.port == 9501 and panel.world.tls is True


def test_an_encrypted_link_is_not_offered_another_port():
    # Already encrypted is the end of it, whatever port the game names.
    app, backend = _app(port=4443, tls=True)
    _mssp(app, ("SSL", "9501"))
    assert _said(backend) == []


def test_a_number_python_cannot_read_is_ignored_quietly():
    # "²" passes isdigit() and int() refuses it, and so does a number thousands of digits long:
    # either would stop the MSSP reply half handled. The offer that follows is still made.
    app, backend = _app()
    _mssp(app, ("SSL", "²"))
    _mssp(app, ("SSL", "9" * 5000))
    assert _said(backend) == []
    _mssp(app, ("SSL", "9501"))
    assert len(_said(backend)) == 1


def test_the_window_takes_the_port_on_its_own_thread(monkeypatch):
    # The engine asks from the loop thread, where no window may be touched: saving the world
    # can put up a message box. So the answer is given at once and the work handed over.
    import pytest

    wx = pytest.importorskip("wx")
    from genericmud.ui import session_panel

    handed: list = []

    def no_window(_window):
        raise AssertionError("touched the window off its own thread")

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel.world = World("gw", "gw.example", 9500, tls=False)
    panel._alive = True
    monkeypatch.setattr(wx, "GetTopLevelParent", no_window)
    monkeypatch.setattr(wx, "CallAfter", lambda function, *args: handed.append((function, args)))

    assert panel.use_secure_port(9501) is True
    assert handed == [(panel._take_secure_port, (9501,))]
    assert panel.world.port == 9500  # nothing changed yet, off the window's thread


def test_a_world_that_was_never_saved_is_not_saved_by_secure(monkeypatch):
    # "Connect, don't save" means the world is not kept; switching its port doesn't change that.
    import pytest

    wx = pytest.importorskip("wx")
    from genericmud.ui import session_connection, session_panel

    saved: list = []
    scheduled: list = []

    class Frame:
        def _save_world(self, world, *, replacing=None):
            saved.append(world)
            return True

    class Loop:
        def call_soon_threadsafe(self, callback, *args):
            scheduled.append(callback)

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel.world = World("visit", "visit.example", 9500, tls=False)
    panel.app = None
    panel._loop = Loop()
    panel._alive = True
    monkeypatch.setattr(wx, "GetTopLevelParent", lambda _window: Frame())
    monkeypatch.setattr(wx, "CallAfter", lambda function, *args: function(*args))
    monkeypatch.setattr(
        session_connection, "load_worlds", lambda: [World("other", "other.example", 4000)]
    )

    assert panel.use_secure_port(9501) is True
    assert saved == []
    assert panel.world.port == 9501 and panel.world.tls is True
    assert len(scheduled) == 1  # the tab still switches


def test_the_switch_says_where_it_ended_up_after_the_reconnect():
    # "switching this world..." is said before the link drops, and the disconnect and reconnect
    # lines cut it off; what is left to hear is said once the tab is back, queued behind them.
    import asyncio

    import pytest

    pytest.importorskip("wx")
    from genericmud.ui import session_panel

    class Connection:
        connected = True
        connecting = False

    class Voice:
        def __init__(self) -> None:
            self.said: list[tuple[str, bool]] = []

        def speak(self, text, *, channel, interrupt):
            self.said.append((text, interrupt))

    posted: list = []
    voice = Voice()
    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel._alive = True
    panel._reconnecting = False
    panel._connection = Connection()
    panel.app = object()
    panel.world = World("gw", "gw.example", 9501, tls=True)
    panel._voice = voice
    panel._post = posted.append

    def do_disconnect():
        Connection.connected = False

    async def reconnect():
        Connection.connected = True

    panel._do_disconnect = do_disconnect
    panel._reconnect = reconnect
    asyncio.run(panel._switch_secure_port())
    assert voice.said == [("gw is on port 9501 now, encrypted.", False)]
    assert posted and "gw is on port 9501 now, encrypted." in posted[-1]["text"]
