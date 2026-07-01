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
    def __init__(self, on_message: MessageHandler, *, token: str = "") -> None:
        self._on_message = on_message
        self._token = token
        self._ws: Any = None
        self._server: Any = None

    async def start(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
        self._server = await websockets.serve(self._handle, host, port)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, ws: Any, *_: Any) -> None:
        # A per-run token defeats cross-site WebSocket hijacking: the bridge binds localhost, so any
        # web page the user visits can open ws://127.0.0.1:<port>, but only OUR page (served with the
        # token in its URL) knows the secret. A connection that doesn't first send a matching
        # {type:hello, token} is closed and never becomes the renderer (so it can't send input or
        # receive MUD output). An empty token means unauthenticated (tests/legacy), as before.
        authed = self._token == ""
        if authed:
            self._ws = ws  # no token configured: last renderer wins, as before
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not authed:
                    if message.get("type") == "hello" and message.get("token") == self._token:
                        authed = True
                        self._ws = ws  # only an authenticated connection becomes the renderer
                        continue
                    await ws.close(code=1008, reason="unauthorized")
                    return
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
