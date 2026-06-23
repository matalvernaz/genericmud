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
