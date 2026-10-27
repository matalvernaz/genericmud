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
import time
import traceback
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from genericmud.automation.channels import ChannelPolicy
from genericmud.automation.engine import (
    AutomationEngine,
    Callback,
    EngineSink,
    MatchContext,
    PackWindow,
)
from genericmud.automation.variables import VariableEntry, list_variables
from genericmud.bridge import protocol
from genericmud.completion import OutputWordIndex
from genericmud.config.atomic import atomic_write_text
from genericmud.config.worlds import config_dir
from genericmud.logpaths import diagnostic_log_dir, log_count, world_log_dir
from genericmud.mapping import RoomMap, load_map, save_map, spoken_count
from genericmud.model.buffer import Buffer, Line
from genericmud.navigation import Navigator, SafeWalk, expand_speedwalk
from genericmud.packs import ActivationResult, PackStore, activate_world, user_rules
from genericmud.packs.pack_updates import PackUpdates
from genericmud.packs.setup import GitUpdateResult
from genericmud.protocol import msdp
from genericmud.protocol import telnet as T
from genericmud.protocol.charset import AUTO, ServerTextCodec
from genericmud.protocol.msp import parse_msp_line
from genericmud.protocol.oob import OobMessage, ServerStatus, from_subnegotiation
from genericmud.render.ansi import parse_ansi
from genericmud.review.channels import ChannelReview
from genericmud.review.cursor import ReviewCursor
from genericmud.safepath import (
    is_unc,
    is_unsafe,
    legacy_world_component,
    within,
    world_component,
)
from genericmud.scripting import user_scripts
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.vipmud_dialect import VipMudPack
from genericmud.session.credentials import CredentialStore
from genericmud.session.hub import SessionHub
from genericmud.session.log import SessionLogger
from genericmud.session.login import AutoLogin
from genericmud.sound.bus import (
    DEFAULT_CATEGORY,
    MUSIC_CATEGORY,
    CueTiming,
    SoundBackend,
    SoundBus,
)
from genericmud.sound.media import MediaLibrary, media_url
from genericmud.voice.router import REVIEW_CHANNEL, SYSTEM_CHANNEL, VoiceRouter

if TYPE_CHECKING:
    from genericmud.config.worlds import World
    from genericmud.session.diaglog import DiagnosticLog

SPEEDWALK_PREFIX = "."  # ".3n2e" expands to n,n,n,e,e (leading char disambiguates)
SAFE_PREFIX = ".."  # "..3n2e" walks the same route one step at a time, halting if blocked
CLIENT_PREFIX = "/"  # "/alias", "/trigger", ... ; unknown /verbs pass through to the MUD
MAX_ALIAS_DEPTH = 20  # guard against an alias/trigger that re-fires itself forever
# New rooms learned before the map is written again. Exploring is exactly when a session
# is long and a crash is most costly, and a write every twenty-five rooms is nothing.
_MAP_SAVE_INTERVAL = 25
_PLUGIN_TICK_SECONDS = 0.25  # OnPluginTick cadence (MUSHclient ticks faster; see _arm_plugin_ticks)
_PROMPT_IDLE_FLUSH_SECONDS = 0.25  # emit a newline-less prompt if the server sends no more data
_MAX_SIMULATE_DEPTH = 8  # Simulate within a simulated line, this many deep at most
_STATE_SAVE_DELAY_SECONDS = 2.0  # a pack's SaveState is written this soon after it asks
# A MUD that never sends WONT ECHO after the login password would leave every command going
# out as a password. Mudlet's answer, copied here: during the first five minutes of a
# connection, a minute after the last line typed while hidden, stop hiding and say so. Later
# in a session (changing a password in-game) only WONT ECHO or Escape ends the hiding.
_LOGIN_PHASE_SECONDS = 300.0
_HIDDEN_INPUT_TIMEOUT_SECONDS = 60.0
# Transport statuses that mean the socket went away or was replaced (transport/connection.py).
_SOCKET_CHANGES = (
    "disconnected",
    "protocol error",
    "reconnect failed",
    "connection lost",
    "reconnected",
)
_MEDIA_DEFAULT_VOLUME = 50  # Client.Media.Play's volume when the server leaves it out (spec)
_MEDIA_FADEAWAY_MS = 5000  # Client.Media.Stop's fadeaway when neither Play nor Stop says (spec)
_MEDIA_LONGEST_MS = 24 * 60 * 60 * 1000  # positions and fades past a day are a server's mistake
_MEDIA_TRACKED = 64  # Client.Media cues remembered for Stop before finished ones are dropped
# MSDP variables that mean the player's location changed; both the table and flat forms.
_MSDP_ROOM_VARS = frozenset(
    {"ROOM", "ROOM_NAME", "ROOM_AREA", "ROOM_EXITS", "ROOM_VNUM", "AREA_NAME"}
)
# Interactive /alias and /trigger register under their own source so the soundpack builder's
# reload (which clears user_rules.SOURCE) can't silently delete them.
INTERACTIVE_SOURCE = "user-interactive"

_REVIEW_VERBS = frozenset(
    {
        "prev_line", "next_line", "prev_word", "next_word", "prev_char", "next_char",
        "top", "bottom", "spell_line",
    }
)
_CHANNEL_REVIEW_VERBS = frozenset(
    {"prev", "next", "older", "newer", "prev_word", "next_word"}
)


def _files(count: int) -> str:
    """A spoken file count: "1 file", "2 files". Read aloud, "1 files" sounds like a bug."""
    return f"{count} file" if count == 1 else f"{count} files"


def _parse_count(text: str) -> int | None:
    """A recall count from a keymap action, or None if it isn't a number.

    Keymaps are user-editable, so a fat-fingered ``recall:oops`` reaches here; without
    this guard the int() raised out of key dispatch and the key silently died forever.
    """
    try:
        return int(text)
    except ValueError:
        return None
# Toggles reachable from the keymap: attribute -> what the toggle announces.
_TOGGLE_ACTIONS = {
    "follow_mode": "follow mode",
    "interrupt_mode": "interrupt mode",
    "autoretype": "auto-repeat",  # spoken as "auto-repeat on/off" (empty Enter resends last cmd)
}


def _media_int(value: object, default, low: int, high: int):
    """A Client.Media number clamped to its range; servers send strings and floats too."""
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):  # OverflowError: infinity, 400-digit numbers
        return default
    return max(low, min(high, number))


def _media_is_zero(value: object) -> bool:
    """Whether a Client.Media number is exactly 0, the volume that asks for a preload."""
    try:
        return float(value) == 0.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _media_continues(value: object) -> bool:
    """Client.Media.Play's ``continue``: true unless the server sent false, either as a JSON
    boolean or as the text "false". The spec has clients take every value as a quoted string
    too, for servers that can't send JSON types, and Mudlet restarts the track on both."""
    if isinstance(value, str):
        return value.strip().lower() != "false"
    return value is not False


def _media_true(value: object) -> bool:
    """A Client.Media yes-or-no that's off unless the server sent true, as a JSON boolean or
    as the text "true" (servers that can't send JSON types quote every value)."""
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True


def _media_timing(body: dict) -> CueTiming | None:
    """Client.Media.Play's start, finish, fadein and fadeout (milliseconds), or None when it
    gave none of them."""
    start, finish, fade_in, fade_out = (
        _media_int(body.get(key), 0, 0, _MEDIA_LONGEST_MS)
        for key in ("start", "finish", "fadein", "fadeout")
    )
    if not (start or finish or fade_in or fade_out):
        return None
    return CueTiming(
        start=start / 1000, finish=finish / 1000, fade_in=fade_in / 1000, fade_out=fade_out / 1000
    )


def _default_schedule(delay: float, callback: Callable[[], None]) -> object | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.call_later(delay, callback)


class _PostSoundBackend(SoundBackend):
    """SoundBus backend that posts renderer protocol messages (the Web Audio path)."""

    def __init__(self, post) -> None:
        self._post = post

    def play(
        self, file: str, channel: str, gain: float, pan: float, loop: bool, times: int = 1,
        timing: CueTiming | None = None,
    ) -> None:
        # The renderer can't count, trim or fade: it plays the file whole.
        self._post(protocol.sound(file, channel, gain, pan, loop))

    def music(
        self, file: str, channel: str, gain: float, times: int = -1,
        timing: CueTiming | None = None,
    ) -> None:
        self._post(protocol.music(file, channel, gain))

    def stop(self, channel: str) -> None:
        self._post(protocol.stop_sound(channel))


