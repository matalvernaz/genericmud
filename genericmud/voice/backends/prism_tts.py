"""Prism voice backend (preferred on every platform).

Prism (PyPI ``prismatoid``) is one API over every screen reader and system TTS: NVDA,
JAWS, ZDSR, PC-Talker, System Access, Narrator/UIA, SAPI and OneCore on Windows;
VoiceOver and AVSpeech on macOS; Orca and speech-dispatcher on Linux. It replaces
accessible_output2, which is Windows/macOS-only and unmaintained.

``acquire_best()`` picks the highest-priority backend that is actually running, so output
speaks in the user's own NVDA (or Orca, or VoiceOver) voice, rate and settings -- the
headline capability of a self-voicing MUD client -- and quietly lands on SAPI/OneCore when
no screen reader is up. The controller DLLs ship inside the wheel, so nothing has to be
placed by hand.

The module is named ``prism_tts`` rather than ``prism`` so that ``import prism`` below can
never resolve to this file.
"""

from __future__ import annotations

from genericmud.voice.backends.base import VoiceBackend


class PrismBackend(VoiceBackend):
    def __init__(self) -> None:
        import prism  # optional dependency; absent or unsupported platform raises

        # The Context owns the native registry: the acquired backend's handle dies with it,
        # so hold the reference for the life of this object rather than letting it fall out
        # of scope at the end of __init__.
        self._context = prism.Context()
        self._backend = self._context.acquire_best()
        features = self._backend.features
        # `output` speaks AND brailles, so a braille-only user still gets the line; not every
        # backend implements it, and calling an unsupported entry point raises per utterance.
        self._speak_via_output = bool(features.supports_output)
        if not self._speak_via_output and not features.supports_speak:
            # Nothing audible here -- fail construction so the factory tries the next backend.
            raise RuntimeError(f"prism backend {self._backend.name!r} cannot speak")

    def speak(self, text: str) -> None:
        # Prism rejects empty text and embedded NULs with an exception rather than treating
        # them as a no-op; a blank MUD line is normal traffic, so filter before the call.
        text = text.replace("\x00", "")
        if not text:
            return
        if self._speak_via_output:
            self._backend.output(text, interrupt=False)
        else:
            self._backend.speak(text, interrupt=False)

    def stop(self) -> None:
        self._backend.stop()
