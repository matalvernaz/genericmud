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


def _install_fake_prism(monkeypatch, backend: _FakePrismBackend) -> None:
    module = types.ModuleType("prism")
    module.Context = lambda: types.SimpleNamespace(acquire_best=lambda: backend)
    monkeypatch.setitem(sys.modules, "prism", module)


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
