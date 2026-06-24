"""End-to-end sound-path trace: the full success chain logs, and each candidate's signature."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.packs import PackStore
from genericmud.protocol.telnet import DataReceived
from genericmud.session.diaglog import DiagnosticLog
from genericmud.sound.pygame_backend import PygameSoundBackend
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend, RecordingDiag


class _Channel:
    def play(self, *_a, **_k):
        pass

    def set_volume(self, *_a):
        pass


class _Mixer:
    """Minimal pygame.mixer stand-in: never raises, so the trace reaches backend.play OK."""

    def get_num_channels(self):
        return 8

    def Channel(self, _index):  # noqa: N802 - mirrors pygame.mixer.Channel
        return _Channel()

    def Sound(self, path):  # noqa: N802 - mirrors pygame.mixer.Sound
        return ("sound", path)


def _is_subsequence(needles, haystack):
    it = iter(haystack)
    return all(needle in it for needle in needles)


def _app(diag, *, packs=None, sound_backend=None):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    posted: list[dict] = []
    app = EngineApp(
        voice, send=[].append, post=posted.append, keymap=load_keymap("vipmud"),
        packs=packs, sound_backend=sound_backend, diag=diag, name="gw",
    )
    return app, backend, posted


def _install_lua(store, tmp_path, name, body):
    src = tmp_path / name
    src.write_text(body, encoding="utf-8")
    return store.install(src, world="mud", trust=True)


def test_full_success_chain_logs_in_order(tmp_path):
    diag = RecordingDiag()
    backend = PygameSoundBackend(_Mixer(), diag=diag)
    store = PackStore(tmp_path / "store")
    _install_lua(store, tmp_path, "boom.lua",
                 'mud.trigger("boom", function() mud.play("boom.wav") end)')
    app, _backend, _posted = _app(diag, packs=store, sound_backend=backend)

    app.on_connect("mud")  # pack.load, pack.counts, pack.summary; backend.active(native)
    assert ("backend.active", {"kind": "native"}) in diag.events
    assert diag.fields("pack.counts")["triggers"] == 1  # the pack armed its trigger

    app.on_telnet_event(DataReceived(b"boom\r\n"))  # drives the whole cue
    assert _is_subsequence(
        ["trigger.fire", "play.entry", "play.resolve", "sink.gain", "backend.play"],
        diag.stages(),
    )
    assert diag.fields("backend.play")["result"] == "OK"


def test_inert_pack_is_visible_as_zero_cues(tmp_path):
    # Candidate D: a pack loads but never fires -> no play.entry, sink.cues stays 0.
    diag = RecordingDiag()
    store = PackStore(tmp_path / "store")
    _install_lua(store, tmp_path, "quiet.lua",
                 'mud.trigger("never matches this", function() mud.play("x.wav") end)')
    app, _backend, _posted = _app(diag, packs=store)
    app.on_connect("mud")
    app.on_telnet_event(DataReceived(b"some unrelated line\r\n"))
    assert "trigger.fire" not in diag.stages()
    assert app.sink.cues == 0  # what diag:where reports as "0 cues attempted"


def test_muted_category_logs_zero_effective_gain(tmp_path):
    # Candidate F: the cue reaches the backend but effective gain is 0 -> silent "success".
    diag = RecordingDiag()
    app, _backend, _posted = _app(diag)
    app.sound.set_muted("sound", True)
    app.sink.play("/abs/hit.wav", "sound", 1.0)
    fields = diag.fields("sink.gain")
    assert fields["muted"] is True
    assert fields["effective"] == 0.0


def test_client_error_is_logged_and_spoken_once(tmp_path):
    diag = RecordingDiag()
    app, backend, posted = _app(diag)
    app.on_ws_message({"type": "client_error", "scope": "audio", "file": "x.wav",
                       "error": "fetch 404"})
    app.on_ws_message({"type": "client_error", "scope": "audio", "file": "y.wav",
                       "error": "decode failed"})
    errors = [e for e in diag.events if e[0] == "client.error"]
    assert len(errors) == 2  # both traced
    # Both echoed to output; only the first spoken (mirrors the native once-spoken policy).
    assert sum("x.wav" in str(m.get("text", "")) for m in posted) == 1
    assert len(backend.spoken) == 1 and "x.wav" in backend.spoken[0]


def test_diag_where_speaks_the_log_path_and_summary(tmp_path):
    diag = DiagnosticLog(tmp_path / "diag.log")
    diag.start()
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    app = EngineApp(voice, post=[].append, keymap=load_keymap("vipmud"), diag=diag, name="gw")
    app._handle_key("alt+shift+d")
    diag.stop()
    summary = " ".join(backend.spoken)
    assert "diagnostic log diag.log" in summary
    assert "backend post" in summary  # no native backend injected here
    assert "cues attempted" in summary
