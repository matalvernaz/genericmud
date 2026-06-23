"""Local static HTTP server for the frontend and sound files.

Serves the renderer over http (not file://: Chromium/WebView2 blocks ES module
imports and fetch() from a file:// origin). Routes ``/sounds/<rel>`` to a
configured sound root (for MSP and soundpack audio) and everything else to the
frontend directory. Forces JS MIME types so modules load even where the OS maps
.js oddly (Windows). Path components are sanitised so neither root can be escaped.
"""

from __future__ import annotations

import http.server
import os
import threading
import urllib.parse

STATIC_HOST = "127.0.0.1"
STATIC_PORT = 8730
SOUNDS_PREFIX = "/sounds/"


class _AppServer(http.server.ThreadingHTTPServer):
    frontend_dir: str = ""
    sound_root: str | None = None


class _Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".html": "text/html",
    }

    def log_message(self, *args: object) -> None:
        pass  # quiet

    def translate_path(self, path: str) -> str:
        server: _AppServer = self.server  # type: ignore[assignment]
        clean = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
        clean = clean.replace("\\", "/")  # backslash is a path separator on Windows -- split it too
        if clean.startswith(SOUNDS_PREFIX) and server.sound_root:
            base, relative = server.sound_root, clean[len(SOUNDS_PREFIX) :]
        else:
            base, relative = server.frontend_dir, clean.lstrip("/")
        # Drop '.'/'..'/empty segments, then confirm the result really stays under the root. The
        # filter alone isn't enough: on Windows a drive letter or backslash survives it and escapes
        # os.path.join, and a symlink can point outside -- realpath + containment is the real guard.
        parts = [p for p in relative.split("/") if p and p not in (".", "..")]
        root = os.path.realpath(base)
        candidate = os.path.realpath(os.path.join(root, *parts))
        if candidate != root and not candidate.startswith(root + os.sep):
            return root
        return candidate


def serve_static(
    frontend_dir: str,
    host: str = STATIC_HOST,
    port: int = STATIC_PORT,
    sound_root: str | None = None,
) -> _AppServer:
    """Serve the frontend (and optional sound root) on a daemon thread."""
    server = _AppServer((host, port), _Handler)
    server.frontend_dir = str(frontend_dir)
    server.sound_root = str(sound_root) if sound_root else None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
