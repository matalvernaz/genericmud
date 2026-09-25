"""Web-launcher behavior that must remain safe in a windowed executable."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

from genericmud import web_launcher
from genericmud.web_launcher import _report_startup_error


def test_startup_error_is_safe_without_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    _report_startup_error("no console")


def test_startup_error_uses_stderr_when_available(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    _report_startup_error("failed")
    assert stream.getvalue() == "failed\n"


def test_launcher_uses_free_local_ports_and_cleans_up(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html></html>", encoding="utf-8")
    calls: dict[str, object] = {}

    class FakeBridge:
        def __init__(self, _handler, *, token):
            calls["token"] = token

        async def start(self, *, port):
            calls["bridge_start_port"] = port
            return 45678

        def post(self, _message):
            pass

        async def stop(self):
            calls["bridge_stopped"] = True

    class FakeConnection:
        def __init__(self):
            self.on_status = None
            self._on_event = None

        def send_line(self, _text):
            pass

        def send_packet(self, _data):
            pass

        def suppress_reconnect(self):
            pass

        async def close(self):
            calls["connection_closed"] = True

    class FakeApp:
        codec = None  # EngineApp's text codec; the launcher shares it with the connection

        def __init__(self, *_args, **_kwargs):
            pass

        def on_ws_message(self, _message):
            pass

        def on_telnet_event(self, _event):
            pass

        def on_connection_status(self, _status):
            pass

        def shutdown(self):
            calls["app_shutdown"] = True

    class FakeStaticServer:
        server_address = ("127.0.0.1", 45679)

        def shutdown(self):
            calls["static_shutdown"] = True

        def server_close(self):
            calls["static_closed"] = True

    def serve_static(_frontend, *, port, sound_root):
        calls["static_start_port"] = port
        calls["sound_root"] = sound_root
        return FakeStaticServer()

    def create_window(_title, *, url):
        calls["url"] = url

    fake_webview = SimpleNamespace(create_window=create_window, start=lambda: None)
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(web_launcher, "WsBridge", FakeBridge)
    monkeypatch.setattr(web_launcher, "MudConnection", FakeConnection)
    monkeypatch.setattr(web_launcher, "EngineApp", FakeApp)
    monkeypatch.setattr(web_launcher, "VoiceRouter", lambda _backend: object())
    monkeypatch.setattr(web_launcher, "make_voice_backend", lambda: object())
    monkeypatch.setattr(web_launcher, "resource_root", lambda: tmp_path)
    monkeypatch.setattr(web_launcher, "serve_static", serve_static)

    web_launcher.run(
        SimpleNamespace(host=None, port=4000, tls=False, sounds=None)
    )

    assert calls["bridge_start_port"] == 0
    assert calls["static_start_port"] == 0
    assert "http://127.0.0.1:45679/" in calls["url"]
    assert "port=45678" in calls["url"]
    assert calls["static_shutdown"] is True
    assert calls["static_closed"] is True
    assert calls["app_shutdown"] is True
    assert calls["connection_closed"] is True
    assert calls["bridge_stopped"] is True
