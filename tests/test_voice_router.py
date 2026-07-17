"""Tests for the VoiceRouter and its fast-output governor."""

from __future__ import annotations

from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def test_governor_coalesces_burst_then_summarizes():
    backend = RecordingBackend()
    clock = [0.0]
    router = VoiceRouter(backend, rate=5, clock=lambda: clock[0])

    for i in range(12):
        router.speak(f"line{i}")
    # capacity 5: first five spoken, the rest suppressed
    assert backend.spoken == ["line0", "line1", "line2", "line3", "line4"]

    clock[0] = 10.0  # let the bucket refill
    router.speak("after")
    assert backend.spoken[5] == "7 more lines"
    assert backend.spoken[6] == "after"


def test_non_governed_channel_not_throttled():
    backend = RecordingBackend()
    router = VoiceRouter(backend, rate=1, clock=lambda: 0.0)
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
    router = VoiceRouter(backend, rate=1, clock=lambda: clock[0])
    router.speak("x")
    router.speak("y")  # suppressed (capacity 1)
    router.flush()
    assert backend.stops >= 1
    clock[0] = 100.0
    router.speak("z")
    assert "z" in backend.spoken
    assert not any("more lines" in s for s in backend.spoken)


def test_muted_passthrough_speaks_nothing():
    backend = RecordingBackend()
    router = VoiceRouter(backend, clock=lambda: 0.0)
    router.set_muted(True)
    router.speak("nope")
    assert backend.spoken == []


def test_interrupt_stops_speech_but_keeps_the_suppressed_backlog():
    backend = RecordingBackend()
    clock = [0.0]
    router = VoiceRouter(backend, rate=2, clock=lambda: clock[0])
    router.speak("one")
    router.speak("two")
    router.speak("dropped")  # over budget: suppressed
    router.interrupt()  # follow mode barging in on movement
    assert backend.stops == 1
    clock[0] = 10.0
    router.speak("after")
    assert "1 more lines" in backend.spoken  # the backlog notice survived the interrupt
