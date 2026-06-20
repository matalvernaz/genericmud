"""Integration core: turns the telnet event stream and renderer messages into
engine processing, self-voice, and outbound render messages.

Decoupled from transport and UI via three injected callables — ``send`` (to the
MUD), ``post`` (to the renderer), ``schedule`` (timers) — plus a VoiceRouter, so
the whole glue layer is unit-testable without a socket, a webview, or NVDA.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from genericmud.automation.engine import AutomationEngine, EngineSink
from genericmud.bridge import protocol
from genericmud.model.buffer import Buffer, Line
from genericmud.protocol import telnet as T
from genericmud.protocol.oob import OobMessage, ServerStatus, from_subnegotiation
from genericmud.review.cursor import ReviewCursor
from genericmud.voice.router import VoiceRouter

REVIEW_CHANNEL = "review"
_REVIEW_VERBS = frozenset(
    {"prev_line", "next_line", "prev_word", "next_word", "prev_char", "next_char", "top", "bottom"}
)


def _default_schedule(delay: float, callback: Callable[[], None]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_later(delay, callback)


class AppSink(EngineSink):
    """Routes engine side effects to the MUD, the renderer, and the voice router."""

    def __init__(self, *, send, post, schedule, voice: VoiceRouter, buffer: Buffer) -> None:
        self._send = send
        self._post = post
        self._schedule = schedule
        self._voice = voice
        self._buffer = buffer

    def send(self, text: str) -> None:
        self._send(text)

    def echo(self, text: str, channel: str = "main") -> None:
        self._buffer.append(Line(text, channel=channel))
        self._post(protocol.echo(text, channel))

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self._voice.speak(text, channel, interrupt)

    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
    ) -> None:
        self._post(protocol.sound(file, channel, gain, pan, loop))

    def stop(self, channel: str) -> None:
        self._post(protocol.stop_sound(channel))

    def music(self, file: str, channel: str = "music") -> None:
        self._post(protocol.music(file))

    def schedule(self, delay: float, callback: Callable[[], None]) -> None:
        self._schedule(delay, callback)


class EngineApp:
    def __init__(
        self,
        voice: VoiceRouter,
        *,
        send: Callable[[str], None] | None = None,
        post: Callable[[dict], None] | None = None,
        schedule: Callable[[float, Callable[[], None]], None] | None = None,
        keymap: dict[str, str] | None = None,
    ) -> None:
        self.buffer = Buffer()
        self.voice = voice
        self._send = send or (lambda _text: None)
        self._post = post or (lambda _message: None)
        self.sink = AppSink(
            send=self._send,
            post=self._post,
            schedule=schedule or _default_schedule,
            voice=voice,
            buffer=self.buffer,
        )
        self.engine = AutomationEngine(self.sink)
        self.review = ReviewCursor(self.buffer)
        self.keymap = keymap or {}
        self._pending = ""
        self._gauges: dict[str, object] = {}

    # --- inbound from the MUD (telnet events) ---

    def on_telnet_event(self, event: T.Event) -> None:
        if isinstance(event, T.DataReceived):
            self._feed_text(event.data.decode("utf-8", "replace"))
        elif isinstance(event, T.Subnegotiation):
            self._handle_subnegotiation(event)
        elif isinstance(event, T.Command) and event.command in (T.GA, T.EOR):
            self._flush_prompt()  # prompt arrived without a trailing newline

    def _feed_text(self, text: str) -> None:
        self._pending += text
        while "\n" in self._pending:
            raw, self._pending = self._pending.split("\n", 1)
            self._emit_line(raw.rstrip("\r"))

    def _flush_prompt(self) -> None:
        if self._pending:
            self._emit_line(self._pending)
            self._pending = ""

    def _emit_line(self, text: str) -> None:
        line = Line(text)
        self.engine.process_line(line)
        if not line.gagged:
            self.voice.speak(line.plain_text)
        if not (line.gagged and not line.display_when_gagged):
            self.buffer.append(line)
            self._post(
                protocol.line(
                    line.plain_text,
                    gagged=line.gagged,
                    display_when_gagged=line.display_when_gagged,
                )
            )

    def _handle_subnegotiation(self, sub: T.Subnegotiation) -> None:
        result = from_subnegotiation(sub.option, sub.payload)
        if isinstance(result, list):
            for message in result:
                if isinstance(message, OobMessage):
                    self._gauges[message.name] = message.value
            if result:
                self._post(protocol.status(self._gauges))
        elif isinstance(result, ServerStatus):
            self._gauges.update(result.data)
            self._post(protocol.status(self._gauges))

    # --- inbound from the renderer (WS messages) ---

    def on_ws_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == protocol.INPUT:
            for line in self.engine.process_input(message.get("text", "")):
                self._send(line)
        elif kind == protocol.KEY:
            self._handle_key(message.get("key", ""))

    def _handle_key(self, combo: str) -> None:
        action = self.keymap.get(combo)
        if action is None:
            self.engine.press_key(combo)  # user-defined macro
            return
        namespace, _, argument = action.partition(":")
        if namespace == "recall":
            self._speak_review(self.review.recall(int(argument)) or "no message")
        elif namespace == "review" and argument in _REVIEW_VERBS:
            if not self.review.active:
                self.review.enter()
            self._speak_review(getattr(self.review, argument)())
        elif namespace == "voice" and argument == "flush":
            self.voice.flush()
        # "soundpack:toggle" and other namespaces are wired as features land.

    def _speak_review(self, text: str) -> None:
        self.voice.speak(text, channel=REVIEW_CHANNEL, interrupt=True)
        self._post(protocol.review(text))
