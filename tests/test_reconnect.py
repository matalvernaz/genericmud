"""Auto-reconnect: backoff policy, reconnect gating, and the retry loop."""

from __future__ import annotations

import asyncio
import time

from genericmud.transport.connection import (
    _QUIT_GRACE_SECONDS,
    MudConnection,
    ReconnectPolicy,
)


async def _noop_sleep(_delay):
    pass


class _FakeWriter:
    def write(self, data):
        pass

    def is_closing(self):
        return False

    def close(self):
        pass


def test_policy_backoff_caps_and_gives_up():
    policy = ReconnectPolicy(base_delay=1, max_delay=10, max_attempts=4)
    assert [policy.delay_for(n) for n in (1, 2, 3, 4)] == [1, 2, 4, 8]
    assert policy.delay_for(5) is None  # past max_attempts
    assert ReconnectPolicy(base_delay=5, max_delay=10).delay_for(3) == 10  # capped at max_delay


def test_policy_unlimited_never_gives_up():
    assert ReconnectPolicy(max_attempts=0).delay_for(1000) is not None


def test_should_reconnect_gating():
    conn = MudConnection()
    assert conn._should_reconnect() is False  # off by default
    conn.auto_reconnect = True
    assert conn._should_reconnect() is True
    conn._closing = True
    assert conn._should_reconnect() is False  # a deliberate close suppresses reconnect


def test_quit_command_suppresses_reconnect():
    conn = MudConnection()
    conn.auto_reconnect = True
    conn._writer = _FakeWriter()
    conn.send_line("  QUIT  ")  # stripped + lowercased against the quit set
    assert conn._should_reconnect() is False  # the close following a quit is intentional


def test_non_quit_command_still_reconnects():
    conn = MudConnection()
    conn.auto_reconnect = True
    conn._writer = _FakeWriter()
    conn.send_line("kill dragon")
    assert conn._should_reconnect() is True


def test_stale_quit_no_longer_suppresses():
    conn = MudConnection()
    conn.auto_reconnect = True
    conn._quit_sent_at = time.monotonic() - (_QUIT_GRACE_SECONDS + 1)
    assert conn._should_reconnect() is True  # quit too long ago to explain this drop


async def test_connect_clears_quit_marker(monkeypatch):
    conn = MudConnection()
    conn._quit_sent_at = time.monotonic()

    class _Reader:
        async def read(self, _n):
            return b""  # immediate EOF so the read loop exits cleanly

    async def fake_open(host, port, ssl=None):
        return _Reader(), _FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    await conn.connect("host", 23)
    assert conn._quit_sent_at is None  # a fresh connection re-arms reconnect
    if conn._read_task is not None:
        await conn._read_task  # drain the EOF read loop


