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
from genericmud.app.session_log import SessionLogMixin
from genericmud.app.sink import AppSink
from genericmud.app.soundpacks import SoundpacksMixin
from genericmud.app.typed_commands import CLIENT_PREFIX, TypedCommandsMixin
from genericmud.app.world_files import WorldFilesMixin
from genericmud.automation.channels import ChannelPolicy
from genericmud.automation.engine import AutomationEngine, PackWindow
from genericmud.automation.variables import VariableEntry, list_variables
from genericmud.bridge import protocol
from genericmud.completion import OutputWordIndex
from genericmud.config.worlds import (
    DEFAULT_CONNECT_METHOD,
    config_dir,
    normalize_connect_method,
    normalize_post_connect,
)
from genericmud.logpaths import world_log_dir
from genericmud.mapping import RoomMap, spoken_count
from genericmud.model.buffer import Buffer, Line, Link
from genericmud.navigation import Navigator, SafeWalk, expand_speedwalk
from genericmud.packs import ActivationResult, PackStore
from genericmud.packs.pack_updates import PackUpdates
from genericmud.protocol import msdp
from genericmud.protocol import telnet as T
from genericmud.protocol.charset import AUTO, ServerTextCodec
from genericmud.protocol.msp import SoundCue, parse_msp_line
from genericmud.protocol.mxp import IMAGE_MARK, MxpParser, resolve_image_url
from genericmud.protocol.oob import OobMessage, ServerStatus, from_subnegotiation
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
from genericmud.voice.router import REVIEW_CHANNEL, SYSTEM_CHANNEL, VoiceRouter

if TYPE_CHECKING:
    from genericmud.config.worlds import World
    from genericmud.session.diaglog import DiagnosticLog

SPEEDWALK_PREFIX = "."  # ".3n2e" expands to n,n,n,e,e (leading char disambiguates)
SAFE_PREFIX = ".."  # "..3n2e" walks the same route one step at a time, halting if blocked


# New rooms learned before the map is written again. Exploring is exactly when a session
# is long and a crash is most costly, and a write every twenty-five rooms is nothing.
_MAP_SAVE_INTERVAL = 25


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
# MSDP variables that mean the player's location changed; both the table and flat forms.
_MSDP_ROOM_VARS = frozenset(
    {"ROOM", "ROOM_NAME", "ROOM_AREA", "ROOM_EXITS", "ROOM_VNUM", "AREA_NAME"}
)


# An <A HREF> link is opened, never typed at the MUD: only these schemes are followed, so a
# MUD can't send the platform off to launch a local application through the shell.
_LINK_URL_SCHEMES = ("http://", "https://")
# Ports a game may send a player to over MSSP: the ordinary range, plus HTTPS, which games do
# use for TLS. Anything else privileged is refused -- taking a status reply's word for it would
# write the number into the world file and dial it.
_ALLOWED_LOW_PORTS = frozenset({443})
# The longest text a link may type into the command box. Starting a command is what the
# spec has in mind ("tell Ariadne "); nobody needs a paragraph begun for them.
_MAX_TYPED_LINK = 200

_REVIEW_VERBS = frozenset(
    {
        "prev_line", "next_line", "prev_word", "next_word", "prev_char", "next_char",
        "top", "bottom", "spell_line",
    }
)
# Alt+PageDown / Alt+PageUp: jump from MXP link to MXP link through the output.
_LINK_JUMPS = frozenset({"next_link", "prev_link"})
_CHANNEL_REVIEW_VERBS = frozenset(
    {"prev", "next", "older", "newer", "prev_word", "next_word"}
)


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


def _menu_note(link: Link) -> str:
    """What to say after a link's words when following it asks rather than acts."""
    return f" (menu of {len(link.choices)})" if link.choices else ""


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


