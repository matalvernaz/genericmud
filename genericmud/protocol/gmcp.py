"""GMCP (Generic Mud Communication Protocol) message parsing.

GMCP rides telnet option 201. Each subnegotiation payload is a package name
followed by an optional JSON document, e.g. ``Char.Vitals {"hp":42}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GmcpMessage:
    package: str
    data: Any  # parsed JSON (dict/list/scalar) or None when the package has no body


def parse_gmcp(payload: bytes) -> GmcpMessage:
    text = payload.decode("utf-8", "replace")
    split = text.find(" ")
    if split == -1:
        return GmcpMessage(text.strip(), None)
    package = text[:split].strip()
    body = text[split + 1 :].strip()
    if not body:
        return GmcpMessage(package, None)
    try:
        return GmcpMessage(package, json.loads(body))
    except json.JSONDecodeError:
        # Tolerate non-conforming servers: keep the raw body rather than drop it.
        return GmcpMessage(package, body)
