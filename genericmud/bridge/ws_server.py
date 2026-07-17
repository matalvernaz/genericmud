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
_SEND_QUEUE_MAX = 2000  # bounded outbound backlog: a slow/absent renderer drops oldest, never OOMs

MessageHandler = Callable[[dict[str, Any]], Any]


class WsBridge:
    def __init__(self, on_message: MessageHandler, *, token: str = "") -> None:
        self._on_message = on_message
        self._token = token
        self._ws: Any = None
        self._server: Any = None
        self._outbox: asyncio.Queue | None = None  # created on the loop in start()
        self._connected: asyncio.Event | None = None  # set while a renderer is authenticated
        self._sender_task: Any = None

    async def start(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
        self._outbox = asyncio.Queue(maxsize=_SEND_QUEUE_MAX)
        self._connected = asyncio.Event()
        self._sender_task = asyncio.create_task(self._sender())
        self._server = await websockets.serve(self._handle, host, port)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, ws: Any, *_: Any) -> None:
        # A per-run token defeats cross-site WebSocket hijacking: the bridge binds localhost, so
        # any web page the user visits can open ws://127.0.0.1:<port>, but only OUR page (served
        # with the token in its URL) knows the secret. A connection that doesn't first send a
        # matching {type:hello, token} is closed and never becomes the renderer (so it can't send
        # input or receive MUD output). An empty token means unauthenticated (tests/legacy).
        authed = self._token == ""
        if authed:
            self._ws = ws  # no token configured: last renderer wins, as before
            self._mark_connected(True)
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
                        self._mark_connected(True)
                        continue
                    await ws.close(code=1008, reason="unauthorized")
                    return
                result = self._on_message(message)
                if inspect.isawaitable(result):
                    await result
        finally:
            if self._ws is ws:
                self._ws = None
                self._mark_connected(False)

    def _mark_connected(self, connected: bool) -> None:
        if self._connected is None:
            return
        if connected:
            self._connected.set()
        else:
            self._connected.clear()

    async def send(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is not None:
            await ws.send(json.dumps(message))

    async def _sender(self) -> None:
        """Single drain loop for post()ed messages.

        One task owns every fire-and-forget send, so a MUD flood can't spawn unbounded tasks and
        a closed renderer socket can't raise into engine code. Messages queued before a renderer
        authenticates are HELD (not dropped) until one connects.
        """
        assert self._outbox is not None
        while True:
            message = await self._outbox.get()
            if self._connected is not None:
                await self._connected.wait()  # hold the backlog until a renderer is present
            ws = self._ws
            if ws is None:
                continue  # renderer vanished between wait and read; drop this one
            try:
                await ws.send(json.dumps(message))
            except Exception:  # noqa: BLE001 - a closed/broken renderer must not kill the bridge
                pass

    def post(self, message: dict[str, Any]) -> None:
        """Fire-and-forget send from synchronous engine code running on the loop.

        Enqueues onto a bounded backlog drained by the single :meth:`_sender` task. When the
        backlog is full (renderer far behind), the OLDEST message is dropped so memory stays
        bounded and the freshest output still gets through.
        """
        outbox = self._outbox
        if outbox is None:
            return  # bridge not started
        try:
            outbox.put_nowait(message)
        except asyncio.QueueFull:
            try:
                outbox.get_nowait()  # drop oldest, make room for the newest
                outbox.put_nowait(message)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def stop(self) -> None:
        if self._sender_task is not None:
            self._sender_task.cancel()
            self._sender_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
