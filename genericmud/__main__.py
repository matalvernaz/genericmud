"""Launch genericMud: engine + WS bridge in an asyncio thread, pywebview window
on the main thread (WebView2 on Windows).

Self-voice prefers NVDA (its Controller Client DLL), falls back to SAPI5 (always
present on Windows), then to a console print. The engine stack is built inside
the loop thread so the SAPI COM object lives in the right apartment.

Run on Windows after ``pip install -e .[gui,voice]``:
``py -m genericmud <host> <port> [--tls]``  (or double-click run.bat).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.bridge.static_server import STATIC_HOST, STATIC_PORT, serve_static
from genericmud.bridge.ws_server import DEFAULT_PORT, WsBridge
from genericmud.config.keymap import load_keymap
from genericmud.resources import resource_root
from genericmud.transport.connection import MudConnection
from genericmud.voice.backends.base import VoiceBackend
from genericmud.voice.router import VoiceRouter


class _PrintBackend(VoiceBackend):
    def speak(self, text: str) -> None:
        print("SPEAK:", text)

    def stop(self) -> None:
        pass


def _make_voice_backend() -> VoiceBackend:
    if sys.platform == "win32":
        try:
            from genericmud.voice.backends.nvda import NvdaBackend

            return NvdaBackend()
        except Exception:  # DLL absent / NVDA not running
            pass
        try:
            from genericmud.voice.backends.sapi import SapiBackend

            return SapiBackend()
        except Exception:  # pywin32 missing / SAPI unavailable
            pass
    return _PrintBackend()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="genericmud")
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=4000)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--sounds", default=None, help="directory of sound files (MSP/soundpacks)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    import webview  # lazy so the package imports without a GUI present

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def boot() -> None:
        voice = VoiceRouter(_make_voice_backend())
        holder: dict[str, EngineApp] = {}
        bridge = WsBridge(lambda message: holder["app"].on_ws_message(message))
        connection = MudConnection()
        app = EngineApp(
            voice,
            send=connection.send_line,
            post=bridge.post,
            schedule=loop.call_later,
            keymap=load_keymap("vipmud"),
        )
        holder["app"] = app
        connection._on_event = app.on_telnet_event  # late-bound to break the cycle
        await bridge.start(port=DEFAULT_PORT)
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
    webview.create_window("genericMud", url=f"http://{STATIC_HOST}:{STATIC_PORT}/index.html")
    webview.start()


if __name__ == "__main__":
    main()
