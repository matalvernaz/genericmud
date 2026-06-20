"""The engine<->renderer message contract (localhost WebSocket, JSON).

These type strings and builders are the single source of truth for the protocol;
the frontend mirrors the same ``type`` values. Speech is NOT sent here — the
engine self-voices natively (NVDA/etc.); the renderer handles display, Web Audio
sounds, status, and input/keys.
"""

from __future__ import annotations

from typing import Any

# Engine -> renderer
LINE = "line"
SOUND = "sound"
STOP_SOUND = "stop_sound"
MUSIC = "music"
STATUS = "status"
ECHO = "echo"
REVIEW = "review"
CONNECTED = "connected"
DISCONNECTED = "disconnected"

# Renderer -> engine
INPUT = "input"
KEY = "key"
CONNECT = "connect"
DISCONNECT = "disconnect"


def line(
    text: str, channel: str = "main", gagged: bool = False, display_when_gagged: bool = False
) -> dict[str, Any]:
    return {
        "type": LINE,
        "text": text,
        "channel": channel,
        "gagged": gagged,
        "display_when_gagged": display_when_gagged,
    }


def sound(
    file: str, channel: str = "sound", gain: float = 1.0, pan: float = 0.0, loop: bool = False
) -> dict[str, Any]:
    return {"type": SOUND, "file": file, "channel": channel, "gain": gain, "pan": pan, "loop": loop}


def stop_sound(channel: str = "sound") -> dict[str, Any]:
    return {"type": STOP_SOUND, "channel": channel}


def music(file: str) -> dict[str, Any]:
    return {"type": MUSIC, "file": file}


def status(gauges: dict[str, Any]) -> dict[str, Any]:
    return {"type": STATUS, "gauges": gauges}


def echo(text: str, channel: str = "main") -> dict[str, Any]:
    return {"type": ECHO, "text": text, "channel": channel}


def review(text: str) -> dict[str, Any]:
    return {"type": REVIEW, "text": text}


def connected(world: str) -> dict[str, Any]:
    return {"type": CONNECTED, "world": world}


def disconnected() -> dict[str, Any]:
    return {"type": DISCONNECTED}


def input_message(text: str) -> dict[str, Any]:
    return {"type": INPUT, "text": text}


def key_message(key: str) -> dict[str, Any]:
    return {"type": KEY, "key": key}
