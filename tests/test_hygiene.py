"""Resource-bound + path-safety hygiene: sound-cache LRU (#15) and log-name sanitize (#17)."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.sound.pygame_backend import _MAX_CACHED_SOUNDS, PygameSoundBackend
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


class _MiniMixer:
    def Sound(self, path):  # noqa: N802 - mirrors pygame.mixer.Sound
        return ("sound", path)


def test_sound_decode_cache_is_bounded():
    backend = PygameSoundBackend(_MiniMixer())
    for i in range(_MAX_CACHED_SOUNDS + 100):  # a hostile MSP flood of unique filenames
        backend._sound(f"/abs/s{i}.wav")
    assert len(backend._sounds) <= _MAX_CACHED_SOUNDS


def _app() -> EngineApp:
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    return EngineApp(voice, send=[].append, post=[].append, keymap={})


def test_log_filename_is_sanitized(tmp_path):
    app = _app()
    app.log_dir = tmp_path
    app.name = "../../evil"  # a pack-derived world name trying to escape the logs dir
    app._toggle_log()
    try:
        path = app.logger.path.resolve()
        assert tmp_path.resolve() in path.parents  # stayed under the logs directory
        assert ".." not in app.logger.path.name
    finally:
        app._toggle_log()  # stop logging (closes the file)
