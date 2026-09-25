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
from genericmud.bridge.static_server import STATIC_HOST, serve_static
from genericmud.bridge.ws_server import WsBridge
from genericmud.config.keymap import load_keymap
from genericmud.protocol.charset import AUTO
from genericmud.resources import resource_root
from genericmud.session.crashlog import install_loop_exception_handler
from genericmud.session.diaglog import make_diagnostic_log
from genericmud.transport.connection import MudConnection
from genericmud.voice.factory import make_voice_backend
from genericmud.voice.router import VoiceRouter

_BOOT_TIMEOUT_SECONDS = 10
_SHUTDOWN_TIMEOUT_SECONDS = 5


def _report_startup_error(message: str) -> None:
    """Write a launcher error when a console exists; windowed builds have no stderr."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        print(message, file=stream)
    except (OSError, ValueError):
        return


def run(args) -> None:
    import webview  # lazy: only needed for the web path

    loop = asyncio.new_event_loop()
    install_loop_exception_handler(loop)  # capture engine-thread coroutine crashes
    ready = threading.Event()
    # A per-run secret the page must echo back before the WS bridge accepts it, so a random web
    # page the user visits can't hijack the localhost bridge and drive the MUD (CSWSH).
    token = secrets.token_urlsafe(32)
    boot_error: list[Exception] = []  # a boot failure recorded here aborts the UI (no dead window)
    app_instance: EngineApp | None = None
    bridge_instance: WsBridge | None = None
    connection_instance: MudConnection | None = None
    ws_port: int | None = None

    async def boot() -> None:
        nonlocal app_instance, bridge_instance, connection_instance, ws_port
        try:
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
                suppress_reconnect=connection.suppress_reconnect,
                encoding=getattr(args, "encoding", AUTO),
            )
            connection.text_codec = app.codec  # commands go out in the world's encoding too
            holder["app"] = app
            connection._on_event = app.on_telnet_event
            # Without this, a disconnect (connection._status) goes nowhere and the browser/screen
            # reader never learns the session died -- the native launcher already wires it.
            connection.on_status = app.on_connection_status
            app_instance = app
            bridge_instance = bridge
            connection_instance = connection
            ws_port = await bridge.start(port=0)
            if args.host:
                try:
                    await connection.connect(args.host, args.port, tls=args.tls)
                    bridge.post(protocol.connected(f"{args.host}:{args.port}"))
                except OSError as error:
                    bridge.post(protocol.echo(f"* Connect failed: {error}"))
        except Exception as error:  # noqa: BLE001 - surface a boot failure instead of a dead UI
            boot_error.append(error)
            _report_startup_error(f"genericMud failed to start: {error}")
        finally:
            ready.set()  # always release the launcher, success or failure

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(boot())
            loop.run_forever()
        finally:
            loop.close()

    worker = threading.Thread(target=run_loop, daemon=True)
    worker.start()

    def stop_engine() -> None:
        async def shutdown() -> None:
            if app_instance is not None:
                app_instance.shutdown()
            if connection_instance is not None:
                await connection_instance.close()
            if bridge_instance is not None:
                await bridge_instance.stop()

        # `ready` is set inside boot() -- i.e. during run_until_complete, BEFORE run_forever
        # starts -- so the caller can reach here in the window where the loop is not yet
        # "running". Gate on is_closed(), not is_running(): run_coroutine_threadsafe still
        # schedules the shutdown and it executes once run_forever begins. The old is_running()
        # guard skipped teardown in that window, leaking the app/connection/bridge on exit.
        if not loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
            try:
                future.result(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except Exception as error:  # noqa: BLE001 - process exit still stops daemon resources
                _report_startup_error(f"genericMud shutdown failed: {error}")
        if not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)

    if not ready.wait(timeout=_BOOT_TIMEOUT_SECONDS):
        _report_startup_error("genericMud engine startup timed out.")
        return
    if boot_error:
        # The engine never came up (e.g. the WS port is taken); don't open a window whose input
        # and self-voice are permanently dead.
        _report_startup_error(f"genericMud could not start: {boot_error[0]}")
        stop_engine()
        return
    if ws_port is None:
        _report_startup_error("genericMud could not start its local bridge.")
        stop_engine()
        return

    frontend_dir = resource_root() / "frontend"
    if not (frontend_dir / "index.html").is_file():
        _report_startup_error(f"frontend not found at {frontend_dir}")
        stop_engine()
        return
    try:
        static_server = serve_static(str(frontend_dir), port=0, sound_root=args.sounds)
    except OSError as error:
        _report_startup_error(f"genericMud could not start its local web server: {error}")
        stop_engine()
        return
    static_port = static_server.server_address[1]
    url = (
        f"http://{STATIC_HOST}:{static_port}/index.html"
        f"?token={token}&port={ws_port}"
    )
    try:
        webview.create_window("genericMud", url=url)
        webview.start()
    finally:
        static_server.shutdown()
        static_server.server_close()
        stop_engine()
