"""Output router: trigger->channel routing, per-channel speech/audio policy, recall."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.automation.channels import ChannelPolicy, ChannelRouter
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Buffer, Line
from genericmud.protocol.telnet import DataReceived
from genericmud.review.cursor import ReviewCursor
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _app(rate: float = 20.0) -> tuple[EngineApp, RecordingBackend, list, list]:
    backend = RecordingBackend()
    voice = VoiceRouter(backend, rate=rate, clock=lambda: 0.0)
    posted: list[dict] = []
    app = EngineApp(voice, post=posted.append, keymap=load_keymap("vipmud"))
    return app, backend, [], posted


def test_channel_router_default_and_override():
    router = ChannelRouter()
    default = router.policy("main")
    assert default.speak and default.display
    router.set_policy("muted", ChannelPolicy(speak=False))
    assert router.policy("muted").speak is False


def test_trigger_routes_to_interrupting_channel():
    app, backend, _sent, _posted = _app()
    app.engine.add_trigger("tells you", None, channel="tell")  # default 'tell' policy interrupts
    app.on_telnet_event(DataReceived(b"Bob tells you hi\r\n"))
    assert any("Bob tells you hi" in s for s in backend.spoken)
    assert backend.stops >= 1  # the 'tell' policy barges in over current speech


def test_gag_from_speech_channel_displays_but_stays_silent():
    app, backend, _sent, posted = _app()
    app.channels.set_policy("cosmetic", ChannelPolicy(speak=False, display=True))
    app.engine.add_trigger("wounds itch", None, channel="cosmetic")
    app.on_telnet_event(DataReceived(b"Your wounds itch.\r\n"))
    assert not any("wounds itch" in s for s in backend.spoken)  # routed out of speech
    assert any(m["type"] == "line" and "wounds itch" in m["text"] for m in posted)  # still shown


def test_routed_channel_bypasses_the_main_flood_governor():
    app, backend, _sent, _posted = _app(rate=3)  # main is governed at 3 lines
    app.engine.add_trigger("CHAT", None, channel="chat")  # 'chat' voice is ungoverned
    for i in range(8):
        app.on_telnet_event(DataReceived(f"CHAT line {i}\r\n".encode()))
    spoken = [s for s in backend.spoken if s.startswith("CHAT line")]
    assert len(spoken) == 8  # nothing coalesced — only 'main' is flood-governed


def test_per_channel_recall():
    buffer = Buffer()
    buffer.append(Line("room desc", channel="main"))
    buffer.append(Line("Bob tells you hi", channel="tell"))
    buffer.append(Line("you hit the goblin", channel="main"))
    cursor = ReviewCursor(buffer)
    assert cursor.recall(1) == "you hit the goblin"  # overall newest
    assert cursor.recall(1, channel="tell") == "Bob tells you hi"
    assert cursor.recall(1, channel="main") == "you hit the goblin"
