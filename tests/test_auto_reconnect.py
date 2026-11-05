"""Fixed recovery scheduling with controlled sleeps and sockets, never an external MUD."""

from __future__ import annotations

import asyncio

import pytest

from genericmud.protocol.telnet import IAC, OPT_MCCP2, SB, SE, WILL
from genericmud.transport.connection import MudConnection, ReconnectPolicy
from tests.test_reconnect import _ClosingWriter, _IdleReader


class Sleeps:
    def __init__(self):
        self.waits = asyncio.Queue()

    async def __call__(self, delay):
        wake = asyncio.get_running_loop().create_future()
        await self.waits.put((delay, wake))
        await wake

    async def next(self, expected):
        delay, wake = await asyncio.wait_for(self.waits.get(), 1)
        assert delay == expected
        return wake


def setup(monkeypatch, open_connection):
    sleeps = Sleeps()
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(asyncio, "sleep", sleeps)
    connection = MudConnection()
    statuses = []
    connection.on_status = statuses.append
    connection.configure_reconnect(True, 5)
    return connection, sleeps, statuses


@pytest.mark.parametrize("interval", [1, 5, 300])
def test_fixed_policy_is_unlimited_and_preserves_custom_finite_policy(interval):
    policy = ReconnectPolicy(max_attempts=0, fixed_interval=interval)
    assert [policy.delay_for(n) for n in (1, 2, 1000, 10**100)] == [interval] * 4
    assert ReconnectPolicy(max_attempts=2, fixed_interval=interval).delay_for(3) is None


async def test_failures_wait_after_each_attempt_then_recover_without_spamming(monkeypatch):
    calls = []

    async def open_connection(host, port, ssl=None):
        calls.append(host)
        if len(calls) <= 10:
            raise OSError("unavailable")
        return _IdleReader(), _ClosingWriter()

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    with pytest.raises(OSError):
        await conn.connect("mud.example", 23)
    assert conn.start_reconnect()
    assert not conn.start_reconnect()
    try:
        for attempt in range(10):
            wake = await sleeps.next(5)
            assert len(calls) == attempt + 1
            assert not conn.start_reconnect()
            wake.set_result(None)
        retry = conn._reconnect_task
        await asyncio.wait_for(asyncio.shield(retry), 1)
        assert conn.connected and not conn.connecting
        assert conn._reconnect_task is None
        assert statuses == ["connection unavailable; reconnecting every 5s", "reconnected"]
    finally:
        await conn.close()


async def test_immediate_eof_still_waits_between_connections(monkeypatch):
    calls = []

    async def open_connection(host, port, ssl=None):
        calls.append(host)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, _ClosingWriter()

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    await conn.connect("mud.example", 23)
    try:
        for count in (1, 2, 3):
            wake = await sleeps.next(5)
            assert len(calls) == count
            wake.set_result(None)
        await sleeps.next(5)
        assert len(calls) == 4
    finally:
        await conn.close()


async def test_disable_backoff_then_reenable_uses_one_new_task(monkeypatch):
    async def open_connection(*args, **kwargs):
        raise OSError("unavailable")

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    with pytest.raises(OSError):
        await conn.connect("mud.example", 23)
    conn.start_reconnect()
    wake = await sleeps.next(5)
    old = conn._reconnect_task
    conn.configure_reconnect(False, 5)
    await asyncio.gather(old, return_exceptions=True)
    assert wake.cancelled() and not conn.connecting
    conn.configure_reconnect(True, 1)
    await sleeps.next(1)
    try:
        assert conn.connecting and conn._reconnect_task is not old
        active = conn._reconnect_task
        conn.configure_reconnect(True, 1)
        assert conn._reconnect_task is active
    finally:
        await conn.close()


