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
import time
from collections.abc import Callable
from dataclasses import dataclass

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
# MXP is intentionally omitted: we don't parse it yet, so advertising DO MXP just
# makes servers emit MXP markup that leaks into the output. Re-add when parsed.
_ACCEPT_REMOTE = frozenset(
    {T.OPT_MCCP2, T.OPT_GMCP, T.OPT_MSDP, T.OPT_MSSP, T.OPT_EOR, T.OPT_SGA}
)
# Local (our-side) options we offer by replying WILL when the server says DO.
_ENABLE_LOCAL = frozenset({T.OPT_TTYPE, T.OPT_NAWS})

# A server-initiated close (the read loop hits EOF) is indistinguishable from a network
# drop, so a logout the player asked for would otherwise auto-reconnect. We treat the link
# as deliberately closed when one of these commands was sent just before it dropped.
DEFAULT_QUIT_COMMANDS = frozenset({"quit", "qq", "logout", "rent"})
_QUIT_GRACE_SECONDS = 30.0  # a quit older than this no longer suppresses reconnect


@dataclass
class ReconnectPolicy:
    """Exponential backoff schedule for auto-reconnect."""

    base_delay: float = 1.0
    max_delay: float = 30.0
    max_attempts: int = 6  # 0 = retry forever

    def delay_for(self, attempt: int) -> float | None:
        """Seconds to wait before ``attempt`` (1-based), or None once exhausted."""
        if self.max_attempts and attempt > self.max_attempts:
            return None
        return min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))


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
        # Auto-reconnect (off by default; the UI enables it and wires on_status).
        self.auto_reconnect = False
        self.reconnect_policy = ReconnectPolicy()
        self.on_status: Callable[[str], None] | None = None
        self._closing = False  # True only on a deliberate disconnect (suppresses reconnect)
        # Quit commands seen on this connection suppress reconnect on the close that follows.
        self.quit_commands = set(DEFAULT_QUIT_COMMANDS)
        self._quit_sent_at: float | None = None  # monotonic time of the last quit command
        self._target: tuple[str, int, bool, ssl.SSLContext | None] | None = None
        self._dispatch_fault_seen = False  # speak the first consumer fault, then stay quiet

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
        self._closing = False
        self._quit_sent_at = None  # a prior session's quit must not suppress this one's drops
        self._target = (host, port, tls, ssl_context)
        ctx: ssl.SSLContext | None = None
        if tls or ssl_context is not None:
            ctx = ssl_context or ssl.create_default_context()
        self._reader, self._writer = await asyncio.open_connection(host, port, ssl=ctx)
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader is not None
        protocol_error: str | None = None
        try:
            while True:
                raw = await self._reader.read(_READ_CHUNK)
                if not raw:
                    break
                try:
                    events = self._parser.receive(raw)
                except Exception as exc:  # noqa: BLE001 - hostile/malformed stream (bomb, no-SE flood)
                    # A parse failure means the parser state is now unreliable AND reconnecting
                    # would hit the same bytes, so stop and DON'T auto-reconnect. Tell the user --
                    # for a blind user, silence here is worse than the disconnect itself.
                    protocol_error = f"{type(exc).__name__}: {exc}"
                    break
                for event in events:
                    try:
                        self._dispatch(event)
                    except Exception as exc:  # noqa: BLE001 - one bad event mustn't drop the session
                        # Consumer/parser-of-payload fault (e.g. a malformed OOB payload). Keep the
                        # connection up and surface it once, rather than dying or spamming.
                        if not self._dispatch_fault_seen:
                            self._dispatch_fault_seen = True
                            self._status(f"error handling server data: {type(exc).__name__}")
        except (OSError, asyncio.IncompleteReadError):
            pass  # a network drop (TimeoutError/ConnectionReset are OSErrors): reconnect below
        finally:
            self._teardown()
            if protocol_error is not None:
                self._status(f"protocol error, disconnected: {protocol_error}")
            elif self._should_reconnect():
                asyncio.create_task(self._reconnect_loop())  # noqa: RUF006 - fire-and-forget
            elif not self._closing:
                # A drop we won't reconnect (quit/auto_reconnect off); confirm it to the user.
                # A user-initiated close() stays quiet -- the UI already announced it.
                self._status("disconnected")

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
        if text.strip().lower() in self.quit_commands:
            self._quit_sent_at = time.monotonic()  # the close that follows is intentional

    async def close(self) -> None:
        """Deliberately disconnect; this suppresses auto-reconnect."""
        self._closing = True
        if self._read_task is not None:
            self._read_task.cancel()
        self._teardown()

    def _teardown(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            self._writer.close()
        self._writer = None
        self._reader = None

    def _should_reconnect(self) -> bool:
        if not self.auto_reconnect or self._closing:
            return False
        if self._quit_sent_at is not None:
            return time.monotonic() - self._quit_sent_at > _QUIT_GRACE_SECONDS
        return True

    def _status(self, message: str) -> None:
        if self.on_status is not None:
            self.on_status(message)

    async def _reconnect_loop(self) -> None:
        assert self._target is not None
        host, port, tls, ssl_context = self._target
        attempt = 1
        while True:
            delay = self.reconnect_policy.delay_for(attempt)
            if delay is None:
                self._status("reconnect failed; giving up")
                return
            self._status(f"connection lost; reconnecting in {int(delay)}s (attempt {attempt})")
            await asyncio.sleep(delay)
            if self._closing:  # user disconnected during the backoff wait
                return
            try:
                await self.connect(host, port, tls=tls, ssl_context=ssl_context)
            except OSError:
                attempt += 1
                continue
            self._status("reconnected")
            return
