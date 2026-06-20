"""Pick the best available self-voice backend for the platform.

Order on Windows: the user's NVDA (Controller Client) > SAPI5 > console print.
Constructed on the thread that will use it (SAPI is COM, apartment-bound).
"""

from __future__ import annotations

import sys

from genericmud.voice.backends.base import VoiceBackend


class PrintBackend(VoiceBackend):
    def speak(self, text: str) -> None:
        print("SPEAK:", text)

    def stop(self) -> None:
        pass


def make_voice_backend() -> VoiceBackend:
    if sys.platform == "win32":
        try:
            from genericmud.voice.backends.nvda import NvdaBackend

            return NvdaBackend()
        except Exception:  # DLL absent / NVDA not running
            pass
        try:
            from genericmud.voice.backends.sapi import SapiBackend

            return SapiBackend()
        except Exception:  # pywin32 / SAPI unavailable
            pass
    return PrintBackend()
