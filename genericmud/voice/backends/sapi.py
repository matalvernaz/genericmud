"""SAPI5 self-voice backend (Windows) via the built-in SpVoice COM object.

Always present on Windows — no extra DLL — so it's the audible fallback when
NVDA's Controller Client isn't installed, letting the app speak on first run.
Speaks asynchronously; stop() purges the queue so interruption works. Must be
constructed on the thread that uses it (COM apartment) — the launcher builds it
inside the engine's event-loop thread.
"""

from __future__ import annotations

from genericmud.voice.backends.base import VoiceBackend

_SVSF_ASYNC = 1
_SVSF_PURGE_BEFORE_SPEAK = 2


class SapiBackend(VoiceBackend):
    def __init__(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        self._voice = win32com.client.Dispatch("SAPI.SpVoice")

    def speak(self, text: str) -> None:
        self._voice.Speak(text, _SVSF_ASYNC)

    def stop(self) -> None:
        self._voice.Speak("", _SVSF_ASYNC | _SVSF_PURGE_BEFORE_SPEAK)
