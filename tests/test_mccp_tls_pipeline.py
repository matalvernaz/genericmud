"""Spike #2: TLS/MCCP/telnet byte-pipeline robustness.

TLS is transparent to the telnet layer (the stdlib ssl transport hands us
already-decrypted bytes), so the parser-level risk is the MCCP toggle and TCP
fragmentation. This proves the decoded application data and the ordered control
events are identical regardless of how the stream is chopped — including splits
inside the zlib region and inside the ``IAC SB MCCP2 IAC SE`` marker.
"""

from __future__ import annotations

import zlib

from genericmud.protocol import telnet as T


def build_stream() -> tuple[bytes, bytes, bytes]:
    """Return (full_wire, expected_app_data, gmcp_payload).

    Layout: clear text + GA prompt marker + WILL GMCP + MCCP2-start marker,
    then a zlib-compressed region carrying room text, a GMCP subnegotiation,
    and an escaped IAC (0xFF 0xFF -> one 0xFF in app data).
    """
    pre = b"Welcome to the MUD\r\n"
    ga = bytes([T.IAC, T.GA])
    will_gmcp = bytes([T.IAC, T.WILL, T.OPT_GMCP])
    mccp_start = bytes([T.IAC, T.SB, T.OPT_MCCP2, T.IAC, T.SE])

    gmcp_payload = b'Char.Vitals {"hp":42}'
    inner_wire = (
        b"You see a room.\r\n"
        + bytes([T.IAC, T.SB, T.OPT_GMCP])
        + gmcp_payload
        + bytes([T.IAC, T.SE])
        + b"After GMCP"
        + bytes([T.IAC, T.IAC])  # escaped IAC -> single 0xFF
        + b" done\r\n"
    )
    full = pre + ga + will_gmcp + mccp_start + zlib.compress(inner_wire)
    expected_app = pre + b"You see a room.\r\n" + b"After GMCP" + bytes([T.IAC]) + b" done\r\n"
    return full, expected_app, gmcp_payload


def canonical(events: list[T.Event]) -> list[T.Event]:
    """Merge consecutive DataReceived so fragmentation doesn't change the form."""
    out: list[T.Event] = []
    for e in events:
        if isinstance(e, T.DataReceived) and out and isinstance(out[-1], T.DataReceived):
            out[-1] = T.DataReceived(out[-1].data + e.data)
        else:
            out.append(e)
    return out


def app_data(events: list[T.Event]) -> bytes:
    return b"".join(e.data for e in events if isinstance(e, T.DataReceived))


def test_reference_decode_is_correct():
    full, expected_app, gmcp_payload = build_stream()
    parser = T.TelnetParser()
    events = canonical(parser.receive(full))

    assert app_data(events) == expected_app
    assert parser.mccp.active is True
    assert T.Command(T.GA) in events
    assert T.Negotiation(T.WILL, T.OPT_GMCP) in events
    assert T.Subnegotiation(T.OPT_MCCP2, b"") in events
    assert T.Subnegotiation(T.OPT_GMCP, gmcp_payload) in events


def test_two_way_split_at_every_boundary():
    full, _, _ = build_stream()
    reference = canonical(T.TelnetParser().receive(full))
    for k in range(len(full) + 1):
        parser = T.TelnetParser()
        events = parser.receive(full[:k]) + parser.receive(full[k:])
        assert canonical(events) == reference, f"mismatch splitting at byte {k}"


def test_byte_by_byte_fragmentation():
    full, _, _ = build_stream()
    reference = canonical(T.TelnetParser().receive(full))
    parser = T.TelnetParser()
    events: list[T.Event] = []
    for i in range(len(full)):
        events.extend(parser.receive(full[i : i + 1]))
    assert canonical(events) == reference
    assert parser.mccp.active is True


def test_escaped_iac_in_plain_data():
    parser = T.TelnetParser()
    events = parser.receive(b"a" + bytes([T.IAC, T.IAC]) + b"b")
    assert app_data(events) == b"a" + bytes([T.IAC]) + b"b"


def test_standalone_eor_prompt_marker():
    parser = T.TelnetParser()
    events = canonical(parser.receive(b"HP:100>" + bytes([T.IAC, T.EOR])))
    assert events == [T.DataReceived(b"HP:100>"), T.Command(T.EOR)]
