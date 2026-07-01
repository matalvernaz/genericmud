"""The read loop must contain hostile-payload faults, not die or reconnect-storm on them."""

from __future__ import annotations

import zlib

from genericmud.protocol.telnet import DataReceived
from genericmud.transport.connection import MudConnection


class _FakeReader:
    """An asyncio.StreamReader stand-in: yields queued chunks, then EOF (b'')."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def _mccp_bomb() -> bytes:
    comp = zlib.compressobj()
    return comp.compress(b"\x00" * (5 * 1024 * 1024)) + comp.flush()


async def test_protocol_error_disconnects_without_reconnect():
    conn = MudConnection()
    statuses: list[str] = []
    conn.on_status = statuses.append
    conn.auto_reconnect = True  # even with reconnect ON, a parse bomb must NOT reconnect
    conn._target = ("host", 1, False, None)
    conn._parser.mccp.activate()  # arm MCCP so the bomb is decompressed
    conn._reader = _FakeReader([_mccp_bomb()])

    await conn._read_loop()

    assert any("protocol error" in s for s in statuses)
    assert not any("reconnect" in s for s in statuses)


async def test_clean_eof_reports_disconnect_when_reconnect_off():
    conn = MudConnection()
    statuses: list[str] = []
    conn.on_status = statuses.append
    conn.auto_reconnect = False
    conn._reader = _FakeReader([b"hello\r\n"])

    await conn._read_loop()

    assert "disconnected" in statuses


async def test_consumer_fault_is_contained_and_connection_survives():
    calls = {"n": 0}

    def on_event(_event):
        calls["n"] += 1
        raise RuntimeError("consumer blew up")

    conn = MudConnection(on_event=on_event)
    statuses: list[str] = []
    conn.on_status = statuses.append
    conn.auto_reconnect = False
    # Two data chunks: the consumer raises on each, but the loop must keep going to EOF.
    conn._reader = _FakeReader([b"line one\r\n", b"line two\r\n"])

    await conn._read_loop()

    assert calls["n"] == 2  # both events were dispatched despite the faults
    assert sum("error handling server data" in s for s in statuses) == 1  # surfaced once only
    assert "disconnected" in statuses  # clean shutdown, not a crash


async def test_normal_data_dispatches_events():
    events: list[object] = []
    conn = MudConnection(on_event=events.append)
    conn.on_status = lambda _s: None
    conn._reader = _FakeReader([b"You see a dragon\r\n"])

    await conn._read_loop()

    assert any(isinstance(e, DataReceived) for e in events)
