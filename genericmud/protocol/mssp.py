"""MSSP (Mud Server Status Protocol) parsing.

MSSP rides telnet option 70. The payload is flat VAR/VAL pairs (its own control
bytes, distinct from MSDP's). Repeated names collapse into a list. Used to read
a server's advertised name, player count, and supported features — handy for a
world directory.
"""

from __future__ import annotations

from collections.abc import Callable

MSSP_VAR = 1
MSSP_VAL = 2
_CONTROL = bytes([MSSP_VAR, MSSP_VAL])


def _utf8(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def parse_mssp(
    payload: bytes, decode: Callable[[bytes], str] = _utf8
) -> dict[str, str | list[str]]:
    """Parse an MSSP payload. ``decode`` turns each name and value into text; the MSSP
    spec names no encoding, so a legacy MUD's name can arrive in its own."""
    out: dict[str, str | list[str]] = {}
    i = 0
    n = len(payload)
    while i < n:
        if payload[i] != MSSP_VAR:
            i += 1
            continue
        i += 1
        name, i = _read_until_control(payload, i, n, decode)
        if i < n and payload[i] == MSSP_VAL:
            i += 1
            value, i = _read_until_control(payload, i, n, decode)
            _accumulate(out, name, value)
    return out


def _read_until_control(
    data: bytes, i: int, n: int, decode: Callable[[bytes], str]
) -> tuple[str, int]:
    start = i
    while i < n and data[i] not in _CONTROL:
        i += 1
    return decode(data[start:i]), i


def _accumulate(out: dict[str, str | list[str]], name: str, value: str) -> None:
    existing = out.get(name)
    if existing is None:
        out[name] = value
    elif isinstance(existing, list):
        existing.append(value)
    else:
        out[name] = [existing, value]
