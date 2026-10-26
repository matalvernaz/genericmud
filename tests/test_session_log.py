"""Session logging: the SessionLogger file primitive + the app log:toggle key."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.protocol.telnet import DataReceived
from genericmud.session.log import SessionLogger
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def test_session_logger_writes_then_appends(tmp_path):
    path = tmp_path / "logs" / "s.log"  # parent dir created on start
    logger = SessionLogger(path)
    logger.start()
    logger.log("line one")
    logger.log("line two")
    logger.stop()
    assert path.read_text(encoding="utf-8") == "line one\nline two\n"
    assert not logger.active

    reopened = SessionLogger(path)
    reopened.start()
    reopened.log("line three")
    reopened.stop()
    assert path.read_text(encoding="utf-8").splitlines() == ["line one", "line two", "line three"]


def _app(tmp_path):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(
        voice, send=sent.append, post=posted.append,
        keymap=load_keymap("vipmud"), name="gw", log_dir=tmp_path,
    )
    return app, backend, sent, posted


def test_log_toggle_captures_output_and_commands_then_stops(tmp_path):
    app, backend, _sent, _posted = _app(tmp_path)
    app._handle_key("alt+shift+l")  # log:toggle on
    assert app.logger is not None and app.logger.active
    path = app.logger.path
    assert path.name.startswith("gw-")  # named after the session

    app.on_telnet_event(DataReceived(b"You see a dragon\r\n"))
    app.on_ws_message({"type": "input", "text": "kill dragon"})
    app._handle_key("alt+shift+l")  # toggle off
    assert app.logger is None
    assert any("logging" in spoken for spoken in backend.spoken)

    text = path.read_text(encoding="utf-8")
    assert "You see a dragon" in text  # incoming output logged
    assert "> kill dragon" in text  # sent command logged

    app.on_telnet_event(DataReceived(b"after stop\r\n"))
    assert "after stop" not in path.read_text(encoding="utf-8")  # nothing after stop


def test_log_write_failure_stops_logging_without_dropping_the_line(tmp_path):
    # A failed write (disk full, media removed) must not abort _emit_line -- that runs
    # before speak/append, so a raised OSError silenced and dropped every later line.
    app, backend, _sent, posted = _app(tmp_path)
    app._handle_key("alt+shift+l")  # logging on

    class _Boom:
        active = True

        def log(self, _text):
            return False  # simulate a write fault that auto-stopped logging

    app.logger = _Boom()
    app.on_telnet_event(DataReceived(b"A goblin hits you\r\n"))
    # The line still reached speech and the buffer; only logging stopped.
    assert any("goblin" in spoken for spoken in backend.spoken)
    assert app.buffer.lines()[-1].plain_text == "A goblin hits you"
    assert any("logging stopped" in spoken.lower() for spoken in backend.spoken)


def test_session_logger_auto_stops_on_write_error(tmp_path):
    logger = SessionLogger(tmp_path / "s.log")
    logger.start()
    logger._handle.close()  # make the next write raise ValueError/OSError on a closed file
    assert logger.log("x") is False
    assert not logger.active  # auto-stopped


def _world(**settings):
    from genericmud.config.worlds import World

    return World("gw", "gw.example", 23, **settings)


def test_a_world_can_log_automatically_when_it_connects(tmp_path):
    app, backend, _sent, posted = _app(tmp_path)
    app.apply_world_settings(_world(log_automatically=True))
    app.on_connect("gw")
    assert app.logger is not None and app.logger.active
    app.on_telnet_event(DataReceived(b"Welcome!\r\n"))
    lines = app.logger.path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("Session started ")
    assert "Welcome!" in lines
    # Said in the output rather than aloud, over the connecting announcements.
    assert any(m.get("text", "").startswith("* Logging to gw-") for m in posted)
    assert not any("logging" in spoken.lower() for spoken in backend.spoken)


def test_stopping_a_log_by_hand_keeps_it_stopped_when_the_world_reconnects(tmp_path):
    app, _backend, _sent, _posted = _app(tmp_path)
    app.apply_world_settings(_world(log_automatically=True))
    app.on_connect("gw")
    app._handle_key("alt+shift+l")  # stopped by hand
    app.on_connection_status("connection lost")
    app.on_connection_status("reconnected")
    assert app.logger is None


def test_logs_by_day_and_by_world_append_each_session_with_its_time(tmp_path):
    from datetime import date

    app, _backend, _sent, _posted = _app(tmp_path)
    app.apply_world_settings(_world(log_files="day"))
    for _session in range(2):
        app._handle_key("alt+shift+l")
        app.on_telnet_event(DataReceived(b"hello\r\n"))
        app._handle_key("alt+shift+l")
    day_file = tmp_path / f"gw-{date.today():%Y-%m-%d}.log"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert [line.startswith("Session started ") for line in lines] == [True, False, True, False]
    app.apply_world_settings(_world(log_files="world"))
    app._handle_key("alt+shift+l")
    assert app.logger.path == tmp_path / "gw.log"


def test_a_world_can_leave_what_you_type_out_of_its_log(tmp_path):
    app, _backend, _sent, _posted = _app(tmp_path)
    app.apply_world_settings(_world(log_commands=False))
    app._handle_key("alt+shift+l")
    app.on_telnet_event(DataReceived(b"You see a dragon\r\n"))
    app.on_ws_message({"type": "input", "text": "kill dragon"})
    text = app.logger.path.read_text(encoding="utf-8")
    assert "You see a dragon" in text
    assert ">" not in text


def test_a_log_that_cant_start_says_so_and_the_world_still_connects(tmp_path):
    blocked = tmp_path / "logs"
    blocked.write_text("a file where the logs folder should be", encoding="utf-8")
    app, backend, _sent, _posted = _app(blocked)
    app.apply_world_settings(_world(log_automatically=True))
    app.on_connect("gw")
    assert app.logger is None
    assert any(spoken.startswith("Couldn't start logging") for spoken in backend.spoken)
