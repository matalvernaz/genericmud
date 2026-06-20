"""Localhost WebSocket bridge between the Python engine and the web renderer.

Transport only: it serializes/deserializes JSON and dispatches inbound messages
to one callback. All engine wiring (voice, sounds, connection) lives in the app
launcher. A single renderer connection is expected (the pywebview window, or a
browser in fallback mode).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from typing import Any

import websockets

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8731

MessageHandler = Callable[[dict[str, Any]], Any]


class WsBridge:
    def __init__(self, on_message: MessageHandler) -> None:
        self._on_message = on_message
        self._ws: Any = None
        self._server: Any = None

    async def start(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
        self._server = await websockets.serve(self._handle, host, port)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, ws: Any, *_: Any) -> None:
        self._ws = ws  # last renderer wins; single-window app
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                result = self._on_message(message)
                if inspect.isawaitable(result):
                    await result
        finally:
            if self._ws is ws:
                self._ws = None

    async def send(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is not None:
            await ws.send(json.dumps(message))

    def post(self, message: dict[str, Any]) -> None:
        """Fire-and-forget send from synchronous engine code running on the loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.send(message))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