@pytest.mark.parametrize("late_result", ["cancel", "socket", "error"])
@pytest.mark.parametrize("reenable", [False, True])
async def test_disable_pending_automatic_handshake_rejects_late_results(
    monkeypatch, late_result, reenable,
):
    started = asyncio.Event()
    writer = _ClosingWriter()
    calls = []

    async def open_connection(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("unavailable")
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if late_result == "cancel":
                raise
            if late_result == "error":
                raise OSError("late failure") from None
        return _IdleReader(), writer

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    with pytest.raises(OSError):
        await conn.connect("mud.example", 23)
    conn.start_reconnect()
    wake = await sleeps.next(5)
    wake.set_result(None)
    await asyncio.wait_for(started.wait(), 1)
    old = conn._reconnect_task
    conn.configure_reconnect(False, 5)
    if reenable:
        conn.configure_reconnect(True, 5)  # On before the old cancellation has drained
    await asyncio.gather(old, return_exceptions=True)
    try:
        if reenable:
            await sleeps.next(5)
            assert conn._reconnect_task is not old
        assert not conn.connected
        assert "reconnected" not in statuses
        assert len(calls) == 2
        if late_result == "socket":
            assert writer.closed
    finally:
        await conn.close()


async def test_interval_changes_only_subsequent_waits(monkeypatch):
    async def open_connection(*args, **kwargs):
        raise OSError("unavailable")

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    with pytest.raises(OSError):
        await conn.connect("mud.example", 23)
    conn.start_reconnect()
    wake = await sleeps.next(5)
    old = conn._reconnect_task
    conn.configure_reconnect(True, 300)
    try:
        assert conn._reconnect_task is old and not wake.done()
        wake.set_result(None)
        await sleeps.next(300)
        assert statuses == ["connection unavailable; reconnecting every 5s",
                            "connection unavailable; reconnecting every 300s"]
    finally:
        await conn.close()


async def test_off_keeps_live_socket_and_on_recovers_a_later_drop(monkeypatch):
    reader = asyncio.StreamReader()

    async def open_connection(*args, **kwargs):
        return reader, _ClosingWriter()

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    await conn.connect("mud.example", 23)
    conn.configure_reconnect(True, 300)
    assert conn.connected and conn._reconnect_task is None
    conn.configure_reconnect(False, 5)
    assert conn.connected and conn._reconnect_task is None
    reader.feed_eof()
    await conn._read_task
    assert not conn.connecting and statuses == ["disconnected"]
    conn.configure_reconnect(True, 5)
    try:
        await sleeps.next(5)
        assert statuses[-1] == "connection lost; reconnecting every 5s"
    finally:
        await conn.close()


@pytest.mark.parametrize("stop", ["close", "quit", "goodbye", "protocol"])
async def test_settings_cannot_revive_a_closed_session(monkeypatch, stop):
    reader = asyncio.StreamReader()

    async def open_connection(*args, **kwargs):
        return reader, _ClosingWriter()

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    await conn.connect("mud.example", 23)
    if stop == "close":
        await conn.close()
    else:
        if stop == "quit":
            conn.send_line("quit")
        elif stop == "goodbye":
            conn.suppress_reconnect()
        else:
            # MCCP activated, then an invalid zlib stream: the real parser rejects it.
            reader.feed_data(bytes([IAC, WILL, OPT_MCCP2, IAC, SB, OPT_MCCP2, IAC, SE])
                             + b"not zlib")
        reader.feed_eof()
        await conn._read_task
    if stop in ("quit", "goodbye"):
        assert statuses == ["disconnected"]  # retain the normal terminal announcement/hooks
    elif stop == "protocol":
        assert len(statuses) == 1 and statuses[0].startswith("protocol error, disconnected")
    else:
        assert statuses == []  # a UI close has its own announcement
    before = list(statuses)
    conn.configure_reconnect(False, 5)
    conn.configure_reconnect(True, 5)
    assert not conn.connected and not conn.connecting
    assert sleeps.waits.empty() and statuses == before


async def test_next_interval_starts_only_after_the_pending_attempt_finishes(monkeypatch):
    pending = asyncio.get_running_loop().create_future()
    connecting = asyncio.Event()
    calls = []

    async def open_connection(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("unavailable")
        connecting.set()
        return await pending

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    with pytest.raises(OSError):
        await conn.connect("mud.example", 23)
    conn.start_reconnect()
    wake = await sleeps.next(5)
    wake.set_result(None)
    await asyncio.wait_for(connecting.wait(), 1)
    conn.configure_reconnect(True, 1)
    try:
        assert len(calls) == 2 and sleeps.waits.empty()
        assert not conn.start_reconnect()
        pending.set_exception(OSError("attempt finished"))
        await sleeps.next(1)
        assert len(calls) == 2
    finally:
        await conn.close()


@pytest.mark.parametrize("interval", [0, 301, True, 5.0, "5"])
def test_invalid_runtime_interval_is_rejected(interval):
    with pytest.raises(ValueError, match="1 to 300"):
        MudConnection().configure_reconnect(True, interval)


async def test_reenable_cannot_overlap_a_socket_whose_reader_has_not_stopped(monkeypatch):
    writer = _ClosingWriter()

    async def open_connection(*args, **kwargs):
        return _IdleReader(), writer

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    await conn.connect("mud.example", 23)
    writer.close()  # TCP is closing but the old read task hasn't processed the EOF yet
    try:
        assert not conn.connected and conn.connecting
        conn.configure_reconnect(True, 5)
        assert conn._reconnect_task is None
        assert sleeps.waits.empty()
    finally:
        await conn.close()


async def test_switching_off_does_not_cancel_the_initial_one_shot_attempt(monkeypatch):
    pending = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def open_connection(*args, **kwargs):
        started.set()
        return await pending

    conn, sleeps, statuses = setup(monkeypatch, open_connection)
    initial = asyncio.create_task(conn.connect("mud.example", 23))
    await asyncio.wait_for(started.wait(), 1)
    conn.configure_reconnect(False, 5)
    try:
        assert not initial.done() and not pending.cancelled()
        pending.set_result((_IdleReader(), _ClosingWriter()))
        await asyncio.wait_for(initial, 1)
        assert conn.connected and not conn.auto_reconnect
        assert conn._reconnect_task is None
    finally:
        await conn.close()
