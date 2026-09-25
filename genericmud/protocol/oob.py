"""Out-of-band message normalization.

GMCP and MSDP carry the same kind of structured server data through different
wire formats. This collapses both into a uniform ``OobMessage`` stream so that
triggers, scripts, status gauges, and the UI subscribe to one shape regardless
of which protocol a given server speaks. MSSP normalizes to ``ServerStatus``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from genericmud.protocol import telnet as T
from genericmud.protocol.gmcp import parse_gmcp
from genericmud.protocol.msdp import parse_msdp
from genericmud.protocol.mssp import parse_mssp


@dataclass(frozen=True)
class OobMessage:
    name: str  # GMCP package ("Char.Vitals") or MSDP variable ("HEALTH")
    value: Any
    source: str  # "gmcp" or "msdp"


@dataclass(frozen=True)
class ServerStatus:
    data: dict[str, str | list[str]]


def _utf8(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def from_subnegotiation(
    option: int,
    payload: bytes,
    decode: Callable[[bytes], str] = _utf8,
    decode_gmcp: Callable[[bytes], str] = _utf8,
) -> list[OobMessage] | ServerStatus | None:
    """Normalize a telnet subnegotiation into out-of-band messages.

    Returns a list of :class:`OobMessage` for GMCP/MSDP (GMCP yields one,
    MSDP yields one per top-level variable), a :class:`ServerStatus` for MSSP,
    or ``None`` for options handled elsewhere. ``decode`` turns MSDP and MSSP text into
    ``str`` and ``decode_gmcp`` GMCP's, which differ because only GMCP fixes an encoding
    (see ``ServerTextCodec.decode_payload`` and ``decode_gmcp``).
    """
    if option == T.OPT_GMCP:
        message = parse_gmcp(payload, decode_gmcp)
        return [OobMessage(message.package, message.data, "gmcp")]
    if option == T.OPT_MSDP:
        return [
            OobMessage(name, value, "msdp") for name, value in parse_msdp(payload, decode).items()
        ]
    if option == T.OPT_MSSP:
        return ServerStatus(parse_mssp(payload, decode))
    return None
