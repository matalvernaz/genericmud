"""Telnet option-negotiation policy: what the client accepts, and what it deliberately refuses."""

from __future__ import annotations

from genericmud import __version__
from genericmud.protocol import telnet as T
from genericmud.protocol.telnet import Event, Negotiation, Subnegotiation
from genericmud.transport.connection import (
    GMCP_SUPPORTS,
    MTTS_BITS,
    MTTS_SCREEN_READER,
    MudConnection,
)


class _FakeWriter:
    """An asyncio.StreamWriter stand-in that records what the client sends."""

    def __init__(self) -> None:
        self.sent = bytearray()

    def write(self, data: bytes) -> None:
        self.sent.extend(data)

    def is_closing(self) -> bool:
        return False


def _negotiate(*events: Event) -> bytes:
    """Drive the negotiation policy with server-side events; return the client's replies."""
    conn = MudConnection()
    writer = _FakeWriter()
    conn._writer = writer
    for event in events:
        conn._dispatch(event)
    return bytes(writer.sent)


def test_accepts_msp():
    # SMAUG derivatives and most spec-compliant servers hold back every !!SOUND/!!MUSIC
    # tag until the client answers DO, so refusing option 90 silently kills all MSP audio.
    assert bytes([T.IAC, T.DO, T.OPT_MSP]) in _negotiate(Negotiation(T.WILL, T.OPT_MSP))


def test_accepts_the_oob_options():
    sent = _negotiate(
        Negotiation(T.WILL, T.OPT_GMCP),
        Negotiation(T.WILL, T.OPT_MSDP),
        Negotiation(T.WILL, T.OPT_MSSP),
    )
    for option in (T.OPT_GMCP, T.OPT_MSDP, T.OPT_MSSP):
        assert bytes([T.IAC, T.DO, option]) in sent


def test_gmcp_handshake_subscribes_to_packages():
    # Core.Hello alone subscribes to nothing: spec-compliant servers send no GMCP at all
    # until Core.Supports.Set arrives, which leaves room tracking and packs with no data.
    sent = _negotiate(Negotiation(T.WILL, T.OPT_GMCP))
    assert b"Core.Hello" in sent
    assert __version__.encode() in sent  # not a frozen "0.1" that misreports the client
    assert b"Core.Supports.Set" in sent
    for package in GMCP_SUPPORTS:
        assert f'"{package}"'.encode() in sent


def test_ttype_answers_the_mtts_cycle():
    conn = MudConnection()
    writer = _FakeWriter()
    conn._writer = writer
    replies = []
    for _ in range(4):
        writer.sent.clear()
        conn._dispatch(Subnegotiation(T.OPT_TTYPE, bytes([1])))
        replies.append(bytes(writer.sent))
    assert b"GENERICMUD" in replies[0]
    assert b"ANSI" in replies[1]
    assert f"MTTS {MTTS_BITS}".encode() in replies[2]
    # Repeating the last entry is how MTTS signals the cycle is over.
    assert replies[3] == replies[2]


def test_dont_ttype_resets_the_cycle_so_a_hotboot_still_hears_screen_reader():
    # A copyover is announced with DONT TTYPE. If the cycle isn't reset, the first SEND after
    # the hotboot answers with the bitvector, the server records that as our client NAME, and
    # the screen-reader bit is never parsed -- so servers that strip ASCII art and progress
    # bars for us quietly stop, for the rest of the session, on the same TCP connection.
    conn = MudConnection()
    writer = _FakeWriter()
    conn._writer = writer
    for _ in range(4):  # exhaust the cycle, as a full pre-copyover handshake would
        conn._dispatch(Subnegotiation(T.OPT_TTYPE, bytes([1])))

    conn._dispatch(Negotiation(T.DONT, T.OPT_TTYPE))
    conn._dispatch(Negotiation(T.DO, T.OPT_TTYPE))
    writer.sent.clear()
    conn._dispatch(Subnegotiation(T.OPT_TTYPE, bytes([1])))

    assert b"GENERICMUD" in bytes(writer.sent)
    assert f"MTTS {MTTS_BITS}".encode() not in bytes(writer.sent)


def test_server_sb_mccp3_does_not_start_inflating_plaintext():
    # MCCP3 compresses the client's output, so the client sends SB 87; we never offer it.
    # Acting on a server's unsolicited SB 87 feeds plaintext to zlib, and the MCCPError that
    # follows ends the session with auto-reconnect deliberately suppressed.
    parser = T.TelnetParser()
    parser.receive(bytes([T.IAC, T.SB, T.OPT_MCCP3, T.IAC, T.SE]))
    assert not parser.mccp.active


def test_mtts_declares_a_screen_reader():
    # The bit MUDs read to suppress ASCII art, maps and progress bars. This client is
    # always driving a screen reader, so it is never conditional.
    assert MTTS_BITS & MTTS_SCREEN_READER


def test_refuses_mxp_until_it_is_parsed():
    sent = _negotiate(Negotiation(T.WILL, T.OPT_MXP))
    assert bytes([T.IAC, T.DONT, T.OPT_MXP]) in sent
    assert bytes([T.IAC, T.DO, T.OPT_MXP]) not in sent


def test_refuses_unknown_options():
    unknown = 137
    assert bytes([T.IAC, T.DONT, unknown]) in _negotiate(Negotiation(T.WILL, unknown))
