"""Integration test for MudConnection over a real loopback socket.

Scripts a server that performs the WILL GMCP / WILL MCCP2 handshake and then
sends a zlib-compressed line, verifying the connection negotiates correctly
(replies DO + GMCP hello) and decodes post-MCCP data end to end.
"""

from __future__ import annotations

import asyncio
import zlib

from genericmud.protocol import telnet as T
from genericmud.transport.connection import MudConnection


async def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline_steps = int(timeout / 0.02)
    for _ in range(deadline_steps):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_loopback_handshake_and_mccp():
    received: list[T.Event] = []
    client_sent = bytearray()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def drain_client() -> None:
            try:
                while True:
                    chunk = await reader.read(1024)
                    if not chunk:
                        break
                    client_sent.extend(chunk)
            except (ConnectionResetError, asyncio.CancelledError):
                pass

        collector = asyncio.create_task(drain_client())
        writer.write(b"Hi\r\n")
        writer.write(bytes([T.IAC, T.WILL, T.OPT_GMCP]))
        writer.write(bytes([T.IAC, T.WILL, T.OPT_MCCP2]))
        await writer.drain()
        await asyncio.sleep(0.05)  # allow the client to reply DO before we compress
        writer.write(bytes([T.IAC, T.SB, T.OPT_MCCP2, T.IAC, T.SE]))
        writer.write(zlib.compress(b"Compressed room\r\n"))
        await writer.drain()
        await asyncio.sleep(0.05)
        collector.cancel()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    conn = MudConnection(on_event=received.append)
    await conn.connect("127.0.0.1", port)

    got = await _wait_for(
        lambda: any(
            isinstance(e, T.DataReceived) and b"Compressed room" in e.data for e in received
        )
    )
    await conn.close()
    server.close()
    await server.wait_closed()

    assert got, "never received the post-MCCP compressed line"
    app = b"".join(e.data for e in received if isinstance(e, T.DataReceived))
    assert b"Hi\r\n" in app
    assert b"Compressed room\r\n" in app
    assert conn.parser.mccp.active is True

    # The client must have accepted both options and sent its GMCP hello.
    assert bytes([T.IAC, T.DO, T.OPT_GMCP]) in client_sent
    assert bytes([T.IAC, T.DO, T.OPT_MCCP2]) in client_sent
    assert b"Core.Hello" in client_sent
