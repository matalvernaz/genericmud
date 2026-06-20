"""Telnet IAC byte-state-machine.

Separates a raw server byte stream into application data and telnet control
events (option negotiation, subnegotiation, standalone commands such as the
GA/EOR prompt markers). Owns the MCCP decompression toggle because the
compression start boundary is defined by the ``IAC SB MCCP2 IAC SE``
subnegotiation — i.e. it is a telnet event.

The parser is intentionally transport-agnostic and synchronous: feed it bytes
with :meth:`receive`, get back an ordered list of events. ``MudConnection``
wraps it with asyncio sockets/TLS and an option-response policy.

References: RFC 854 (telnet), RFC 855 (options), MCCP2 spec (option 86).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from genericmud.transport.mccp import MCCPState

# --- Telnet command bytes (RFC 854) ---
IAC = 255  # Interpret As Command — escape byte
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250  # Subnegotiation Begin
SE = 240  # Subnegotiation End
GA = 249  # Go Ahead — prompt marker on some MUDs
EOR = 239  # End Of Record — prompt marker (paired with option TELOPT_EOR)
NOP = 241

NEGOTIATION_COMMANDS = frozenset({WILL, WONT, DO, DONT})

# --- Telnet option codes used by MUDs ---
OPT_BINARY = 0
OPT_ECHO = 1
OPT_SGA = 3  # Suppress Go Ahead
OPT_EOR = 25  # End Of Record negotiation (TELOPT_EOR)
OPT_TTYPE = 24  # Terminal Type / MTTS
OPT_NAWS = 31  # Negotiate About Window Size
OPT_CHARSET = 42
OPT_MSDP = 69  # Mud Server Data Protocol
OPT_MSSP = 70  # Mud Server Status Protocol
OPT_MCCP2 = 86  # Mud Client Compression Protocol v2
OPT_MCCP3 = 87  # Mud Client Compression Protocol v3
OPT_MXP = 91  # Mud eXtension Protocol
OPT_GMCP = 201  # Generic Mud Communication Protocol


@dataclass(frozen=True)
class DataReceived:
    """A run of application bytes (ANSI intact; line-splitting happens above)."""

    data: bytes


@dataclass(frozen=True)
class Negotiation:
    """An ``IAC WILL/WONT/DO/DONT <option>`` sequence."""

    command: int
    option: int


@dataclass(frozen=True)
class Subnegotiation:
    """An ``IAC SB <option> <payload> IAC SE`` sequence (payload IAC-unescaped)."""

    option: int
    payload: bytes


@dataclass(frozen=True)
class Command:
    """A standalone ``IAC <command>`` (e.g. GA/EOR prompt markers, NOP)."""

    command: int


Event = DataReceived | Negotiation | Subnegotiation | Command

# Parser states
_S_TEXT = 0
_S_IAC = 1
_S_NEG = 2  # after IAC + WILL/WONT/DO/DONT, awaiting option
_S_SB_OPT = 3  # after IAC SB, awaiting option
_S_SB_DATA = 4  # collecting subnegotiation payload
_S_SB_IAC = 5  # saw IAC inside subnegotiation payload


@dataclass
class TelnetParser:
    """Incremental telnet stream parser. State persists across ``receive`` calls."""

    mccp: MCCPState = field(default_factory=MCCPState)
    _state: int = _S_TEXT
    _neg_command: int = 0
    _sb_option: int = 0
    _sb_buffer: bytearray = field(default_factory=bytearray)
    _text: bytearray = field(default_factory=bytearray)

    def receive(self, raw: bytes) -> list[Event]:
        """Feed raw server bytes; return ordered events.

        Application data and control events are emitted in stream order so that
        prompt markers (GA/EOR) stay correctly positioned relative to the text
        they follow.
        """
        data = self.mccp.decompress(raw) if self.mccp.active else raw
        events: list[Event] = []
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            state = self._state

            if state == _S_TEXT:
                if b == IAC:
                    self._state = _S_IAC
                else:
                    self._text.append(b)

            elif state == _S_IAC:
                if b == IAC:
                    self._text.append(IAC)  # escaped 0xFF in data
                    self._state = _S_TEXT
                elif b in NEGOTIATION_COMMANDS:
                    self._neg_command = b
                    self._state = _S_NEG
                elif b == SB:
                    self._state = _S_SB_OPT
                else:
                    # Standalone command (GA, EOR, NOP, or unknown).
                    self._flush_text(events)
                    events.append(Command(b))
                    self._state = _S_TEXT

            elif state == _S_NEG:
                self._flush_text(events)
                events.append(Negotiation(self._neg_command, b))
                self._state = _S_TEXT

            elif state == _S_SB_OPT:
                self._sb_option = b
                self._sb_buffer.clear()
                self._state = _S_SB_DATA

            elif state == _S_SB_DATA:
                if b == IAC:
                    self._state = _S_SB_IAC
                else:
                    self._sb_buffer.append(b)

            elif state == _S_SB_IAC:
                if b == SE:
                    self._flush_text(events)
                    option = self._sb_option
                    events.append(Subnegotiation(option, bytes(self._sb_buffer)))
                    self._sb_buffer.clear()
                    self._state = _S_TEXT
                    if option in (OPT_MCCP2, OPT_MCCP3) and not self.mccp.active:
                        # Everything after SE in this chunk is compressed.
                        self.mccp.activate()
                        tail = data[i + 1 :]
                        data = self.mccp.decompress(tail)
                        i = 0
                        n = len(data)
                        continue
                elif b == IAC:
                    self._sb_buffer.append(IAC)  # escaped 0xFF inside payload
                    self._state = _S_SB_DATA
                else:
                    # Malformed (IAC X, X != SE/IAC) inside SB: be lenient, keep both.
                    self._sb_buffer.append(IAC)
                    self._sb_buffer.append(b)
                    self._state = _S_SB_DATA

            i += 1

        self._flush_text(events)
        return events

    def _flush_text(self, events: list[Event]) -> None:
        if self._text:
            events.append(DataReceived(bytes(self._text)))
            self._text.clear()
