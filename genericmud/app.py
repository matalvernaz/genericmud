"""Integration core: turns the telnet event stream and renderer messages into
engine processing, self-voice, and outbound render messages.

Decoupled from transport and UI via three injected callables — ``send`` (to the
MUD), ``post`` (to the renderer), ``schedule`` (timers) — plus a VoiceRouter, so
the whole glue layer is unit-testable without a socket, a webview, or NVDA.
"""

from __future__ import annotations

import asyncio
import json
import re
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from genericmud.automation.channels import ChannelPolicy
from genericmud.automation.engine import AutomationEngine, Callback, EngineSink, MatchContext
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
from genericmud.safepath import is_unsafe, sanitize_component
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.session.credentials import CredentialStore
from genericmud.session.hub import SessionHub
from genericmud.session.log import SessionLogger
from genericmud.session.login import AutoLogin
from genericmud.sound.bus import SoundBackend, SoundBus
from genericmud.voice.router import VoiceRouter

if TYPE_CHECKING:
    from genericmud.session.diaglog import DiagnosticLog

REVIEW_CHANNEL = "review"
SPEEDWALK_PREFIX = "."  # ".3n2e" expands to n,n,n,e,e (leading char disambiguates)
SAFE_PREFIX = ".."  # "..3n2e" walks the same route one step at a time, halting if blocked
CLIENT_PREFIX = "/"  # "/alias", "/trigger", ... ; unknown /verbs pass through to the MUD
MAX_ALIAS_DEPTH = 20  # guard against an alias/trigger that re-fires itself forever
_PLUGIN_TICK_SECONDS = 0.25  # OnPluginTick cadence (MUSHclient ticks faster; see _arm_plugin_ticks)

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
        self, *, send, post, schedule, voice: VoiceRouter, buffer: Buffer, sound: SoundBus,
        diag: DiagnosticLog | None = None, send_raw=None,
    ) -> None:
        self._send = send
        self._send_raw = send_raw
        self._post = post
        self._schedule = schedule
        self._voice = voice
        self._buffer = buffer
        self._sound = sound
        self._diag = diag
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

    def echo(self, text: str, channel: str = "main") -> None:
        self._buffer.append(Line(text, channel=channel))
        self._post(protocol.echo(text, channel))

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self._voice.speak(text, channel, interrupt)

    def stop_speech(self) -> None:
        self._voice.flush()

    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
    ) -> None:
        self.cues += 1
        if self._diag is not None:
            policy = self._sound.policy(channel)
            self._diag.event(
                "sink.gain", file=file, channel=channel, master=self._sound.master,
                cat_gain=policy.gain, muted=policy.muted, cue_gain=gain,
                effective=self._sound.effective_gain(channel, gain),
            )
        self._sound.play(file, channel, gain, pan, loop)

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

    def schedule(self, delay: float, callback: Callable[[], None]) -> None:
        self._schedule(delay, callback)


