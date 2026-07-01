"""A speech-backend fault must not crash/mute the app; a pack must not mute the client."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from genericmud.voice.backends.base import VoiceBackend
from genericmud.voice.router import VoiceRouter


class _FaultyBackend(VoiceBackend):
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.fail = True

    def speak(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("COM boom")
        self.spoken.append(text)

    def stop(self) -> None:
        if self.fail:
            raise RuntimeError("stop boom")


def test_router_swallows_backend_fault_and_recovers():
    backend = _FaultyBackend()
    router = VoiceRouter(backend, clock=lambda: 0.0)
    router.speak("hello")  # backend raises; must not propagate
    router.flush()  # stop raises; must not propagate
    backend.fail = False
    router.speak("world")
    assert backend.spoken == ["world"]  # the app kept working after the fault


def test_pack_cannot_mute_reserved_channels():
    engine = AutomationEngine()
    api = ScriptApi(engine)
    for channel in ("main", "system", "tell", "review"):
        api.set_channel(channel, speak=False, display=False)
        assert engine.channels.policy(channel).speak is True  # left at the safe default


def test_pack_can_configure_its_own_channel():
    engine = AutomationEngine()
    api = ScriptApi(engine)
    api.set_channel("spam", speak=False)
    assert engine.channels.policy("spam").speak is False
