"""Pick the best available self-voice backend for the platform.

Prism first on every platform: it routes to whatever screen reader is running — NVDA,
JAWS, Orca, VoiceOver — so output speaks in the user's own voice and settings, and it
lands on the system TTS (SAPI/OneCore, AVSpeech, speech-dispatcher) when none is. The
per-platform fallbacks below only matter when prism is missing or its wheel doesn't
support the host: SAPI5 on Windows, the built-in ``say`` on macOS, speech-dispatcher's
``spd-say`` on Linux. Console print is the last resort everywhere. Constructed on the
thread that uses it (SAPI is COM, apartment-bound).
"""

from __future__ import annotations

import sys

from genericmud.voice.backends.base import VoiceBackend


class PrintBackend(VoiceBackend):
    def speak(self, text: str) -> None:
        # A PyInstaller ``--windowed`` process has no stdout. This backend is the
        # last resort when native speech is unavailable, so it must degrade to
        # silence instead of crashing every UI announcement.
        if sys.stdout is None:
            return
        try:
            print("SPEAK:", text)
        except (OSError, ValueError):
            return

    def stop(self) -> None:
        pass


def make_voice_backend() -> VoiceBackend:
    # Prism routes to the running screen reader (NVDA/JAWS/Orca/VoiceOver → the user's own
    # voice) and bundles the controller DLLs, so it's preferred over the raw system TTS.
    try:
        from genericmud.voice.backends.prism_tts import PrismBackend

        return PrismBackend()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            from genericmud.voice.backends.sapi import SapiBackend

            return SapiBackend()
        except Exception:  # pywin32 / SAPI unavailable
            pass
    elif sys.platform == "darwin":
        try:
            from genericmud.voice.backends.subprocess_tts import MacSayBackend

            return MacSayBackend()
        except Exception:  # `say` missing (unheard of on macOS)
            pass
    elif sys.platform.startswith("linux"):
        try:
            from genericmud.voice.backends.subprocess_tts import SpeechDispatcherBackend

            return SpeechDispatcherBackend()
        except Exception:  # speech-dispatcher not installed
            pass
    return PrintBackend()
