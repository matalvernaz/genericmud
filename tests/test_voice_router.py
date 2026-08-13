"""Tests for the VoiceRouter and its fast-output governor."""

from __future__ import annotations

import sys

from genericmud.voice.factory import PrintBackend
from genericmud.voice.router import REVIEW_CHANNEL, SYSTEM_CHANNEL, VoiceRouter
from tests.helpers import RecordingBackend


def test_governor_coalesces_burst_then_summarizes():
    backend = RecordingBackend()
    clock = [0.0]
    router = VoiceRouter(backend, rate=5, burst=5, clock=lambda: clock[0])

    for i in range(12):
        router.speak(f"line{i}")
    # burst 5: first five spoken, the rest suppressed
    assert backend.spoken == ["line0", "line1", "line2", "line3", "line4"]

    clock[0] = 10.0  # let the bucket refill
    router.speak("after")
    assert backend.spoken[5] == "7 more lines"
    assert backend.spoken[6] == "after"


def test_a_screenful_arriving_at_once_is_not_coalesced():
    """A room description / who list lands in one packet -- it must not trip the governor.

    Regression: burst capacity used to be welded to the sustained rate, so any arrival
    over 20 lines was truncated with an "N more lines" notice at zero elapsed time.
    """
    backend = RecordingBackend()
    router = VoiceRouter(backend, clock=lambda: 0.0)  # defaults, no time passes

    for i in range(60):
        router.speak(f"line{i}")

    assert backend.spoken == [f"line{i}" for i in range(60)]
    assert not any("more line" in spoken for spoken in backend.spoken)


def test_non_governed_channel_not_throttled():
    backend = RecordingBackend()
    router = VoiceRouter(backend, rate=1, burst=1, clock=lambda: 0.0)
    router.speak("a", channel="tell")
    router.speak("b", channel="tell")
    router.speak("c", channel="tell")
    assert backend.spoken == ["a", "b", "c"]


def test_interrupt_stops_before_speaking():
    backend = RecordingBackend()
    router = VoiceRouter(backend, clock=lambda: 0.0)
    router.speak("urgent", interrupt=True)
    assert backend.stops == 1
    assert backend.spoken == ["urgent"]


def test_flush_stops_and_clears_backlog():
    backend = RecordingBackend()
    clock = [0.0]
    router = VoiceRouter(backend, rate=1, burst=1, clock=lambda: clock[0])
    router.speak("x")
    router.speak("y")  # suppressed (burst 1)
    router.flush()
    assert backend.stops >= 1
    clock[0] = 100.0
    router.speak("z")
    assert "z" in backend.spoken
    assert not any("more line" in s for s in backend.spoken)  # matches singular and plural


def test_muted_passthrough_speaks_nothing():
    backend = RecordingBackend()
    router = VoiceRouter(backend, clock=lambda: 0.0)
    router.set_muted(True)
    router.speak("nope")
    assert backend.spoken == []


def test_muted_still_answers_a_review_gesture():
    # Ctrl+M is meant to hand the transcript to the screen reader, not to disable the review
    # keys. Review speech is the whole response to Alt+Up, and app._speak_review's
    # protocol.review message is not rendered by the wx UI, so muting it left Alt+Up,
    # Ctrl+1-9 and Alt+T/C doing nothing at all -- no speech and no text.
    backend = RecordingBackend()
    router = VoiceRouter(backend, clock=lambda: 0.0)
    router.set_muted(True)
    router.speak("you are standing in a field", channel=REVIEW_CHANNEL)
    assert backend.spoken == ["you are standing in a field"]


def test_muted_still_speaks_the_client_talking_about_itself():
    # Connection state, rule errors and command feedback. These echo to the output box too,
    # but a user in passthrough mode should not have to go looking for them.
    backend = RecordingBackend()
    router = VoiceRouter(backend, clock=lambda: 0.0)
    router.set_muted(True)
    router.speak("Disconnected", channel=SYSTEM_CHANNEL)
    assert backend.spoken == ["Disconnected"]


def test_interrupt_stops_speech_but_keeps_the_suppressed_backlog():
    backend = RecordingBackend()
    clock = [0.0]
    router = VoiceRouter(backend, rate=2, burst=2, clock=lambda: clock[0])
    router.speak("one")
    router.speak("two")
    router.speak("dropped")  # over budget: suppressed
    router.interrupt()  # follow mode barging in on movement
    assert backend.stops == 1
    clock[0] = 10.0
    router.speak("after")
    assert "1 more line" in backend.spoken  # the backlog notice survived the interrupt


def test_print_fallback_is_safe_without_a_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    PrintBackend().speak("windowed build has no stdout")
