"""Prism voice backend (preferred on every platform).

Prism (PyPI ``prismatoid``) is one API over every screen reader and system TTS: NVDA,
JAWS, ZDSR, PC-Talker, System Access, Narrator/UIA, SAPI and OneCore on Windows;
VoiceOver and AVSpeech on macOS; Orca and speech-dispatcher on Linux. It replaces
accessible_output2, which is Windows/macOS-only and unmaintained.

``create_best()`` builds the highest-priority backend that actually initializes, so output
speaks in the user's own NVDA (or Orca, or VoiceOver) voice, rate and settings -- the
headline capability of a self-voicing MUD client -- and quietly lands on SAPI/OneCore when
no screen reader is up. The controller DLLs ship inside the wheel, so nothing has to be
placed by hand.

Unlike accessible_output2, which re-resolved the running reader on every single utterance,
a prism backend handle is bound to whichever reader was up when it was built, and prism's
NVDA notes are explicit that it never reconnects. So a reader that crashes and relaunches, or
one the user starts after genericMud, would leave the client talking to a stale handle for the
rest of the session. This backend follows prism's availability events and rebuilds on the next
line instead. It must be a rebuild: ``acquire_best()`` would hit prism's weak-pointer cache
and hand the dead handle straight back.

The module is named ``prism_tts`` rather than ``prism`` so that ``import prism`` below can
never resolve to this file.
"""

from __future__ import annotations

import threading

from genericmud.voice.backends.base import VoiceBackend

# How often prism polls for a screen reader appearing or going away. Well inside the gap
# between a reader relaunching and the next MUD line arriving, without waking constantly.
_AVAILABILITY_POLL_MS = 2000


class PrismBackend(VoiceBackend):
    def __init__(self) -> None:
        import prism  # optional dependency; absent or unsupported platform raises

        self._stale = threading.Event()
        # The Context owns the native registry: the acquired backend's handle dies with it,
        # so hold the reference for the life of this object rather than letting it fall out
        # of scope at the end of __init__.
        self._context = prism.Context(
            on_availability=self._availability_changed,
            poll_interval_ms=_AVAILABILITY_POLL_MS,
        )
        self._acquire()

    def _availability_changed(self, _backend: object, _name: str, _available: bool) -> None:
        """Prism's availability hook, called on prism's own dispatch thread."""
        # Only raise the flag. Re-acquiring here would build the new handle on the wrong
        # thread, and the Windows readers are COM and apartment-bound; speak() already runs
        # on the thread that owns this backend.
        self._stale.set()

    def _acquire(self) -> None:
        """Build the best backend prism can see right now.

        Raises if nothing available can produce speech, which is how construction failure
        hands the factory on to the next backend.
        """
        # create_best, never acquire_best: acquire_best hits prism's weak-pointer cache and
        # returns the first live+initialized instance with no availability probe. We still hold
        # the old handle at this point, so the cache never misses and a re-acquire would hand
        # back the very reader that just died. create_best runs the factory and initialize()
        # afresh, which is the destroy-and-re-create prism's NVDA notes call for.
        backend = self._context.create_best()
        features = backend.features
        # `output` speaks and, where the backend supports braille, brailles in one call. Only
        # prism's NVDA and JAWS backends set SUPPORTS_BRAILLE -- not Orca, not VoiceOver, not
        # the system TTS ones -- so this reaches a display on Windows and nowhere else yet.
        # Calling an entry point a backend doesn't implement raises per utterance.
        speak_via_output = bool(features.supports_output)
        if not speak_via_output and not features.supports_speak:
            raise RuntimeError(f"prism backend {backend.name!r} cannot speak")
        self._backend = backend
        self._speak_via_output = speak_via_output

    def _reacquire_if_stale(self) -> None:
        if not self._stale.is_set():
            return
        self._stale.clear()
        try:
            self._acquire()
        except Exception:
            # Keep the handle we already have. A failed re-acquire costs at most the event;
            # going mute because the new pick refused to speak would be worse than staying
            # on a reader that may still work.
            pass

    def speak(self, text: str) -> None:
        # Prism rejects empty text and embedded NULs with an exception rather than treating
        # them as a no-op; a blank MUD line is normal traffic, so filter before the call.
        text = text.replace("\x00", "")
        if not text:
            return
        self._reacquire_if_stale()
        if self._speak_via_output:
            self._backend.output(text, interrupt=False)
        else:
            self._backend.speak(text, interrupt=False)

    def stop(self) -> None:
        # Deliberately no re-acquire: barge-in has to be immediate, and the next speak() is
        # a few milliseconds away and will take the fresh handle anyway.
        self._backend.stop()
