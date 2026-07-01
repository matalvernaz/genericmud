"""The localhost WS bridge must require the per-run token before accepting a renderer (#1)."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from genericmud.bridge.ws_server import WsBridge


async def test_ws_bridge_requires_token():
    received: list[dict] = []
    bridge = WsBridge(lambda m: received.append(m), token="s3cret-token")
    port = await bridge.start(host="127.0.0.1", port=0)
    try:
        # A page with no token (a hijack attempt) is closed and its input never dispatched.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"type": "input", "text": "look"}))
            with pytest.raises(websockets.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=2)
        await asyncio.sleep(0.05)
        assert received == []

        # The real page authenticates first, then its input is dispatched.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"type": "hello", "token": "s3cret-token"}))
            await ws.send(json.dumps({"type": "input", "text": "north"}))
            await asyncio.sleep(0.05)
        assert any(m.get("type") == "input" and m.get("text") == "north" for m in received)
    finally:
        await bridge.stop()


async def test_ws_bridge_wrong_token_rejected():
    received: list[dict] = []
    bridge = WsBridge(lambda m: received.append(m), token="right")
    port = await bridge.start(host="127.0.0.1", port=0)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"type": "hello", "token": "wrong"}))
            with pytest.raises(websockets.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=2)
        await asyncio.sleep(0.05)
        assert received == []
    finally:
        await bridge.stop()


async def test_ws_bridge_no_token_is_open_for_legacy_and_tests():
    received: list[dict] = []
    bridge = WsBridge(lambda m: received.append(m))  # no token configured
    port = await bridge.start(host="127.0.0.1", port=0)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"type": "input", "text": "hi"}))
            await asyncio.sleep(0.05)
        assert any(m.get("type") == "input" for m in received)
    finally:
        await bridge.stop()
