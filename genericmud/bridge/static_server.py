"""Local static HTTP server for the frontend.

The renderer must be served over http, not file://: Chromium/WebView2 blocks ES
module imports and fetch() from a file:// origin (opaque-origin CORS). Forces JS
MIME types so modules load even where the OS registry maps .js oddly (Windows).
"""

from __future__ import annotations

import http.server
import threading
from functools import partial

STATIC_HOST = "127.0.0.1"
STATIC_PORT = 8730


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


def serve_static(
    directory: str, host: str = STATIC_HOST, port: int = STATIC_PORT
) -> http.server.ThreadingHTTPServer:
    """Serve ``directory`` on a daemon thread; returns the running server."""
    handler = partial(_Handler, directory=directory)
    server = http.server.ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
