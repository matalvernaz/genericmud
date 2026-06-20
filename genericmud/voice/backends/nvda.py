"""NVDA self-voice backend (Windows).

Routes speech through the user's running NVDA via its Controller Client DLL, so
output speaks in the user's own synth/voice/rate — the headline capability VIPMud
has and an Electron/Web-Speech client cannot match. Requires
``nvdaControllerClient.dll`` (shipped beside the app or on PATH). Only
instantiable on Windows; importing the module elsewhere is safe.
"""

from __future__ import annotations

import ctypes

from genericmud.voice.backends.base import VoiceBackend


class NvdaBackend(VoiceBackend):
    def __init__(self, dll_path: str = "nvdaControllerClient.dll") -> None:
        # ctypes.windll exists only on Windows; constructing this elsewhere raises.
        self._dll = ctypes.windll.LoadLibrary(dll_path)  # type: ignore[attr-defined]

    def speak(self, text: str) -> None:
        # Non-zero return means NVDA isn't running; treat speech as best-effort.
        self._dll.nvdaController_speakText(text)

    def stop(self) -> None:
        self._dll.nvdaController_cancelSpeech()
