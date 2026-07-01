"""Hostile-input hardening for the transport/protocol parsers (untrusted server bytes)."""

from __future__ import annotations

import zlib

import pytest

from genericmud.protocol.msdp import (
    MSDP_TABLE_OPEN,
    MSDP_VAL,
    MSDP_VAR,
    parse_msdp,
)
from genericmud.protocol.telnet import (
    IAC,
    OPT_GMCP,
    SB,
    SE,
    Subnegotiation,
    TelnetParser,
    TelnetProtocolError,
)
from genericmud.transport.mccp import MCCPError, MCCPState

# --- MCCP decompression bomb (#5) ---


def test_mccp_normal_roundtrip():
    comp = zlib.compressobj()
    payload = comp.compress(b"hello world") + comp.flush()
    state = MCCPState()
    state.activate()
    assert state.decompress(payload) == b"hello world"


def test_mccp_rejects_decompression_bomb():
    comp = zlib.compressobj()
    # ~5 MiB of zeros compresses to a few KB but inflates past the per-read cap.
    payload = comp.compress(b"\x00" * (5 * 1024 * 1024)) + comp.flush()
    state = MCCPState()
    state.activate()
    with pytest.raises(MCCPError):
        state.decompress(payload)


def test_mccp_rejects_malformed_stream():
    state = MCCPState()
    state.activate()
    with pytest.raises(MCCPError):
        state.decompress(b"\xff\xff\xff\xffnot a zlib stream")


# --- Telnet subnegotiation flood (#6) ---


def test_telnet_subnegotiation_normal():
    parser = TelnetParser()
    events = parser.receive(bytes([IAC, SB, OPT_GMCP]) + b"Core.Ping" + bytes([IAC, SE]))
    subs = [e for e in events if isinstance(e, Subnegotiation)]
    assert subs and subs[0].payload == b"Core.Ping"


def test_telnet_subnegotiation_flood_without_se_is_rejected():
    parser = TelnetParser()
    parser.receive(bytes([IAC, SB, OPT_GMCP]))  # open SB, never send IAC SE
    with pytest.raises(TelnetProtocolError):
        for _ in range(20):  # > 1 MiB of payload
            parser.receive(b"A" * 100_000)


# --- MSDP nesting (#12) ---


def test_msdp_simple_pair():
    payload = bytes([MSDP_VAR]) + b"HP" + bytes([MSDP_VAL]) + b"42"
    assert parse_msdp(payload) == {"HP": "42"}


def test_msdp_deep_nesting_does_not_recurse_to_death():
    # VAR a VAL TABLE_OPEN, repeated: each TABLE_OPEN is the value of a VAR, so it recurses one
    # level per repetition. 5000 levels would blow the Python stack without the depth cap.
    nested = (bytes([MSDP_VAR]) + b"a" + bytes([MSDP_VAL]) + bytes([MSDP_TABLE_OPEN])) * 5000
    result = parse_msdp(nested)  # must not raise RecursionError
    assert isinstance(result, dict)
