"""MCCP (Mud Client Compression Protocol) stream decompression.

MCCP v2/v3 compress the server->client byte stream with raw zlib once the
option is negotiated. Compression begins immediately after the server sends
the ``IAC SB MCCP2 IAC SE`` marker, so activation happens mid-stream. This
module is a pure codec: it knows how to inflate, not *when* to start. The
telnet layer owns the "when" because the start boundary is defined by a telnet
subnegotiation (see :mod:`genericmud.protocol.telnet`).
"""

from __future__ import annotations

import zlib


class MCCPState:
    """Streaming zlib inflate for the MCCP server->client channel.

    Inactive by default and passes bytes through untouched. Once
    :meth:`activate` is called, every subsequent :meth:`decompress` call
    inflates against a persistent zlib stream, so it tolerates the
    compressed payload being split across arbitrary TCP reads.
    """

    def __init__(self) -> None:
        self._decompressor: zlib._Decompress | None = None

    @property
    def active(self) -> bool:
        return self._decompressor is not None

    def activate(self) -> None:
        """Begin decompression. Subsequent bytes are treated as a zlib stream."""
        if self._decompressor is None:
            self._decompressor = zlib.decompressobj()

    def decompress(self, data: bytes) -> bytes:
        """Return ``data`` inflated when active, else unchanged.

        Safe to call with partial input; the underlying zlib stream buffers
        across calls.
        """
        if self._decompressor is None:
            return data
        return self._decompressor.decompress(data)
