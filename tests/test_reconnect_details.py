"""A tab re-reads its world's connection details when it reconnects.

How a tab connected used to be frozen when it opened, so editing a world's port or TLS switch
and pressing Ctrl+D went back out on the old ones. The details come from the saved world now,
all at once -- which is what keeps a new host from arriving with the old encoding, the reason
the freeze existed in the first place.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.config.worlds import World
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


class _Connection:
    def __init__(self) -> None:
        self.text_codec = "the old codec"


class _App:
    def __init__(self) -> None:
        self.encodings: list[str] = []
        self.codec = "new codec"

    def set_encoding(self, encoding: str) -> None:
        self.encodings.append(encoding)

    def apply_world_settings(self, world: World) -> None:
        self.told = world


def _panel(world: World):
    from genericmud.ui import session_panel

    panel = session_panel.SessionPanel.__new__(session_panel.SessionPanel)
    panel.world = world
    panel.app = _App()
    panel._connection = _Connection()
    return panel


def _saving(monkeypatch, *worlds: World) -> None:
    from genericmud.ui import session_connection

    monkeypatch.setattr(session_connection, "load_worlds", lambda: list(worlds))


def test_a_reconnect_takes_the_port_and_tls_the_world_has_now(monkeypatch):
    _saving(monkeypatch, World("Pen", "pen.example", 9501, tls=True))
    panel = _panel(World("Pen", "pen.example", 9500, tls=False))

    panel._adopt_saved_world()

    assert panel.world.port == 9501
    assert panel.world.tls is True


def test_it_takes_the_host_and_encoding_in_the_same_breath(monkeypatch):
    # Together is the point: a new host with the old encoding was the reason for freezing them.
    _saving(monkeypatch, World("Pen", "new.example", 4000, encoding="cp1252"))
    panel = _panel(World("Pen", "old.example", 4000, encoding="auto"))

    panel._adopt_saved_world()

    assert panel.world.host == "new.example"
    assert panel.world.encoding == "cp1252"
    assert panel.app.encodings == ["cp1252"]
    assert panel._connection.text_codec == panel.app.codec


def test_the_name_is_matched_without_caring_about_case(monkeypatch):
    _saving(monkeypatch, World("pen", "pen.example", 9501, tls=True))
    panel = _panel(World("Pen", "pen.example", 9500, tls=False))

    panel._adopt_saved_world()
    assert panel.world.port == 9501


def test_a_world_that_was_never_saved_keeps_what_it_opened_with(monkeypatch):
    # The command line makes a world with no saved record; there is nothing to re-read.
    _saving(monkeypatch)
    panel = _panel(World("pen.example", "pen.example", 9500))

    panel._adopt_saved_world()
    assert panel.world.port == 9500
    assert panel.app.encodings == []


def test_the_sounds_folder_waits_for_a_new_tab(monkeypatch):
    # Packs load when a tab starts, so adopting a new folder here would point at sounds that
    # were never loaded.
    _saving(monkeypatch, World("Pen", "pen.example", 4000, sounds="D:/new"))
    panel = _panel(World("Pen", "pen.example", 4000, sounds="D:/old"))

    panel._adopt_saved_world()
    assert panel.world.sounds == "D:/old"


def test_a_new_encoding_replaces_the_codec():
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        encoding="utf-8",
    )
    original = app.codec
    app.set_encoding("cp1252")
    assert app.encoding == "cp1252"
    assert app.codec is not original


def test_the_same_encoding_leaves_the_codec_alone():
    # A reconnect with nothing changed must not throw away a codec mid-stream state.
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        encoding="utf-8",
    )
    original = app.codec
    app.set_encoding("utf-8")
    assert app.codec is original


# --- the transport's own reconnect asks the same question ---------------------------


async def test_an_automatic_reconnect_asks_where_to_dial():
    # A drop nobody asked for: the world may have been edited since the link was made.
    from genericmud.transport.connection import MudConnection, ReconnectPolicy

    connection = MudConnection()
    connection._target = ("old.example", 4000, False, None)
    connection.reconnect_policy = ReconnectPolicy(base_delay=0.0, max_delay=0.0, max_attempts=1)
    dialled: list[tuple] = []

    async def connect(host, port, *, tls=False, ssl_context=None):
        dialled.append((host, port, tls))
        connection._closing = True  # one attempt is enough to see where it went

    connection.connect = connect
    connection.set_target_provider(lambda: ("new.example", 9501, True))
    await connection._reconnect_loop()

    assert dialled == [("new.example", 9501, True)]


async def test_a_provider_that_breaks_leaves_the_reconnect_working():
    from genericmud.transport.connection import MudConnection, ReconnectPolicy

    connection = MudConnection()
    connection._target = ("old.example", 4000, False, None)
    connection.reconnect_policy = ReconnectPolicy(base_delay=0.0, max_delay=0.0, max_attempts=1)
    dialled: list[tuple] = []

    async def connect(host, port, *, tls=False, ssl_context=None):
        dialled.append((host, port, tls))
        connection._closing = True

    def broken():
        raise RuntimeError("no worlds file here")

    connection.connect = connect
    connection.set_target_provider(broken)
    await connection._reconnect_loop()

    assert dialled == [("old.example", 4000, False)]


async def test_the_window_answers_with_the_world_as_it_stands_now(monkeypatch):
    _saving(monkeypatch, World("Pen", "new.example", 9501, tls=True))
    panel = _panel(World("Pen", "old.example", 9500, tls=False))

    assert panel._target_for_reconnect() == ("new.example", 9501, True)
    assert panel.world.port == 9501


def test_the_engine_hears_the_port_and_tls_too(monkeypatch):
    # The engine checks an MSSP offer of an encrypted port against the link it believes it is
    # on. Left as the tab opened, a world edited onto that port and TLS was offered it again.
    from genericmud.protocol.telnet import OPT_MSSP, WILL, Negotiation, Subnegotiation

    _saving(monkeypatch, World("Pen", "pen.example", 9501, tls=True))
    panel = _panel(World("Pen", "pen.example", 9500, tls=False))
    backend = RecordingBackend()
    panel.app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0),
        send=[].append,
        post=[].append,
        keymap=load_keymap("vipmud"),
        name="Pen",
    )
    panel.app.apply_world_settings(panel.world)

    panel._adopt_saved_world()
    panel.app.on_telnet_event(Negotiation(WILL, OPT_MSSP))
    panel.app.on_telnet_event(Subnegotiation(OPT_MSSP, b"\x01SSL\x029501"))

    assert not [line for line in backend.spoken if "encrypted" in line]


def test_a_reconnect_that_finds_the_link_up_changes_nothing(monkeypatch):
    # The link came back while the reconnect waited its turn: the session is live, so its
    # encoding must not be swapped under it.
    import asyncio

    _saving(monkeypatch, World("Pen", "new.example", 9501, encoding="cp1251"))
    panel = _panel(World("Pen", "pen.example", 9500, encoding="auto"))
    panel._alive = True
    panel._connection.connected = True

    asyncio.run(panel._reconnect())

    assert panel.world.host == "pen.example" and panel.world.encoding == "auto"
    assert panel.app.encodings == []
