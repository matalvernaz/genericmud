"""Cross-platform self-voice backend selection, the prism backend, and queued subprocess TTS."""

from __future__ import annotations

import sys
import time
import types

import pytest

from genericmud.voice import factory
from genericmud.voice.backends import subprocess_tts
from genericmud.voice.backends.prism_tts import PrismBackend
from genericmud.voice.backends.subprocess_tts import (
    MacSayBackend,
    QueuedTtsBackend,
    SpeechDispatcherBackend,
)


@pytest.fixture(autouse=True)
def _no_screen_reader(monkeypatch):
    # prism is installed on every CI runner, so the factory would return PrismBackend before
    # reaching the platform branch. Force the screen-reader path to be unavailable so these
    # tests deterministically exercise the platform TTS selection.
    def _unavailable(*_a, **_k):
        raise RuntimeError("no screen reader in test")

    monkeypatch.setattr("genericmud.voice.backends.prism_tts.PrismBackend", _unavailable)


def test_factory_picks_say_on_macos(monkeypatch):
    monkeypatch.setattr(factory.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess_tts.shutil, "which", lambda _cmd: "/usr/bin/say")
    backend = factory.make_voice_backend()
    try:
        assert isinstance(backend, MacSayBackend)
    finally:
        backend.close()


def test_factory_picks_speech_dispatcher_on_linux(monkeypatch):
    monkeypatch.setattr(factory.sys, "platform", "linux")
    monkeypatch.setattr(subprocess_tts.shutil, "which", lambda _cmd: "/usr/bin/spd-say")
    backend = factory.make_voice_backend()
    try:
        assert isinstance(backend, SpeechDispatcherBackend)
    finally:
        backend.close()


def test_factory_falls_back_to_print_when_no_tts(monkeypatch):
    # No screen reader, no platform TTS on PATH: degrade to the silent print backend
    # rather than crash the app's first announcement.
    monkeypatch.setattr(factory.sys, "platform", "linux")
    monkeypatch.setattr(subprocess_tts.shutil, "which", lambda _cmd: None)
    assert isinstance(factory.make_voice_backend(), factory.PrintBackend)


class _FakePrismBackend:
    """Stands in for a prism-acquired screen reader, with prism's own input rules enforced."""

    name = "FakeReader"

    def __init__(self, *, supports_output: bool = True, supports_speak: bool = True) -> None:
        self.features = types.SimpleNamespace(
            supports_output=supports_output, supports_speak=supports_speak
        )
        self.calls: list[tuple[str, str]] = []
        self.stops = 0

    def _check(self, text: str) -> None:
        # Real prism raises PrismInvalidParamError on either of these rather than no-opping.
        assert text, "prism rejects empty text"
        assert "\x00" not in text, "prism rejects embedded NULs"

    def output(self, text: str, interrupt: bool = False) -> None:
        self._check(text)
        self.calls.append(("output", text))

    def speak(self, text: str, interrupt: bool = False) -> None:
        self._check(text)
        self.calls.append(("speak", text))

    def stop(self) -> None:
        self.stops += 1


class _FakePrismContext:
    """Stands in for prism.Context, holding the availability hook the backend registers.

    ``create_best`` hands out each queued backend in turn and repeats the last one, so a test
    can script what a rebuild finds. A queued ``Exception`` is raised instead of returned.

    ``acquire_best`` deliberately models prism's weak-pointer cache: once an instance has been
    handed out it comes back on every later call, with no availability check (see
    FrozenRegistry::acquire_best). Re-acquiring that way returns the reader that just died, so
    a backend written against it keeps a dead handle -- and these tests must fail, not pass.
    """

    def __init__(self, backends, *, on_availability=None, poll_interval_ms: int = 0) -> None:
        self._backends = list(backends)
        self.on_availability = on_availability
        self.poll_interval_ms = poll_interval_ms
        self.creations = 0
        self._cached = None

    def create_best(self):
        picked = self._backends[min(self.creations, len(self._backends) - 1)]
        self.creations += 1
        if isinstance(picked, Exception):
            raise picked
        self._cached = picked
        return picked

    def acquire_best(self):
        if self._cached is not None:
            return self._cached
        return self.create_best()


def _install_fake_prism(monkeypatch, *backends) -> list[_FakePrismContext]:
    """Install a fake ``prism`` module; the returned list receives each Context created."""
    contexts: list[_FakePrismContext] = []

    def _context(**kwargs):
        ctx = _FakePrismContext(backends, **kwargs)
        contexts.append(ctx)
        return ctx

    module = types.ModuleType("prism")
    module.Context = _context
    monkeypatch.setitem(sys.modules, "prism", module)
    return contexts


def test_prism_speaks_through_output_so_braille_displays_get_the_line(monkeypatch):
    # `output` reaches both speech and braille; a braille-only user hears nothing otherwise.
    reader = _FakePrismBackend()
    _install_fake_prism(monkeypatch, reader)
    backend = PrismBackend()
    backend.speak("you are standing in a field")
    backend.stop()
    assert reader.calls == [("output", "you are standing in a field")]
    assert reader.stops == 1


def test_prism_falls_back_to_speak_when_the_reader_has_no_output(monkeypatch):
    reader = _FakePrismBackend(supports_output=False)
    _install_fake_prism(monkeypatch, reader)
    PrismBackend().speak("a tell arrives")
    assert reader.calls == [("speak", "a tell arrives")]


