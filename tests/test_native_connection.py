"""Native session lifecycle methods with controlled sockets, without OS focus or windows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from genericmud.config.ui_prefs import UiPrefs
from genericmud.config.worlds import World
from tests.helpers import RecordingBackend

pytest.importorskip("wx", reason="native session lifecycle requires wxPython")
from genericmud.ui import wx_app  # noqa: E402


class Panel:
    # Run the real lifecycle methods without constructing controls or requiring a display.
    _start = wx_app.SessionPanel._start
    _start_inner = wx_app.SessionPanel._start_inner
    _send = wx_app.SessionPanel._send
    _reconnect = wx_app.SessionPanel._reconnect
    _do_disconnect = wx_app.SessionPanel._do_disconnect
    _teardown = wx_app.SessionPanel._teardown
    close = wx_app.SessionPanel.close
    disconnect = wx_app.SessionPanel.disconnect
    is_connected = wx_app.SessionPanel.is_connected

    def is_connecting(self):
        return wx_app.SessionPanel.is_connecting(self)

    def __init__(self, monkeypatch):
        self._loop = asyncio.get_running_loop()
        self._alive = True
        self._connect_finished = False
        self._reconnecting = False
        self._connection = self.app = self._voice = None
        self._packs = self._credentials = self._hub = self._diag = self._on_pref = None
        self._prefs = UiPrefs()
        self._keymap = {}
        self._focused = False
        self._questions = []
        self.world = World("Test", "mud.example", 4000)
        self.posted = []
        self.backend = RecordingBackend()
        self._post = self.posted.append
        self._send_raw = self._sound_error = self._ask_from_pack = lambda *args: None
        self.close_notepads = lambda: None
        monkeypatch.setattr(wx_app, "make_voice_backend", lambda: self.backend)
        monkeypatch.setattr(wx_app, "make_sound_backend", lambda **kwargs: None)


class Writer:
    def __init__(self):
        self.closed = False
        self.sent = []

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    def get_extra_info(self, name):
        return None

    def write(self, data):
        self.sent.append(data)


class Reader:
    async def read(self, size):
        await asyncio.Event().wait()


def _observe_close(monkeypatch, connection):
    """Observe the close task started by the panel, without starting cleanup ourselves."""
    started = asyncio.get_running_loop().create_future()
    close = connection.close

    async def observed_close():
        if not started.done():
            started.set_result(asyncio.current_task())
        await close()

    monkeypatch.setattr(connection, "close", observed_close)
    return started


async def _wait_for_close(started):
    task = await asyncio.wait_for(asyncio.shield(started), 1)
    await asyncio.wait_for(asyncio.shield(task), 1)


async def _close_panel(panel, monkeypatch):
    if panel._alive:
        started = _observe_close(monkeypatch, panel._connection) if panel._connection else None
        panel.close()
        if started is not None:
            await _wait_for_close(started)


@pytest.mark.parametrize("drop", ["eof", "reset"])
@pytest.mark.parametrize("queued", [True, False], ids=["queued-disconnect", "immediate-disconnect"])
async def test_disconnect_with_a_ready_reader_announces_once(monkeypatch, drop, queued):
    reading = asyncio.Event()
    result = asyncio.get_running_loop().create_future()

    class ReadyReader:
        async def read(self, size):
            reading.set()
            return await result

    async def open_connection(*args, **kwargs):
        return ReadyReader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    await panel._start()
    await asyncio.wait_for(reading.wait(), 1)
    closed = _observe_close(monkeypatch, panel._connection)
    panel.backend.spoken.clear()
    panel.posted.clear()
    statuses = []
    on_status = panel.app.on_connection_status

    def status(message):
        statuses.append(message)
        on_status(message)

    monkeypatch.setattr(panel.app, "on_connection_status", status)
    panel._connection.on_status = status
    hooks = []
    panel.app._vip_connected = panel.app._mush_connected = True
    panel.app._vip_packs = [SimpleNamespace(
        connection_changed=lambda connected: hooks.append(("vip", connected)),
    )]
    panel.app._mush_packs = [SimpleNamespace(
        dispatch_disconnect=lambda: hooks.append(("mush", False)),
    )]
    if queued:
        panel.disconnect()  # queue _do_disconnect before the reader resumes
    if drop == "eof":
        result.set_result(b"")
    else:
        result.set_exception(ConnectionResetError("controlled reset"))
    if not queued:
        panel._do_disconnect()  # the reader is already ready, ahead of the close task
    try:
        await _wait_for_close(closed)
        assert statuses == ["disconnected"]
        assert panel.backend.spoken == ["Disconnected from Test."]
        assert [message["text"] for message in panel.posted if message["type"] == "echo"] == [
            "* Disconnected from Test",
        ]
        assert hooks == [("mush", False), ("vip", False)]
        assert not panel.is_connected() and not panel.is_connecting()
    finally:
        panel.app._vip_packs = panel.app._mush_packs = []
        await _close_panel(panel, monkeypatch)


@pytest.mark.parametrize("stop", ["close", "disconnect"])
async def test_stop_before_startup_never_opens_a_socket(monkeypatch, stop):
    opened = []

    async def open_connection(*args, **kwargs):
        opened.append(args)
        return Reader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    getattr(panel, stop)()
    await asyncio.sleep(0)  # the queued teardown/disconnect runs before startup
    try:
        await panel._start()
        assert opened == []
        assert not panel.is_connected()
        assert not any("Connected to" in text for text in panel.backend.spoken)
        if stop == "disconnect":
            await panel._reconnect()  # the stopped tab is still reusable
            assert panel.is_connected()
    finally:
        await _close_panel(panel, monkeypatch)


@pytest.mark.parametrize("stop", ["close", "disconnect"])
@pytest.mark.parametrize("result", ["socket", "error", "cancel"])
@pytest.mark.parametrize("phase", ["initial", "manual"])
async def test_stop_during_handshake_has_no_late_status(monkeypatch, stop, result, phase):
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    writer = Writer()

    async def open_connection(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if result == "cancel":
                raise
        if result == "error":
            raise OSError("late refusal")
        return Reader(), writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    if phase == "manual":
        panel._connect_finished = True
        await panel._start()
        starting = asyncio.create_task(panel._reconnect())
    else:
        starting = asyncio.create_task(panel._start())
    await asyncio.wait_for(started.wait(), 1)
    panel.backend.spoken.clear()
    closed = _observe_close(monkeypatch, panel._connection)
    getattr(panel, stop)()
    try:
        await _wait_for_close(closed)  # observe the panel's cleanup; don't initiate another close
        assert cancelled.is_set()
        assert starting.done()
        assert not panel.is_connected()
        assert not panel.app.engine.connected
        if stop == "close":
            assert panel.app._closed
        assert panel.backend.spoken == ([] if stop == "close" else ["Disconnected from Test."])
        if result == "socket":
            assert writer.closed
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 1)
        await _close_panel(panel, monkeypatch)


@pytest.mark.parametrize("state", ["initial", "backoff", "handshake", "connected", "idle"])
async def test_ctrl_d_and_menu_offer_to_stop_pending_connections(monkeypatch, state):
    panel = Panel(monkeypatch)
    panel._connect_finished = state != "initial"
    panel._connection = SimpleNamespace(
        connected=state == "connected", connecting=state in ("backoff", "handshake"),
    )
    actions = []
    panel.disconnect = lambda: actions.append("disconnect")
    panel.reconnect = lambda: actions.append("reconnect")
    labels = []
    frame = SimpleNamespace(
        _active_panel=lambda: panel,
        _connection_item=SimpleNamespace(
            GetItemLabel=lambda: "", SetItemLabel=labels.append, Enable=lambda value: None,
        ),
    )
    wx_app.GenericMudFrame._refresh_connection_item(frame)
    wx_app.GenericMudFrame._on_disconnect(frame, None)
    expected = "reconnect" if state == "idle" else "disconnect"
    assert actions == [expected]
    assert labels == [f"&{expected.capitalize()}\tCtrl+D"]


async def test_closing_a_tab_twice_only_shuts_down_once(monkeypatch):
    panel = Panel(monkeypatch)
    calls = []
    panel.app = SimpleNamespace(
        shutdown=lambda: calls.append("shutdown"), sound=SimpleNamespace(flush=lambda: None),
    )
    panel.close()
    panel.close()
    await asyncio.sleep(0)
    assert calls == ["shutdown"]


@pytest.mark.parametrize("result", ["socket", "error"])
async def test_initial_result_ready_before_teardown_is_not_announced(monkeypatch, result):
    started = asyncio.Event()
    release = asyncio.Event()
    writer = Writer()

    async def open_connection(*args, **kwargs):
        started.set()
        await release.wait()
        if result == "error":
            raise OSError("late refusal")
        return Reader(), writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    starting = asyncio.create_task(panel._start())
    await asyncio.wait_for(started.wait(), 1)
    closed = _observe_close(monkeypatch, panel._connection)
    release.set()  # startup will resume ahead of the teardown callback
    panel.close()  # _alive changes synchronously, teardown is queued after startup
    await _wait_for_close(closed)
    await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 1)
    assert not panel.is_connected()
    assert not panel.app.engine.connected
    assert panel.backend.spoken == []
    if result == "socket":
        assert writer.closed


@pytest.mark.parametrize("backoff", [False, True], ids=["connected", "backoff"])
async def test_tab_close_alone_finishes_transport_cleanup(monkeypatch, backoff):
    from genericmud.transport.connection import ReconnectPolicy

    dropped = asyncio.Event()
    statuses = []
    writer = Writer()

    async def open_connection(*args, **kwargs):
        reader = asyncio.StreamReader()
        if backoff:
            reader.feed_eof()
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    await panel._start()
    connection = panel._connection
    if backoff:
        connection.reconnect_policy = ReconnectPolicy(base_delay=3600, max_delay=3600)
        on_status = connection.on_status

        def status(message):
            statuses.append(message)
            on_status(message)
            dropped.set()

        connection.on_status = status
        await asyncio.wait_for(dropped.wait(), 1)
    before_close = list(statuses)
    closed = _observe_close(monkeypatch, connection)
    panel.close()
    await _wait_for_close(closed)
    assert panel.app._closed
    assert writer.closed
    assert connection._read_task.done()
    if backoff:
        assert connection._reconnect_task.done()
    assert not connection.connected and not connection.connecting
    assert statuses == before_close


@pytest.mark.parametrize("phase", ["initial", "manual", "backoff", "connected"])
async def test_ctrl_d_stops_the_attempt_then_reuses_the_same_tab(monkeypatch, phase):
    from genericmud.transport.connection import ReconnectPolicy

    started = asyncio.Event()
    backoff = asyncio.Event()
    opens = []

    async def open_connection(*args, **kwargs):
        opens.append(args)
        started.set()
        if len(opens) == 1 and phase in ("initial", "manual"):
            await asyncio.Event().wait()
        reader = asyncio.StreamReader()
        if len(opens) == 1 and phase == "backoff":
            reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    if phase == "manual":
        panel._connect_finished = True  # initialize the tab without opening its first socket
        await panel._start()
        attempt = asyncio.create_task(panel._reconnect())
    else:
        attempt = asyncio.create_task(panel._start())
    await asyncio.wait_for(started.wait(), 1)
    connection = panel._connection
    app = panel.app
    if phase == "backoff":
        connection.reconnect_policy = ReconnectPolicy(base_delay=3600, max_delay=3600)

        def status(message):
            app.on_connection_status(message)
            backoff.set()

        connection.on_status = status
        await asyncio.wait_for(backoff.wait(), 1)
    if phase in ("connected", "backoff"):
        await attempt
    panel.backend.spoken.clear()
    frame = SimpleNamespace(_active_panel=lambda: panel)
    wx_app.GenericMudFrame._on_disconnect(frame, None)

    async def stopped():
        while panel.is_connected() or panel.is_connecting():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(stopped(), 1)
        await asyncio.gather(attempt, return_exceptions=True)
        assert panel.backend.spoken == ["Disconnected from Test."]
        await panel._reconnect()
        assert panel.app is app and panel._connection is connection
        assert panel.is_connected() and app.engine.connected
        assert len(opens) == 2
        assert panel.backend.spoken.count("Reconnected to Test.") == 1
    finally:
        await _close_panel(panel, monkeypatch)
        assert not connection.connected and not connection.connecting
        assert connection._read_task.done()


@pytest.mark.parametrize("connects", [True, False])
async def test_initial_success_and_failure_stay_one_shot(monkeypatch, connects):
    opened = []
    writer = Writer()

    async def open_connection(*args, **kwargs):
        opened.append(args)
        if not connects:
            raise OSError("connection refused")
        return Reader(), writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    panel.world.post_connect_commands = "look"
    try:
        await panel._start()
        assert panel._connect_finished
        assert panel.is_connected() == connects
        assert panel.app.engine.connected == connects
        assert len(opened) == 1
        assert not panel._connection.connecting
        assert writer.sent == ([b"look\r\n"] if connects else [])
        assert panel.backend.spoken == (["Connected to Test."] if connects else [
            "Connect failed: connection refused",
        ])
    finally:
        await _close_panel(panel, monkeypatch)
        assert not panel._connection.connected and not panel._connection.connecting
