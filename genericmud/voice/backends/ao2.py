"""accessible_output2 voice backend (preferred).

Speaks through whatever screen reader is running (NVDA/JAWS/...), so streaming MUD
output is read in the user's own NVDA voice and settings rather than a separate
SAPI voice. accessible_output2 bundles the screen-reader controller DLLs, so no
manual DLL placement is needed; it falls back to SAPI internally only when no
screen reader is active.
"""

from __future__ import annotations

from genericmud.voice.backends.base import VoiceBackend


class Ao2Backend(VoiceBackend):
    def __init__(self) -> None:
        from accessible_output2.outputs.auto import Auto  # Windows-only at runtime

        self._speaker = Auto()

    def speak(self, text: str) -> None:
        self._speaker.output(text, interrupt=False)

    def stop(self) -> None:
        try:
            self._speaker.output("", interrupt=True)  # interrupt clears current speech
        except Exception:
            pass