class EngineApp:
    def __init__(
        self,
        voice: VoiceRouter,
        *,
        send: Callable[[str], None] | None = None,
        send_raw: Callable[[bytes], None] | None = None,
        post: Callable[[dict], None] | None = None,
        schedule: Callable[[float, Callable[[], None]], None] | None = None,
        keymap: dict[str, str] | None = None,
        packs: PackStore | None = None,
        sound_backend: SoundBackend | None = None,
        name: str = "",
        log_dir: Path | None = None,
        credentials: CredentialStore | None = None,
        hub: SessionHub | None = None,
        diag: DiagnosticLog | None = None,
    ) -> None:
        self.buffer = Buffer()
        self.voice = voice
        self.packs = packs
        self._send = send or (lambda _text: None)
        self._post = post or (lambda _message: None)
        self._schedule = schedule or _default_schedule
        self._diag = diag
        # Native (wx) injects a pygame backend; otherwise sounds post to the renderer (which
        # nothing consumes in native mode -- so a "post" backend there is candidate A for silence).
        self._diag_backend_kind = "native" if sound_backend is not None else "post"
        self.sound = SoundBus(sound_backend or _PostSoundBackend(self._post))
        self.sink = AppSink(
            send=self._send,
            send_raw=send_raw,
            post=self._post,
            schedule=self._schedule,
            voice=voice,
            buffer=self.buffer,
            sound=self.sound,
            diag=diag,
        )
        self.engine = AutomationEngine(self.sink, sound=self.sound)
        self.engine.diag = diag  # sound-path trace, reached by ScriptApi/loader via the engine
        if diag is not None:
            diag.event("backend.active", kind=self._diag_backend_kind)
        self.hub = hub
        self.engine.hub = hub  # cross-session bus, scriptable via ScriptApi
        self.engine.session_name = name
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
        self._user_aliases: dict[str, str] = {}  # interactively-made aliases: pattern -> command
        self._user_triggers: dict[str, str] = {}
        self._dispatch_depth = 0  # recursion guard for alias/trigger -> command -> alias
        self._pending = ""
        self._gauges: dict[str, object] = {}
        self._last_activation: ActivationResult | None = None  # for the diag:where summary
        self._client_error_spoken = False  # speak the first renderer error, echo the rest
        self._mush_packs: list[MushclientPack] = []  # loaded MUSHclient packs (hook dispatch)
        self._msdp_offered = False  # server sent WILL MSDP (replayed to late-loading packs)
        self._ticks_armed = False  # the OnPluginTick chain is scheduled at most once
        self._closed = False  # ends the tick chain at shutdown
        self._msdp_routed = 0  # subnegotiation count, for throttling the diag trace

    # --- soundpacks ---

    def activate_packs(self, world: str) -> ActivationResult | None:
        """Load the packs enabled for ``world`` and announce the outcome aloud.

        Call after constructing the app and before connecting, so triggers are
        armed when data arrives. No-op (returns None) when no store is wired.
        """
        if self.packs is None:
            return None
        result = activate_world(self.packs, world, self.engine)
        self._last_activation = result
        self._mush_packs = [
            pack for pack in result.packs.values() if isinstance(pack, MushclientPack)
        ]
        self._announce_activation(result)
        return result

    def on_connect(self, world: str) -> ActivationResult | None:
        """The on-connect sequence: register with the hub, activate packs, arm login."""
        if self.hub is not None and self.name:
            self.hub.register(self.name, self._dispatch_remote)
        # Seed saved pack variables BEFORE packs load: OnPluginInstall's `if
        # GetVariable(x) ~= nil` checks are exactly how MUSHclient packs keep user
        # settings (volumes, toggles) across sessions -- with nothing seeded, every
        # launch silently reset them to defaults.
        self._restore_pack_vars()
        result = self.activate_packs(world)
        if self._diag is not None:
            # Always emitted, even with zero packs: this is the marker that on_connect
            # survived activation, and `mush=` is what lifecycle dispatch keys off.
            self._diag.event(
                "pack.activated",
                total=len(result.packs) if result is not None else 0,
                mush=len(self._mush_packs),
            )
        try:
            self._dispatch_plugin_lifecycle()
        except Exception as exc:  # noqa: BLE001 - sound hooks must not kill session setup
            if self._diag is not None:
                self._diag.event(
                    "plugin.lifecycle.error",
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                )
            self.voice.speak(
                "Soundpack startup hooks failed; sounds may be off.",
                channel="system", interrupt=False,
            )
        self.begin_login(world)
        return result

    def _dispatch_plugin_lifecycle(self) -> None:
        """Run each MUSHclient pack's install/connect hooks (MUSHclient calls these
        itself; packs set their variable defaults in OnPluginInstall -- Erion turns
        every sound toggle on there, so skipping it leaves the pack gated silent)."""
        if not self._mush_packs:
            return
        if self._diag is not None:
            # NB: event()'s first positional is named `stage` -- a stage= kwarg here
            # collides and raises (the 0.6.6 silent-session bug).
            self._diag.event("plugin.lifecycle", phase="install+connect",
                             packs=len(self._mush_packs))
        for pack in self._mush_packs:
            pack.dispatch_install()
            pack.dispatch_connect()
        if self._msdp_offered:  # negotiation happened before packs were ready: replay it
            self._dispatch_msdp_start()
        self._arm_plugin_ticks()
        if self._diag is not None:
            self._diag.event("plugin.lifecycle.done")

    def _arm_plugin_ticks(self) -> None:
        """Run each pack's ``OnPluginTick`` on a repeating schedule.

        MUSHclient ticks continuously; Erion's tick IS its music/ambience engine --
        StartMusic/StartWeather/StartAmbiance restart a finished (non-looping) ambience
        and rotate weather. Without it, ambience plays once per room and dies. 0.25s is
        far below MUSHclient's rate but ambience-restart latency is imperceptible, and
        each dispatch is a few variable reads. Skipped while disconnected so a tick
        can't restart music after quit (the flush would be fighting the tick).
        """
        if self._ticks_armed or not any(p.has_hook("OnPluginTick") for p in self._mush_packs):
            return
        self._ticks_armed = True

        def tick() -> None:
            if self._closed:
                return  # session torn down: let the chain end
            if self.engine.connected:
                for pack in self._mush_packs:
                    pack.dispatch("OnPluginTick")
            self._schedule(_PLUGIN_TICK_SECONDS, tick)

        self._schedule(_PLUGIN_TICK_SECONDS, tick)

    def _dispatch_msdp_start(self) -> None:
        """Tell packs MSDP is on (the transport already answered DO). The SENT_DO round
        is where an MSDP soundpack sends its REPORT list; Erion's server streams
        nothing (so no combat/ambience cues exist) until those REPORTs arrive."""
        if not self._mush_packs:
            return
        if self._diag is not None:
            self._diag.event("msdp.start", packs=len(self._mush_packs))
        for pack in self._mush_packs:
            pack.dispatch_telnet_request(T.OPT_MSDP, "WILL")
            pack.dispatch_telnet_request(T.OPT_MSDP, "SENT_DO")

    def shutdown(self) -> None:
        """Release session resources on close: leave the hub and stop logging."""
        self._closed = True  # ends the OnPluginTick chain
        self._persist_pack_vars()
        if self.hub is not None and self.name:
            self.hub.unregister(self.name)
        if self.logger is not None:
            self.logger.stop()
            self.logger = None

    # --- pack-variable persistence (MUSHclient SaveState equivalent) ---

    def _pack_vars_path(self) -> Path | None:
        if self.packs is None or not self.name:
            return None  # headless/test sessions have nothing to persist against
        root = getattr(self.packs, "root", None)
        if root is None:
            return None
        return Path(root).parent / "state" / f"{sanitize_component(self.name)}-vars.json"

    def _restore_pack_vars(self) -> None:
        path = self._pack_vars_path()
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # no state yet, or an unreadable file: packs fall back to defaults
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str) and key != "sppath":
                self.engine.set_var(key, value)

    def _persist_pack_vars(self) -> None:
        path = self._pack_vars_path()
        if path is None:
            return
        # sppath is wiring (where THIS install keeps its sounds), not a user setting;
        # persisting it would pin a moved/reinstalled pack to its old location.
        data = {k: v for k, v in self.engine.all_vars().items() if k != "sppath"}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            pass  # a state write must never break session close

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
        triggers = sum(
            len(bucket["trigger"]) for bucket in self.engine.registrations_by_source().values()
        )
        if self._diag is not None:
            self._diag.event(
                "pack.summary",
                loaded=",".join(result.loaded) or "none",
                failed=",".join(result.failed) or "none",
                untrusted=",".join(result.skipped_untrusted) or "none",
                conflicts=len(result.conflicts),
                triggers=triggers,
                sppath=self.engine.get_var("sppath") or "",
            )
        parts: list[str] = []
        if result.loaded:
            parts.append(f"{len(result.loaded)} soundpack{'s' if len(result.loaded) != 1 else ''}")
        # Packs loaded but armed no triggers can never make a sound -- say so at connect, not
        # only in the trace, so a blind user learns the pack is inert without reading a file.
        if result.loaded and triggers == 0:
            parts.append("packs loaded but no triggers registered")
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
        elif (
            isinstance(event, T.Negotiation)
            and event.command == T.WILL
            and event.option == T.OPT_MSDP
        ):
            # Server offers MSDP (once per connection, so a reconnect re-arms too).
            self._msdp_offered = True
            self._dispatch_msdp_start()

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
            # MSP filenames come straight from the untrusted server. Block the dangerous shapes
            # (absolute / UNC / drive / ..) so a hostile MUD can't open an arbitrary file -- a
            # Windows UNC path leaks the NTLM hash on open, before decode even fails. A safe
            # relative name passes through to the backend as before.
            if is_unsafe(cue.file):
                if self._diag is not None:
                    self._diag.event("msp.cue", kind=cue.kind, file=cue.file, blocked=True)
                continue
            if self._diag is not None:
                self._diag.event("msp.cue", kind=cue.kind, file=cue.file, volume=cue.volume)
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
        if sub.option == T.OPT_MSDP and self._mush_packs:
            self._route_msdp(sub.payload)

    def _route_msdp(self, payload: bytes) -> None:
        """Hand a raw MSDP payload to each MUSHclient pack's telnet-subnegotiation
        hook (Erion's MSDP_handler parses it and plays the matching cue)."""
        self._msdp_routed += 1
        if self._diag is not None and (self._msdp_routed <= 50 or self._msdp_routed % 200 == 0):
            # Full trace for the first packets (the diagnostic window), then sampled --
            # MSDP streams continuously and would otherwise fill the log's byte cap.
            self._diag.event(
                "msdp.route", n=self._msdp_routed, size=len(payload),
                preview=repr(payload[:40]), packs=len(self._mush_packs),
            )
        for pack in self._mush_packs:
            pack.dispatch_telnet_subnegotiation(T.OPT_MSDP, payload)

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
            text = message.get("text", "")
            # A client command ("/alias x = a;b") must not be split on the separator.
            commands = [text] if text.startswith(CLIENT_PREFIX) else self._split_commands(text)
            for command in commands:
                self._dispatch_command(command)
        elif kind == protocol.KEY:
            self._handle_key(message.get("key", ""))
        elif kind == protocol.CLIENT_ERROR:
            self._handle_client_error(message)

    def _handle_client_error(self, message: dict) -> None:
        """A renderer-side failure (e.g. Web Audio couldn't load/decode a sound)."""
        scope = message.get("scope", "client")
        file = message.get("file", "")
        detail = message.get("error", "")
        if self._diag is not None:
            self._diag.event("client.error", scope=scope, file=file, error=detail)
        summary = f"{scope} error: {file} {detail}".strip()
        self._post(protocol.echo(f"* {summary}"))
        if not self._client_error_spoken:  # speak the first; the rest stay in the output
            self._client_error_spoken = True
            self.voice.speak(summary, channel="system", interrupt=False)

    def _split_commands(self, text: str) -> list[str]:
        """Split stacked input on the separator ("n;n;look"); empty separator = off."""
        separator = self.command_separator
        if not separator or separator not in text:
            return [text]
        return [part for part in text.split(separator) if part != ""]

    def _dispatch_command(self, text: str, *, allow_client: bool = True) -> None:
        if text.strip():
            self._log(f"> {text}")
        if allow_client and self._client_command(text):
            return
        if self._safe_speedwalk(text) or self._speedwalk(text):
            return
        for line in self.engine.process_input(text):
            self._send(line)
            self.nav.record(line)  # build the breadcrumb trail from manual walking

    def _dispatch_remote(self, text: str) -> None:
        """Deliver a command sent from ANOTHER session (mud.send_to / mud.broadcast).

        Cross-session text must NOT run this session's client commands (/alias, /trigger, ...):
        a pack loaded for one world could otherwise reprogram the user's other sessions. It's
        treated as game input (aliases still expand); a leading '/' goes to the MUD literally.
        """
        self._dispatch_command(text, allow_client=False)

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

    # --- interactive alias/trigger commands (the no-scripting "easy method") ---

    def _client_command(self, text: str) -> bool:
        """Handle "/alias", "/trigger", "/to", ...; False = not one (send to the MUD)."""
        if not text.startswith(CLIENT_PREFIX):
            return False
        verb, _, rest = text[len(CLIENT_PREFIX) :].partition(" ")
        verb, rest = verb.lower(), rest.strip()
        if verb in ("alias", "trigger"):
            self._define_user_rule(verb, rest)
        elif verb == "unalias":
            self._remove_user_rule("alias", rest)
        elif verb == "untrigger":
            self._remove_user_rule("trigger", rest)
        elif verb == "aliases":
            self._list_user_rules("alias")
        elif verb == "triggers":
            self._list_user_rules("trigger")
        elif verb == "to":
            self._send_to_session(rest)
        else:
            return False  # unknown /verb -> let the MUD have it (some use slash commands)
        return True

    def _user_rules(self, kind: str) -> dict[str, str]:
        return self._user_aliases if kind == "alias" else self._user_triggers

    def _define_user_rule(self, kind: str, rest: str) -> None:
        pattern, separator, command = rest.partition("=")
        pattern, command = pattern.strip(), command.strip()
        if not separator or not pattern or not command:
            self._speak_system(f"usage: /{kind} <text> = <command>")
            return
        self._user_rules(kind)[pattern] = command
        callback = self._user_rule_callback(command)
        if kind == "alias":
            self.engine.remove_alias(pattern)  # replace any existing alias for this text
            self.engine.add_alias(
                f"^{re.escape(pattern)}$", callback,
                regex=True, name=pattern, source="user", keep_evaluating=False,
            )
        else:
            self.engine.remove_trigger(pattern)
            self.engine.add_trigger(  # (?i): match the MUD's output regardless of case
                f"(?i){re.escape(pattern)}", callback, regex=True, name=pattern, source="user"
            )
        self._speak_system(f"{kind} {pattern} added")

    def _remove_user_rule(self, kind: str, pattern: str) -> None:
        pattern = pattern.strip()
        if self._user_rules(kind).pop(pattern, None) is None:
            self._speak_system(f"no {kind} {pattern}")
            return
        if kind == "alias":
            self.engine.remove_alias(pattern)
        else:
            self.engine.remove_trigger(pattern)
        self._speak_system(f"{kind} {pattern} removed")

    def _list_user_rules(self, kind: str) -> None:
        rules = self._user_rules(kind)
        if not rules:
            self._speak_system("no aliases" if kind == "alias" else "no triggers")
            return
        self._speak_system("; ".join(f"{pattern} = {body}" for pattern, body in rules.items()))

    def _send_to_session(self, rest: str) -> None:
        session, _, command = rest.partition(" ")
        command = command.strip()
        if not session or not command:
            self._speak_system("usage: /to <session> <command>")
            return
        if self.hub is None or not self.hub.send_to(session, command):
            self._speak_system(f"no session {session}")

    def _user_rule_callback(self, command: str) -> Callback:
        def callback(_ctx: MatchContext) -> None:
            self._run_user_command(command)

        return callback

    def _run_user_command(self, command: str) -> None:
        if self._dispatch_depth >= MAX_ALIAS_DEPTH:
            return  # an alias/trigger looped on itself; stop rather than hang
        self._dispatch_depth += 1
        try:
            for piece in self._split_commands(command):
                self._dispatch_command(piece)
        finally:
            self._dispatch_depth -= 1

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
        elif namespace == "diag" and argument == "where":
            self._diag_where()
        # "soundpack:toggle" and other namespaces are wired as features land.

    def _diag_where(self) -> None:
        """Speak the diagnostic log path + a one-line summary (often names the failure)."""
        triggers = sum(
            len(bucket["trigger"]) for bucket in self.engine.registrations_by_source().values()
        )
        packs = len(self._last_activation.loaded) if self._last_activation else 0
        name = self._diag.path.name if self._diag is not None else "off"
        self._speak_system(
            f"diagnostic log {name}; backend {self._diag_backend_kind}; "
            f"{packs} packs, {triggers} triggers; {self.sink.cues} cues attempted"
        )

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
        if message.startswith(("disconnected", "protocol error", "reconnect failed")):
            # A terminal drop (quit, network death we won't reconnect): silence the pack's
            # looping music/ambience -- nothing will ever stop those cues otherwise. A
            # "reconnecting" status keeps them; the session is expected to resume.
            # Also mark the engine disconnected: packs read IsConnected(), and the
            # OnPluginTick chain pauses so a tick can't restart music after quit.
            self.engine.connected = False
            self.sound.flush()
        elif message.startswith("reconnected"):
            self.engine.connected = True
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
        # Sanitize the world name -- a pack-derived world could be named "../.." and escape the
        # logs directory when joined onto the path.
        safe_name = sanitize_component(self.name or "session")
        path = self.log_dir / f"{safe_name}-{stamp}.log"
        self.logger = SessionLogger(path)
        self.logger.start()
        self._speak_system(f"logging to {path.name}")
