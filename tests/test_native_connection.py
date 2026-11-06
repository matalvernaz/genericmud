"""Native session lifecycle methods with controlled sockets, without OS focus or windows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from genericmud.config.ui_prefs import UiPrefs
from genericmud.config.worlds import World
from tests.helpers import RecordingBackend

wx = pytest.importorskip("wx", reason="native session lifecycle requires wxPython")
from genericmud.ui import main_window, session_connection, session_panel  # noqa: E402


class Panel:
    # Run the real lifecycle methods without constructing controls or requiring a display.
    _start = session_panel.SessionPanel._start
    _start_inner = session_panel.SessionPanel._start_inner
    _send = session_panel.SessionPanel._send
    _reconnect = session_panel.SessionPanel._reconnect
    _adopt_saved_world = session_panel.SessionPanel._adopt_saved_world
    _target_for_reconnect = session_panel.SessionPanel._target_for_reconnect
    _do_disconnect = session_panel.SessionPanel._do_disconnect
    _teardown = session_panel.SessionPanel._teardown
    close = session_panel.SessionPanel.close
    disconnect = session_panel.SessionPanel.disconnect
    is_connected = session_panel.SessionPanel.is_connected
    apply_connection_settings = session_panel.SessionPanel.apply_connection_settings
    _apply_connection_settings = session_panel.SessionPanel._apply_connection_settings

    def is_connecting(self):
        return session_panel.SessionPanel.is_connecting(self)

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
        # No saved worlds here: a reconnect re-reads the saved world, and these tests are not
        # about that -- the real config folder should not be read while testing either.
        monkeypatch.setattr(session_connection, "load_worlds", list)
        self.posted = []
        self.backend = RecordingBackend()
        self._post = self.posted.append
        self._send_raw = self._sound_error = self._ask_from_pack = lambda *args: None
        self.close_notepads = lambda: None
        monkeypatch.setattr(session_connection, "make_voice_backend", lambda: self.backend)
        monkeypatch.setattr(session_connection, "make_sound_backend", lambda **kwargs: None)


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


def _capture_timers(panel):
    timers = []
    loop = panel._loop
    panel._loop = SimpleNamespace(
        call_later=lambda delay, callback: timers.append((delay, callback)),
        call_soon_threadsafe=loop.call_soon_threadsafe,
    )
    return timers


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
    main_window.GenericMudFrame._refresh_connection_item(frame)
    main_window.GenericMudFrame._on_disconnect(frame, None)
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
    retry = connection._reconnect_task
    closed = _observe_close(monkeypatch, connection)
    panel.close()
    await _wait_for_close(closed)
    assert panel.app._closed
    assert writer.closed
    assert connection._read_task.done()
    if backoff:
        assert retry.done()
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
    main_window.GenericMudFrame._on_disconnect(frame, None)

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
    panel._prefs.auto_reconnect = False
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


async def test_initial_failure_recovers_without_rebuilding_the_session(monkeypatch, tmp_path):
    from genericmud.packs import PackStore

    recovered = asyncio.Event()
    opened = []
    writer = Writer()

    async def open_connection(*args, **kwargs):
        opened.append(args)
        if len(opened) == 1:
            raise OSError("unavailable")
        recovered.set()
        return Reader(), writer

    async def no_wait(delay):
        pass

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    panel = Panel(monkeypatch)
    timers = _capture_timers(panel)
    store = PackStore(tmp_path / "store")
    source = tmp_path / "hooks.xml"
    source.write_text(
        "<muclient><plugin id='hooks' name='hooks'/><script><![CDATA[\n"
        "function OnPluginInstall() Note('installed') end\n"
        "function OnPluginConnect() Note('plugin ready') Send('hello') end\n"
        "]]></script></muclient>", encoding="utf-8",
    )
    store.install(source, world="Test", trust=True)
    panel._packs = store
    panel.world.post_connect_commands = "look"
    await panel._start()
    app = panel.app
    try:
        await asyncio.wait_for(recovered.wait(), 1)
        assert panel.app is app
        assert panel.is_connected() and app.engine.connected
        assert writer.sent == [b"look\r\n", b"hello\r\n"]
        assert len(opened) == 2
        assert "Reconnected to Test." not in panel.backend.spoken
        assert panel.backend.spoken.count("installed") == 1
        assert panel.backend.spoken.count("plugin ready") == 1
        for delay, callback in tuple(timers):
            if delay < 5:
                callback()
        assert panel.backend.spoken.count("Reconnected to Test.") == 1
    finally:
        await _close_panel(panel, monkeypatch)


@pytest.mark.parametrize("enabled", [False, True])
async def test_manual_reconnect_respects_preference_on_next_drop(monkeypatch, enabled):
    reader = asyncio.StreamReader()

    async def open_connection(*args, **kwargs):
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    panel._prefs.auto_reconnect = enabled
    panel._connect_finished = True  # initialize a stopped tab for manual reconnect
    await panel._start()
    try:
        await panel._reconnect()
        assert panel.is_connected()
        assert panel._connection.auto_reconnect == enabled
        reader.feed_eof()
        await panel._connection._read_task
        assert panel.is_connecting() == enabled
    finally:
        await _close_panel(panel, monkeypatch)


@pytest.mark.parametrize("accepted", [False, True])
async def test_connection_settings_save_and_reach_all_sessions(monkeypatch, tmp_path, accepted):
    from genericmud.config.ui_prefs import load_ui_prefs, save_ui_prefs
    from tests.test_auto_reconnect import Sleeps

    async def open_connection(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    sleeps = Sleeps()
    monkeypatch.setattr(asyncio, "sleep", sleeps)
    prefs = UiPrefs(auto_reconnect=False)
    panels = [Panel(monkeypatch), Panel(monkeypatch)]
    for panel in panels:
        panel._prefs = prefs
        await panel._start()
    path = tmp_path / "ui-prefs.toml"
    monkeypatch.setattr(main_window, "save_ui_prefs", lambda value: save_ui_prefs(value, path))
    destroyed = []

    class Dialog:
        def __init__(self, parent, current):
            assert current is prefs

        def ShowModal(self):
            return wx.ID_OK if accepted else wx.ID_CANCEL

        def get_settings(self):
            return True, 300

        def Destroy(self):
            destroyed.append(True)

    monkeypatch.setattr(main_window, "ConnectionSettingsDialog", Dialog)
    frame = SimpleNamespace(
        _prefs=prefs,
        book=SimpleNamespace(GetPageCount=lambda: 2, GetPage=lambda index: panels[index]),
    )
    try:
        main_window.GenericMudFrame._on_connection_settings(frame, None)
        assert destroyed == [True]
        if accepted:
            await sleeps.next(300)
            await sleeps.next(300)
            assert load_ui_prefs(path) == prefs
            assert prefs.auto_reconnect and prefs.reconnect_interval == 300
            assert all(panel.is_connecting() for panel in panels)
            assert (panels[0]._connection._reconnect_task
                    is not panels[1]._connection._reconnect_task)
            added = Panel(monkeypatch)
            added._prefs = prefs
            panels.append(added)
            await added._start()
            await sleeps.next(300)
            assert added._connection.auto_reconnect and added.is_connecting()
        else:
            assert not path.exists() and not prefs.auto_reconnect
            assert all(not panel.is_connecting() for panel in panels)
    finally:
        for panel in panels:
            await _close_panel(panel, monkeypatch)


async def test_settings_on_cannot_revive_an_intentionally_disconnected_panel(monkeypatch):
    async def open_connection(*args, **kwargs):
        return Reader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    panel = Panel(monkeypatch)
    await panel._start()
    closed = _observe_close(monkeypatch, panel._connection)
    panel.disconnect()
    await _wait_for_close(closed)
    try:
        assert panel._prefs.auto_reconnect is True
        panel._apply_connection_settings(False, 5)
        panel._apply_connection_settings(True, 5)
        assert not panel.is_connected() and not panel.is_connecting()
        await panel._reconnect()
        assert panel.is_connected()
    finally:
        await _close_panel(panel, monkeypatch)


async def test_initial_recovery_restores_login_and_the_world_codec(monkeypatch):
    connected = asyncio.Event()
    writer = Writer()
    attempts = []

    async def open_connection(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("unavailable")
        connected.set()
        return Reader(), writer

    async def no_wait(delay):
        pass

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    panel = Panel(monkeypatch)
    timers = _capture_timers(panel)
    panel.world.encoding = "cp1251"
    panel.world.connect_method = "diku"
    panel.world.post_connect_commands = "я"
    panel._credentials = SimpleNamespace(get=lambda world: ("hero", "secret"))
    await panel._start()
    assert writer.sent == []  # no blind login is typed before a socket exists
    try:
        await asyncio.wait_for(connected.wait(), 1)
        assert panel.app.engine.connected
        assert writer.sent == [b"hero\r\n", b"secret\r\n", b"\xff\xff\r\n"]
        assert "Reconnected to Test." not in panel.backend.spoken
        for delay, callback in tuple(timers):
            if delay < 5:
                callback()
        assert panel.backend.spoken.count("Reconnected to Test.") == 1
    finally:
        await _close_panel(panel, monkeypatch)


async def test_an_initial_failure_says_why_before_it_keeps_trying(monkeypatch):
    # Retrying for ever hides a typo in the host or a refused certificate unless the reason is
    # said. It rides in the status that is spoken, since a line of its own would be cut off by it.
    waiting = asyncio.Event()
    real_sleep = asyncio.sleep

    async def open_connection(*args, **kwargs):
        raise OSError("connection refused")

    async def hold(delay):
        if delay == 0:
            return await real_sleep(0)
        waiting.set()
        await asyncio.get_running_loop().create_future()  # until the tab closes

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(asyncio, "sleep", hold)
    panel = Panel(monkeypatch)  # automatic reconnection on, as it is by default
    try:
        await panel._start()
        await asyncio.wait_for(waiting.wait(), 1)
        assert panel.backend.spoken == [
            "connection unavailable: connection refused; reconnecting every 5s"
        ]
    finally:
        await _close_panel(panel, monkeypatch)
