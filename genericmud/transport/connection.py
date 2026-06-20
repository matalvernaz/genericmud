"""Asyncio MUD transport: TCP/TLS socket + telnet option negotiation.

Owns the network read loop, feeds bytes through :class:`TelnetParser`, applies a
minimal-but-correct option-negotiation policy (the bits needed to bring up
MCCP/GMCP/MSDP/MSSP/MXP/TTYPE/NAWS against a real MUD), and forwards every
parsed event to a consumer callback. TLS is just an ``ssl`` context passed to
``asyncio.open_connection`` — transparent to everything above this layer.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable

from genericmud.protocol import telnet as T
from genericmud.protocol.telnet import (
    Event,
    Negotiation,
    Subnegotiation,
    TelnetParser,
)

_READ_CHUNK = 4096
TERMINAL_TYPE = b"GENERICMUD"
DEFAULT_COLUMNS = 120
DEFAULT_ROWS = 40
_TTYPE_SEND = 1  # IAC SB TTYPE SEND ... — server requests our terminal type
_TTYPE_IS = 0  # IAC SB TTYPE IS <name> IAC SE — our reply

# Remote (server-side WILL) options we accept by replying DO.
_ACCEPT_REMOTE = frozenset(
    {T.OPT_MCCP2, T.OPT_GMCP, T.OPT_MSDP, T.OPT_MSSP, T.OPT_MXP, T.OPT_EOR, T.OPT_SGA}
)
# Local (our-side) options we offer by replying WILL when the server says DO.
_ENABLE_LOCAL = frozenset({T.OPT_TTYPE, T.OPT_NAWS})


class MudConnection:
    """A single live connection to a MUD server.

    Pass ``on_event`` to receive every :class:`Event` in stream order. Option
    negotiation is handled internally before forwarding, so consumers see a
    clean data/GMCP/MSDP/MXP/MSP event stream.
    """

    def __init__(self, on_event: Callable[[Event], None] | None = None) -> None:
        self._on_event = on_event
        self._parser = TelnetParser()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._remote_enabled: set[int] = set()
        self._local_enabled: set[int] = set()

    @property
    def parser(self) -> TelnetParser:
        return self._parser

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(
        self,
        host: str,
        port: int,
        *,
        tls: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        ctx: ssl.SSLContext | None = None
        if tls or ssl_context is not None:
            ctx = ssl_context or ssl.create_default_context()
        self._reader, self._writer = await asyncio.open_connection(host, port, ssl=ctx)
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                raw = await self._reader.read(_READ_CHUNK)
                if not raw:
                    break
                for event in self._parser.receive(raw):
                    self._dispatch(event)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            await self.close()

    def _dispatch(self, event: Event) -> None:
        if isinstance(event, Negotiation):
            self._handle_negotiation(event)
        elif isinstance(event, Subnegotiation):
            self._handle_subnegotiation(event)
        if self._on_event is not None:
            self._on_event(event)

    def _handle_negotiation(self, neg: Negotiation) -> None:
        cmd, opt = neg.command, neg.option
        if cmd == T.WILL:
            if opt in _ACCEPT_REMOTE and opt not in self._remote_enabled:
                self._remote_enabled.add(opt)
                self._send_command(T.DO, opt)
                if opt == T.OPT_GMCP:
                    self._send_gmcp_hello()
            elif opt not in _ACCEPT_REMOTE:
                self._send_command(T.DONT, opt)
        elif cmd == T.DO:
            if opt in _ENABLE_LOCAL and opt not in self._local_enabled:
                self._local_enabled.add(opt)
                self._send_command(T.WILL, opt)
                if opt == T.OPT_NAWS:
                    self._send_naws(DEFAULT_COLUMNS, DEFAULT_ROWS)
            elif opt not in _ENABLE_LOCAL:
                self._send_command(T.WONT, opt)
        elif cmd == T.WONT:
            self._remote_enabled.discard(opt)
        elif cmd == T.DONT:
            self._local_enabled.discard(opt)

    def _handle_subnegotiation(self, sub: Subnegotiation) -> None:
        # The server asks for our terminal type; reply with a single TTYPE IS.
        if sub.option == T.OPT_TTYPE and sub.payload[:1] == bytes([_TTYPE_SEND]):
            self.send_subnegotiation(T.OPT_TTYPE, bytes([_TTYPE_IS]) + TERMINAL_TYPE)

    # --- outbound helpers ---

    def _raw_write(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("not connected")
        self._writer.write(data)

    def _send_command(self, command: int, option: int) -> None:
        self._raw_write(bytes([T.IAC, command, option]))

    def send_subnegotiation(self, option: int, payload: bytes) -> None:
        """Send ``IAC SB <option> <payload> IAC SE`` with payload IAC-escaped."""
        escaped = payload.replace(bytes([T.IAC]), bytes([T.IAC, T.IAC]))
        self._raw_write(bytes([T.IAC, T.SB, option]) + escaped + bytes([T.IAC, T.SE]))

    def _send_gmcp_hello(self) -> None:
        self.send_subnegotiation(
            T.OPT_GMCP, b'Core.Hello {"client":"genericMud","version":"0.1"}'
        )

    def _send_naws(self, columns: int, rows: int) -> None:
        payload = bytes([columns >> 8, columns & 0xFF, rows >> 8, rows & 0xFF])
        self.send_subnegotiation(T.OPT_NAWS, payload)

    def send_line(self, text: str) -> None:
        """Send a user command line (CRLF-terminated, IAC-escaped)."""
        data = text.encode("utf-8").replace(bytes([T.IAC]), bytes([T.IAC, T.IAC]))
        self._raw_write(data + b"\r\n")

    async def close(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            self._writer.close()
        self._writer = None
        self._reader = None
