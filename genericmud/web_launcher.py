"""Web/pywebview UI launcher (the cross-platform path; used with --web).

Kept as the future Mac/Linux path now that native wx is the default on Windows.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import threading

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.bridge.static_server import STATIC_HOST, STATIC_PORT, serve_static
from genericmud.bridge.ws_server import DEFAULT_PORT, WsBridge
from genericmud.config.keymap import load_keymap
from genericmud.resources import resource_root
from genericmud.session.crashlog import install_loop_exception_handler
from genericmud.session.diaglog import make_diagnostic_log
from genericmud.transport.connection import MudConnection
from genericmud.voice.factory import make_voice_backend
from genericmud.voice.router import VoiceRouter


def run(args) -> None:
    import webview  # lazy: only needed for the web path

    loop = asyncio.new_event_loop()
    install_loop_exception_handler(loop)  # capture engine-thread coroutine crashes
    ready = threading.Event()
    # A per-run secret the page must echo back before the WS bridge accepts it, so a random web
    # page the user visits can't hijack the localhost bridge and drive the MUD (CSWSH).
    token = secrets.token_urlsafe(32)

    async def boot() -> None:
        voice = VoiceRouter(make_voice_backend())
        holder: dict[str, EngineApp] = {}
        bridge = WsBridge(lambda message: holder["app"].on_ws_message(message), token=token)
        connection = MudConnection()
        app = EngineApp(
            voice,
            send=connection.send_line,
            send_raw=connection.send_packet,
            post=bridge.post,
            schedule=loop.call_later,
            keymap=load_keymap("vipmud"),
            diag=make_diagnostic_log(),
        )
        holder["app"] = app
        connection._on_event = app.on_telnet_event
        await bridge.start(port=DEFAULT_PORT)
        if args.host:
            try:
                await connection.connect(args.host, args.port, tls=args.tls)
                bridge.post(protocol.connected(f"{args.host}:{args.port}"))
            except OSError as error:
                print(f"connect failed: {error}", file=sys.stderr)
        ready.set()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(boot())
        loop.run_forever()

    threading.Thread(target=run_loop, daemon=True).start()
    ready.wait(timeout=10)

    frontend_dir = resource_root() / "frontend"
    if not (frontend_dir / "index.html").is_file():
        print(f"frontend not found at {frontend_dir}", file=sys.stderr)
    serve_static(str(frontend_dir), sound_root=args.sounds)
    url = f"http://{STATIC_HOST}:{STATIC_PORT}/index.html?token={token}"
    webview.create_window("genericMud", url=url)
    webview.start()
