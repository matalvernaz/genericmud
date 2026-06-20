"""Tests for the static frontend server (serves the real frontend dir)."""

from __future__ import annotations

import urllib.error
import urllib.request

from genericmud.bridge.static_server import serve_static
from genericmud.resources import resource_root


def _get(base: str, path: str):
    return urllib.request.urlopen(base + path, timeout=5)


def test_serves_index_and_modules_with_correct_mime():
    server = serve_static(str(resource_root() / "frontend"), port=0)
    try:
        _host, port = server.server_address
        base = f"http://127.0.0.1:{port}"

        with _get(base, "/index.html") as response:
            assert response.status == 200
            assert "text/html" in response.headers.get("Content-Type", "")
            assert "genericMud" in response.read().decode("utf-8")

        # ES modules must be served with a JS MIME type or the browser rejects them.
        with _get(base, "/src/app.js") as response:
            assert response.status == 200
            assert "javascript" in response.headers.get("Content-Type", "")
    finally:
        server.shutdown()


def test_missing_file_is_404():
    server = serve_static(str(resource_root() / "frontend"), port=0)
    try:
        _host, port = server.server_address
        try:
            _get(f"http://127.0.0.1:{port}", "/nope.html")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
