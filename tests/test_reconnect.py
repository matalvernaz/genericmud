"""Auto-reconnect: backoff policy, reconnect gating, and the retry loop."""

from __future__ import annotations

import asyncio

from genericmud.transport.connection import MudConnection, ReconnectPolicy


async def _noop_sleep(_delay):
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
