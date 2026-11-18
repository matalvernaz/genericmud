"""Physical routing stays separate from channel identity and uses no real audio hardware."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from genericmud.app import EngineApp
from genericmud.config import audio_routes
from genericmud.config.audio_routes import AudioOutput, load_audio_routes, save_audio_routes
from genericmud.model.buffer import Line
from genericmud.packs.user_rules import UserRules, UserTrigger, register_rules
from genericmud.scripting.api import ScriptApi
from genericmud.sound import bass_library as lib
from genericmud.sound import output_devices
from genericmud.sound.bass_backend import BassSoundBackend, make_sound_backend
from genericmud.sound.bus import CueTiming, SoundBackend, SoundBus
from genericmud.sound.output_devices import AudioRouteUnavailable, available_outputs, output_context
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend
from tests.test_bass_backend import _FakeBass

HEADPHONES = AudioOutput("win32", "endpoint-headphones", "Наушники")
SPEAKERS = AudioOutput("win32", "endpoint-usb", "USB speakers")


class Devices(_FakeBass):
    def __init__(self):
        super().__init__()
        self.current = 1
        self.devices = {
            1: ("", "Default", lib.DEVICE_ENABLED | lib.DEVICE_INIT),
            2: (HEADPHONES.identifier, HEADPHONES.name, lib.DEVICE_ENABLED),
            3: (SPEAKERS.identifier, SPEAKERS.name, lib.DEVICE_ENABLED),
        }
        self.initialized = [1]
        self.broken = set()
        self.init_calls = []

    def BASS_GetDeviceInfo(self, index, target):  # noqa: N802
        if index not in self.devices:
            return 0
        identifier, name, flags = self.devices[index]
        info = target._obj
        info.driver, info.name, info.flags = identifier.encode(), name.encode(), flags
        return 1

    def BASS_GetDevice(self):  # noqa: N802
        return self.current

    def BASS_SetDevice(self, index):  # noqa: N802
        if index not in self.initialized:
            return 0
        self.current = index
        return 1

    def BASS_Init(self, index, _rate, flags, _window, _class):  # noqa: N802
        self.init_calls.append((index, flags))
        if index not in self.initialized:
            self.initialized.append(index)
        identity, name, old_flags = self.devices[index]
        self.devices[index] = (identity, name, old_flags | lib.DEVICE_INIT)
        self.broken.discard(index)
        self.current = index
        return 1

    def BASS_IsStarted(self):  # noqa: N802
        return 0 if self.current in self.broken else 2  # inactive, but ready to play

    def BASS_ChannelGetDevice(self, stream):  # noqa: N802
        return self.streams[stream]["device"]

    def BASS_StreamCreateFile(self, *args):  # noqa: N802
        stream = super().BASS_StreamCreateFile(*args)
        if stream:
            self.streams[stream]["device"] = self.current
        return stream


@pytest.fixture
def routes(tmp_path, monkeypatch):
    path = tmp_path / "audio-routes.json"
    monkeypatch.setattr(audio_routes, "routes_path", lambda: path)
    save_audio_routes({"alerts": HEADPHONES, "music": SPEAKERS})
    return path


@pytest.fixture
def core(monkeypatch):
    core = Devices()
    monkeypatch.setattr(lib, "_default_device", 1)
    monkeypatch.setattr(output_devices, "sys", SimpleNamespace(platform="win32"))
    return core


def test_machine_routes_round_trip_unicode_without_device_indices(routes):
    assert load_audio_routes() == {"alerts": HEADPHONES, "music": SPEAKERS}
    text = routes.read_text(encoding="utf-8")
    assert "Наушники" in text
    assert "index" not in text
    assert load_audio_routes(routes.parent / "old-install.json") == {}


def test_legacy_routes_default_to_system_output_and_both_policies_round_trip(core, routes):
    data = json.loads(routes.read_text(encoding="utf-8"))
    for output in data["routes"].values():
        output.pop("fallback", None)
    routes.write_text(json.dumps(data), encoding="utf-8")
    assert load_audio_routes()["alerts"].fallback == "default"
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
    notice = SoundBus(BassSoundBackend(core, route_devices=True)).play("old.wav", route="alerts")
    assert "System default" in notice
    assert [s["device"] for s in core.streams.values()] == [1]
    save_audio_routes({"alerts": HEADPHONES, "private": replace(HEADPHONES, fallback="silent")})
    assert load_audio_routes() == {
        "alerts": HEADPHONES, "private": replace(HEADPHONES, fallback="silent"),
    }
    assert json.loads(routes.read_text(encoding="utf-8"))["version"] == 1


@pytest.mark.parametrize("invalid", ["unknown", None, True, 1, []])
def test_invalid_policy_cannot_discard_an_explicit_silence_preference(core, routes, invalid):
    data = json.loads(routes.read_text(encoding="utf-8"))
    data["routes"]["alerts"]["fallback"] = invalid
    routes.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid fallback"):
        load_audio_routes()
    with pytest.raises(AudioRouteUnavailable, match="staying silent"):
        SoundBus(BassSoundBackend(core, route_devices=True)).play("cue.wav", route="alerts")
    assert not core.streams


def test_unknown_route_uses_default_output_and_preserves_channel_controls(core, routes):
    bus = SoundBus(BassSoundBackend(core, route_devices=True), master=0.5)
    bus.set_volume("warning", 0.4)
    notice = bus.play("critical.wav", "warning", gain=0.5, pan=-0.75, loop=True,
                      route="not configured here")
    assert "not configured" in notice and "System default" in notice
    stream = list(core.streams.values())[0]
    assert stream["device"] == 1 and stream["playing"]
    assert stream["attributes"][lib.ATTRIB_VOL] == pytest.approx(0.1)
    assert stream["attributes"][lib.ATTRIB_PAN] == -0.75
    assert stream["flags"] & lib.SAMPLE_LOOP
    bus.stop("warning")
    assert not core.streams


def test_missing_device_falls_back_once_per_cue_then_recovers(core, routes):
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    notice = bus.play("critical.wav", route="alerts")
    assert "System default" in notice
    assert [s["device"] for s in core.streams.values()] == [1]
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, lib.DEVICE_ENABLED)
    assert bus.play("recovered.wav", route="alerts") is None
    assert [s["device"] for s in core.streams.values()] == [2]
    assert load_audio_routes()["alerts"] == HEADPHONES


def test_fallback_notifications_do_not_repeat_until_a_new_outage(core, routes):
    voice = RecordingBackend()
    app = EngineApp(VoiceRouter(voice), sound_backend=BassSoundBackend(core, route_devices=True))
    for outage in range(2):
        core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
        for _ in range(5):
            app.sink.play("critical.wav", route="alerts")
        assert len(voice.spoken) == outage + 1
        assert "System default" in voice.spoken[-1]
        assert len(app.buffer.lines()) == outage + 1
        assert [s["device"] for s in core.streams.values()] == [1]
        core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, lib.DEVICE_ENABLED)
        app.sink.play("recovered.wav", route="alerts")
        assert [s["device"] for s in core.streams.values()] == [2]
        assert len(voice.spoken) == outage + 1


def test_stay_silent_reports_once_and_recovers_without_default_playback(core, routes):
    save_audio_routes({"alerts": replace(HEADPHONES, fallback="silent")})
    voice = RecordingBackend()
    app = EngineApp(VoiceRouter(voice), sound_backend=BassSoundBackend(core, route_devices=True))
    for outage in range(2):
        core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
        for _ in range(5):
            app.sink.play("private.wav", route="alerts")
        assert not core.streams
        assert len(voice.spoken) == outage + 1 and "staying silent" in voice.spoken[-1]
        core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, lib.DEVICE_ENABLED)
        app.sink.play("private.wav", route="alerts")
        assert [s["device"] for s in core.streams.values()] == [2]
        assert len(voice.spoken) == outage + 1


@pytest.mark.parametrize("data", [[], {}, {"version": 1, "routes": []},
                                     {"version": 1, "routes": {"x": {"identifier": 2}}}])
def test_corrupt_routes_cannot_silently_become_default_output(routes, data):
    routes.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_audio_routes()
    with pytest.raises(AudioRouteUnavailable, match="Cannot read audio routes"):
        SoundBus().play("cue.wav", route="alerts")


def test_enumeration_excludes_pseudo_default_and_disabled_devices(core, monkeypatch):
    monkeypatch.setattr(lib, "load", lambda: lib.Bass(core, None, None))
    monkeypatch.setattr(lib, "start", lambda _bass: None)
    assert available_outputs() == [HEADPHONES, SPEAKERS]
    identity, name, _ = core.devices[2]
    core.devices[2] = (identity, name, 0)
    assert available_outputs() == [SPEAKERS]


def test_windows_initialization_keeps_default_following_and_enables_unicode(monkeypatch):
    from types import SimpleNamespace

    calls = []
    core = SimpleNamespace(
        BASS_SetConfig=lambda *args: calls.append(("config", args)),
        BASS_Init=lambda *args: calls.append(("init", args)) or True,
        BASS_GetDevice=lambda: 1,
    )
    monkeypatch.setattr(lib, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(lib, "_started", False)
    monkeypatch.setattr(lib, "_default_device", None)
    bass = lib.Bass(core, None, None)
    lib.start(bass)
    lib.start(bass)
    assert calls == [
        ("config", (lib.CONFIG_UNICODE, 1)), ("config", (lib.CONFIG_DEV_DEFAULT, 1)),
        ("init", (-1, 44100, 0, None, None)),
    ]
    assert lib._default_device == 1


def test_two_routes_play_simultaneously_and_legacy_play_still_uses_default(core, routes):
    bus = SoundBus(BassSoundBackend(core, route_devices=True), master=0.5)
    bus.set_volume("warning", 0.4)
    bus.play("low.wav", "warning", gain=0.5, pan=-0.75, loop=True, route="alerts")
    bus.play("bell.wav", "notice", route="music")
    bus.play("pack.wav", "sound")
    streams = list(core.streams.values())
    assert [s["device"] for s in streams] == [2, 3, 1]
    assert all(s["playing"] for s in streams)
    assert streams[0]["attributes"][lib.ATTRIB_VOL] == pytest.approx(0.1)
    assert streams[0]["attributes"][lib.ATTRIB_PAN] == -0.75
    assert streams[0]["flags"] & lib.SAMPLE_LOOP
    assert core.current == 1
    first_handle = next(iter(core.streams))
    bus.stop("warning")
    assert first_handle not in core.streams
    assert streams[1]["playing"] and streams[2]["playing"]
    bus.flush()
    assert not core.streams


@pytest.mark.parametrize("second_route, initialized", [("music", [1, 2, 3]), ("alerts", [1, 2])])
def test_two_sessions_share_devices_but_not_channels_or_cleanup(
    core, routes, second_route, initialized,
):
    one = SoundBus(BassSoundBackend(core, route_devices=True))
    two = SoundBus(BassSoundBackend(core, route_devices=True))
    one.play("one.wav", "sound", route="alerts")
    two.play("two.wav", "sound", route=second_route)
    first, second = list(core.streams)
    one.flush()
    assert first not in core.streams and core.streams[second]["playing"]
    assert core.initialized == initialized  # no per-tab BASS_Free


def test_channel_replacement_across_devices_frees_only_its_previous_cue(core, routes):
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    bus.play("old.wav", "same", route="alerts")
    old = next(iter(core.streams))
    bus.play("new.wav", "same", route="music")
    assert old not in core.streams
    new = list(core.streams.values())[0]
    assert len(core.streams) == 1 and new["playing"] and new["device"] == 3


def test_hotplug_resolves_identity_again_not_old_index_or_display_name(core, routes):
    saved = replace(HEADPHONES, fallback="silent")
    save_audio_routes({"alerts": saved})
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    bus.play("one.wav", route="alerts")
    core.devices[2] = ("different-id", HEADPHONES.name, lib.DEVICE_ENABLED)
    with pytest.raises(AudioRouteUnavailable, match="unavailable"):
        bus.play("missing.wav", route="alerts")
    assert not core.streams
    core.devices[4] = (HEADPHONES.identifier, "Renamed headphones", lib.DEVICE_ENABLED)
    bus.play("returned.wav", route="alerts")
    assert list(core.streams.values())[-1]["device"] == 4
    assert load_audio_routes()["alerts"] == saved  # saved assignment/policy weren't discarded


def test_returning_initialized_but_stopped_device_is_reinitialized(core, routes):
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    bus.play("before.wav", route="alerts")
    core.broken.add(2)
    bus.play("after.wav", route="alerts")
    assert core.init_calls == [(2, 0), (2, lib.DEVICE_REINIT)]


def test_missing_device_does_not_leave_previous_default_loop_running(core, routes):
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    bus.play("loop.wav", loop=True)
    previous = next(iter(core.streams))
    routes.unlink()
    notice = bus.play("explicit.wav", route="alerts")
    assert "not configured" in notice and "System default" in notice
    assert previous not in core.streams and len(core.streams) == 1
    assert not list(core.streams.values())[0]["flags"] & lib.SAMPLE_LOOP


def test_saved_device_changes_apply_to_the_next_cue_in_an_existing_session(core, routes):
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    bus.play("first.wav", route="alerts")
    assert list(core.streams.values())[0]["device"] == 2
    save_audio_routes({"alerts": SPEAKERS})
    bus.play("second.wav", route="alerts")
    assert len(core.streams) == 1
    assert list(core.streams.values())[0]["device"] == 3


def test_different_platform_respects_stay_silent(core, routes):
    save_audio_routes({"alerts": replace(HEADPHONES, platform="other-os", fallback="silent")})
    with pytest.raises(AudioRouteUnavailable):
        SoundBus(BassSoundBackend(core, route_devices=True)).play("x.wav", route="alerts")
    assert not core.streams


def test_linux_keeps_default_playback_but_never_guesses_physical_identity(
    core, routes, monkeypatch,
):
    monkeypatch.setattr(output_devices, "sys", SimpleNamespace(platform="linux"))
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    bus.play("default.wav", "legacy")
    assert list(core.streams.values())[0]["device"] == 1
    notice = bus.play("explicit.wav", "routed", route="alerts")
    assert "Windows and macOS" in notice and "System default" in notice
    assert len(core.streams) == 2
    assert all(s["device"] == 1 for s in core.streams.values())
    with pytest.raises(AudioRouteUnavailable, match="Windows and macOS"):
        available_outputs()


def test_context_is_restored_even_if_stream_creation_raises(core):
    with pytest.raises(ValueError):
        with output_context(core, HEADPHONES):
            assert core.current == 2
            raise ValueError("bad file")
    assert core.current == 1


def test_hotplug_cannot_silently_create_stream_on_another_device(core, routes, monkeypatch):
    save_audio_routes({"alerts": replace(HEADPHONES, fallback="silent")})
    # Simulate BASS's documented automatic selection of the lowest initialized device.
    monkeypatch.setattr(core, "BASS_ChannelGetDevice", lambda _stream: 1)
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    with pytest.raises(AudioRouteUnavailable, match="output changed"):
        bus.play("wrong-device.wav", route="alerts")
    assert not core.streams
    assert core.current == 1


def test_device_initialization_failure_stays_silent_and_restores_context(core, routes, monkeypatch):
    save_audio_routes({"alerts": replace(HEADPHONES, fallback="silent")})
    monkeypatch.setattr(core, "BASS_Init", lambda *_args: 0)
    with pytest.raises(AudioRouteUnavailable, match="Cannot open audio output"):
        SoundBus(BassSoundBackend(core, route_devices=True)).play("cue.wav", route="alerts")
    assert not core.streams and core.current == 1


def test_device_loss_between_open_and_start_frees_the_stream(core, routes, monkeypatch):
    save_audio_routes({"alerts": replace(HEADPHONES, fallback="silent")})
    monkeypatch.setattr(core, "BASS_ChannelPlay", lambda *_args: 0)
    with pytest.raises(AudioRouteUnavailable, match="Cannot play on audio output"):
        SoundBus(BassSoundBackend(core, route_devices=True)).play("cue.wav", route="alerts")
    assert not core.streams


@pytest.mark.parametrize("failure", ["initialization", "wrong_device", "start"])
def test_device_failures_fall_back_without_leaking_a_partly_created_cue(
    core, routes, monkeypatch, failure,
):
    if failure == "initialization":
        monkeypatch.setattr(core, "BASS_Init", lambda *_args: 0)
    elif failure == "wrong_device":
        monkeypatch.setattr(core, "BASS_ChannelGetDevice", lambda _stream: 1)
    else:
        play = core.BASS_ChannelPlay
        monkeypatch.setattr(core, "BASS_ChannelPlay", lambda stream, restart:
                            play(stream, restart) if core.streams[stream]["device"] == 1 else 0)
    notice = SoundBus(BassSoundBackend(core, route_devices=True)).play("cue.wav", route="alerts")
    assert "System default" in notice
    assert len(core.streams) == 1
    stream = list(core.streams.values())[0]
    assert stream["device"] == 1 and stream["playing"] and core.current == 1


@pytest.mark.parametrize("available", [True, False])
def test_timing_repeats_and_live_adjustments_use_original_channel(core, routes, available):
    if not available:
        core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
    bus = SoundBus(BassSoundBackend(core, route_devices=True))
    bus.play("cue.wav", "alert", times=2, timing=CueTiming(start=1, finish=3), route="alerts")
    stream = list(core.streams.values())[0]
    assert stream["positions"][lib.POS_BYTE] == 1000
    bus.adjust("alert", gain=0.3, pan=0.5, frequency=32000)
    assert stream["attributes"][lib.ATTRIB_VOL] == 0.3
    assert stream["attributes"][lib.ATTRIB_PAN] == 0.5
    assert stream["attributes"][lib.ATTRIB_FREQ] == 32000
    assert stream["device"] == (2 if available else 1)


@pytest.mark.parametrize("available", [True, False])
def test_routed_cue_effects_and_fade_remain_handle_based(core, routes, available):
    from genericmud.sound.bass_effects import DX8_ECHO

    if not available:
        core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
    backend = BassSoundBackend(core, route_devices=True)
    bus = SoundBus(backend)
    bus.play("echo.wav", "effect", route="alerts")
    effect = backend.add_effect("effect", DX8_ECHO)
    assert effect and effect in core.effects
    bus.fade_away("effect", 0.5)
    stream = list(core.streams.values())[0]
    assert stream["device"] == (2 if available else 1)
    assert stream["slides"]
    bus.flush()
    assert not core.streams and not core.effects


def test_unsupported_backend_reports_once_then_recovers(core, routes):
    voice = RecordingBackend()
    app = EngineApp(VoiceRouter(voice), sound_backend=SoundBackend())
    for _ in range(5):
        app.sink.play("cue.wav", route="alerts")
    assert len(voice.spoken) == 1
    assert "cannot select an output device" in voice.spoken[0]
    assert len(app.buffer.lines()) == 1
    app.sound.set_backend(BassSoundBackend(core, route_devices=True))
    app.sink.play("cue.wav", route="alerts")
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
    app.sink.play("cue.wav", route="alerts")
    app.sink.play("cue.wav", route="alerts")
    assert len(voice.spoken) == 2
    assert "System default" in voice.spoken[-1]
    assert [s["device"] for s in core.streams.values()] == [1]


@pytest.mark.parametrize("fallback", ["default", "silent"])
def test_web_renderer_obeys_route_policy_and_warns_once(routes, fallback):
    save_audio_routes({"alerts": replace(HEADPHONES, fallback=fallback)})
    posted = []
    voice = RecordingBackend()
    app = EngineApp(VoiceRouter(voice), post=posted.append)
    for _ in range(3):
        app.sink.play("explicit.wav", route="alerts")
    sounds = [message for message in posted if message["type"] == "sound"]
    assert len(sounds) == (3 if fallback == "default" else 0)
    assert len(voice.spoken) == 1
    assert "cannot select" in voice.spoken[0]
    app.sink.play("legacy.wav")
    assert any(message["type"] == "sound" for message in posted)


def test_bass_unavailable_uses_existing_backend_for_default_fallback(routes, monkeypatch):
    from genericmud.sound import bass_backend, pygame_backend

    played = []

    class Fallback(SoundBackend):
        def play(self, file, *args, **kwargs):
            played.append(file)

    fallback = Fallback()
    monkeypatch.setattr(bass_backend, "make_bass_backend", lambda **_kw: None)
    monkeypatch.setattr(pygame_backend, "make_pygame_backend", lambda **_kw: fallback)
    bus = SoundBus(make_sound_backend())
    bus.play("default.wav")
    assert "System default" in bus.play("explicit.wav", route="alerts")
    assert played == ["default.wav", "explicit.wav"]
    save_audio_routes({"alerts": replace(HEADPHONES, fallback="silent")})
    with pytest.raises(AudioRouteUnavailable, match="staying silent"):
        bus.play("private.wav", route="alerts")
    assert played == ["default.wav", "explicit.wav"]


def test_test_tone_uses_same_backend_and_closing_preview_releases_its_stream(core, monkeypatch):
    import wave

    from genericmud.sound import output_preview

    backend = BassSoundBackend(core, route_devices=True)
    monkeypatch.setattr(output_preview, "make_bass_backend", lambda **_kw: backend)
    preview = output_preview.OutputPreview()
    try:
        preview.play(HEADPHONES)
        stream = list(core.streams.values())[0]
        assert stream["device"] == 2 and stream["playing"]
        with wave.open(str(preview._file), "rb") as tone:
            assert tone.getnframes() == 11025 and tone.getframerate() == 22050
        path = preview._file
    finally:
        preview.close()
    assert not core.streams and not path.exists()


@pytest.mark.parametrize("fallback", ["default", "silent"])
def test_trigger_route_persists_and_reaches_real_app_playback(core, routes, tmp_path, fallback):
    save_audio_routes({"alerts": replace(HEADPHONES, fallback=fallback)})
    rules = UserRules(triggers=[UserTrigger(
        pattern="danger", sound="warning.wav", audio_route="alerts", channel="health",
    )])
    rules = UserRules.from_json(rules.to_json())
    assert rules.triggers[0].audio_route == "alerts"
    assert UserRules.from_json('{"triggers":[{"pattern":"old"}]}').triggers[0].audio_route == ""
    app = EngineApp(VoiceRouter(RecordingBackend()),
                    sound_backend=BassSoundBackend(core, route_devices=True))
    register_rules(ScriptApi(app.engine, base_dir=tmp_path), rules)
    app.engine.process_line(Line("danger"))
    cue = list(core.streams.values())[0]
    assert cue["device"] == 2 and cue["playing"]
    app.sound.stop("user-health")
    assert not core.streams
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
    app.engine.process_line(Line("danger"))
    assert [s["device"] for s in core.streams.values()] == ([1] if fallback == "default" else [])
    app.sound.flush()
    assert not core.streams


def test_sessions_can_use_different_fallbacks_for_the_same_physical_device(core, routes):
    save_audio_routes({"audible": HEADPHONES, "private": replace(HEADPHONES, fallback="silent")})
    audible = SoundBus(BassSoundBackend(core, route_devices=True))
    private = SoundBus(BassSoundBackend(core, route_devices=True))
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
    audible.play("critical.wav", "sound", route="audible")
    with pytest.raises(AudioRouteUnavailable, match="staying silent"):
        private.play("private.wav", "sound", route="private")
    private.flush()
    assert [s["device"] for s in core.streams.values()] == [1]
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, lib.DEVICE_ENABLED)
    audible.play("recovered.wav", "sound", route="audible")
    private.play("private.wav", "sound", route="private")
    assert [s["device"] for s in core.streams.values()] == [2, 2]
    audible.flush()
    assert len(core.streams) == 1 and list(core.streams.values())[0]["file"] == "private.wav"
    private.flush()
    assert not core.streams


def test_missing_name_has_default_fallback_until_a_local_policy_exists(core, routes):
    save_audio_routes({"private": replace(HEADPHONES, fallback="silent")})
    voice = RecordingBackend()
    app = EngineApp(VoiceRouter(voice), sound_backend=BassSoundBackend(core, route_devices=True))
    for _ in range(3):
        app.sink.play("imported.wav", route="imported route")
    assert len(voice.spoken) == 1 and "System default" in voice.spoken[0]
    assert [s["device"] for s in core.streams.values()] == [1]
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, 0)
    save_audio_routes({"imported route": replace(HEADPHONES, fallback="silent")})
    for _ in range(3):
        app.sink.play("imported.wav", route="imported route")
    assert not core.streams
    assert len(voice.spoken) == 2 and "staying silent" in voice.spoken[-1]
    core.devices[2] = (HEADPHONES.identifier, HEADPHONES.name, lib.DEVICE_ENABLED)
    app.sink.play("imported.wav", route="imported route")
    assert [s["device"] for s in core.streams.values()] == [2]
    assert len(voice.spoken) == 2
