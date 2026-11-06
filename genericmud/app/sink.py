"""The automation engine's side effects, routed through its world's session."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from genericmud.automation.engine import EngineSink
from genericmud.bridge import protocol
from genericmud.model.buffer import Buffer, Line
from genericmud.sound.bus import SoundBus
from genericmud.voice.router import VoiceRouter

if TYPE_CHECKING:
    from genericmud.session.diaglog import DiagnosticLog


class AppSink(EngineSink):
    """Routes engine side effects to the MUD, the renderer, and the voice router."""

    def __init__(
        self, *, send, post, schedule, voice: VoiceRouter, buffer: Buffer, sound: SoundBus,
        diag: DiagnosticLog | None = None, send_raw=None, ask=None, simulate=None,
        save_state=None, window=None, update_packs=None, check_pack_updates=None,
        log_note=None, log_line=None,
    ) -> None:
        self._send = send
        self._send_raw = send_raw
        self._post = post
        self._schedule = schedule
        self._voice = voice
        self._buffer = buffer
        self._sound = sound
        self._diag = diag
        self._ask = ask
        self._simulate = simulate
        self._save_state = save_state
        self._update_packs = update_packs
        self._check_pack_updates = check_pack_updates
        self._window = window
        self._log_note = log_note
        self._log_line = log_line
        self.cues = 0  # play/music calls reaching the bus -- 0 means no cue ever fired

    def send(self, text: str) -> None:
        self._send(text)

    def send_packet(self, data: bytes) -> None:
        if self._send_raw is None:
            # No raw transport wired (headless/tests): trace it so a pack whose MSDP
            # REPORT went nowhere is diagnosable, then drop the packet.
            if self._diag is not None:
                self._diag.event("sendpkt.dropped", size=len(data))
            return
        self._send_raw(data)

    def echo(self, text: str, channel: str = "main", *, log: bool = False) -> None:
        self._buffer.append(Line(text, channel=channel))
        self._post(protocol.echo(text, channel))
        if log:
            self._log_note(text)  # the world's "log notes" setting decides whether it lands

    def type_text(self, text: str) -> None:
        self._post(protocol.input_text(text))

    def log_line(self, text: str) -> None:
        self._log_line(text)

    def file_line(self, text: str, channel: str) -> None:
        self._buffer.append(Line(text, channel=channel))

    def recent_lines(self, count: int) -> list[str]:
        start = max(0, len(self._buffer) - count)
        return [self._buffer[index].plain_text for index in range(start, len(self._buffer))]

    def line_count(self) -> int:
        return len(self._buffer)

    def buffered_line(self, number: int) -> Line | None:
        return self._buffer[number - 1] if 1 <= number <= len(self._buffer) else None

    def simulate(self, text: str) -> None:
        if self._simulate is not None:
            self._simulate(text)

    def save_state(self) -> None:
        if self._save_state is not None:
            self._save_state()

    def update_packs(self, quiet: bool = False) -> None:
        if self._update_packs is not None:
            self._update_packs(quiet=quiet)

    def check_pack_updates(self) -> None:
        if self._check_pack_updates is not None:
            self._check_pack_updates()

    def pack_window(self):
        return self._window() if self._window is not None else None

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self._voice.speak(text, channel, interrupt)

    def stop_speech(self) -> None:
        # A soundpack in a background tab keeps running (MUSHclient packs stop NVDA on their
        # own triggers), but the screen reader is shared: its stop would cut the world in
        # front. Only a world that may speak may stop speech.
        if self._voice.muted:
            return
        self._voice.flush()

    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
        times: int = 1,
    ) -> None:
        self.cues += 1
        if self._diag is not None:
            policy = self._sound.policy(channel)
            self._diag.event(
                "sink.gain", file=file, channel=channel, master=self._sound.master,
                cat_gain=policy.gain, muted=policy.muted, cue_gain=gain,
                effective=self._sound.effective_gain(channel, gain),
            )
        self._sound.play(file, channel, gain, pan, loop, times=times)

    def stop(self, channel: str) -> None:
        self._sound.stop(channel)

    def music(self, file: str, channel: str = "music") -> None:
        self.cues += 1
        if self._diag is not None:
            policy = self._sound.policy(channel)
            self._diag.event(
                "sink.gain", file=file, channel=channel, kind="music",
                master=self._sound.master, cat_gain=policy.gain, muted=policy.muted,
                effective=self._sound.effective_gain(channel),
            )
        self._sound.music(file, channel)

    def schedule(self, delay: float, callback: Callable[[], None]) -> object | None:
        return self._schedule(delay, callback)

    def ask(
        self, request: dict, while_waiting: Callable[[], None] | None = None
    ) -> object | None:
        if self._ask is None:
            return None
        return self._ask(request, while_waiting)