def test_prism_filters_text_the_library_would_raise_on(monkeypatch):
    # Blank lines are ordinary MUD traffic, and a NUL can survive a protocol edge case; both
    # raise out of prism, which would cost the user the utterance (and, in the wx announcer,
    # more than that). They must be filtered before the call, not swallowed after it.
    reader = _FakePrismBackend()
    _install_fake_prism(monkeypatch, reader)
    backend = PrismBackend()
    backend.speak("")
    backend.speak("\x00")
    backend.speak("hea\x00lth: 50")
    assert reader.calls == [("output", "health: 50")]


def test_prism_construction_fails_over_when_the_reader_cannot_speak(monkeypatch):
    # A backend advertising neither entry point would accept every utterance silently; the
    # factory has to learn that here, while it can still try SAPI/say/spd-say instead.
    _install_fake_prism(
        monkeypatch, _FakePrismBackend(supports_output=False, supports_speak=False)
    )
    with pytest.raises(RuntimeError):
        PrismBackend()


def test_prism_takes_a_fresh_reader_after_the_screen_reader_restarts(monkeypatch):
    # NVDA crashing and relaunching, or being restarted with NVDA+Q, is routine. prism hands
    # out a handle fixed at acquire time, so without following availability the client keeps
    # talking to the dead one for the rest of the session and _safe() hides every failure.
    crashed, relaunched = _FakePrismBackend(), _FakePrismBackend()
    contexts = _install_fake_prism(monkeypatch, crashed, relaunched)
    backend = PrismBackend()
    backend.speak("before the crash")
    contexts[0].on_availability(0, "NVDA", True)
    backend.speak("after the relaunch")
    assert crashed.calls == [("output", "before the crash")]
    assert relaunched.calls == [("output", "after the relaunch")]


def test_prism_registers_an_availability_hook_and_polls(monkeypatch):
    # Without both of these the re-acquire above can never fire on a real backend.
    contexts = _install_fake_prism(monkeypatch, _FakePrismBackend())
    PrismBackend()
    assert contexts[0].on_availability is not None
    assert contexts[0].poll_interval_ms > 0


def test_prism_keeps_the_working_reader_when_reacquiring_fails(monkeypatch):
    # A failed re-acquire must cost the event, never the voice. Going mute because the new pick
    # refused is worse than staying on a reader that may well still be alive.
    reader = _FakePrismBackend()
    contexts = _install_fake_prism(monkeypatch, reader, RuntimeError("nothing available"))
    backend = PrismBackend()
    contexts[0].on_availability(0, "NVDA", False)
    backend.speak("still audible")
    assert reader.calls == [("output", "still audible")]


def test_prism_reacquire_is_not_attempted_until_availability_changes(monkeypatch):
    # Re-acquiring per utterance would spin up a native handle for every MUD line.
    contexts = _install_fake_prism(monkeypatch, _FakePrismBackend())
    backend = PrismBackend()
    backend.speak("one")
    backend.speak("two")
    assert contexts[0].creations == 1


def test_factory_prefers_prism_over_the_platform_command(monkeypatch):
    # Undo the autouse fixture: this is the one test that wants the real selection order.
    monkeypatch.setattr("genericmud.voice.backends.prism_tts.PrismBackend", PrismBackend)
    _install_fake_prism(monkeypatch, _FakePrismBackend())
    monkeypatch.setattr(factory.sys, "platform", "linux")
    monkeypatch.setattr(subprocess_tts.shutil, "which", lambda _cmd: "/usr/bin/spd-say")
    assert isinstance(factory.make_voice_backend(), PrismBackend)


def test_queued_backend_requires_the_command_on_path(monkeypatch):
    monkeypatch.setattr(subprocess_tts.shutil, "which", lambda _cmd: None)
    with pytest.raises(RuntimeError):
        QueuedTtsBackend(["definitely-not-a-real-tts"])


def test_queued_backend_speaks_serially_and_stop_drains(monkeypatch):
    # Use a benign real command so the worker actually spawns and waits, exercising the
    # queue/subprocess path without needing an audio device.
    monkeypatch.setattr(subprocess_tts.shutil, "which", lambda _cmd: "/usr/bin/true")
    backend = QueuedTtsBackend(["true"])
    try:
        backend.speak("one")
        backend.speak("two")
        backend.speak("")  # empty text is ignored, not queued
        backend.stop()  # must not raise; drains the queue and terminates any current proc
        time.sleep(0.05)
    finally:
        backend.close()


def test_cancel_command_runs_on_stop(monkeypatch):
    monkeypatch.setattr(subprocess_tts.shutil, "which", lambda _cmd: "/usr/bin/true")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess_tts.subprocess, "run",
        lambda cmd, **_kw: calls.append(cmd),
    )
    backend = QueuedTtsBackend(["true"], cancel_command=["spd-say", "-C"])
    try:
        backend.stop()
        assert ["spd-say", "-C"] in calls  # the daemon-cancel ran
    finally:
        backend.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command for the smoke test")
def test_queued_backend_actually_runs_the_command(monkeypatch, tmp_path):
    # Prove the worker runs the command with the text as the final argv element.
    marker = tmp_path / "spoken.txt"
    script = tmp_path / "fake_tts.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{marker}"\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(subprocess_tts.shutil, "which", lambda _cmd: str(script))
    backend = QueuedTtsBackend([str(script)])
    try:
        backend.speak("hello world")
        for _ in range(100):
            if marker.exists() and marker.read_text().strip():
                break
            time.sleep(0.02)
        assert marker.read_text().strip() == "hello world"
    finally:
        backend.close()
