"""A short device-test tone, with streams and temporary files owned by the settings dialog."""

from __future__ import annotations

import math
import struct
import tempfile
import wave
from pathlib import Path

from genericmud.config.audio_routes import AudioOutput
from genericmud.sound.bass_backend import make_bass_backend
from genericmud.sound.output_devices import AudioRouteUnavailable


class OutputPreview:
    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="genericmud-audio-")
        self._file = Path(self._directory.name) / "test.wav"
        with wave.open(str(self._file), "wb") as output:
            output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            output.writeframes(b"".join(
                struct.pack("<h", round(6000 * math.sin(2 * math.pi * 440 * i / 22050)
                                        * min(1, i / 220, (11024 - i) / 220)))
                for i in range(11025)
            ))
        self._errors: list[str] = []
        self._backend = None

    def play(self, output: AudioOutput) -> None:
        if self._backend is None:
            self._backend = make_bass_backend(on_error=self._errors.append)
        if self._backend is None:
            raise AudioRouteUnavailable("Device selection requires the BASS audio backend")
        self._errors.clear()
        self._backend.play_on_device(output, str(self._file), "test", 1, 0, False)
        if self._errors:
            raise AudioRouteUnavailable(self._errors[0])

    def close(self) -> None:
        if self._backend is not None:
            self._backend.stop("test")
        self._directory.cleanup()