class EngineApp(
    TypedCommandsMixin,
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

    def variable_listing(self) -> tuple[list[VariableEntry], bool]:
        """Every value this session knows, for the MUD Variables dialog and the picker.

        Reads live engine state, so call it on the loop thread; the rows are plain frozen
        values, safe to hand to the UI thread afterwards.
        """
        return list_variables(
            self.engine.all_mud_vars(), self.engine.all_vars(),
            arrivals=self.engine.data_arrival_times(), now=time.monotonic(),
        )

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
                    # Once per message, with both names stored, so a rule that fires on it
                    # can already read the new value through ${mud:...}.
                    self.engine.data_arrived(message.source, message.name, message.value)
                    if message.source == "gmcp":
                        for pack in self._mush_packs:
                            pack.dispatch_gmcp(message.name, message.value)
                        if message.name.lower() == "core.goodbye":
                            self._server_goodbye(message.value)
                        elif message.name.lower() == "room.wrongdir":
                            self._wrong_direction(message.value)
                        elif message.name.lower().split(".")[:2] == ["client", "media"]:
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
                self.engine.data_arrived("mssp", name, value)
            self._note_secure_port(result.data)
            self._post(protocol.status(self._gauges))
        if sub.option == T.OPT_MSDP and self._mush_packs:
            self._route_msdp(sub.payload)

    def _note_secure_port(self, data: dict[str, str]) -> None:
        """Offer the encrypted port the MUD advertises, when this link isn't already on it.

        MSSP is where a game puts the port it never prints in its banner -- both MUDs checked
        by hand here hid it there, one behind an in-game FAQ and one behind nothing at all. It
        is the one thing a player cannot guess, so it is said once, in passing, with the
        command that takes it. Nothing is changed on the strength of it: moving a world to a
        different port is the player's decision.
        """
        if self._secure_port is not None or self._world_tls:
            return
        advertised = ""
        for name, value in data.items():
            if name.strip().upper() in ("SSL", "TLS"):
                advertised = str(value).strip()
                break
        # isdecimal, not isdigit: "²" is a digit that int() refuses, and so is a number longer
        # than Python will convert. Either would stop this MSSP reply half handled.
        port = int(advertised) if advertised.isdecimal() and len(advertised) <= 5 else 0
        plausible = port in _ALLOWED_LOW_PORTS or 1024 <= port <= 65535
        if not plausible or port == self._world_port:
            if advertised and self._diag is not None:
                self._diag.event("mssp.secure_ignored", value=advertised)
            return
        self._secure_port = port
        if self._diag is not None:
            self._diag.event("mssp.secure_offered", port=port, mssp=sorted(data)[:6])
        self._speak_system(
            f"this game offers an encrypted connection on port {port}; /secure switches this "
            "world to it",
            interrupt=False,
        )

    def _use_secure_port(self) -> None:
        """Take up the encrypted port the MUD offered: the window saves the world and reconnects.

        The window owns the world file and the connection, so the engine asks rather than
        acting; where there is no window to ask (the web UI), it says what to change by hand.
        """
        port = self._secure_port
        if port is None:
            self._speak_system("this game has not offered an encrypted port")
            return
        if self._world_tls and self._world_port == port:
            # Already taken: the offer stood before this world was moved onto that port.
            self._speak_system(f"this world is already using its encrypted port, {port}")
            return
        hook = getattr(self.pack_window, "use_secure_port", None)
        if hook is not None and hook(port):
            if self._diag is not None:
                self._diag.event("mssp.secure_taken", port=port)
            self._speak_system(f"switching this world to port {port}, encrypted")
            return
        if self._diag is not None:
            self._diag.event("mssp.secure_unavailable", port=port)
        self._speak_system(
            f"set this world's port to {port} and tick Use TLS in its details"
        )

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

    def _handle_find(self, message: dict) -> None:
        """Search the scrollback and park the review cursor on the hit.

        The engine owns the search because the buffer is the deep copy: it holds far more
        history than the renderer's control does. The matched text goes back as a
        FIND_RESULT so the renderer can put its own caret on the same line where it can.
        """
        term = message.get("term", "")
        forward = bool(message.get("forward", False))
        restart = bool(message.get("restart", False)) or not self.review.active
        if self._anchor_review(message):
            # The reader's own line, in either direction: a search that ignored it both
            # skipped matches between the caret and the last hit and sent a fresh Find back
            # to the newest match, which is the line they had already read.
            include_current = restart
        elif message.get("from_bottom"):
            # The reader has been put back below the newest line, so the search goes on
            # from there: back, the newest line is the first one it looks at; forward,
            # there's nothing past it.
            self.review.enter()
            include_current = not forward
        else:
            if restart:
                # Nothing said where the reader is (the browser renderer has no caret of
                # its own), so a new Find starts at the chosen edge and includes that edge
                # line. Repeats remain exclusive, so F3 advances instead of finding the same
                # line forever.
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

    def _anchor_review(self, message: dict) -> bool:
        """Put the review cursor on the output line the reader is standing on.

        The output box holds fewer lines than the buffer and keeps a caret of its own, so the
        window says which line it is: the line's text, and how far it sits from the end. The
        search then runs from there, which is what makes it go on from the reader's position
        rather than from wherever the last search left off -- and what stops a fresh Find from
        jumping back to the newest match the reader has already read.

        False when the window said nothing or the line is no longer in the buffer, leaving the
        caller to fall back on the edges. A line that was trimmed away is not a position.
        """
        text = str(message.get("text") or "")
        from_end = message.get("from_end")
        if not text or not isinstance(from_end, int):
            return False
        lines = self.buffer.lines()
        matches = [index for index, line in enumerate(lines) if line.plain_text == text]
        if not matches:
            return False
        near = len(lines) - 1 - max(0, from_end)
        self.review.go_to(min(matches, key=lambda index: abs(index - near)))
        return True

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

    def _handle_key(self, combo: str) -> None:
        action = self.keymap.get(combo)
        if action is None and ":" not in combo:
            self.engine.press_key(combo)  # user-defined macro
            return
        # A renderer control with a meaning of its own for a key (Enter in the output box,
        # where there is nothing to send) passes the action itself. Keymap combos never
        # contain a colon and actions always do, so the two can't be confused.
        namespace, _, argument = (action or combo).partition(":")
        if namespace == "review" and argument in _LINK_JUMPS and self.engine.has_key(combo):
            # Erion's history browser uses Alt+PageUp/PageDown: a soundpack's or the player's
            # own hotkey on the key keeps it in that world, as with the Alt+number tab keys.
            self.engine.press_key(combo)
            return
        if namespace == "tab":
            # Switching tabs is the window's job, and the window does it before the key gets
            # here. What reaches the engine is a key the world's soundpack or the player's own
            # hotkey claimed (Erion's channel history reads messages on Alt+1 to Alt+0), so it
            # goes to that binding, as it did before the key had a tab action.
            self.engine.press_key(combo)
            return
        if namespace == "recall":
            # "recall:N" or "recall:<channel>:N" (channel filters the scrollback).
            channel, _, count = argument.rpartition(":")
            n = _parse_count(count)
            if n is not None:
                self._speak_review(self.review.recall(n, channel=channel or None))
        elif namespace == "review" and argument == "link":
            self._activate_link()
        elif namespace == "review" and argument == "copy_line":
            self._copy_review_line()
        elif namespace == "review" and argument in _LINK_JUMPS:
            self._jump_to_link(forward=argument == "next_link")
        elif namespace == "review" and argument in _REVIEW_VERBS:
            if not self.review.active:
                self.review.enter()
            self._speak_review(getattr(self.review, argument)())
            self._announce_link_under_cursor()
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

    def _announce_link_under_cursor(self) -> None:
        """Say what the link the follow key would act on does: the keyboard's version of hovering.

        The link here is the same one Alt+Enter would follow -- including a line's only link,
        which is followed from any word on it. Announcing the caption by a stricter rule than
        the one that follows it meant the key could act on a link the reader had never heard
        described.

        Said once per link, so walking the words of one link says it on arrival and not again.
        That is the link itself, not an equal one: the same exit on the next line is a link
        the reader has not arrived at yet.
        """
        link = self.review.link()
        if link is self._link_announced:
            return
        self._link_announced = link
        if link is not None and link.hint:
            # Queued behind the word rather than barging in on it: the word the cursor just
            # reached is the thing being read, and the caption explains it -- cutting it off
            # left the reader hearing only "link" and never the word it was about.
            self.voice.speak(f"link: {link.hint}", channel=REVIEW_CHANNEL, interrupt=False)

    def _jump_to_link(self, *, forward: bool) -> None:
        """Alt+PageDown / Alt+PageUp: on to the next MXP link, or back to the previous one.

        The review cursor parks on the link's first word, so Alt+Enter follows it, and the
        link's caption is said after it. Starting outside review, the jump counts the newest
        line's own links: back from the end of it, or on from its start.
        """
        fresh = not self.review.active
        if fresh:
            self.review.enter()
        if forward:
            link = self.review.next_link(inclusive=fresh)
        else:
            link = self.review.prev_link(from_line_end=fresh)
        if link is None:
            self._speak_review("no more links")
            return
        said = f"{link.text}{_menu_note(link)}"
        line = self.review.current_line()
        # The line too, unless the link is all of it: "north" alone doesn't say where it is.
        self._speak_review(said if line.strip() == link.text.strip() else f"{said}: {line}")
        self._announce_link_under_cursor()

    def _copy_review_line(self) -> None:
        """Offer the reviewed line for copying, all of it or any part (Ctrl+Shift+C).

        Like every review key, it starts on the newest line when review isn't running yet. The
        window owns the clipboard and the box the words are picked from; the engine only says
        which line.
        """
        if not self.review.active:
            self.review.enter()
        text = self.review.current_line()
        if not text.strip():
            self._speak_review("nothing to copy")
            return
        self._post(protocol.copy_line(text))

    def _activate_link(self) -> None:
        """Follow the MXP link under the review cursor.

        The mouse version of this is "click the words"; the keyboard version is the same
        gesture one level down -- the review cursor's word picks the link, and on a line
        with a single link anywhere on it counts. Nothing is remembered between presses:
        the link lives on its line, so reading back through the scrollback still follows
        the right one.
        """
        if not self.review.active:
            self.review.enter()  # nothing reviewed yet, so act on the newest line
        links = self.review.links()
        if not links:
            self._speak_review("no link on this line")
            return
        chosen = self.review.link()
        if chosen is None:
            # Several links and the word isn't in one: name them rather than guess, so one
            # more Alt+Right lands on a word that picks the intended one. A link with no
            # words of its own (an image, or an empty <SEND>) is named by what it does.
            named = "; ".join(
                f"{number} {link.text or link.command}{_menu_note(link)}"
                for number, link in enumerate(links, 1)
            )
            self._speak_review(f"{len(links)} links: {named}")
            return
        if chosen.choices:
            # A menu is a question, not an answer: following it asks which command was meant,
            # and nothing goes to the MUD until the player picks one. The question is traced
            # as well as the answer, so a session's log says what was offered and what was
            # taken.
            if self._diag is not None:
                self._diag.event(
                    "mxp.menu", text=chosen.text, choices=len(chosen.choices)
                )
            self._post(protocol.menu(chosen.text or chosen.command, list(chosen.choices)))
            return
        self._follow_link(
            chosen.text, chosen.command, insert=chosen.insert, hint=chosen.hint
        )

    def _activate_link_at(self, text: str, from_end: int, column: int = -1) -> None:
        """Follow the link on the output line the reader is on (Enter in the output box).

        The output box holds fewer lines than the buffer and has a caret of its own, so the
        window says which line it is -- its text, and how far it sits from the end -- and which
        word of it the caret is on. The review cursor moves to that line and that word, so
        Enter never acts on some newer line the reader hasn't heard, and a line carrying
        several links follows the one under the caret rather than giving up. A line that can't
        be found follows nothing.
        """
        lines = self.buffer.lines()
        near = len(lines) - 1 - max(0, from_end)
        matches = [index for index, line in enumerate(lines) if line.plain_text == text]
        if not text.strip() or not matches:
            self._speak_review("no link on this line")
            return
        self.review.go_to(min(matches, key=lambda index: abs(index - near)))
        if column >= 0:
            self.review.go_to_column(column)
        self._activate_link()

    def _follow_link(
        self, text: str, command: str, *, insert: bool = False, hint: str = ""
    ) -> None:
        """Do what a link asks, whoever asked it: the review cursor or a click in the browser.

        ``hint`` is the caption the MUD gave the link -- what following it is meant to do. It is
        said with the command, because "a sword: buy sword" leaves out what the MUD was
        offering, and the reader is about to send a command on the strength of it.
        """
        if not command.strip():
            return
        if self._diag is not None:
            # Not "mxp.link": a link can come from Pueblo's <a xch_cmd> too, and this is the one
            # place every markup's links are followed from.
            self._diag.event("link.follow", text=text, command=command, insert=insert, hint=hint)
        if insert:
            refusal = self._typed_link_refusal(command)
            if refusal is not None:
                if self._diag is not None:
                    self._diag.event(
                        "link.refused", text=text, command=command, reason=refusal
                    )
                self._speak_system(f"Link not followed: {refusal}.")
                return
            # <SEND PROMPT>: the command is the opening of one the player finishes, so it
            # belongs in their command box, not on the wire -- sent as it stands it would
            # reach the MUD as a command they never gave. Nothing leaves until they press
            # Enter themselves. The trailing space the MUD wrote is kept: "tell Ariadne " is
            # meant to have a name typed after it.
            self._post(protocol.input_text(command))
            said = f"in the command box: {command.strip()}"
            self._speak_system(f"{said}. {hint}" if hint else said)
            return
        command = command.strip()
        if command.lower().startswith(_LINK_URL_SCHEMES):
            self._open_link(command)
            return
        # Said before the command goes, so the caption explains what is being sent rather
        # than arriving behind it.
        said = f"{text}: {command}" if text else f"link: {command}"
        self._speak_system(f"{said}. {hint}" if hint else said)
        # A link is the MUD's text, not the player's, so it may not run a client command:
        # a MUD could otherwise tag any word with <SEND HREF="/quit"> and end the session.
        self._dispatch_command(command, allow_client=False)

    def _typed_link_refusal(self, command: str) -> str | None:
        """Why a link's text may not be typed into the command box, or None when it may.

        Whatever lands in the box goes out as the player's own typing when they press Enter,
        with genericMud's commands allowed and the separator splitting it. So a MUD may start
        one plain command there and nothing more: not a genericMud command, which could plant
        a trigger or an alias to fire later, and not a second command hidden behind the
        separator, which the player would send without meaning to.
        """
        if command.lstrip().startswith(CLIENT_PREFIX):
            return "it would type a genericMud command"
        separator = self.command_separator
        if separator and separator in command:
            return "it would type more than one command"
        if len(command) > _MAX_TYPED_LINK:
            return "what it would type is too long for a command"
        if not command.isprintable():
            return "what it would type has control characters in it"
        return None

    def _open_link(self, url: str) -> None:
        """Open an <A HREF> link in the browser, announcing it because focus moves."""
        window = self.pack_window
        if window is not None:
            if not window.open_url(url):
                self._speak_system("no browser to open that link")
                return
        else:
            # Nothing native to ask: the web UI runs inside a browser, so the renderer opens
            # the tab. Only the schemes checked by the caller ever reach here.
            self._post(protocol.open_url(url))
        self._speak_system(f"opening {url}")

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
