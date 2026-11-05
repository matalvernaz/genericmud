"""GMCP (Generic Mud Communication Protocol) message parsing.

GMCP rides telnet option 201. Each subnegotiation payload is a package name
followed by an optional JSON document, e.g. ``Char.Vitals {"hp":42}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GmcpMessage:
    package: str
    data: Any  # parsed JSON (dict/list/scalar) or None when the package has no body


# The package name ends at the first space, or at a line break when one comes first: a few
# servers put the JSON on the next line ("Char.Vitals\n{...}").
_PACKAGE_END = re.compile(r"[ \t\r\n]")


def _utf8(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def parse_gmcp(payload: bytes, decode: Callable[[bytes], str] = _utf8) -> GmcpMessage:
    """Split a payload into package and JSON body. ``decode`` turns the bytes into text:
    GMCP is UTF-8 by spec, but the session can supply a fallback for servers that aren't."""
    # A byte order mark is not part of the package name, though some servers send one.
    text = decode(payload).removeprefix("\ufeff")
    split = _PACKAGE_END.search(text)
    if split is None:
        return GmcpMessage(text.strip(), None)
    package = text[: split.start()].strip()
    body = text[split.end() :].strip()
    if not body:
        return GmcpMessage(package, None)
    try:
        # strict=False lets control characters stand inside strings: IRE games send ANSI colour
        # escapes inside Comm.Channel.Text, and strict JSON refused the whole message, so none
        # of its fields could be read.
        return GmcpMessage(package, json.loads(body, strict=False))
    except json.JSONDecodeError:
        # Tolerate non-conforming servers: keep the raw body rather than drop it.
        return GmcpMessage(package, body)