async def test_reconnect_loop_retries_then_succeeds(monkeypatch):
    conn = MudConnection()
    conn._target = ("host", 23, False, None)
    conn.reconnect_policy = ReconnectPolicy(base_delay=0.01, max_attempts=5)
    statuses: list[str] = []
    conn.on_status = statuses.append
    attempts = {"n": 0}

    async def fake_connect(host, port, *, tls=False, ssl_context=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("connection refused")

    monkeypatch.setattr(conn, "connect", fake_connect)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    await conn._reconnect_loop()
    assert attempts["n"] == 3  # two failures then success
    assert any("reconnected" in status for status in statuses)


async def test_reconnect_loop_gives_up_after_max(monkeypatch):
    conn = MudConnection()
    conn._target = ("host", 23, False, None)
    conn.reconnect_policy = ReconnectPolicy(base_delay=0.01, max_attempts=2)
    statuses: list[str] = []
    conn.on_status = statuses.append

    async def always_fail(host, port, *, tls=False, ssl_context=None):
        raise OSError("connection refused")

    monkeypatch.setattr(conn, "connect", always_fail)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    await conn._reconnect_loop()
    assert any("giving up" in status for status in statuses)


class _ClosingWriter(_FakeWriter):
    def __init__(self):
        self.closed = False

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class _IdleReader:
    async def read(self, _n):
        await asyncio.Event().wait()  # a live socket with nothing to say
        return b""


async def test_disconnect_during_the_reconnect_handshake_is_not_announced(monkeypatch):
    # connect() already drops a socket that finishes opening after Disconnect. The loop
    # still said "reconnected", so the engine marked itself connected, spoke it, and
    # re-armed auto-login for a session the user had just closed.
    statuses: list[str] = []
    handshake_started = asyncio.Event()
    release_handshake = asyncio.Event()
    writer = _ClosingWriter()

    async def slow_open_connection(host, port, ssl=None):
        handshake_started.set()
        await release_handshake.wait()
        return _IdleReader(), writer

    monkeypatch.setattr(asyncio, "open_connection", slow_open_connection)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    conn = MudConnection()
    conn.on_status = statuses.append
    conn.auto_reconnect = True
    conn._target = ("host", 23, False, None)
    loop_task = asyncio.create_task(conn._reconnect_loop())
    await handshake_started.wait()
    conn.auto_reconnect = False  # what the window's Disconnect does first
    await conn.close()
    release_handshake.set()
    await loop_task

    assert writer.closed
    assert not conn.connected
    assert "reconnected" not in statuses


async def test_connecting_while_the_handshake_is_in_flight(monkeypatch):
    # The window offers to reconnect whenever the tab isn't connected, and a handshake still
    # in flight isn't connected either: a second connect() there opens a second socket.
    handshake_started = asyncio.Event()
    release_handshake = asyncio.Event()

    async def slow_open_connection(host, port, ssl=None):
        handshake_started.set()
        await release_handshake.wait()
        return _IdleReader(), _ClosingWriter()

    monkeypatch.setattr(asyncio, "open_connection", slow_open_connection)
    conn = MudConnection()
    assert conn.connecting is False
    connecting = asyncio.create_task(conn.connect("host", 23))
    await handshake_started.wait()
    assert conn.connecting is True
    assert conn.connected is False
    release_handshake.set()
    await connecting
    assert conn.connecting is False
    assert conn.connected is True
    await conn.close()


class _EndedReader:
    async def read(self, _n):
        return b""  # the server closed the link


def _backoff_setup(monkeypatch):
    """A link that drops at once, a backoff held in its wait, and no server for the retry."""
    sleeping = asyncio.Event()
    wake = asyncio.Event()
    opened: list[str] = []

    async def held_sleep(_delay):
        sleeping.set()
        await wake.wait()

    async def open_then_refuse(host, port, ssl=None):
        opened.append(host)
        if len(opened) > 1:
            raise OSError("connection refused")
        return _EndedReader(), _ClosingWriter()

    monkeypatch.setattr(asyncio, "open_connection", open_then_refuse)
    monkeypatch.setattr(asyncio, "sleep", held_sleep)
    conn = MudConnection()
    conn.auto_reconnect = True
    conn.reconnect_policy = ReconnectPolicy(max_attempts=1)
    statuses: list[str] = []
    conn.on_status = statuses.append
    return conn, sleeping, wake, opened, statuses


async def test_connecting_while_the_backoff_waits_and_not_once_it_gives_up(monkeypatch):
    conn, sleeping, wake, opened, statuses = _backoff_setup(monkeypatch)
    await conn.connect("host", 23)
    await sleeping.wait()  # the link dropped; the backoff waits before its first try
    assert conn.connected is False
    assert conn.connecting is True  # a manual reconnect now would race the backoff's
    wake.set()
    await conn._reconnect_task
    assert any("giving up" in status for status in statuses)
    assert opened == ["host", "host"]
    assert conn.connecting is False  # a dead end again, where a manual reconnect belongs


async def test_a_close_during_the_backoff_wait_ends_connecting_without_a_retry(monkeypatch):
    conn, sleeping, wake, opened, _statuses = _backoff_setup(monkeypatch)
    await conn.connect("host", 23)
    await sleeping.wait()
    await conn.close()
    wake.set()
    await conn._reconnect_task
    assert conn.connecting is False
    assert opened == ["host"]  # the backoff saw the close and didn't try again