class AppSink(EngineSink):
    """Routes engine side effects to the MUD, the renderer, and the voice router."""

    def __init__(
        self, *, send, post, schedule, voice: VoiceRouter, buffer: Buffer, sound: SoundBus,
        diag: DiagnosticLog | None = None, send_raw=None, ask=None, simulate=None,
        save_state=None, window=None, update_packs=None, check_pack_updates=None,
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
        map_dir: Path | None = None,
        credentials: CredentialStore | None = None,
        hub: SessionHub | None = None,
        diag: DiagnosticLog | None = None,
        suppress_reconnect: Callable[[], None] | None = None,
        encoding: str = AUTO,
        other_worlds: Callable[[], Iterable[str]] | None = None,
        clock: Callable[[], float] | None = None,
        withdraw_option: Callable[[int], None] | None = None,
        media: MediaLibrary | None = None,
    ) -> None:
        self.buffer = Buffer()
        self.voice = voice
        self.packs = packs
        # Checks this world's packs for updates on connect; /update installs them.
        self.pack_updates: PackUpdates | None = None
        self._world_packs: list[str] = []  # the packs enabled for this session's world
        self._send = send or (lambda _text: None)
        self._post = post or (lambda _message: None)
        # Wired by the UI to the transport; the engine only knows the server said goodbye.
        self._suppress_reconnect = suppress_reconnect or (lambda: None)
        self._schedule = schedule or _default_schedule
        self._diag = diag
        # Native (wx) injects a pygame backend; otherwise sounds post to the renderer (which
        # nothing consumes in native mode -- so a "post" backend there is candidate A for silence).
        self._diag_backend_kind = "native" if sound_backend is not None else "post"
        self.sound = SoundBus(sound_backend or _PostSoundBackend(self._post))
        # Server media (MSP U=, GMCP Client.Media) resolves to files on disk and downloads into a
        # per-world cache. Only with native audio: the web renderer fetches relative names from
        # its own /sounds/ route and couldn't play a path from the cache.
        self._native_audio = sound_backend is not None
        self.media = media or MediaLibrary(
            config_dir() / "media" / world_component(name), on_error=self._media_failed
        )
        # Set by !!SOUND(Off U=...) or !!MUSIC(Off U=...): one default for both kinds, which the
        # MSP spec calls the "default URL for all requests" and Mudlet keeps as one location.
        self._msp_url = ""
        self._media_url = ""  # Client.Media.Default
        self._media_cues: dict[str, dict[str, object]] = {}  # bus channel -> Client.Media.Play
        self._media_gen: dict[str, int] = {}  # bumps per cue/stop so a late download can't play
        self._media_seq = 0  # unique channels for keyless Client.Media sounds, which overlap
        self.sink = AppSink(
            send=self._send,
            send_raw=send_raw,
            post=self._post,
            schedule=self._schedule,
            voice=voice,
            buffer=self.buffer,
            sound=self.sound,
            diag=diag,
            ask=self._ask_player,
            simulate=self._simulate,
            save_state=self._request_save,
            window=lambda: self.pack_window,
            update_packs=self.update_packs,
            check_pack_updates=self.check_pack_updates,
        )
        # Set by a front end that can show dialogs (the wx window): called with the question
        # and a function to call while it waits. A window-less session's questions are
        # cancelled.
        self.asker: Callable[[dict, Callable[[], None] | None], object] | None = None
        # Set by a front end with a window: what packs' clipboard, command box, notepad and
        # browser calls use. Without one they work on a stand-in kept by the pack.
        self.pack_window: PackWindow | None = None
        self.engine = AutomationEngine(self.sink, sound=self.sound)
        self.engine.diag = diag  # sound-path trace, reached by ScriptApi/loader via the engine
        if diag is not None:
            diag.event("backend.active", kind=self._diag_backend_kind)
        self.hub = hub
        self.engine.hub = hub  # cross-session bus, scriptable via ScriptApi
        self.engine.session_name = name
        self.review = ReviewCursor(self.buffer)
        self.channels = self.engine.channels  # router lives on the engine (scriptable)
        self.chan_review = ChannelReview(self.buffer, known=self.channels.names)
        self.word_index = OutputWordIndex()  # feeds input autocomplete in the UI
        # Speech-ergonomics toggles (MUDBall parity); the UI seeds them from saved
        # prefs and pref_sink writes a change back so they survive a restart.
        self.follow_mode = False  # interrupt speech on room movement only
        self.interrupt_mode = False  # every incoming line interrupts instead of queueing
        self.autoretype = False  # Enter on an empty input resends the last command
        self.pref_sink: Callable[[str, bool], None] | None = None
        self._last_input = ""  # what autoretype resends
        # Alerts barge in; everything else stays on the governed 'main' channel by default.
        self.channels.set_policy("tell", ChannelPolicy(interrupt=True))
        self.channels.set_policy("system", ChannelPolicy(interrupt=True))
        self.keymap = keymap or {}
        self.nav = Navigator()  # breadcrumb trail + GMCP room for speedwalk/where-am-I
        # Room graph, on MUDs that report rooms over GMCP/MSDP. Loaded per world in
        # on_connect; empty (and inert) on MUDs that share nothing.
        self.map = RoomMap()
        self.map_dir = Path(map_dir) if map_dir else config_dir() / "maps"
        self._map_unsaved = 0  # new rooms learned since the last write
        self.command_separator = ";"  # stacked input ("n;n;look"); set "" to disable
        self.name = name  # session label, used for the log filename
        self.log_dir = Path(log_dir) if log_dir else world_log_dir()
        self.logger: SessionLogger | None = None
        # The world's logging settings (apply_world_settings): start on connect, which files
        # (config.worlds.LOG_FILES), and whether typed commands go in. Stopping a log by hand
        # holds for the rest of the tab, so a reconnect doesn't start it again.
        self.log_automatically = False
        self.log_files = "session"
        self.log_commands = True
        self.download_media = True  # fetch the sounds a MUD offers (the world's choice)
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
        self._vip_packs: list[VipMudPack] = []  # loaded VIPMud packs (OnConnect/OnDisconnect)
        self._vip_connected = False  # which of the two the VIPMud packs last heard
        self._focused = False  # this world is the one in front (see on_focus_changed)
        self._mush_connected = False  # the same for the MUSHclient packs' hooks
        self._pack_timers_cancelled = False  # a dropped connection cancelled the packs' timers
        self._simulate_depth = 0  # Simulate inside a line that was itself simulated
        self._save_scheduled = False  # a pack's SaveState is waiting to be written
        self._user_script_runtimes: list[user_scripts.LoadedUserScript] = []
        self._user_script_generation = 0
        self._msdp_offered = False  # server sent WILL MSDP (replayed to late-loading packs)
        self._input_masked = False  # server WILL ECHO (password entry): don't log/store input
        self._clock = clock or time.monotonic
        # Wired by the UI to the transport: tell the server to turn an option off (DONT).
        self._withdraw_option = withdraw_option or (lambda _option: None)
        self._login_phase_started = self._clock()  # restarted with every connection
        self._hidden_answers = 0  # lines typed since the MUD hid input; the second gets a hint
        self._hidden_timeout_token = 0  # bumped to cancel a pending hidden-input timeout
        self._ticks_armed = False  # the OnPluginTick chain is scheduled at most once
        self._closed = False  # ends the tick chain at shutdown
        # The world's text encoding, both directions: the UI hands this same codec to the
        # connection so typed commands go out in the encoding the output arrives in.
        self.codec = ServerTextCodec(encoding, on_latch=self._on_encoding_latch)
        # Names of the saved worlds, so an older ASCII-only folder is only adopted when it
        # isn't another world's own folder (see _world_path). None: no saved worlds known.
        self._other_worlds = other_worlds
        self._world_paths: dict[tuple[Path, str], Path] = {}  # resolved once per session
        self._msdp_routed = 0  # subnegotiation count, for throttling the diag trace
        self._prompt_gen = 0  # bumps per data chunk so a stale idle prompt-flush no-ops

    def variable_listing(self) -> tuple[list[VariableEntry], bool]:
        """Every value this session knows, for the MUD Variables dialog and the picker.

        Reads live engine state, so call it on the loop thread; the rows are plain frozen
        values, safe to hand to the UI thread afterwards.
        """
        return list_variables(self.engine.all_mud_vars(), self.engine.all_vars())

    def pack_notepads(self) -> dict[str, str]:
        """The MUSHclient notepads this session's packs have written, by title (a copy).
        Mush-Z keeps its channel log in one and never opens it itself. Loop thread."""
        notepads: dict[str, str] = {}
        for pack in self._mush_packs:
            notepads.update(pack.notepads())
        return notepads

    def _ask_player(
        self, request: dict, while_waiting: Callable[[], None] | None = None
    ) -> object | None:
        """Hand a pack's question to the window; with no window, or if it fails, it's Cancel."""
        asker = self.asker
        if asker is None:
            return None
        try:
            return asker(request, while_waiting)
        except Exception as exc:  # noqa: BLE001 - a failed dialog must not kill the pack's script
            if self._diag is not None:
                self._diag.event(
                    "pack.ask.error", kind=request.get("kind"),
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                )
            return None

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
        self._vip_packs = [pack for pack in result.packs.values() if isinstance(pack, VipMudPack)]
        self._vip_connected = False
        self._mush_connected = False
        self._pack_timers_cancelled = False
        self._announce_activation(result)
        return result

    def apply_world_settings(self, world: World) -> None:
        """Take a world's own command stacking and logging settings. Called before the world
        connects and again when it's edited while open, so the change is felt at once."""
        self.command_separator = world.command_separator if world.command_stacking else ""
        self.log_automatically = world.log_automatically
        self.log_files = world.log_files
        self.log_commands = world.log_commands
        self.download_media = world.download_sounds

    def on_connect(self, world: str, *, socket_open: bool = True) -> ActivationResult | None:
        """The on-connect sequence: register with the hub, activate packs, arm login.

        Packs hear that the MUD is connected once the connection is open: now when it
        already is, or when on_socket_open() says so. The window readies packs before it
        opens the connection, and a connection that fails never says so.
        """
        if not socket_open:
            self.engine.connected = False  # packs' IsConnected(), until it opens
        if self.hub is not None and self.name:
            self.hub.register(self.name, self._dispatch_remote)
        if self.log_automatically:
            self.start_logging(announce=False)  # first, so the log has the whole session
        # Seed saved pack variables BEFORE packs load: OnPluginInstall's `if
        # GetVariable(x) ~= nil` checks are exactly how MUSHclient packs keep user
        # settings (volumes, toggles) across sessions -- with nothing seeded, every
        # launch silently reset them to defaults.
        self._restore_pack_vars()
        self._load_map()  # this world's room graph, if one has been built before
        self.reload_user_rules()  # field-based automation loads alongside installed packs
        self.reload_user_scripts()
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
            self._dispatch_plugin_install()
        except Exception as exc:  # noqa: BLE001 - sound hooks must not kill session setup
            if self._diag is not None:
                self._diag.event(
                    "plugin.lifecycle.error",
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                )
            self.voice.speak(
                "Soundpack startup hooks failed; sounds may be off.",
                channel=SYSTEM_CHANNEL, interrupt=False,
            )
        if socket_open:
            self.on_socket_open()
        self.begin_login(world)
        self._check_pack_updates(result)
        return result

    def on_focus_changed(self, focused: bool) -> None:
        """This world came to the front (the selected tab, while genericMud's window is the
        active one) or went behind. MUSHclient packs hear each change, as MUSHclient tells
        a world's handlers and plugins; packs installed later hear it as they arrive."""
        if focused == self._focused:
            return
        self._focused = focused
        for pack in self._mush_packs:
            pack.dispatch_focus(focused)

    def on_socket_open(self) -> None:
        """The connection to the MUD is open: packs hear it, as MUSHclient's
        OnPluginConnect and the world's connect handler, and VIP Mud's OnConnect, once.
        Anything a pack sends then reaches the MUD."""
        self.engine.connected = True
        self._vip_connection_changed(True)

    def _check_pack_updates(self, result: ActivationResult | None) -> None:
        """Ask this world's packs' repositories whether they have updates, in the background
        (each at most once a day), so a pack with one can say so."""
        if self.packs is None or result is None:
            return
        self._world_packs = [*result.loaded, *result.skipped_untrusted, *result.failed]
        if self.pack_updates is None:
            self.pack_updates = PackUpdates(
                self.packs,
                announce=lambda text: self._speak_system(text, interrupt=False),
                installed=self._packs_updated,
                diag=self._diag,
            )
        self.pack_updates.check_due(self._world_packs)

    def update_packs(self, *, quiet: bool = False) -> None:
        """Install whatever this world's packs' repositories have new (/update, or a pack's
        own update command)."""
        if self.pack_updates is None:
            if not quiet:
                self._speak_system("There are no soundpacks to update in this session.")
            return
        self.pack_updates.install(self._world_packs, quiet=quiet)

    def check_pack_updates(self) -> None:
        """Say whether this world's packs' repositories have anything new (a pack's own
        "update check")."""
        if self.pack_updates is None:
            self._speak_system("There are no soundpacks to update in this session.")
            return
        self.pack_updates.check_now(self._world_packs)

    def _packs_updated(self, results: dict[str, GitUpdateResult], quiet: bool) -> None:
        """Tell each updated MUSHclient pack, the way its own updater plugin told it, so it
        plays its update sounds and shows its changelog."""
        activation = self._last_activation
        if activation is None:
            return
        for pack_id, result in results.items():
            pack = activation.packs.get(pack_id)
            if not isinstance(pack, MushclientPack):
                continue
            try:
                pack.pack_updated(result, quiet=quiet)
            except Exception as exc:  # noqa: BLE001 - a pack's handler must not stop the rest
                if self._diag is not None:
                    self._diag.event("pack.update_notice_failed", id=pack_id, error=repr(exc))

    def _vip_connection_changed(self, connected: bool) -> None:
        """Run the VIPMud packs' OnConnect or OnDisconnect alias, once per change, and the
        MUSHclient packs' connect and disconnect hooks.

        The unofficial Prometheus pack loads its settings and switches its sound classes on
        in OnConnect and saves them in OnDisconnect, so without these it stays silent and
        forgets every setting.
        """
        self._mush_connection_changed(connected)
        if connected == self._vip_connected:
            return
        self._vip_connected = connected
        for pack in self._vip_packs:
            try:
                pack.connection_changed(connected)
            except Exception as exc:  # noqa: BLE001 - one pack's hook must not stop the rest
                if self._diag is not None:
                    self._diag.event(
                        "vipmud.connection_hook.error", connected=connected,
                        error=f"{type(exc).__name__}: {exc}",
                        traceback=traceback.format_exc(),
                    )

    def _mush_connection_changed(self, connected: bool) -> None:
        """Run the MUSHclient packs' OnPluginDisconnect or OnPluginConnect, once per change.

        Their first connect is announced along with their install hook when the session
        starts; after that they hear every drop and reconnect, as in MUSHclient.
        """
        if connected == self._mush_connected:
            return
        self._mush_connected = connected
        resume = connected and self._pack_timers_cancelled
        if resume:
            self._pack_timers_cancelled = False
        for pack in self._mush_packs:
            if connected:
                if resume:
                    pack.resume_timers()  # only after a drop that cancelled them
                pack.dispatch_connect()
            else:
                pack.dispatch_disconnect()

    def _simulate(self, text: str) -> None:
        """A pack's Simulate: the text goes through triggers, the output and speech as if
        the MUD had sent it, a line at a time. A trigger simulating the line it matched would
        go round forever, so it stops a few deep."""
        if self._simulate_depth >= _MAX_SIMULATE_DEPTH:
            if self._diag is not None:
                self._diag.event("pack.simulate", status="refused", reason="nested too deep")
            return
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and not lines[-1]:
            lines.pop()  # the text's own line ending
        self._simulate_depth += 1
        try:
            for line in lines:
                self._emit_line(line)
        finally:
            self._simulate_depth -= 1

    def _request_save(self) -> None:
        """A pack's SaveState: save the packs' variables shortly. Aardwolf's pack saves on
        every setting it changes, so asks close together make one write."""
        if self._save_scheduled or self._closed:
            return
        self._save_scheduled = True

        def save() -> None:
            self._save_scheduled = False
            if not self._closed:
                self._persist_pack_vars()

        self._schedule(_STATE_SAVE_DELAY_SECONDS, save)

    def _dispatch_plugin_install(self) -> None:
        """Run each MUSHclient pack's install hook (MUSHclient calls it as the world
        opens; packs set their variable defaults in OnPluginInstall -- Erion turns every
        sound toggle on there, so skipping it leaves the pack gated silent). Their connect
        hook follows once the connection is open (on_socket_open)."""
        if not self._mush_packs:
            return
        if self._diag is not None:
            # NB: event()'s first positional is named `stage` -- a stage= kwarg here
            # collides and raises (the 0.6.6 silent-session bug).
            self._diag.event("plugin.lifecycle", phase="install",
                             packs=len(self._mush_packs))
        for pack in self._mush_packs:
            pack.dispatch_install()
        if self._focused:  # the world is in front as its packs arrive, as in MUSHclient
            for pack in self._mush_packs:
                pack.dispatch_focus(True)
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
        """Open the MSDP tap (the transport already answered DO).

        Two subscribers, and both have to ask or the server stays silent. An MSDP soundpack
        sends its own REPORT list on the SENT_DO round -- Erion's server streams nothing
        until those arrive. The client itself asks too, or a MUD that speaks MSDP instead of
        GMCP leaves every ${mud:...} reference, gauge and room lookup empty with no error.
        """
        if self._diag is not None:
            self._diag.event("msdp.start", packs=len(self._mush_packs))
        self._send_msdp(msdp.CMD_LIST, msdp.REPORTABLE_VARIABLES)
        for pack in self._mush_packs:
            pack.dispatch_telnet_request(T.OPT_MSDP, "WILL")
            pack.dispatch_telnet_request(T.OPT_MSDP, "SENT_DO")

    def _send_msdp(self, command: str, value: str) -> None:
        self.sink.send_packet(T.subnegotiation(T.OPT_MSDP, msdp.encode_msdp(command, value)))

    def _subscribe_msdp(self, reportable: object) -> None:
        """REPORT the variables we can use that this server actually advertises.

        Asking for a variable a server doesn't have gets an error line on some codebases and
        is ignored on others, so intersect rather than guess. Ordered by our list, not the
        server's, so the request is stable across sessions.
        """
        if not isinstance(reportable, list):
            return  # a scalar or table here is a non-conforming server; nothing safe to ask for
        available = {str(name) for name in reportable}
        wanted = [name for name in msdp.STANDARD_REPORTS if name in available]
        if self._diag is not None:
            self._diag.event("msdp.subscribe", offered=len(available), wanted=len(wanted))
        for name in wanted:
            self._send_msdp(msdp.CMD_REPORT, name)

    def shutdown(self) -> None:
        """Release session resources on close: leave the hub and stop logging."""
        self._closed = True  # ends the OnPluginTick chain
        self._vip_connection_changed(False)  # closing the tab drops the connection too
        for pack in self._mush_packs:
            pack.dispatch_close()  # plugins put their settings in variables to be saved
        self.engine.cancel_timers()  # cancel pending pack timers so none fire post-close
        self._persist_pack_vars()
        self._save_map()
        if self.hub is not None and self.name:
            self.hub.unregister(self.name, self._dispatch_remote)
        if self.logger is not None:
            self.logger.stop()
            self.logger = None

    # --- user rules (the no-code soundpack builder) ---

    def user_rules_dir(self) -> Path | None:
        """This world's user-pack dir (rules, sounds, and scripts); None headless."""
        if self.packs is None or not self.name:
            return None
        root = getattr(self.packs, "root", None)
        if root is None:
            return None
        return self._world_path(Path(root).parent / "userpacks")

    def _world_path(self, root: Path, suffix: str = "") -> Path:
        """This world's file or folder under ``root``, found where older builds filed it.

        Names used to be filed ASCII-only, so "Café" went under "Caf". Where the current
        name has nothing yet and the old one does, the old one is still this world's: its
        rules and map carry on after an update instead of seeming to vanish. Unless another
        saved world is literally called "Caf": then that folder is its own, and adopting
        it would mix the two worlds' rules and maps. Resolved once per session, so a save
        at close lands where the load at connect read from.
        """
        key = (root, suffix)
        if key not in self._world_paths:
            self._world_paths[key] = self._resolve_world_path(root, suffix)
        return self._world_paths[key]

    def _resolve_world_path(self, root: Path, suffix: str) -> Path:
        current = root / f"{world_component(self.name)}{suffix}"
        old_name = legacy_world_component(self.name)
        if not old_name or current.exists():
            return current
        legacy = root / f"{old_name}{suffix}"
        if legacy == current or not legacy.exists() or self._owned_by_another_world(old_name):
            return current
        return legacy

    def _owned_by_another_world(self, component: str) -> bool:
        """Whether another saved world has a claim on the old folder ``component``.

        It does if it is filed under that name now, or if an older build filed it there
        too ("Café" and "Cafè" were both "Caf"). Deciding between claimants needs every
        world at once, which config/world_files.py does at launch by copying the folder
        to each; a session on its own leaves an ambiguous folder alone.
        """
        if self._other_worlds is None:
            return False
        own = self.name.casefold()
        folded = component.casefold()  # on NTFS and APFS "Caf" and "CAF" are one folder
        for name in self._other_worlds():
            if name.casefold() == own:
                continue
            claims = {world_component(name).casefold(), legacy_world_component(name).casefold()}
            if folded in claims:
                return True
        return False

    def reload_user_rules(self) -> None:
        """(Re)register the world's field-based automation; safe to call live after a save.

        Validates the new rules against a throwaway engine BEFORE removing the live ones, so a
        rule with a bad advanced-regex can't clear every user rule and leave the world silent
        with an invalid rules.json still on disk. On a validation failure the working set is kept
        and the error is spoken/traced instead of swallowed by the reload's threadsafe callback.
        """
        pack_dir = self.user_rules_dir()
        if pack_dir is None:
            return
        rules = user_rules.load_rules(pack_dir)
        try:
            user_rules.register_rules(
                ScriptApi(AutomationEngine(), source=user_rules.SOURCE, base_dir=str(pack_dir)),
                rules,
            )
        except Exception as error:  # noqa: BLE001 - a bad rule must not destroy the working set
            if self._diag is not None:
                self._diag.event(
                    "user_rules.reload_failed", error=f"{type(error).__name__}: {error}"
                )
            self.voice.speak(f"Rule not applied: {error}", channel=SYSTEM_CHANNEL, interrupt=False)
            return
        self.engine.remove_source(user_rules.SOURCE)
        user_rules.register_rules(
            ScriptApi(self.engine, source=user_rules.SOURCE, base_dir=str(pack_dir)), rules
        )

    def reload_user_scripts(self, announce: bool = False) -> bool:
        """Transactionally replace this world's ordered native-Lua automation scripts."""
        pack_dir = self.user_rules_dir()
        if pack_dir is None:
            return False
        next_generation = self._user_script_generation + 1
        prefix = f"user-script:{next_generation}"
        try:
            loaded = user_scripts.load_scripts(self.engine, pack_dir, source_prefix=prefix)
        except Exception as error:  # noqa: BLE001 - keep the previous working script set live
            if self._diag is not None:
                self._diag.event(
                    "user_scripts.reload_failed", error=f"{type(error).__name__}: {error}"
                )
            self.voice.speak(
                f"Scripts not applied: {type(error).__name__}: {error}",
                channel=SYSTEM_CHANNEL, interrupt=False,
            )
            return False

        for item in self._user_script_runtimes:
            self.engine.remove_source(item.source)
        self._user_script_runtimes = loaded
        self._user_script_generation = next_generation
        if self._diag is not None:
            self._diag.event(
                "user_scripts.reloaded", count=len(loaded), generation=next_generation
            )
        if announce:
            count = len(loaded)
            self.voice.speak(
                f"{count} automation script{'s' if count != 1 else ''} loaded.",
                channel=SYSTEM_CHANNEL, interrupt=False,
            )
        return True

    # --- pack-variable persistence (MUSHclient SaveState equivalent) ---

    # --- room map ---

    def _map_path(self) -> Path | None:
        """This world's map file, or None for a session with no name to file it under."""
        if not self.name:
            return None
        return self._world_path(self.map_dir, ".json")

    def _load_map(self) -> None:
        path = self._map_path()
        if path is None:
            return
        self.map = load_map(path)
        self._map_unsaved = 0

    def _save_map(self) -> None:
        path = self._map_path()
        if path is None or not self.map.rooms:
            return
        if save_map(self.map, path):
            self._map_unsaved = 0

    def _pack_vars_path(self) -> Path | None:
        if self.packs is None or not self.name:
            return None  # headless/test sessions have nothing to persist against
        root = getattr(self.packs, "root", None)
        if root is None:
            return None
        return self._world_path(Path(root).parent / "state", "-vars.json")

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
        # persisting it would pin a moved/reinstalled pack to its old location. A VIPMud pack
        # that saves its settings in its own files starts each session without its variables.
        transient = {"sppath"}.union(*(
            pack.written_variables for pack in self._vip_packs if pack.saves_its_own_settings
        ))
        data = {k: v for k, v in self.engine.all_vars().items() if k not in transient}
        try:
            atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))
        except OSError:
            pass  # a state write must never break session close

    def begin_login(self, world: str) -> None:
        """Arm auto-login for ``world`` if credentials are stored for it."""
        self._login_phase_started = self._clock()  # a fresh connection means fresh prompts
        self._login = None
        if self.credentials is None:
            return
        creds = self.credentials.get(world)
        if creds is None:
            return
        username, password = creds
        self._login = AutoLogin(username, password, self._send_for_login)

    def _send_for_login(self, text: str) -> None:
        """Auto-login's typing. On a slow link its password goes out before the WILL ECHO
        that hides the prompt arrives, so it starts the hidden-input timeout regardless."""
        self._send(text)
        self._arm_hidden_timeout()

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
            count = len(result.loaded)
            parts.append(f"{count} soundpack{'s' if count != 1 else ''} loaded")
        # Packs loaded but armed no triggers can never make a sound -- say so at connect, not
        # only in the trace, so a blind user learns the pack is inert without reading a file.
        if result.loaded and triggers == 0:
            parts.append("packs loaded but no triggers registered")
        for pack_id in result.skipped_untrusted:
            parts.append(
                f"{pack_id} not loaded because it isn't trusted yet — trust it in Manage "
                "Soundpacks, then reconnect"
            )
        for pack_id, error in result.failed.items():
            parts.append(f"{pack_id} failed to load: {error}")
        for conflict in result.conflicts:
            who = " and ".join(conflict.sources)
            parts.append(f"{conflict.kind} {conflict.token} bound by {who}")
        if not parts:
            return
        summary = "; ".join(parts)
        self.voice.speak(summary, channel=SYSTEM_CHANNEL, interrupt=False)
        self._post(protocol.echo(f"* {summary}"))

    # --- inbound from the MUD (telnet events) ---

    def on_telnet_event(self, event: T.Event) -> None:
        if isinstance(event, T.DataReceived):
            self._feed_text(self._decode_server_bytes(event.data))
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
        elif (
            isinstance(event, T.Negotiation)
            and event.option == T.OPT_ECHO
            and event.command in (T.WILL, T.WONT)  # DO/DONT ask whether WE echo: not this
        ):
            # The server takes over echo (IAC WILL ECHO) right before a password prompt and
            # releases it (WONT) afterward. While it holds echo, the input is a secret: keep
            # it out of the session log, the command history, and the autoretype buffer, and
            # send it exactly as typed (_dispatch_secret).
            self._set_input_masked(event.command == T.WILL)

    def _decode_server_bytes(self, data: bytes) -> str:
        """Decode server text in the world's encoding (see protocol/charset.py).

        Many legacy MUDs (notably Spanish-language ones) send an 8-bit encoding; the old
        hard utf-8/replace decode turned every accented letter into U+FFFD, which a screen
        reader speaks as garbage. ``auto`` guesses; a world set to its real encoding reads
        exactly what the MUD sent, including Cyrillic and CJK text the guess can't recover.
        """
        return self.codec.decode(data)

    def _on_encoding_latch(self, position: int) -> None:
        if self._diag is not None:
            self._diag.event("encoding.latch", encoding="cp1252", at=position)

    def _feed_text(self, text: str) -> None:
        self._pending += text
        while "\n" in self._pending:
            raw, self._pending = self._pending.split("\n", 1)
            self._emit_line(raw.rstrip("\r"))
        if self._pending:
            # A prompt with no trailing newline and no GA/EOR (some MUDs) would otherwise sit in
            # _pending unspoken forever, deadlocking auto-login. Flush it if no more bytes arrive
            # shortly; a newer chunk bumps the generation so this scheduled flush no-ops. GA/EOR
            # still flush immediately via _flush_prompt, making this only a backstop.
            self._prompt_gen += 1
            gen = self._prompt_gen
            self._schedule(_PROMPT_IDLE_FLUSH_SECONDS, lambda: self._idle_flush_prompt(gen))

    def _idle_flush_prompt(self, gen: int) -> None:
        if gen == self._prompt_gen and self._pending:
            self._flush_prompt()

    def _flush_prompt(self) -> None:
        if self._pending:
            self._emit_line(self._pending)
            self._pending = ""

    def _emit_line(self, text: str) -> None:
        text, cues = parse_msp_line(text)  # strip MSP markers before colour parsing
        for cue in cues:
            channel = MUSIC_CATEGORY if cue.kind == "music" else DEFAULT_CATEGORY
            if cue.is_stop:
                if cue.url:  # the spec's way to set where later cues download from
                    self._msp_url = cue.url
                if self._diag is not None:
                    self._diag.event("msp.cue", kind=cue.kind, stop=True)
                self._stop_media(channel)
                continue
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
            gain = cue.volume / 100.0
            url = cue.url or self._msp_url
            times = max(1, cue.repeats)  # L= plays that many times; -1 loops until stopped
            if cue.kind == "music":
                self._start_media(
                    cue.file, url, channel,
                    lambda path, gain=gain, times=-1 if cue.loops_forever else times: (
                        self.sound.music(path, MUSIC_CATEGORY, gain=gain, times=times)
                    ),
                )
            else:
                self._start_media(
                    cue.file, url, channel,
                    lambda path, gain=gain, loop=cue.loops_forever, times=times: self.sound.play(
                        path, DEFAULT_CATEGORY, gain=gain, loop=loop, times=times
                    ),
                )
        spans = parse_ansi(text)
        plain = "".join(span.text for span in spans)
        if not plain.strip():
            return  # blank line: any sound cues already fired; don't show/speak "blank"
        for pack in self._mush_packs:
            pack.dispatch_line(plain)
        line = Line(plain, spans=spans)
        self.engine.process_line(line)  # may set line.channel and gag flags
        self._log(line.plain_text)  # full session log, including gagged-from-speech
        if self._login is not None and not self._login.done:
            self._login.feed(line.plain_text)  # answer name/password prompts
        if self._walk is not None and self._walk.active:
            self._walk.on_line(line.plain_text)  # halt the walk if a step was blocked
        if line.substitute is not None:
            # A pack replaced the line (VIPMud #Sub). The log, login and the walker keep what
            # the MUD sent; the player sees and hears the replacement.
            line.plain_text, line.spans = line.substitute, []
            if not line.plain_text.strip():
                return
        policy = self.channels.policy(line.channel)
        if policy.speak and not line.gagged and self.engine.line_speech:
            self.voice.speak(
                line.plain_text,
                channel=(policy.voice or line.channel),
                interrupt=policy.interrupt or self.interrupt_mode,
            )
        if policy.display and not (line.gagged and not line.display_when_gagged):
            self.buffer.append(line)
            self.word_index.add_line(line.plain_text)
            self._post(
                protocol.line(
                    line.plain_text,
                    gagged=line.gagged,
                    display_when_gagged=line.display_when_gagged,
                )
            )

    def _handle_subnegotiation(self, sub: T.Subnegotiation) -> None:
        result = from_subnegotiation(
            sub.option, sub.payload, self.codec.decode_payload, self.codec.decode_gmcp
        )
        if isinstance(result, list):
            msdp_room_changed = False
            for message in result:
                if isinstance(message, OobMessage):
                    self._gauges[message.name] = message.value
                    self.engine.set_mud_var(message.name, message.value)
                    self.engine.set_mud_var(f"{message.source}.{message.name}", message.value)
                    if message.source == "gmcp":
                        for pack in self._mush_packs:
                            pack.dispatch_gmcp(message.name, message.value)
                        if message.name.lower() == "core.goodbye":
                            self._server_goodbye(message.value)
                        elif message.name.lower() == "room.wrongdir":
                            self._wrong_direction(message.value)
                        elif message.name.lower().startswith("client.media."):
                            self._client_media(message.name, message.value)
                    elif message.name == msdp.REPORTABLE_VARIABLES:
                        self._subscribe_msdp(message.value)
                    elif message.name in _MSDP_ROOM_VARS:
                        msdp_room_changed = True
                    if self._is_room_info(message):
                        self._update_room(message.value)
            # Once per packet, not once per variable: a server reports every changed variable
            # together, so updating per message would announce one move several times over.
            if msdp_room_changed and (room := self._msdp_room()):
                self._update_room(room)
            if result:
                self._post(protocol.status(self._gauges))
        elif isinstance(result, ServerStatus):
            self._gauges.update(result.data)
            for name, value in result.data.items():
                self.engine.set_mud_var(name, value)
                self.engine.set_mud_var(f"mssp.{name}", value)
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

    def _msdp_room(self) -> dict:
        """The navigator's room shape from MSDP's variables, or ``{}`` if none are set.

        Servers report either a ``ROOM`` table or flat ``ROOM_*`` variables, and MSDP names
        are uppercase, so neither form reaches ``Navigator.where()`` untranslated. VNUM is
        carried because two rooms can share a name and it's what makes a move detectable.
        """
        table = self._gauges.get("ROOM")
        if not isinstance(table, dict):
            table = {}
        room = {
            "name": table.get("NAME") or self._gauges.get("ROOM_NAME"),
            "area": (
                table.get("AREA") or self._gauges.get("ROOM_AREA")
                or self._gauges.get("AREA_NAME")
            ),
            "exits": table.get("EXITS") or self._gauges.get("ROOM_EXITS"),
            "vnum": table.get("VNUM") or self._gauges.get("ROOM_VNUM"),
        }
        return {key: value for key, value in room.items() if value}

    # --- server media (MSP U= and GMCP Client.Media) ---

    def _start_media(
        self, name: str, url: str, channel: str, play: Callable[[str], None]
    ) -> None:
        """Play ``name`` through ``play``: from disk when it's there, after a download when the
        server said where to get it, else by name as before (the backend reports it missing)."""
        gen = self._media_gen[channel] = self._media_gen.get(channel, 0) + 1
        if not self._native_audio:
            play(name)
            return
        path = self.media.locate(name, self._media_sounds_root())
        if path is not None:
            play(path)
            return
        if url and self.download_media:
            address = media_url(url, name)
            flushes = self.sound.flushes

            def ready(downloaded: str) -> None:
                # A stop, or a newer cue on this channel, arrived while the file downloaded,
                # or all sound was cut: the panic key, a dropped link, the tab closing.
                if self._media_gen.get(channel) == gen and self.sound.flushes == flushes:
                    play(downloaded)

            if self.media.fetch(name, address, ready):
                if self._diag is not None:
                    self._diag.event("media.download", file=name, url=address)
                return
        play(name)

    def _media_sounds_root(self) -> str | None:
        """The folder server media is looked for in before the download cache: @sppath when it
        lies inside the player's Sounds folder or an installed pack, else the Sounds folder.

        @sppath is a variable any pack, or MUD text a pack runs as script, can set, and
        ScriptApi._sounds_root holds packs to the same rule; taken as it is, a cue could name
        any file under "/". A UNC @sppath isn't even checked: on Windows, resolving one is
        enough to send the player's NTLM hash to that host.
        """
        sounds_dir = self.engine.sounds_dir
        sppath = self.engine.get_var("sppath")
        if not sppath or is_unc(sppath):
            return sounds_dir
        packs_dir = getattr(self.packs, "packs_dir", None)
        trusted = [root for root in (sounds_dir, packs_dir) if root]
        if any(within(root, sppath) for root in trusted):
            return sppath
        return sounds_dir

    def _stop_media(self, channel: str) -> None:
        self._media_gen[channel] = self._media_gen.get(channel, 0) + 1
        self._media_cues.pop(channel, None)
        self.sound.stop(channel)

    def _fade_away_media(self, channel: str, seconds: float) -> None:
        self._media_gen[channel] = self._media_gen.get(channel, 0) + 1  # still downloading: never
        self._media_cues.pop(channel, None)
        self.sound.fade_away(channel, seconds)

    def _media_failed(self, url: str, reason: str) -> None:
        if self._diag is not None:
            self._diag.event("media.download", url=url, failed=reason)
        self._post(protocol.echo(f"* sound download failed: {url} ({reason})"))

    def _client_media(self, package: str, data: object) -> None:
        """GMCP ``Client.Media.*`` (Mudlet's media spec): Default, Load, Play and Stop."""
        verb = package.rsplit(".", 1)[-1].lower()
        body = data if isinstance(data, dict) else {}
        if verb == "default":
            self._media_url = str(body.get("url") or "")
        elif verb == "load":
            name = str(body.get("name") or "")
            if not name or is_unsafe(name):
                return
            self._media_preload(name, self._media_source(body))
        elif verb == "play":
            self._media_play(body)
        elif verb == "stop":
            self._media_stop(body)

    def _media_play(self, body: dict) -> None:
        name = str(body.get("name") or "")
        kind = str(body.get("type") or "sound").lower()
        if not name or kind not in ("sound", "music"):  # video has nowhere to play
            return
        if is_unsafe(name):  # the same hostile shapes MSP refuses
            if self._diag is not None:
                self._diag.event("media.play", file=name, blocked=True)
            return
        if _media_is_zero(body.get("volume")):
            # Mudlet's preload: volume 0 asks for the file ahead of time, and plays nothing.
            self._media_preload(name, self._media_source(body))
            return
        gain = _media_int(body.get("volume"), _MEDIA_DEFAULT_VOLUME, 1, 100) / 100.0
        loops = _media_int(body.get("loops"), 1, -1, 1_000_000)
        loop = loops < 0  # -1 loops until stopped; otherwise it plays that many times
        times = max(1, loops)
        key = str(body.get("key") or "")
        if kind == "music":
            channel = MUSIC_CATEGORY
            current = self._media_cues.get(channel)
            # "continue" (default true): the same track asked for again keeps playing rather
            # than restarting, which is what a server re-sending an area's music means.
            if _media_continues(body.get("continue")) and current and current["name"] == name:
                return
        elif key:
            channel = f"media:{key}"
        else:
            self._media_seq += 1
            channel = f"media-{self._media_seq}"
        self._forget_finished_media()
        timing = _media_timing(body)
        self._media_cues[channel] = {
            "name": name,
            "type": kind,
            "key": key,
            "tag": str(body.get("tag") or ""),
            "priority": _media_int(body.get("priority"), 0, 0, 100),
            "fadeout": timing.fade_out if timing is not None else 0.0,  # a fadeaway's length
        }
        url = self._media_source(body)
        if self._diag is not None:
            self._diag.event("media.play", file=name, kind=kind, channel=channel)
        caption = " ".join(str(body.get("caption") or "").split())
        if caption:
            # The spec's closed caption, for players who can't hear the sound. Shown in the
            # output, in brackets as Mudlet prints them, where the review keys find it, but not
            # spoken over the sound itself.
            self.sink.echo(f"[{caption}]")
        if kind == "music":
            self._start_media(
                name, url, channel,
                lambda path: self.sound.music(
                    path, channel, gain, times=-1 if loop else times, timing=timing
                ),
            )
        else:
            self._start_media(
                name, url, channel,
                lambda path: self.sound.play(
                    path, channel, gain=gain, loop=loop, times=times, timing=timing
                ),
            )

    def _media_preload(self, name: str, url: str) -> None:
        """Client.Media's Load, and a Play at volume 0: fetch the file now, play nothing."""
        if not name or is_unsafe(name) or not url or not self.download_media:
            return
        if self._native_audio and self.media.locate(name, self._media_sounds_root()) is None:
            self.media.fetch(name, media_url(url, name))

    def _media_source(self, body: dict) -> str:
        """Where a Load or Play downloads from: its own url, which then stays the default for
        later cues, else the default. The spec lets Play leave url out once Default or Load
        has given one, and Mudlet keeps the last url a Load or Play gave."""
        url = str(body.get("url") or "")
        if url:
            self._media_url = url
        return self._media_url

    def _media_stop(self, body: dict) -> None:
        """Stop the Client.Media cues matching every field given; an empty body stops them all.
        ``priority`` matches cues at or below it, as the spec defines. With ``fadeaway`` they
        fade out first, over the fadeout their Play gave, else this Stop's, else five seconds."""
        wanted = {
            field: str(body[field])
            for field in ("name", "type", "key", "tag")
            if body.get(field) not in (None, "")
        }
        ceiling = _media_int(body.get("priority"), None, 0, 100)
        fade_away = _media_true(body.get("fadeaway"))
        stop_fade = _media_int(body.get("fadeout"), _MEDIA_FADEAWAY_MS, 0, _MEDIA_LONGEST_MS)
        for channel, cue in list(self._media_cues.items()):
            if any(str(cue[field]) != value for field, value in wanted.items()):
                continue
            if ceiling is not None and int(cue["priority"]) > ceiling:  # type: ignore[call-overload]
                continue
            if fade_away:
                seconds = float(cue.get("fadeout") or 0.0) or stop_fade / 1000  # type: ignore[arg-type]
                self._fade_away_media(channel, seconds)
            else:
                self._stop_media(channel)

    def _forget_finished_media(self) -> None:
        """Drop finished one-shots so a long session's keyless cues don't pile up forever."""
        if len(self._media_cues) < _MEDIA_TRACKED:
            return
        for channel in list(self._media_cues):
            if channel.startswith("media-") and not self.sound.is_playing(channel):
                self._media_cues.pop(channel, None)
                self._media_gen.pop(channel, None)

    def _server_goodbye(self, reason: object) -> None:
        """GMCP ``Core.Goodbye``: the server is closing this link deliberately, and the body
        says why (kicked, banned, shutting down, logged in elsewhere). Suppress first: the
        drop can arrive before the speech does, and reconnecting into a ban helps nobody."""
        text = reason.strip() if isinstance(reason, str) else ""
        closed = "The MUD closed the connection"
        spoken = f"{closed}: {text}" if text else f"{closed}."
        if self._diag is not None:
            self._diag.event("gmcp.goodbye", reason=text)
        self._suppress_reconnect()
        self.voice.speak(spoken, channel=SYSTEM_CHANNEL, interrupt=True)
        self._post(protocol.echo(f"* {spoken}"))

    def _wrong_direction(self, direction: object) -> None:
        """GMCP ``room.wrongdir``: the MUD says that exit doesn't exist. Stop walking.

        Authoritative where the blocked-movement line patterns are an English guess, so a
        route built from a stale edge halts here instead of firing its remaining steps.
        The exit is deliberately NOT removed from the map: a closed door and a door that
        was never there look identical from here, and dropping a real exit loses a route.
        """
        if self._walk is None or not self._walk.active:
            return
        self._walk.cancel()
        name = str(direction).strip() if direction else ""
        self._speak_system(f"no exit {name}, walk stopped" if name else "walk stopped, no exit")

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
        was = self.map.here
        known = len(self.map.rooms)
        room_id = self.map.observe(room)
        if len(self.map.rooms) > known:
            self._map_unsaved += 1
            if self._map_unsaved >= _MAP_SAVE_INTERVAL:
                self._save_map()  # a crash mustn't cost a whole session's exploring
        # A MUD that identifies its rooms gives an exact movement signal. Comparing whole
        # reports instead counts a re-send of the SAME room (an exit list that grew, a
        # detail that changed) as a move, which advances a walk the player hasn't taken a
        # step of. Only reports carrying no id fall back to comparing the payload.
        moved = room_id != was if room_id else changed
        if moved:
            self._on_player_moved()  # follow mode: the new room barges over the old one
        if moved and self._walk is not None and self._walk.active:
            self._walk.on_room_change()  # confirmed move -> advance the safe-walk

    def _on_player_moved(self) -> None:
        """Movement signal (a direction sent, or GMCP room change) for follow mode.

        Cuts the current utterance so the next room reads immediately; ordinary
        lines keep queueing. Both signals may fire for one move — stop is idempotent.
        """
        if self.follow_mode:
            self.voice.interrupt()

    # --- inbound from the renderer (WS messages) ---

    def on_ws_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == protocol.INPUT:
            text = message.get("text", "")
            if self._input_masked:
                self._dispatch_secret(text)
                return
            if text.strip():
                self._last_input = text
            elif self.autoretype and self._last_input:
                # A blank line normally goes to the MUD as-is (pagers use it); with
                # autoretype on, an empty Enter repeats the last typed input instead.
                text = self._last_input
            # A client command ("/alias x = a;b") must not be split on the separator.
            commands = [text] if text.startswith(CLIENT_PREFIX) else self._split_commands(text)
            for command in commands:
                self._dispatch_command(command)
        elif kind == protocol.KEY:
            self._handle_key(message.get("key", ""))
        elif kind == protocol.CLIENT_ERROR:
            self._handle_client_error(message)
        elif kind == protocol.FIND:
            self._handle_find(message)

    def _handle_find(self, message: dict) -> None:
        """Search the scrollback and park the review cursor on the hit.

        The engine owns the search because the buffer is the deep copy: it holds far more
        history than the renderer's control does. The matched text goes back as a
        FIND_RESULT so the renderer can put its own caret on the same line where it can.
        """
        term = message.get("term", "")
        forward = bool(message.get("forward", False))
        restart = bool(message.get("restart", False)) or not self.review.active
        if message.get("from_bottom"):
            # The reader has been put back below the newest line, so the search goes on
            # from there: back, the newest line is the first one it looks at; forward,
            # there's nothing past it.
            self.review.enter()
            include_current = not forward
        else:
            if restart:
                # A new Find starts at the chosen edge and includes that edge line. Repeats
                # remain exclusive, so F3 advances instead of finding the same line forever.
                self.review.enter()
                if forward:
                    self.review.top()
            include_current = restart
        hit = self.review.search(
            term,
            forward=forward,
            case_sensitive=bool(message.get("case_sensitive", False)),
            include_current=include_current,
        )
        self._post(protocol.find_result(hit, found=bool(hit)))
        self._speak_review(hit or f"not found: {term}")

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
            self.voice.speak(summary, channel=SYSTEM_CHANNEL, interrupt=False)

    def _split_commands(self, text: str) -> list[str]:
        """Split stacked input on the separator ("n;n;look"); an empty separator is off.

        The separator typed twice is sent as itself, as in MUSHclient: "say hi;; bye" is the
        one command "say hi; bye". Soundpacks' scripts never come through here.
        """
        separator = self.command_separator
        if not separator or separator not in text:
            return [text]
        commands: list[str] = []
        current: list[str] = []
        index = 0
        while index < len(text):
            if not text.startswith(separator, index):
                current.append(text[index])
                index += 1
            elif text.startswith(separator, index + len(separator)):
                current.append(separator)  # doubled: the character itself
                index += 2 * len(separator)
            else:
                commands.append("".join(current))
                current = []
                index += len(separator)
        commands.append("".join(current))
        return [command for command in commands if command != ""]

    def _dispatch_command(self, text: str, *, allow_client: bool = True) -> None:
        if text.strip():
            if self._login is not None and not self._login.done:
                # The user is typing their own login (or already playing): stand down so a
                # later in-game line can't make auto-login send the stored credentials.
                self._login.disarm()
            if self.log_commands:
                self._log("> ***" if self._input_masked else f"> {text}")
        if allow_client and self._client_command(text):
            return
        if self._safe_speedwalk(text) or self._speedwalk(text):
            return
        for line in self.engine.process_input(text):
            self._send(line)
            if self.nav.record(line):  # build the breadcrumb trail from manual walking
                self._on_player_moved()

    def _dispatch_secret(self, text: str) -> None:
        """Send what was typed while the server hides input (a password), exactly as typed.

        The normal input path would rewrite or reroute it: auto-repeat swapped it for the
        last command (the username), the separator split "abc;def" in two, ".2news" ran as
        a speedwalk, and aliases, client commands and the breadcrumb trail all read it.
        """
        if text.strip():
            if self._login is not None and not self._login.done:
                self._login.disarm()  # the user is answering the prompt themselves
            if self.log_commands:
                self._log("> ***")
        self._send(text)
        self._hidden_answers += 1
        if self._hidden_answers == 2:
            # Once per hidden stretch, like Mudlet's box saying "still hidden" when it comes
            # back: a MUD that never ends the hiding leaves this the only clue to the way out.
            self._speak_system(
                "The MUD is still hiding what you type. If it shouldn't be, press Escape.",
                interrupt=False,
            )
        self._arm_hidden_timeout()

    def _arm_hidden_timeout(self) -> None:
        """(Re)start the minute after which a login password prompt that never ended lets go.

        Measured from the last line sent, never from the prompt, so it can't fire while
        someone is still typing. A line past the login phase still cancels any earlier
        deadline, so that one can't fire mid-session either.
        """
        self._hidden_timeout_token += 1
        if self._clock() - self._login_phase_started >= _LOGIN_PHASE_SECONDS:
            return
        token = self._hidden_timeout_token
        self._schedule(_HIDDEN_INPUT_TIMEOUT_SECONDS, lambda: self._hidden_input_timed_out(token))

    def _hidden_input_timed_out(self, token: int) -> None:
        if token != self._hidden_timeout_token or not self._input_masked:
            return
        if self._diag is not None:
            self._diag.event("input.unhidden", reason="timeout")
        self._withdraw_option(T.OPT_ECHO)  # so the MUD's next WILL ECHO is a fresh request
        self.forget_hidden_input()
        self._speak_system(
            "The MUD didn't say it had stopped hiding what you type, so genericMud stopped "
            "hiding it.",
            interrupt=False,
        )

    def _unhide_on_request(self) -> None:
        """Escape: treat typing as ordinary until the MUD hides it again (its next WILL ECHO)."""
        if not self._input_masked:
            return  # nothing hidden: Escape does nothing
        self.forget_hidden_input()
        self._speak_system("Typing is no longer hidden.")

    def forget_hidden_input(self) -> None:
        """Treat typing as ordinary again, for when no WONT ECHO is coming.

        The socket went away or was replaced (the window's Disconnect reports itself
        through on_connection_status too); a login prompt timed out; or the user pressed
        Escape.
        """
        self._set_input_masked(False)

    def _set_input_masked(self, masked: bool) -> None:
        """Track whether the server hides typed input; tell the renderer when that flips.

        The renderer keeps its own Up-arrow history, so it has to know as well, or the
        password is one keypress away for anyone at the keyboard.
        """
        if masked == self._input_masked:
            return
        self._input_masked = masked
        self._hidden_answers = 0
        if not masked:
            # Hiding a prompt keeps a pending timeout: auto-login's password may have gone
            # out before the WILL ECHO for it arrived.
            self._hidden_timeout_token += 1
        self._post(protocol.input_masked(masked))

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
        self._start_safe_walk(steps)
        return True

    def _start_safe_walk(
        self, steps: list[str], announcement: str = "", waypoints: list[str] | None = None
    ) -> None:
        """Begin a step-at-a-time walk, superseding any walk already running.

        ``waypoints`` (a mapped route only) is the room each step should land in, so the
        walk halts if something moves the player off it.
        """
        self._cancel_walk()  # don't leave both walks' timers firing into each other

        def send_and_record(direction: str) -> None:
            self._send(direction)
            if self.nav.record(direction):
                self._on_player_moved()

        self._walk = SafeWalk(
            steps,
            send=send_and_record,
            schedule=self._schedule,
            announce=self._speak_system,
            waypoints=waypoints,
            locate=lambda: self.map.here,
            reports_rooms=self.nav.room is not None,
        )
        if announcement:
            self._speak_system(announcement)
        self._walk.start()

    def _cancel_walk(self) -> None:
        if self._walk is not None and self._walk.active:
            self._walk.cancel()

    def _speedwalk(self, text: str) -> bool:
        """Expand and send a "." speedwalk run (e.g. ".3n2e"); False if not one."""
        if not text.startswith(SPEEDWALK_PREFIX):
            return False
        steps = expand_speedwalk(text[len(SPEEDWALK_PREFIX) :])
        if not steps:
            return False
        self._cancel_walk()  # an immediate speedwalk supersedes a step-by-step one in progress
        self._on_player_moved()  # one interrupt for the whole burst, not one per step
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
        elif verb == "goto":
            self._goto(rest)
        elif verb == "label":
            self._label_room(rest)
        elif verb == "map":
            self._speak_system(self.map.summary())
        elif verb == "update" and not rest:
            self.update_packs()
        elif verb == "logs":
            self._speak_logs()
        else:
            return False  # unknown /verb -> let the MUD have it (some use slash commands)
        return True

    # --- room map ---

    def _goto(self, query: str) -> None:
        """Walk to a mapped room named by ``query``, step by step so it can halt."""
        if not query:
            self._speak_system("usage: /goto <room name>")
            return
        if not self.map.rooms:
            self._speak_system("no map yet; this MUD may not tell the client where you are")
            return
        if not self.map.here:
            self._speak_system("this room is not on the map, so there is nowhere to walk from")
            return
        matches = self.map.search(query)
        if not matches:
            self._speak_system(f"no mapped room matches {query}")
            return
        for room in matches:
            route = self.map.route_to(room.id)
            if route is None:
                continue
            path, waypoints = route
            target = room.label or room.name
            if not path:
                self._speak_system(f"you are already at {target}")
                return
            # Through SafeWalk, not a burst: it halts on a blocked exit, and a route
            # assembled from remembered rooms is exactly where a stale edge shows up.
            self._start_safe_walk(
                path, f"walking to {target}, {spoken_count(len(path), 'step')}", waypoints=waypoints
            )
            return
        # Every match is somewhere the map knows of but can't currently reach.
        found = matches[0].label or matches[0].name
        self._speak_system(f"{found} is on the map but no known route leads there from here")

    def _label_room(self, text: str) -> None:
        """Name the current room so /goto can find it later."""
        if not text:
            self._speak_system("usage: /label <name for this room>")
            return
        if not self.map.set_label(text):
            self._speak_system("this room is not on the map, so it cannot be labelled")
            return
        self._save_map()  # a label is deliberate work; don't risk losing it to a crash
        self._speak_system(f"this room is now {text}")

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
                regex=True, name=pattern, source=INTERACTIVE_SOURCE, keep_evaluating=False,
            )
        else:
            self.engine.remove_trigger(pattern)
            self.engine.add_trigger(  # (?i): match the MUD's output regardless of case
                f"(?i){re.escape(pattern)}", callback, regex=True, name=pattern,
                source=INTERACTIVE_SOURCE,
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
            n = _parse_count(count)
            if n is not None:
                self._speak_review(self.review.recall(n, channel=channel or None))
        elif namespace == "review" and argument in _REVIEW_VERBS:
            if not self.review.active:
                self.review.enter()
            self._speak_review(getattr(self.review, argument)())
        elif namespace == "voice" and argument == "flush":
            self.voice.flush()
        elif namespace == "voice" and argument in _TOGGLE_ACTIONS:
            self._toggle(argument)
        elif namespace == "input" and argument == "autoretype":
            self._toggle("autoretype")
        elif namespace == "input" and argument == "unhide":
            self._unhide_on_request()
        elif namespace == "chan":
            self._handle_channel_review(argument)
        elif namespace == "sound" and argument == "flush":
            self.sound.flush()  # panic key: cut all playing audio (Shift+F11)
        elif namespace == "nav":
            self._handle_nav(argument)
        elif namespace == "map" and argument == "where":
            self._speak_system(self.map.describe())
        elif namespace == "log" and argument == "toggle":
            self._toggle_log()
        elif namespace == "diag" and argument == "where":
            self._diag_where()
        # "soundpack:toggle" and other namespaces are wired as features land.

    def _speak_logs(self) -> None:
        """`/logs`: where each kind of log is kept, and how many are in each folder.

        Two folders, two audiences -- a session log to read back, a diagnostic file to send
        to someone when something is wrong -- so it is worth being able to ask which is
        which without opening a file manager and listening to a list.
        """
        worlds = world_log_dir()
        diagnostics = diagnostic_log_dir()
        self._speak_system(
            f"world logs in {worlds} ({_files(log_count(worlds))}); "
            f"diagnostics in {diagnostics} ({_files(log_count(diagnostics))})"
        )

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

    def _toggle(self, attr: str) -> None:
        """Flip a speech-ergonomics toggle, announce it, and persist via pref_sink."""
        value = not getattr(self, attr)
        setattr(self, attr, value)
        if self.pref_sink is not None:
            self.pref_sink(attr, value)
        self._speak_system(f"{_TOGGLE_ACTIONS[attr]} {'on' if value else 'off'}")

    def _handle_channel_review(self, action: str) -> None:
        # "chan:prev|next|older|newer|prev_word|next_word" or "chan:recall:N".
        verb, _, count = action.partition(":")
        if verb == "recall":
            n = _parse_count(count)
            if n is not None:
                self._speak_review(self.chan_review.recent(n))
        elif verb in _CHANNEL_REVIEW_VERBS:
            self._speak_review(getattr(self.chan_review, self._chan_method(verb))())

    @staticmethod
    def _chan_method(verb: str) -> str:
        # prev/next cycle channels; the rest are same-named ChannelReview methods.
        return {"prev": "prev_channel", "next": "next_channel"}.get(verb, verb)

    def _speak_review(self, text: str) -> None:
        # A review gesture on an empty buffer (fresh session, failed connect) returns "".
        # Speaking "" still interrupts, so it would silence the client and announce nothing;
        # give the user feedback instead. Non-blank lines are the only real content here --
        # blank server lines are never buffered.
        text = text or "no message"
        self.voice.speak(text, channel=REVIEW_CHANNEL, interrupt=True)
        self._post(protocol.review(text))

    def _speak_system(self, text: str, *, interrupt: bool = True) -> None:
        self.voice.speak(text, channel=SYSTEM_CHANNEL, interrupt=interrupt)
        self._post(protocol.echo(f"* {text}"))

    def after_reconnect(self) -> None:
        """Reattach this session to a socket that is live again.

        Shared by the transport's own backoff loop and a manual reconnect from the UI, so
        the two cannot drift. The state it resets is all per-socket -- a socket that is up
        while the engine still believes it is disconnected leaves packs paused and
        auto-login stranded at the login screen.
        """
        self.engine.connected = True
        # A fresh socket is a fresh byte stream: drop any half-decoded multibyte tail and
        # the legacy-encoding latch, or a drop mid-character mis-decodes the whole reconnect.
        self.codec.reset()
        # Echo state belongs to the dead socket: no WONT ECHO is coming for it. Through
        # forget_hidden_input, as on_connection_status does, so the window's history and
        # the hidden-input timeout hear about it too.
        self.forget_hidden_input()
        # A fresh socket means fresh login prompts; the old AutoLogin is marked done, so
        # re-arm it or a reconnect strands the user at the login screen.
        self.begin_login(self.name)

    def on_connection_status(self, message: str) -> None:
        """Surface a transport status line (reconnecting, reconnected) to the user.

        The end of a session arrives here whatever caused it. A dropped link and a quit
        both end in the transport's own "disconnected"; the UI's deliberate close emits
        no status at all, so _do_disconnect calls this method itself. Naming the world in
        one place is what keeps those three worded identically. A reconnect arrives here
        the same way, from the backoff loop or from the window's Reconnect, so both get
        the per-socket reset and the VIPMud packs' OnConnect.
        """
        spoken = message
        echoed = f"* {message}"
        if message.startswith(_SOCKET_CHANGES):
            # Echo state belongs to the socket that just went away. Left armed, every
            # command after it would go out as a password: no client commands, aliases or
            # speedwalks, and "***" in the log, because no WONT ECHO is ever coming.
            self.forget_hidden_input()
        if message.startswith(("disconnected", "protocol error", "reconnect failed")):
            # A terminal drop (quit, network death we won't reconnect): silence the pack's
            # looping music/ambience -- nothing will ever stop those cues otherwise. A
            # "reconnecting" status keeps them; the session is expected to resume.
            # Also mark the engine disconnected: packs read IsConnected(), and the
            # OnPluginTick chain pauses so a tick can't restart music after quit.
            self.engine.connected = False
            self.engine.cancel_timers()  # a pending #alarm must not fire into a dead session
            self._pack_timers_cancelled = True
            self.sound.flush()
            if message.startswith("protocol error"):
                # The raw exception is developer-speak; speak plain language and leave the
                # detail in the output log (echoed below) for anyone who wants it.
                spoken = ("Lost connection: the MUD sent data the client couldn't read. "
                          "Not reconnecting.")
            elif message == "disconnected":
                # Name the world, so a quit, a dropped link and Ctrl+D all read the same.
                label = self.name or "the MUD"
                spoken = f"Disconnected from {label}."
                echoed = f"* Disconnected from {label}"
        elif message.startswith("reconnected"):
            self.after_reconnect()
            if message == "reconnected":
                # Named like the disconnect above, and worded the same whether the backoff
                # loop or the window's Reconnect brought the session back.
                label = self.name or "the MUD"
                spoken = f"Reconnected to {label}."
                echoed = f"* Reconnected to {label}"
        self.voice.speak(spoken, channel=SYSTEM_CHANNEL, interrupt=True)
        self._post(protocol.echo(echoed))  # raw detail stays reviewable in the output
        # After the status is said, so a pack's own goodbye or welcome isn't cut off by it.
        if message.startswith(("disconnected", "protocol error", "reconnect failed",
                               "connection lost")):
            self._vip_connection_changed(False)
        elif message.startswith("reconnected"):
            self._vip_connection_changed(True)

    def _log(self, text: str) -> None:
        if self.logger is not None and self.logger.active and not self.logger.log(text):
            # The write failed and logging auto-stopped; say so once rather than let the
            # fault abort the line (which would silence and drop everything after it).
            self.voice.speak("Session logging stopped: write failed.", channel=SYSTEM_CHANNEL,
                             interrupt=True)
            self._post(protocol.echo("* Session logging stopped: write failed."))

    def _toggle_log(self) -> None:
        """Alt+Shift+L: start or stop logging by hand."""
        if self.logger is not None and self.logger.active:
            name = self.logger.path.name
            self.logger.stop()
            self.logger = None
            self._speak_system(f"logging stopped: {name}")
            return
        self.start_logging()

    def start_logging(self, *, announce: bool = True) -> None:
        """Log to the file the world's log_files setting names. Every log appends and begins
        with the time, so a day's or a world's file reads as one session after another."""
        if self.logger is not None and self.logger.active:
            return
        now = datetime.now()
        # Sanitize the world name -- a pack-derived world could be named "../.." and escape the
        # logs directory when joined onto the path.
        safe_name = world_component(self.name or "session")
        if self.log_files == "world":
            path = self.log_dir / f"{safe_name}.log"
        elif self.log_files == "day":
            path = self.log_dir / f"{safe_name}-{now:%Y-%m-%d}.log"
        else:
            path = self.log_dir / f"{safe_name}-{now:%Y%m%d-%H%M%S}.log"
        logger = SessionLogger(path)
        try:
            logger.start()
        except OSError as error:
            self._speak_system(f"Couldn't start logging: {error.strerror or error}")
            return
        self.logger = logger
        self._log(f"Session started {now:%Y-%m-%d %H:%M:%S}")
        if announce:
            self._speak_system(f"logging to {path.name}")
        else:
            self._post(protocol.echo(f"* Logging to {path.name}"))
