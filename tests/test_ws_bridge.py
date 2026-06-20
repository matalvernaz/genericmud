"""Loopback test for the WebSocket bridge contract."""

from __future__ import annotations

import asyncio
import json

import websockets

from genericmud.bridge import protocol as P
from genericmud.bridge.ws_server import WsBridge


async def test_bridge_roundtrip():
    received: list[dict] = []
    bridge = WsBridge(received.append)
    port = await bridge.start(port=0)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
        # renderer -> engine
        await client.send(json.dumps(P.input_message("look")))
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received[0]["type"] == P.INPUT
        assert received[0]["text"] == "look"

        # engine -> renderer
        await bridge.send(P.line("You see a room", channel="main"))
        raw = await asyncio.wait_for(client.recv(), timeout=1.0)
        message = json.loads(raw)
        assert message["type"] == P.LINE
        assert message["text"] == "You see a room"

    await bridge.stop()
