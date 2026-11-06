"""Shared test helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from genericmud.automation.engine import EngineSink
from genericmud.model.buffer import Line


@dataclass
class RecordingSink(EngineSink):
    """Captures every engine side effect for assertions, with a manual scheduler."""

    sent: list[str] = field(default_factory=list)
    packets: list[bytes] = field(default_factory=list)
    echoed: list[tuple[str, str]] = field(default_factory=list)
    echo_flags: list[bool] = field(default_factory=list)  # each echoed line's `log` flag
    filed: list[tuple[str, str]] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)  # put in the command box, unsent
    typed_only_if_empty: list[bool] = field(default_factory=list)  # ... and whether it may replace
    logged: list[str] = field(default_factory=list)  # written to the log without being shown
    spoken: list[tuple[str, str, bool]] = field(default_factory=list)
    played: list[dict[str, Any]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    musics: list[str] = field(default_factory=list)
    scheduled: list[tuple[float, Callable[[], None]]] = field(default_factory=list)
    speech_stops: int = 0
    lines: list[str] = field(default_factory=list)  # the output window, oldest first
    answers: list[Any] = field(default_factory=list)  # the player's replies to pack dialogs
    asked: list[dict] = field(default_factory=list)  # the dialogs packs asked for
    styled: dict[int, list] = field(default_factory=dict)  # colour runs for lines[index]
    simulated: list[str] = field(default_factory=list)  # text packs handed in as if from the MUD
    save_requests: int = 0
    update_requests: list[bool] = field(default_factory=list)  # quiet flag of each pack update ask
    update_checks: int = 0

    def send(self, text: str) -> None:
        self.sent.append(text)

    def send_packet(self, data: bytes) -> None:
        self.packets.append(data)

    def echo(self, text: str, channel: str = "main", *, log: bool = False) -> None:
        self.echoed.append((text, channel))
        self.echo_flags.append(log)

    def type_text(self, text: str, *, only_if_empty: bool = False) -> None:
        self.typed.append(text)
        self.typed_only_if_empty.append(only_if_empty)

    def log_line(self, text: str) -> None:
        self.logged.append(text)

    def file_line(self, text: str, channel: str) -> None:
        self.filed.append((text, channel))

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self.spoken.append((text, channel, interrupt))

    def stop_speech(self) -> None:
        self.speech_stops += 1

    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
        times: int = 1,
    ) -> None:
        self.played.append(
            {"file": file, "channel": channel, "gain": gain, "pan": pan, "loop": loop,
             "times": times}
        )

    def stop(self, channel: str) -> None:
        self.stopped.append(channel)

    def music(self, file: str, channel: str = "music") -> None:
        self.musics.append(file)

    def schedule(self, delay: float, callback: Callable[[], None]) -> None:
        self.scheduled.append((delay, callback))

    def ask(self, request: dict, while_waiting: Callable[[], None] | None = None) -> Any:
        self.asked.append(request)
        return self.answers.pop(0) if self.answers else None

    def recent_lines(self, count: int) -> list[str]:
        return self.lines[-count:] if count > 0 else []

    def line_count(self) -> int:
        return len(self.lines)

    def buffered_line(self, number: int) -> Line | None:
        if not 1 <= number <= len(self.lines):
            return None
        return Line(self.lines[number - 1], spans=self.styled.get(number - 1, []))

    def simulate(self, text: str) -> None:
        self.simulated.append(text)

    def save_state(self) -> None:
        self.save_requests += 1

    def update_packs(self, quiet: bool = False) -> None:
        self.update_requests.append(quiet)

    def check_pack_updates(self) -> None:
        self.update_checks += 1

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
