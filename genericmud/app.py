"""Integration core: turns the telnet event stream and renderer messages into
engine processing, self-voice, and outbound render messages.

Decoupled from transport and UI via three injected callables — ``send`` (to the
MUD), ``post`` (to the renderer), ``schedule`` (timers) — plus a VoiceRouter, so
the whole glue layer is unit-testable without a socket, a webview, or NVDA.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from genericmud.automation.channels import ChannelPolicy
from genericmud.automation.engine import AutomationEngine, EngineSink
from genericmud.bridge import protocol
from genericmud.config.worlds import config_dir
from genericmud.model.buffer import Buffer, Line
from genericmud.navigation import Navigator, SafeWalk, expand_speedwalk
from genericmud.packs import ActivationResult, PackStore, activate_world
from genericmud.protocol import telnet as T
from genericmud.protocol.msp import parse_msp_line
from genericmud.protocol.oob import OobMessage, ServerStatus, from_subnegotiation
from genericmud.render.ansi import parse_ansi
from genericmud.review.cursor import ReviewCursor
from genericmud.session.credentials import CredentialStore
from genericmud.session.log import SessionLogger
from genericmud.session.login import AutoLogin
from genericmud.sound.bus import SoundBackend, SoundBus
from genericmud.voice.router import VoiceRouter

REVIEW_CHANNEL = "review"
SPEEDWALK_PREFIX = "."  # ".3n2e" expands to n,n,n,e,e (leading char disambiguates)
SAFE_PREFIX = ".."  # "..3n2e" walks the same route one step at a time, halting if blocked
_REVIEW_VERBS = frozenset(
    {"prev_line", "next_line", "prev_word", "next_word", "prev_char", "next_char", "top", "bottom"}
)


def _default_schedule(delay: float, callback: Callable[[], None]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_later(delay, callback)


class _PostSoundBackend(SoundBackend):
    """SoundBus backend that posts renderer protocol messages (the Web Audio path)."""

    def __init__(self, post) -> None:
        self._post = post

    def play(self, file: str, channel: str, gain: float, pan: float, loop: bool) -> None:
        self._post(protocol.sound(file, channel, gain, pan, loop))

    def music(self, file: str, channel: str, gain: float) -> None:
        self._post(protocol.music(file, channel, gain))

    def stop(self, channel: str) -> None:
        self._post(protocol.stop_sound(channel))


class AppSink(EngineSink):
    """Routes engine side effects to the MUD, the renderer, and the voice router."""

    def __init__(
        self, *, send, post, schedule, voice: VoiceRouter, buffer: Buffer, sound: SoundBus
    ) -> None:
        self._send = send
        self._post = post
        self._schedule = schedule
        self._voice = voice
        self._buffer = buffer
        self._sound = sound

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
        self._sound.play(file, channel, gain, pan, loop)

    def stop(self, channel: str) -> None:
        self._sound.stop(channel)

    def music(self, file: str, channel: str = "music") -> None:
        self._sound.music(file, channel)

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
        packs: PackStore | None = None,
        sound_backend: SoundBackend | None = None,
        name: str = "",
        log_dir: Path | None = None,
        credentials: CredentialStore | None = None,
    ) -> None:
        self.buffer = Buffer()
        self.voice = voice
        self.packs = packs
        self._send = send or (lambda _text: None)
        self._post = post or (lambda _message: None)
        self._schedule = schedule or _default_schedule
        # Native (wx) injects a pygame backend; otherwise sounds post to the renderer.
        self.sound = SoundBus(sound_backend or _PostSoundBackend(self._post))
        self.sink = AppSink(
            send=self._send,
            post=self._post,
            schedule=self._schedule,
            voice=voice,
            buffer=self.buffer,
            sound=self.sound,
        )
        self.engine = AutomationEngine(self.sink, sound=self.sound)
        self.review = ReviewCursor(self.buffer)
        self.channels = self.engine.channels  # router lives on the engine (scriptable)
        # Alerts barge in; everything else stays on the governed 'main' channel by default.
        self.channels.set_policy("tell", ChannelPolicy(interrupt=True))
        self.channels.set_policy("system", ChannelPolicy(interrupt=True))
        self.keymap = keymap or {}
        self.nav = Navigator()  # breadcrumb trail + GMCP room for speedwalk/where-am-I
        self.command_separator = ";"  # stacked input ("n;n;look"); set "" to disable
        self.name = name  # session label, used for the log filename
        self.log_dir = Path(log_dir) if log_dir else config_dir() / "logs"
        self.logger: SessionLogger | None = None
        self.credentials = credentials
        self._login: AutoLogin | None = None
        self._walk: SafeWalk | None = None
        self._pending = ""
        self._gauges: dict[str, object] = {}

    # --- soundpacks ---

    def activate_packs(self, world: str) -> ActivationResult | None:
        """Load the packs enabled for ``world`` and announce the outcome aloud.

        Call after constructing the app and before connecting, so triggers are
        armed when data arrives. No-op (returns None) when no store is wired.
        """
        if self.packs is None:
            return None
        result = activate_world(self.packs, world, self.engine)
        self._announce_activation(result)
        return result

    def on_connect(self, world: str) -> ActivationResult | None:
        """The on-connect sequence: activate enabled packs, then arm auto-login."""
        result = self.activate_packs(world)
        self.begin_login(world)
        return result

    def begin_login(self, world: str) -> None:
        """Arm auto-login for ``world`` if credentials are stored for it."""
        self._login = None
        if self.credentials is None:
            return
        creds = self.credentials.get(world)
        if creds is None:
            return
        username, password = creds
        self._login = AutoLogin(username, password, self._send)

    def _announce_activation(self, result: ActivationResult) -> None:
        parts: list[str] = []
        if result.loaded:
            parts.append(f"{len(result.loaded)} soundpack{'s' if len(result.loaded) != 1 else ''}")
        for pack_id in result.skipped_untrusted:
            parts.append(f"{pack_id} not loaded, not trusted")
        for pack_id, error in result.failed.items():
            parts.append(f"{pack_id} failed to load: {error}")
        for conflict in result.conflicts:
            who = " and ".join(conflict.sources)
            parts.append(f"{conflict.kind} {conflict.token} bound by {who}")
        if not parts:
            return
        summary = "; ".join(parts)
        self.voice.speak(summary, channel="system", interrupt=False)
        self._post(protocol.echo(f"* {summary}"))

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
        text, cues = parse_msp_line(text)  # strip MSP markers before colour parsing
        for cue in cues:
            if cue.kind == "music":
                self.sound.music(cue.file)
            else:
                self.sound.play(cue.file, gain=cue.volume / 100.0)
        spans = parse_ansi(text)
        plain = "".join(span.text for span in spans)
        if not plain.strip():
            return  # blank line: any sound cues already fired; don't show/speak "blank"
        line = Line(plain, spans=spans)
        self.engine.process_line(line)  # may set line.channel and gag flags
        self._log(line.plain_text)  # full session log, including gagged-from-speech
        if self._login is not None and not self._login.done:
            self._login.feed(line.plain_text)  # answer name/password prompts
        if self._walk is not None and self._walk.active:
            self._walk.on_line(line.plain_text)  # halt the walk if a step was blocked
        policy = self.channels.policy(line.channel)
        if policy.speak and not line.gagged:
            self.voice.speak(
                line.plain_text,
                channel=(policy.voice or line.channel),
                interrupt=policy.interrupt,
            )
        if policy.display and not (line.gagged and not line.display_when_gagged):
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
                    if self._is_room_info(message):
                        self._update_room(message.value)
            if result:
                self._post(protocol.status(self._gauges))
        elif isinstance(result, ServerStatus):
            self._gauges.update(result.data)
            self._post(protocol.status(self._gauges))

    @staticmethod
    def _is_room_info(message: OobMessage) -> bool:
        return (
            message.source == "gmcp"
            and message.name.lower() == "room.info"
            and isinstance(message.value, dict)
        )

    def _update_room(self, room: dict) -> None:
        changed = room != self.nav.room
        self.nav.update_room(room)
        if changed and self._walk is not None and self._walk.active:
            self._walk.on_room_change()  # confirmed move -> advance the safe-walk

    # --- inbound from the renderer (WS messages) ---

    def on_ws_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == protocol.INPUT:
            for command in self._split_commands(message.get("text", "")):
                self._dispatch_command(command)
        elif kind == protocol.KEY:
            self._handle_key(message.get("key", ""))

    def _split_commands(self, text: str) -> list[str]:
        """Split stacked input on the separator ("n;n;look"); empty separator = off."""
        separator = self.command_separator
        if not separator or separator not in text:
            return [text]
        return [part for part in text.split(separator) if part != ""]

    def _dispatch_command(self, text: str) -> None:
        if text.strip():
            self._log(f"> {text}")
        if self._safe_speedwalk(text) or self._speedwalk(text):
            return
        for line in self.engine.process_input(text):
            self._send(line)
            self.nav.record(line)  # build the breadcrumb trail from manual walking

    def _safe_speedwalk(self, text: str) -> bool:
        """Walk a "..3n2e" run step-by-step, halting if blocked; False if not one."""
        if not text.startswith(SAFE_PREFIX):
            return False
        steps = expand_speedwalk(text[len(SAFE_PREFIX) :])
        if not steps:
            return False

        def send_and_record(direction: str) -> None:
            self._send(direction)
            self.nav.record(direction)

        self._walk = SafeWalk(
            steps, send=send_and_record, schedule=self._schedule, announce=self._speak_system
        )
        self._walk.start()
        return True

    def _speedwalk(self, text: str) -> bool:
        """Expand and send a "." speedwalk run (e.g. ".3n2e"); False if not one."""
        if not text.startswith(SPEEDWALK_PREFIX):
            return False
        steps = expand_speedwalk(text[len(SPEEDWALK_PREFIX) :])
        if not steps:
            return False
        for direction in steps:
            self._send(direction)
            self.nav.record(direction)
        return True

    def _handle_key(self, combo: str) -> None:
        action = self.keymap.get(combo)
        if action is None:
            self.engine.press_key(combo)  # user-defined macro
            return
        namespace, _, argument = action.partition(":")
        if namespace == "recall":
            # "recall:N" or "recall:<channel>:N" (channel filters the scrollback).
            channel, _, count = argument.rpartition(":")
            self._speak_review(
                self.review.recall(int(count), channel=channel or None) or "no message"
            )
        elif namespace == "review" and argument in _REVIEW_VERBS:
            if not self.review.active:
                self.review.enter()
            self._speak_review(getattr(self.review, argument)())
        elif namespace == "voice" and argument == "flush":
            self.voice.flush()
        elif namespace == "sound" and argument == "flush":
            self.sound.flush()  # panic key: cut all playing audio (Shift+F11)
        elif namespace == "nav":
            self._handle_nav(argument)
        elif namespace == "log" and argument == "toggle":
            self._toggle_log()
        # "soundpack:toggle" and other namespaces are wired as features land.

    def _handle_nav(self, action: str) -> None:
        if action == "mark":
            self.nav.clear()
            self._speak_system("breadcrumb dropped")
        elif action == "retrace":
            path = self.nav.retrace()
            if not path:
                self._speak_system("no trail to retrace")
                return
            for direction in path:
                self._send(direction)
            self.nav.clear()  # optimistic: assume the way back succeeded
            self._speak_system(f"retracing {len(path)} steps")
        elif action == "where":
            self._speak_system(self.nav.where())
        elif action == "stop":
            if self._walk is not None and self._walk.active:
                self._walk.cancel()
                self._speak_system("walk stopped")
            else:
                self._speak_system("not walking")

    def _speak_review(self, text: str) -> None:
        self.voice.speak(text, channel=REVIEW_CHANNEL, interrupt=True)
        self._post(protocol.review(text))

    def _speak_system(self, text: str) -> None:
        self.voice.speak(text, channel="system", interrupt=True)
        self._post(protocol.echo(f"* {text}"))

    def on_connection_status(self, message: str) -> None:
        """Surface a transport status line (reconnecting, reconnected) to the user."""
        self._speak_system(message)

    def _log(self, text: str) -> None:
        if self.logger is not None and self.logger.active:
            self.logger.log(text)

    def _toggle_log(self) -> None:
        if self.logger is not None and self.logger.active:
            name = self.logger.path.name
            self.logger.stop()
            self.logger = None
            self._speak_system(f"logging stopped: {name}")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.log_dir / f"{self.name or 'session'}-{stamp}.log"
        self.logger = SessionLogger(path)
        self.logger.start()
        self._speak_system(f"logging to {path.name}")
