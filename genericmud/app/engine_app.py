"""Integration core: turns the telnet event stream and renderer messages into
engine processing, self-voice, and outbound render messages.

Decoupled from transport and UI via three injected callables — ``send`` (to the
MUD), ``post`` (to the renderer), ``schedule`` (timers) — plus a VoiceRouter, so
the whole glue layer is unit-testable without a socket, a webview, or NVDA.
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from genericmud.app.autologin import AutologinMixin
from genericmud.app.key_actions import KeyActionsMixin
from genericmud.app.links import LinksMixin
from genericmud.app.mud_data import MudDataMixin
from genericmud.app.session_log import SessionLogMixin
from genericmud.app.sink import AppSink
from genericmud.app.soundpacks import SoundpacksMixin
from genericmud.app.typed_commands import TypedCommandsMixin
from genericmud.app.walking import WalkingMixin
from genericmud.app.world_files import WorldFilesMixin
from genericmud.automation.channels import ChannelPolicy
from genericmud.automation.engine import AutomationEngine, PackWindow
from genericmud.bridge import protocol
from genericmud.completion import OutputWordIndex
from genericmud.config.worlds import (
    DEFAULT_CONNECT_METHOD,
    config_dir,
    normalize_connect_method,
    normalize_post_connect,
)
from genericmud.logpaths import world_log_dir
from genericmud.mapping import RoomMap
from genericmud.model.buffer import Buffer, Line, Link
from genericmud.navigation import Navigator, SafeWalk
from genericmud.packs import ActivationResult, PackStore
from genericmud.packs.pack_updates import PackUpdates
from genericmud.protocol import telnet as T
from genericmud.protocol.charset import AUTO, ServerTextCodec
from genericmud.protocol.msp import SoundCue, parse_msp_line
from genericmud.protocol.mxp import IMAGE_MARK, MxpParser, resolve_image_url
from genericmud.protocol.pueblo import PuebloParser
from genericmud.render.ansi import parse_ansi
from genericmud.review.channels import ChannelReview
from genericmud.review.cursor import ReviewCursor
from genericmud.safepath import is_unc, is_unsafe, within, world_component
from genericmud.scripting import user_scripts
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.vipmud_dialect import VipMudPack
from genericmud.session.credentials import CredentialStore
from genericmud.session.hub import SessionHub
from genericmud.session.log import SessionLogger
from genericmud.session.login import AutoLogin
from genericmud.sound.bus import (
    DEFAULT_CATEGORY,
    MUSIC_CATEGORY,
    PAUSED,
    CueTiming,
    SoundBackend,
    SoundBus,
)
from genericmud.sound.direction import facing_bearing, stereo_pan
from genericmud.sound.media import MediaLibrary, media_url
from genericmud.sound.renderer_backend import _PostSoundBackend
from genericmud.voice.router import SYSTEM_CHANNEL, VoiceRouter

if TYPE_CHECKING:
    from genericmud.config.worlds import World
    from genericmud.session.diaglog import DiagnosticLog

_PROMPT_IDLE_FLUSH_SECONDS = 0.25  # emit a newline-less prompt if the server sends no more data


# How much text an MXP <P> may hold before it is emitted anyway. A paragraph is meant to
# join a room description's worth of lines, not to buffer a session: past this the client
# says what it has and starts again, so a server that never closes <P> costs nobody their
# memory or their speech.
_MXP_MAX_JOIN_CHARS = 4000
_MXP_MAX_JOIN_LINES = 50
# ...and how long it may hold them. A server sending one line every 45 seconds would reach
# the line cap after half an hour of silence, so size alone does not bound latency.
_MXP_MAX_JOIN_SECONDS = 30.0
# Transport statuses that mean the socket went away or was replaced (transport/connection.py).
_SOCKET_CHANGES = (
    "disconnected",
    "protocol error",
    "reconnect failed",
    "connection lost",
    "connection unavailable",
    "reconnected",
)
_RECONNECT_NOTICE_SECONDS = 1.0  # confirm stability for speech only, never delay socket setup
_MEDIA_DEFAULT_VOLUME = 50  # Client.Media.Play's volume when the server leaves it out (spec)
_MEDIA_FADEAWAY_MS = 5000  # Client.Media.Stop's fadeaway when neither Play nor Stop says (spec)
_MEDIA_LONGEST_MS = 24 * 60 * 60 * 1000  # positions and fades past a day are a server's mistake
_MEDIA_TRACKED = 64  # Client.Media cues remembered for Stop before finished ones are dropped
_MEDIA_TURN_SECONDS = 0.25  # how long placed sounds take to swing round when the player turns


def _redact_secret(text: str, secret: str) -> str:
    """``text`` with ``secret`` taken out of it, as a whole whitespace-separated token.

    A server that echoes what it reads will send a blind login straight back -- MUSH/MOO's
    "connect hero hunter2", or a "Password: hunter2" for Diku -- and that line would be
    spoken and shown, and written to the session log. Matching whole tokens rather than any
    substring keeps a one-character password from redacting half the output, while still
    catching both shapes.
    """
    if not secret:
        return text
    pattern = re.compile(r"(?<!\S)" + re.escape(secret) + r"(?!\S)")
    return pattern.sub("***", text)


def _strip_image_marks(text: str) -> tuple[str, list[int]]:
    """``text`` without its inline-image markers, and where each marker sat once they're gone.

    Kept separate from the parser because the offset is only knowable here: everything between
    the parser and this point (MXP tags, MSP markers, ANSI codes, password redaction) rewrites
    the text, so a position counted any earlier would point at the wrong place.
    """
    if IMAGE_MARK not in text:
        return text, []
    kept: list[str] = []
    positions: list[int] = []
    for char in text:
        if char == IMAGE_MARK:
            positions.append(len(kept))
        else:
            kept.append(char)
    return "".join(kept), positions


def _link_positions(
    text: str, links: list[Link]
) -> list[tuple[int, int, str, bool, list[list[str]], str]]:
    """Where each link's words sit: (start, end, command, insert, menu, hint).

    The parser reports a link by its words, not by position, because everything between the
    parser and here (ANSI, MSP, redaction) rewrites the text. Searching forward from the
    previous link keeps a word that appears twice ("a fountain by a fountain") pointing at the
    right occurrence, in the order the MUD sent them. A link whose words are gone -- a pack
    substituted the line -- is dropped: a wrong position would make the wrong words clickable.
    """
    spans: list[tuple[int, int, str, bool, list[list[str]], str]] = []
    cursor = 0
    for link in links:
        if not link.text:
            continue
        start = text.find(link.text, cursor)
        if start < 0:
            continue
        cursor = start + len(link.text)
        spans.append(
            (
                start,
                cursor,
                link.command,
                link.insert,
                [list(item) for item in link.choices],
                link.hint,
            )
        )
    return spans


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


class EngineApp(
    MudDataMixin,
    TypedCommandsMixin,
    KeyActionsMixin,
    LinksMixin,
    WalkingMixin,
    AutologinMixin,
    SoundpacksMixin,
    WorldFilesMixin,
    SessionLogMixin,
):
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
        connect_method: str = DEFAULT_CONNECT_METHOD,
        post_connect_commands: str = "",
        mxp: bool = True,
        pueblo: bool = False,
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
        # Kept here as well as on the pack host: MXP's replies (<VERSION>, <SUPPORTS>) are
        # raw bytes rather than a line, and this is the only place that needs to send one.
        self._raw_send = send_raw
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
        # The way the player faces, in degrees from north, for sounds the MUD places by compass
        # direction (Client.Media.Listener). North until the MUD says otherwise.
        self._media_facing = 0.0
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
        # Echo what you type into the output (MUSHclient's "echo input"). Off unless the world
        # asks for it, and separate from logging it: either one can be wanted without the other.
        self.echo_input = False
        self.download_media = True  # fetch the sounds a MUD offers (the world's choice)
        self.credentials = credentials
        self._login: AutoLogin | None = None
        # How this world logs in, and what it types once the connection is up (both come from
        # the world file; the username and password themselves come from the credential store).
        self.connect_method = normalize_connect_method(connect_method)
        self.post_connect_commands = normalize_post_connect(post_connect_commands)
        self._post_connect_sent = False  # per connection: a reconnect types them again
        # Login lines to type the moment the socket is up (MUSH/MOO and Diku). Queued rather
        # than sent because on_connect runs before the transport has connected.
        self._pending_login: list[str] = []
        # The password to strip out of anything incoming, while it is in play (see _handle_line).
        self._password_guard = ""
        # MXP: on unless this world turns it off. The parser strips every tag, so what
        # reaches the output is plain text whatever the server sends.
        self.mxp_enabled = mxp
        # Where an <IMAGE> without a URL argument looks for its file. Most MUDs expect the
        # client to have this configured, the same way MSP's U= expects a sounds location.
        self.images_url = ""
        # Pueblo's HTML is read here too. Off unless the world asked for it: switching it on is
        # what makes the client announce itself, and a world that does not speak Pueblo reads
        # that announcement as a character name at its login screen.
        self.pueblo_enabled = pueblo
        self._pueblo = PuebloParser()
        self._pueblo_links_noted = False  # the first offered link is logged, once
        self._pueblo_image_noted = False  # and so is the first picture or sound
        self._mxp = MxpParser()
        self._mxp_join = ""  # text held across lines by <NOBR> or an open <P>
        # Whether this connection's server has offered MXP (IAC WILL MXP). The world's setting
        # only says MXP is welcome; until a server offers it, "<p>" in its text is somebody's
        # words, and on a MUD that never speaks MXP a player could otherwise hold everyone's
        # output with one.
        self._mxp_negotiated = False
        self._mxp_join_links = []  # links gathered in that held text, emitted with it
        # The encrypted port this MUD advertised over MSSP, if it did and this link isn't on
        # it. `None` means no offer stands, which is the answer /secure acts on.
        self._secure_port: int | None = None
        self._world_port = 0
        self._world_tls = False
        # A rename closed this session's log; the next connection opens the world's new one.
        self._reopen_log_after_rename = False
        # The link the review cursor is standing in, so its caption is said once on arrival
        # rather than on every word inside it.
        self._link_announced: Link | None = None
        self._mxp_join_images = []  # ditto for images, whose markers ride in the text itself
        self._mxp_held_lines = 0  # how many lines that hold has swallowed so far
        # One deadline, not one per <P>: paragraphs do not nest and the stream is sequential,
        # so at most one hold is ever open. The token stops a stale timer from flushing a
        # paragraph that has since ended and been replaced.
        self._mxp_timer_armed = False
        self._mxp_timer_token = 0
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
        self._recovery_status: str | None = None  # one announcement per ongoing outage/interval
        self._reconnect_notice_token = 0  # invalidate a confirmation when that socket drops
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
        # Which of USER/PASSWORD this connection has answered over MXP, and whether an MXP
        # login request may be answered at all: only once a connection starts logging in.
        self._mxp_login_sent: set[str] = set()
        self._mxp_login_armed = False
        self._hidden_answers = 0  # lines typed since the MUD hid input; the second gets a hint
        self._hidden_timeout_token = 0  # bumped to cancel a pending hidden-input timeout
        self._ticks_armed = False  # the OnPluginTick chain is scheduled at most once
        self._closed = False  # ends the tick chain at shutdown
        # The world's text encoding, both directions: the UI hands this same codec to the
        # connection so typed commands go out in the encoding the output arrives in.
        self.encoding = encoding
        self.codec = ServerTextCodec(encoding, on_latch=self._on_encoding_latch)
        # Names of the saved worlds, so an older ASCII-only folder is only adopted when it
        # isn't another world's own folder (see _world_path). None: no saved worlds known.
        self._other_worlds = other_worlds
        self._world_paths: dict[tuple[Path, str], Path] = {}  # resolved once per session
        self._msdp_routed = 0  # subnegotiation count, for throttling the diag trace
        self._prompt_gen = 0  # bumps per data chunk so a stale idle prompt-flush no-ops

    def apply_world_settings(self, world: World) -> None:
        """Take a world's own command stacking and logging settings. Called before the world
        connects and again when it's edited while open, so the change is felt at once."""
        self.command_separator = world.command_separator if world.command_stacking else ""
        self.log_automatically = world.log_automatically
        self.log_files = world.log_files
        self.log_commands = world.log_commands
        self.echo_input = world.echo_input
        # How this session reached the world: an offer of an encrypted port has to know whether
        # this link is already encrypted, and whether the offered port is even a change.
        self._world_port = world.port
        self._world_tls = world.tls
        # How this world logs in, and what it types once connected. A reconnect from this tab
        # reads both again, so an edit has to reach the running session and not just the file.
        self.connect_method = world.connect_method
        self.post_connect_commands = world.post_connect_commands
        self.download_media = world.download_sounds
        # MXP is parsed here, not by the window, so switching it off for a world that is already
        # open can take effect at once: no reconnect needed to stop seeing its markup.
        self.mxp_enabled = world.mxp
        self.pueblo_enabled = world.pueblo
        self.images_url = world.images_url

    def on_connect(self, world: str, *, socket_open: bool = True) -> ActivationResult | None:
        """The on-connect sequence: register with the hub, activate packs, arm login.

        Packs hear that the MUD is connected once the connection is open: now when it
        already is, or when on_socket_open() says so. The window readies packs before it
        opens the connection, and a connection that fails never says so.
        """
        if not socket_open:
            self.engine.connected = False  # packs' IsConnected(), until it opens
        self._secure_port = None  # an offer belongs to the connection that made it
        # A new connection starts MXP from nothing: the server has to offer it again, and no
        # mode, paragraph or held text carries over from the last one.
        self._mxp_negotiated = False
        self._mxp = MxpParser()
        self._release_mxp_hold()
        # And Pueblo from nothing: a new socket has to switch its output to HTML again.
        self._pueblo = PuebloParser()
        self._pueblo_links_noted = False
        self._pueblo_image_noted = False
        self._media_facing = 0.0  # facing north again until this connection's MUD says otherwise
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

    def on_socket_open(self) -> None:
        """The connection to the MUD is open: packs hear it, as MUSHclient's
        OnPluginConnect and the world's connect handler, and VIP Mud's OnConnect, once.
        Anything a pack sends then reaches the MUD."""
        self.engine.connected = True
        self._vip_connection_changed(True)

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
            and event.option == T.OPT_MXP
            and event.command in (T.WILL, T.WONT)
        ):
            self._mxp_negotiated = event.command == T.WILL
            if not self._mxp_negotiated:
                self._mxp = MxpParser()
                self._release_mxp_hold()
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

    def _mxp_feed(
        self, text: str
    ) -> tuple[str, list[SoundCue], list[tuple[str, str]], list[tuple[str, str]]]:
        """Parse one line as MXP, returning plain text, its sound cues and its links.

        ``<NOBR>`` and an open ``<P>`` are the only MXP rules that span lines: both mean the
        next line belongs on this one, so the text is held rather than emitted. Everything
        else is decided within the line. Links are held with the text they came from, or a
        link tagged on a swallowed line would point at a paragraph that doesn't contain it.

        The hold is bounded. A ``<P>`` that is never closed would otherwise keep growing and
        keep the user in silence for as long as it lasted, so once enough has been gathered
        the paragraph is emitted in one piece and the stream carries on. Nothing is dropped:
        the join simply restarts, and the parser's own paragraph state is untouched.
        """
        if not self.mxp_enabled or not self._mxp_negotiated:
            return text, [], [], []
        # Links and images seen while a paragraph was being gathered are carried with the text
        # they came from; an image left a marker in that text, which survives the join.
        links = self._mxp_join_links
        self._mxp_join_links = []
        images = self._mxp_join_images
        self._mxp_join_images = []
        held = self._mxp_join
        self._mxp_join = ""
        # Each line from the MUD is parsed once, on its own, and the parsed text is what gets
        # joined. Feeding the held text back in with the next line decoded it twice (an
        # escaped "&lt;password&gt;" came back as a live tag) and hid the next line's own mode
        # escape behind it.
        outcome = self._mxp.feed(text)
        for payload in outcome.replies:
            self._mxp_reply(payload)
        for request in outcome.login:
            self._mxp_login(request)
        links.extend(outcome.links)
        images.extend(outcome.images)
        joined = held + outcome.text
        if not outcome.join_next:
            self._release_mxp_hold()
            return joined, outcome.cues, links, images
        self._mxp_held_lines += 1
        if self._mxp_held_lines >= _MXP_MAX_JOIN_LINES or len(joined) >= _MXP_MAX_JOIN_CHARS:
            self._release_mxp_hold()
            if self._diag is not None:
                self._diag.event("mxp.join_flushed", reason="size")
            return joined, outcome.cues, links, images
        self._mxp_join = joined
        self._mxp_join_links = links  # for a deadline flush, which has no text to re-parse
        self._mxp_join_images = images
        self._arm_mxp_deadline()
        return "", outcome.cues, [], []

    def _release_mxp_hold(self) -> None:
        """Forget a hold, and any deadline waiting on it."""
        self._mxp_join = ""
        self._mxp_join_links = []
        self._mxp_join_images = []
        self._mxp_held_lines = 0
        self._mxp_timer_armed = False
        self._mxp_timer_token += 1  # a timer already scheduled must now do nothing

    def _arm_mxp_deadline(self) -> None:
        """Start the clock on a hold, once, however many lines it swallows."""
        if self._mxp_timer_armed:
            return
        self._mxp_timer_armed = True
        self._mxp_timer_token += 1
        token = self._mxp_timer_token
        self._schedule(
            _MXP_MAX_JOIN_SECONDS, lambda: self._mxp_deadline_reached(token)
        )

    def _mxp_deadline_reached(self, token: int) -> None:
        """Say what a long-held paragraph has so far, rather than leave the user in silence."""
        if token != self._mxp_timer_token or not self._mxp_join or self._closed:
            return
        held = self._mxp_join
        held_links = self._mxp_join_links
        held_images = self._mxp_join_images
        self._release_mxp_hold()
        if self._diag is not None:
            self._diag.event("mxp.join_flushed", reason="timeout")
        self._emit_parsed_line(held, links=held_links, images=held_images)

    def _mxp_reply(self, payload: str) -> None:
        """Answer ``<VERSION>``/``<SUPPORT>`` on a secure line, as the spec requires.

        The secure line (``ESC[1z``) is how the MUD tells a client reply from something a
        player typed, so it is not optional.
        """
        if self._diag is not None:
            self._diag.event("mxp.reply", payload=payload)
        if self._raw_send is None:
            return  # headless: nothing to answer on, and the diag event records it
        try:
            self._raw_send(MxpParser.secure_line(payload).encode("utf-8"))
        except (OSError, ConnectionError):
            pass  # a dead socket must not break the line that provoked the question

    def _emit_line(self, text: str) -> None:
        # Strip markup before anything reads it; links and images ride along with the text.
        if self.pueblo_enabled:
            # Pueblo's HTML replaces MXP rather than nesting inside it: a world that switches
            # to HTML is not speaking MXP, and one line of it can be several lines of text.
            was_active = self._pueblo.active
            for piece in self._pueblo.feed(text):
                self._note_pueblo_links(piece.links)
                self._emit_parsed_line(piece.text, links=list(piece.links))
            if was_active != self._pueblo.active and self._diag is not None:
                self._diag.event("pueblo.mode", html=self._pueblo.active)
            self._note_pueblo_image()
            return
        text, mxp_cues, links, images = self._mxp_feed(text)
        self._emit_parsed_line(text, mxp_cues, links, images)

    def _emit_parsed_line(
        self,
        text: str,
        mxp_cues: list[SoundCue] | None = None,
        links: list[tuple[str, str]] | None = None,
        images: list[tuple[str, str]] | None = None,
    ) -> None:
        """Show and speak a line that has already been through the MXP parser.

        Kept separate because the deadline flushes a held paragraph: sending that text back
        through :meth:`_emit_line` would feed it to the parser again, find the paragraph still
        open, and hold it a second time -- the flush would emit nothing at all.
        """
        text, cues = parse_msp_line(text)  # strip MSP markers before colour parsing
        for cue in [*(mxp_cues or []), *cues]:
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
        if self._password_guard:
            # A blind login is typed before the server has switched echo off, so a server
            # that echoes what it reads sends the password back. It must not reach the
            # output box, the screen reader, the session log or a soundpack.
            redacted = _redact_secret(plain, self._password_guard)
            if redacted != plain:
                plain, spans = redacted, []  # the colours belonged to the text that's gone
        # Inline images: the parser left a marker wherever one sits, and only now is its offset
        # knowable. An image whose address can't be worked out simply isn't there.
        plain, marks = _strip_image_marks(plain)
        if marks:
            spans = [replace(span, text=span.text.replace(IMAGE_MARK, "")) for span in spans]
            spans = [span for span in spans if span.text]
        shown: list[tuple[int, str]] = []
        for (file, base), offset in zip(images or [], marks, strict=False):
            url = resolve_image_url(
                file, base, self.images_url, self.media.trusted_address()
            )
            if url:
                shown.append((offset, url))
                if self._diag is not None:
                    self._diag.event("mxp.image", file=file, url=url)
        if not plain.strip() and not shown:
            return  # blank line: any sound cues already fired; don't show/speak "blank"
        for pack in self._mush_packs:
            pack.dispatch_line(plain)
        line_links = list(links or [])
        # A text client has no way to show a picture, so an image also becomes a link: the
        # same thing MUSHclient does with one (it turns <IMAGE> into a hyperlink), and the
        # only form in which a picture can be reached without seeing it.
        for _offset, url in shown:
            line_links.append(Link(url.rsplit("/", 1)[-1], url))
        line = Line(plain, spans=spans, links=line_links)
        self.engine.process_line(line)  # may set line.channel and gag flags
        self._log(line.plain_text)  # full session log, including gagged-from-speech
        if self._login is not None and not self._login.done:
            self._login.feed(line.plain_text)  # answer name/password prompts
            if self._login.done:
                # The password is away (or the wait gave up): what the world asked to type
                # once connected goes now.
                self._send_post_connect()
        if self._walk is not None and self._walk.active:
            self._walk.on_line(line.plain_text)  # halt the walk if a step was blocked
        if line.substitute is not None:
            # A pack replaced the line (VIPMud #Sub). The log, login and the walker keep what
            # the MUD sent; the player sees and hears the replacement.
            line.plain_text, line.spans = line.substitute, []
            if not line.plain_text.strip():
                return
        policy = self.channels.policy(line.channel)
        if policy.speak and not line.gagged and self.engine.line_speech and line.plain_text.strip():
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
                    # Positions are only meaningful for the words the MUD sent; a substituted
                    # line shows a pack's own text, so it gets none.
                    links=(
                        _link_positions(line.plain_text, line.links)
                        if line.substitute is None
                        else []
                    ),
                    images=shown if line.substitute is None else [],
                )
            )

    def _note_pueblo_links(self, links: tuple[Link, ...]) -> None:
        """Note the first command link a world offers, once per connection.

        "My exits are not followable" has two very different answers -- the world writes them
        as plain words, or it writes them as links this client dropped -- and one line in the
        diagnostics tells them apart without a session of guessing.
        """
        if not links or self._pueblo_links_noted or self._diag is None:
            return
        self._pueblo_links_noted = True
        self._diag.event("pueblo.links", first=links[0].command, count=len(links))

    def _note_pueblo_image(self) -> None:
        """Note the first picture or sound a world sends, once per connection.

        Nothing here shows or plays one yet, and that is the point of noting it: "my MUD's
        pictures are missing" and "my MUD sends no pictures" are different reports, and a reader
        who hears neither cannot tell them apart. One line answers it.
        """
        source = self._pueblo.first_image
        if not source or self._pueblo_image_noted or self._diag is None:
            return
        self._pueblo_image_noted = True
        self._diag.event("pueblo.image", src=source)

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
        """GMCP ``Client.Media.*`` (Mudlet's media spec): Default, Load, Play, Stop, Pause,
        Update and Listener. Bare Client.Media is Default's obsolete name."""
        verb = package.rsplit(".", 1)[-1].lower()
        body = data if isinstance(data, dict) else {}
        if verb in ("default", "media"):
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
        elif verb == "pause":
            self._media_pause(body)
        elif verb == "update":
            self._media_update(body)
        elif verb == "listener":
            self._media_listener(body)

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
        if self._media_resume(name, kind, key, body):
            return
        priority = _media_int(body.get("priority"), 0, 0, 100)
        if not self._media_has_priority(name, kind, priority):
            if self._diag is not None:
                self._diag.event("media.play", file=name, refused="priority", priority=priority)
            return
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
        # Where the sound comes from (Mudlet 5.1): a pan given outright, or a direction turned
        # by the way the player faces. Music plays in the middle, as Mudlet plays it.
        pan = stereo_pan(body.get("pan"), body.get("direction"), self._media_facing)
        placed_by_direction = kind == "sound" and pan is not None and body.get("pan") is None
        self._media_cues[channel] = {
            "name": name,
            "type": kind,
            "key": key,
            "tag": str(body.get("tag") or ""),
            "priority": priority,
            "fadeout": timing.fade_out if timing is not None else 0.0,  # a fadeaway's length
            # A sound placed by direction turns when the player does; one given a pan stays.
            "direction": body.get("direction") if placed_by_direction else None,
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
                    path, channel, gain=gain, pan=pan or 0.0, loop=loop, times=times,
                    timing=timing,
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

    def _media_matching(self, body: dict):
        """The tracked cues matching every name, type, key and tag the body gives, as
        (channel, cue). A key or tag has to be the cue's own, so a sound with none of its own
        never matches one that names a key: Update needs that as much as Stop does."""
        wanted = {
            field: str(body[field])
            for field in ("name", "type", "key", "tag")
            if body.get(field) not in (None, "")
        }
        for channel, cue in list(self._media_cues.items()):
            if any(str(cue[field]) != value for field, value in wanted.items()):
                continue
            yield channel, cue

    def _media_has_priority(self, name: str, kind: str, priority: int) -> bool:
        """Mudlet's rule for a Play that gives a priority (1 to 100; 0 is none set, and always
        plays): a different sound of the same type, playing with an equal or higher priority,
        keeps this one from starting, and a lower one is stopped to make room."""
        if priority <= 0:
            return True
        rivals = [
            (channel, cue) for channel, cue in self._media_cues.items()
            if cue["type"] == kind and cue["name"] != name and int(cue["priority"]) > 0
            and self.sound.is_playing(channel)
        ]
        if any(int(cue["priority"]) >= priority for _channel, cue in rivals):
            return False
        for channel, _cue in rivals:
            self._stop_media(channel)
        return True

    def _media_resume(self, name: str, kind: str, key: str, body: dict) -> bool:
        """A Play for a sound or track that a Pause left paused carries on where it stopped,
        unless the Play says continue false. True when one was resumed."""
        if not _media_continues(body.get("continue")):
            return False
        for channel, cue in self._media_cues.items():
            same = cue["type"] == kind and cue["name"] == name and (not key or cue["key"] == key)
            if same and self.sound.state(channel) == PAUSED:
                self.sound.resume(channel)
                if self._diag is not None:
                    self._diag.event("media.resume", file=name, channel=channel)
                return True
        return False

    def _media_pause(self, body: dict) -> None:
        """Client.Media.Pause: pause the cues matching every field given, all of them when it
        gives none, as Stop matches. ``priority`` pauses those at or below it."""
        ceiling = _media_int(body.get("priority"), None, 0, 100)
        for channel, cue in self._media_matching(body):
            if ceiling is not None and int(cue["priority"]) > ceiling:  # type: ignore[call-overload]
                continue
            if self.sound.is_playing(channel):
                self.sound.pause(channel)

    def _media_update(self, body: dict) -> None:
        """Client.Media.Update: change a playing cue's volume, or where it comes from, gliding
        over ``duration`` milliseconds. It has to name the cue by name, key or tag: an Update
        naming none would move every sound the MUD is playing, so it's refused, as Mudlet does."""
        if not any(body.get(field) not in (None, "") for field in ("name", "key", "tag")):
            if self._diag is not None:
                self._diag.event("media.update", refused="names no sound")
            return
        volume = _media_int(body.get("volume"), None, 0, 100)
        placing = body.get("pan") is not None or body.get("direction") is not None
        seconds = _media_int(body.get("duration"), 0, 0, _MEDIA_LONGEST_MS) / 1000
        for channel, cue in self._media_matching(body):
            gain = volume / 100 if volume is not None else None
            pan = None
            if placing and cue["type"] == "sound":
                pan = stereo_pan(body.get("pan"), body.get("direction"), self._media_facing)
                # A new direction takes over from an earlier pan, and a pan from a direction.
                cue["direction"] = body.get("direction") if body.get("pan") is None else None
                pan = pan or 0.0
            if gain is None and pan is None:
                continue
            if seconds > 0:
                self.sound.slide(channel, gain=gain, pan=pan, seconds=seconds)
            else:
                self.sound.adjust(channel, gain=gain, pan=pan)

    def _media_listener(self, body: dict) -> None:
        """Client.Media.Listener: the way the player now faces. Every sound placed by compass
        direction turns with them, gliding over a quarter of a second."""
        facing = facing_bearing(body.get("facing"))
        if facing is None:
            if self._diag is not None:
                self._diag.event("media.listener", refused=str(body.get("facing")))
            return
        self._media_facing = facing
        for channel, cue in self._media_cues.items():
            if cue["type"] != "sound" or cue.get("direction") is None:
                continue
            if self.sound.is_playing(channel):
                pan = stereo_pan(None, cue["direction"], facing)
                self.sound.slide(channel, pan=pan or 0.0, seconds=_MEDIA_TURN_SECONDS)

    def _forget_finished_media(self) -> None:
        """Drop finished one-shots so a long session's keyless cues don't pile up forever."""
        if len(self._media_cues) < _MEDIA_TRACKED:
            return
        for channel in list(self._media_cues):
            if channel.startswith("media-") and not self.sound.is_playing(channel):
                self._media_cues.pop(channel, None)
                self._media_gen.pop(channel, None)

    def _speak_system(self, text: str, *, interrupt: bool = True) -> None:
        self.voice.speak(text, channel=SYSTEM_CHANNEL, interrupt=interrupt)
        self._post(protocol.echo(f"* {text}"))

    def set_encoding(self, encoding: str) -> None:
        """Take a new encoding for this session, for a world whose details changed.

        Called while the session is disconnected, so no half-decoded bytes are in flight. The
        codec is shared with the connection, so the window re-points that too.
        """
        if encoding == self.encoding:
            return
        self.encoding = encoding
        self.codec = ServerTextCodec(encoding, on_latch=self._on_encoding_latch)

    def rename_world(self, name: str) -> None:
        """Take a new name for this session's world: its log file, its /to address, its words.

        Called only while the session is disconnected. The old log is closed rather than left
        appending under a name the world no longer has, and the next connection opens the new
        one -- which is where that file's fresh "Session started" line comes from.
        """
        if name == self.name:
            return
        if self.logger is not None:
            # Closed for the rename, not because the player asked: the next connection opens
            # the world's new log. A log stopped by hand (or by a failed write) is already
            # inactive, and stays stopped, as it always did.
            self._reopen_log_after_rename = self.logger.active
            self.logger.stop()
            self.logger = None
        if self.hub is not None and self.name:
            self.hub.unregister(self.name, self._dispatch_remote)
        self.name = name
        if self.hub is not None:
            self.hub.register(self.name, self._dispatch_remote)
        # The window has moved the world's map, rules and saved variables to the new name.
        # Paths this session resolved under the old one would write them back there, so any
        # whose file has gone are looked up again; a file that stayed (the new name already
        # had its own) keeps being this session's.
        for key, path in list(self._world_paths.items()):
            if not path.exists():
                del self._world_paths[key]

    def after_reconnect(self) -> None:
        """Reattach this session to a socket that is live again.

        Shared by the transport's own backoff loop and a manual reconnect from the UI, so
        the two cannot drift. The state it resets is all per-socket -- a socket that is up
        while the engine still believes it is disconnected leaves packs paused and
        auto-login stranded at the login screen.
        """
        self.engine.connected = True
        # An offer belongs to the connection that made it: a reconnect asks the MUD again,
        # because a game can move its port and a stale offer would point at nothing.
        self._secure_port = None
        # Logging survives a reconnect because its logger is still open. Only a rename closes
        # it, so only a rename reopens it -- under the name the world has now.
        if self._reopen_log_after_rename:
            self.start_logging(announce=False)
        self._reopen_log_after_rename = False
        # A fresh socket is a fresh byte stream: drop any half-decoded multibyte tail and
        # the legacy-encoding latch, or a drop mid-character mis-decodes the whole reconnect.
        self.codec.reset()
        # MXP starts from nothing too, as on_connect does: the server has to offer it again, and
        # no variable, mode, paragraph or held text carries over from the old socket. Safe to do
        # here because both reconnect paths call this straight after connect(), before the new
        # socket's read loop has run, so the server's fresh offer can't have arrived yet.
        self._mxp_negotiated = False
        self._mxp = MxpParser()
        self._release_mxp_hold()
        self._pueblo = PuebloParser()  # and Pueblo waits to be told again
        self._pueblo_links_noted = False
        self._pueblo_image_noted = False
        self._media_facing = 0.0  # and the player faces north until the MUD says otherwise
        # Echo state belongs to the dead socket: no WONT ECHO is coming for it. Through
        # forget_hidden_input, as on_connection_status does, so the window's history and
        # the hidden-input timeout hear about it too.
        self.forget_hidden_input()
        # A fresh socket means fresh login prompts; the old AutoLogin is marked done, so
        # re-arm it or a reconnect strands the user at the login screen.
        self.begin_login(self.name)
        # The socket is live here (both reconnect paths call this only after connect()), so
        # this is where a blind login and the after-connect commands actually go out.
        self.after_connect()

    def on_connection_status(self, message: str, *, manual: bool = False) -> None:
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
        quiet = False
        if manual or message.startswith(("disconnected", "protocol error", "reconnect failed")):
            self._recovery_status = None
            self._reconnect_notice_token += 1
        if message.startswith(_SOCKET_CHANGES):
            # Echo state belongs to the socket that just went away. Left armed, every
            # command after it would go out as a password: no client commands, aliases or
            # speedwalks, and "***" in the log, because no WONT ECHO is ever coming.
            self.forget_hidden_input()
        if message.startswith(("connection lost", "connection unavailable")):
            # Keep one outage notice across sockets that repeatedly open and immediately
            # close. A changed interval is still worth reporting, even during recovery.
            # The reason a connect failed rides after the colon ("connection unavailable:
            # connection refused; ..."), and a new one is worth saying. Whether the link was
            # lost or never came up is not news on its own.
            head, _, tail = message.partition(";")
            status = f"{head.partition(': ')[2]};{tail}"
            quiet = status == self._recovery_status
            self._recovery_status = status
            self._reconnect_notice_token += 1
            # IsConnected() and plugin ticks must see a dropped link as down even during
            # backoff. after_reconnect() restores it; transient loss keeps timers and packs.
            self.engine.connected = False
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
        if message.startswith("reconnected") and self._recovery_status is not None:
            quiet = True
            self._reconnect_notice_token += 1
            token = self._reconnect_notice_token

            def announce_recovery() -> None:
                if (token != self._reconnect_notice_token or self._closed
                        or not self.engine.connected or self._recovery_status is None):
                    return
                self._recovery_status = None
                # Queue the confirmation, without cutting off a MUD/pack's welcome.
                self.voice.speak(spoken, channel=SYSTEM_CHANNEL, interrupt=False)
                self._post(protocol.echo(echoed))

            self._schedule(_RECONNECT_NOTICE_SECONDS, announce_recovery)
        if not quiet:
            self.voice.speak(spoken, channel=SYSTEM_CHANNEL, interrupt=True)
            self._post(protocol.echo(echoed))  # raw detail stays reviewable in the output
        # Lifecycle hooks run now even if the user-facing confirmation is deferred.
        if message.startswith(("disconnected", "protocol error", "reconnect failed",
                               "connection lost")):
            self._vip_connection_changed(False)
        elif message.startswith("reconnected"):
            self._vip_connection_changed(True)
