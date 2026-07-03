"""Shared test helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from genericmud.automation.engine import EngineSink


@dataclass
class RecordingSink(EngineSink):
    """Captures every engine side effect for assertions, with a manual scheduler."""

    sent: list[str] = field(default_factory=list)
    packets: list[bytes] = field(default_factory=list)
    echoed: list[tuple[str, str]] = field(default_factory=list)
    spoken: list[tuple[str, str, bool]] = field(default_factory=list)
    played: list[dict[str, Any]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    musics: list[str] = field(default_factory=list)
    scheduled: list[tuple[float, Callable[[], None]]] = field(default_factory=list)

    def send(self, text: str) -> None:
        self.sent.append(text)

    def send_packet(self, data: bytes) -> None:
        self.packets.append(data)

    def echo(self, text: str, channel: str = "main") -> None:
        self.echoed.append((text, channel))

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self.spoken.append((text, channel, interrupt))

    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
    ) -> None:
        self.played.append(
            {"file": file, "channel": channel, "gain": gain, "pan": pan, "loop": loop}
        )

    def stop(self, channel: str) -> None:
        self.stopped.append(channel)

    def music(self, file: str, channel: str = "music") -> None:
        self.musics.append(file)

    def schedule(self, delay: float, callback: Callable[[], None]) -> None:
        self.scheduled.append((delay, callback))

    def run_pending(self) -> None:
        """Fire all scheduled callbacks (ignoring delay) and clear the queue."""
        pending = list(self.scheduled)
        self.scheduled.clear()
        for _delay, callback in pending:
            callback()


@dataclass
class RecordingBackend:
    """A VoiceBackend that records what was spoken/stopped, for assertions."""

    spoken: list[str] = field(default_factory=list)
    stops: int = 0

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stops += 1


@dataclass
class RecordingDiag:
    """A DiagnosticLog stand-in that captures events instead of writing a file."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def event(self, stage: str, **fields: Any) -> None:
        self.events.append((stage, fields))

    def stages(self) -> list[str]:
        return [stage for stage, _ in self.events]

    def fields(self, stage: str) -> dict[str, Any]:
        """The fields of the first event with this stage (for single-occurrence asserts)."""
        return next(f for s, f in self.events if s == stage)
