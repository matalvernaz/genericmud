"""Voice backend interface — one TTS/screen-reader target."""

from __future__ import annotations


class VoiceBackend:
    """Speaks text through a platform TTS or screen reader. Subclass per platform."""

    def speak(self, text: str) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError
