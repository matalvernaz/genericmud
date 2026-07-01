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

# A MUD read is a few KB; even a busy screen inflates to well under this. A tiny compressed
# chunk that inflates past it is a decompression bomb, not real output, so we refuse it rather
# than let the server drive unbounded memory allocation on the read-loop thread.
_MAX_DECOMPRESSED_PER_READ = 4 * 1024 * 1024  # 4 MiB


class MCCPError(Exception):
    """The MCCP stream is malformed or inflates past the per-read cap (a zlib bomb)."""


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

        Safe to call with partial input; the underlying zlib stream buffers across calls.
        Bounded: output is capped per call, so a decompression bomb (a few KB inflating to
        gigabytes) raises :class:`MCCPError` instead of exhausting memory. A malformed stream
        raises the same, so the transport can close cleanly rather than die with a bare
        ``zlib.error`` on the read-loop task.
        """
        if self._decompressor is None:
            return data
        try:
            out = self._decompressor.decompress(data, _MAX_DECOMPRESSED_PER_READ)
        except zlib.error as exc:
            raise MCCPError(f"malformed MCCP stream: {exc}") from exc
        if self._decompressor.unconsumed_tail:
            # More than the cap came out of this one read: real MUD output never does this.
            raise MCCPError("MCCP stream inflated past the per-read cap (possible zlib bomb)")
        return out
