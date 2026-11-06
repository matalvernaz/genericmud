"""One world's session state, settings and connection lifecycle.

EngineApp builds the services shared by its mixins and owns their initial state. The window
injects transport, rendering, scheduling and other callbacks so this code does not depend on
either the native or web window. Its bases name the modules that handle each kind of work."""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from genericmud.app.autologin import AutologinMixin
from genericmud.app.incoming_text import IncomingTextMixin
from genericmud.app.key_actions import KeyActionsMixin
from genericmud.app.links import LinksMixin
from genericmud.app.mud_data import MudDataMixin
from genericmud.app.server_media import ServerMediaMixin
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
from genericmud.model.buffer import Buffer, Link
from genericmud.navigation import Navigator, SafeWalk
from genericmud.packs import ActivationResult, PackStore
from genericmud.packs.pack_updates import PackUpdates
from genericmud.protocol.charset import AUTO, ServerTextCodec
from genericmud.protocol.mxp import MxpParser
from genericmud.protocol.pueblo import PuebloParser
from genericmud.review.channels import ChannelReview
from genericmud.review.cursor import ReviewCursor
from genericmud.safepath import world_component
from genericmud.scripting import user_scripts
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.vipmud_dialect import VipMudPack
from genericmud.session.credentials import CredentialStore
from genericmud.session.hub import SessionHub
from genericmud.session.log import SessionLogger
from genericmud.session.login import AutoLogin
from genericmud.sound.bus import SoundBackend, SoundBus
from genericmud.sound.media import MediaLibrary
from genericmud.sound.renderer_backend import _PostSoundBackend
from genericmud.voice.router import SYSTEM_CHANNEL, VoiceRouter

if TYPE_CHECKING:
    from genericmud.config.worlds import World
    from genericmud.session.diaglog import DiagnosticLog

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


def _default_schedule(delay: float, callback: Callable[[], None]) -> object | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.call_later(delay, callback)


class EngineApp(
    IncomingTextMixin,
    MudDataMixin,
    ServerMediaMixin,
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
        # The native window injects BASS, with pygame as a fallback. Without a native backend,
        # sounds go to the web renderer; the native window does not consume those messages.
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
            log_note=self._log_note_asked_for,
            log_line=self._log,
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
        self.log_notes = True  # a world's own setting replaces this when one is opened
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
        self.log_notes = world.log_notes
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
