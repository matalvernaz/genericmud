"""Pick the best available self-voice backend.

Order: accessible_output2 (routes to the running screen reader — NVDA speaks in the
user's own voice/settings) > SAPI5 > console print. Constructed on the thread that
uses it (SAPI is COM, apartment-bound).
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
    # accessible_output2 routes to the running screen reader (NVDA → the user's own
    # voice) and bundles the controller DLLs, so it's preferred over raw SAPI.
    try:
        from genericmud.voice.backends.ao2 import Ao2Backend

        return Ao2Backend()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            from genericmud.voice.backends.sapi import SapiBackend

            return SapiBackend()
        except Exception:  # pywin32 / SAPI unavailable
            pass
    return PrintBackend()
