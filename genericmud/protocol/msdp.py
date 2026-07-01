"""MSDP (Mud Server Data Protocol) parsing.

MSDP rides telnet option 69. The subnegotiation payload is a byte grammar of
VAR/VAL pairs whose values may be plain strings, nested tables, or arrays:

    VAR <name> VAL <value>
    <value> := <string> | TABLE_OPEN <var/val...> TABLE_CLOSE
                        | ARRAY_OPEN (VAL <value>)... ARRAY_CLOSE

``aiomudtelnet`` omits MSDP, so this is ours. Output is a plain dict mirroring
the nesting; scalars are strings (MSDP is untyped on the wire).
"""

from __future__ import annotations

from typing import Any

MSDP_VAR = 1
MSDP_VAL = 2
MSDP_TABLE_OPEN = 3
MSDP_TABLE_CLOSE = 4
MSDP_ARRAY_OPEN = 5
MSDP_ARRAY_CLOSE = 6

# Tables and arrays nest via mutual recursion. A hostile server can send thousands of nested
# MSDP_TABLE_OPEN bytes; without a bound that is a RecursionError on the read-loop thread. Real
# MSDP is a shallow gauge/room structure, so cap the depth and stop recursing past it (the
# remaining bytes are still consumed iteratively -- best-effort data, never a crash).
_MAX_DEPTH = 32

_CONTROL = bytes(
    [MSDP_VAR, MSDP_VAL, MSDP_TABLE_OPEN, MSDP_TABLE_CLOSE, MSDP_ARRAY_OPEN, MSDP_ARRAY_CLOSE]
)


def parse_msdp(payload: bytes) -> dict[str, Any]:
    return _MsdpParser(payload).parse_table_body(top=True)


def _too_deep(depth: int) -> bool:
    return depth >= _MAX_DEPTH


class _MsdpParser:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._i = 0
        self._n = len(data)

    def _peek(self) -> int | None:
        return self._data[self._i] if self._i < self._n else None

    def parse_table_body(self, top: bool = False, depth: int = 0) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while self._i < self._n:
            b = self._data[self._i]
            if b == MSDP_TABLE_CLOSE:
                if not top:
                    self._i += 1
                return out
            if b == MSDP_VAR:
                self._i += 1
                name = self._read_string()
                if self._peek() == MSDP_VAL:
                    self._i += 1
                    out[name] = self._read_value(depth)
            else:
                self._i += 1  # skip stray control byte
        return out

    def _read_string(self) -> str:
        start = self._i
        while self._i < self._n and self._data[self._i] not in _CONTROL:
            self._i += 1
        return self._data[start : self._i].decode("utf-8", "replace")

    def _read_value(self, depth: int) -> Any:
        b = self._peek()
        if b == MSDP_TABLE_OPEN:
            self._i += 1
            if _too_deep(depth):
                return ""  # too deep: stop recursing; the bytes are still consumed iteratively
            return self.parse_table_body(depth=depth + 1)
        if b == MSDP_ARRAY_OPEN:
            self._i += 1
            if _too_deep(depth):
                return ""
            return self._read_array(depth + 1)
        return self._read_string()

    def _read_array(self, depth: int) -> list[Any]:
        arr: list[Any] = []
        while self._i < self._n:
            b = self._data[self._i]
            if b == MSDP_ARRAY_CLOSE:
                self._i += 1
                return arr
            if b == MSDP_VAL:
                self._i += 1
                arr.append(self._read_value(depth))
            else:
                self._i += 1
        return arr
