"""Canonical scripting surface every dialect binds to.

The native Lua ``mud`` table, the MUSHclient compat globals, and the VIPMud
``.set`` interpreter all call through one :class:`ScriptApi` instance. It is a
thin facade over an :class:`AutomationEngine` plus the pack's base directory
(for resolving relative sound paths), so behaviour is identical no matter which
dialect authored a rule.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from genericmud.automation.channels import ChannelPolicy
from genericmud.automation.engine import AutomationEngine, Callback
from genericmud.safepath import is_absolute, is_traversal, is_unc, within

# Channels that carry the primary spoken output + app alerts. A pack must not be able to set
# speak=False on these and silence the client -- for a blind user, total muting is catastrophic
# and imperceptible. Packs route their own spam to their own channels instead. (These names are
# the ones EngineApp wires policies for: main output, system alerts, tells, and the review pane.)
_RESERVED_CHANNELS = frozenset({"main", "system", "tell", "review"})

# Bounds so a hostile/broken pack can't DoS the client through the legitimate API. The script
# guard already caps any single callback at ~1s, but nothing bounded how many timers a pack could
# leave pending (starving the event loop, which freezes the UI = silence for a blind user) or how
# large a variable it could stash.
_MAX_ACTIVE_TIMERS = 1000  # outstanding pack timers; further add_timer calls are refused
_MIN_TIMER_DELAY = 0.01  # clamp near-zero delays so a self-rearming timer can't busy-spin the loop
_MAX_VAR_VALUE_LEN = 1_000_000  # 1 MB; a longer var/shared value is refused


class ScriptApi:
    def __init__(
        self, engine: AutomationEngine, *, source: str = "", base_dir: str | None = None
    ) -> None:
        self._engine = engine
        self._source = source
        self._base_dir = base_dir
        self._sounds_index: dict[str, str] = {}  # basename(lower) -> full path under @sppath
        self._sounds_index_key: str | None = None  # the @sppath the index was built for
        self._active_timers = 0  # pending pack timers, bounded by _MAX_ACTIVE_TIMERS

    @property
    def diag(self):
        """The engine's diagnostic log (None when tracing is off)."""
        return self._engine.diag

    # --- output ---

    def send(self, text: str) -> None:
        self._engine.sink.send(str(text))

    def execute(self, text: str) -> None:
        """Process ``text`` as if the user typed it (MUSHclient ``Execute``).

        Aliases run first; only what falls through goes to the MUD. Distinct from
        :meth:`send`: Erion's history plugin does ``Execute("history_add all=...")``
        expecting its own alias to consume it -- sending that straight to the server
        gets a spoken "no such command" rejection on every captured line.
        """
        for out in self._engine.process_input(str(text)):
            self._engine.sink.send(out)

    def send_packet(self, data: bytes) -> None:
        """Send a pre-framed telnet packet verbatim (MUSHclient ``SendPkt``).

        The pack builds the full ``IAC SB ... IAC SE`` framing itself (that is
        SendPkt's contract), so no wrapping or escaping happens here.
        """
        self._engine.sink.send_packet(data)

    def echo(self, text: str, channel: str = "main") -> None:
        self._engine.sink.echo(str(text), channel)

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self._engine.sink.speak(str(text), channel, interrupt)

    def stop_speech(self) -> None:
        """Cut current speech (packs' interrupt-then-announce idiom, e.g. hp reports)."""
        self._engine.sink.stop_speech()

    def is_connected(self) -> bool:
        return self._engine.connected

    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
    ) -> bool:
        """Play a cue; returns whether the file resolved to something that exists.

        The return drives dialect-level failure reporting: VIPMud's %playhandle is 0
        when a play failed, and packs branch on it (Cosmic Rage speaks the failure and
        auto-fetches the missing file).
        """
        if self._engine.diag is not None:
            self._engine.diag.event(
                "play.entry", source=self._source or "?", file=file,
                channel=channel, gain=gain, loop=loop,
            )
        resolved, exists = self._resolve(file)
        self._engine.sink.play(resolved, channel, gain, pan, loop)
        return exists

    def stop(self, channel: str = "sound") -> None:
        self._engine.sink.stop(channel)

    def is_playing(self, channel: str = "sound") -> bool:
        """Whether a cue is still audible on ``channel`` (backend truth, not history)."""
        return self._engine.sound.is_playing(channel)

    def music(self, file: str, channel: str = "music") -> None:
        if self._engine.diag is not None:
            self._engine.diag.event(
                "play.entry", source=self._source or "?", file=file, channel=channel, kind="music"
            )
        self._engine.sink.music(self._resolve(file)[0], channel)

    def adjust(self, channel: str, gain: float | None = None, pan: float | None = None) -> None:
        """Live volume/pan change on a playing cue (VIPMud #pc, bass setVol/slideVol)."""
        self._engine.sound.adjust(channel, gain, pan)

    # --- variables ---

    def get_var(self, name: str, default: str | None = "") -> str | None:
        return self._engine.get_var(name, default)

    def set_var(self, name: str, value: object) -> None:
        if len(str(value)) > _MAX_VAR_VALUE_LEN:
            return  # refuse an oversized value (memory-exhaustion guard)
        self._engine.set_var(name, value)

    def delete_var(self, name: str) -> None:
        """Remove a variable entirely (VIPMud #unvar / MUSHclient DeleteVariable):
        a deleted variable must read as UNSET (nil / %defined 0), not empty-string."""
        self._engine.delete_var(name)

    def get_gvar(self, name: str) -> str:
        return self._engine.get_gvar(name)

    def set_gvar(self, name: str, value: object) -> None:
        """Set a global (cross-session-persistent-namespace) variable; VIPMud ``#GVAR``.

        Distinct from :meth:`set_var`: globals live in the engine's ``_gvars`` map, which
        ``get_var`` falls back to, so an ``@name`` read still finds a gvar. Same size guard.
        """
        if len(str(value)) > _MAX_VAR_VALUE_LEN:
            return
        self._engine.set_gvar(name, value)

    # --- registration ---

    def add_trigger(self, pattern: str, callback: Callback, **opts: object) -> None:
        opts.setdefault("source", self._source)
        self._engine.add_trigger(pattern, callback, **opts)  # type: ignore[arg-type]

    def add_alias(self, pattern: str, callback: Callback, **opts: object) -> None:
        opts.setdefault("source", self._source)
        self._engine.add_alias(pattern, callback, **opts)  # type: ignore[arg-type]

    def add_key(self, key: str, callback: Callback) -> None:
        self._engine.add_key(key, callback, source=self._source)

    def add_timer(self, delay: float, callback: Callable[[], None]) -> None:
        if self._active_timers >= _MAX_ACTIVE_TIMERS:
            return  # refuse: too many pending timers would starve the event loop (UI freeze)
        self._active_timers += 1

        def wrapped() -> None:
            self._active_timers -= 1
            callback()

        self._engine.sink.schedule(max(delay, _MIN_TIMER_DELAY), wrapped)

    def set_channel(
        self,
        name: str,
        *,
        speak: bool = True,
        display: bool = True,
        interrupt: bool = False,
        voice: str | None = None,
    ) -> None:
        if name in _RESERVED_CHANNELS:
            return  # a pack can't mute/redirect the accessibility-critical channels
        self._engine.channels.set_policy(
            name, ChannelPolicy(speak=speak, display=display, interrupt=interrupt, voice=voice)
        )

    def set_volume(self, category: str, gain: float) -> None:
        self._engine.sound.set_volume(category, float(gain))

    def mute(self, category: str, muted: bool = True) -> None:
        self._engine.sound.set_muted(category, bool(muted))

    def flush(self) -> None:
        """Stop every playing cue (panic path; VIPMud ``#pc 0 stop``)."""
        self._engine.sound.flush()

    def set_master(self, gain: float) -> None:
        self._engine.sound.set_master(float(gain))

    # --- cross-session (multi-character play) ---

    def send_to(self, session: str, text: str) -> bool:
        if self._engine.hub is None:
            return False
        return self._engine.hub.send_to(session, str(text))

    def broadcast(self, text: str) -> int:
        if self._engine.hub is None:
            return 0
        return self._engine.hub.broadcast(str(text), exclude=self._engine.session_name)

    def sessions(self) -> list[str]:
        return self._engine.hub.sessions() if self._engine.hub is not None else []

    def shared_get(self, key: str) -> str:
        return self._engine.hub.shared_get(key) if self._engine.hub is not None else ""

    def shared_set(self, key: str, value: object) -> None:
        if self._engine.hub is not None and len(str(value)) <= _MAX_VAR_VALUE_LEN:
            self._engine.hub.shared_set(key, value)

    @property
    def base_dir(self) -> str | None:
        """The pack's root dir, for dialects that resolve their own paths (e.g. GetInfo)."""
        return self._base_dir

    def _resolve(self, file: str) -> tuple[str, bool]:
        original = file
        resolved = self._confine_media(file)
        exists = bool(resolved) and os.path.exists(resolved)
        # The @sppath basename lookup only ever returns files walked under the Sounds folder, so
        # it's a safe fallback for both a missing file and a rejected (unsafe/escaping) path.
        fallback = self._find_in_sounds_dir(original) if not exists else None
        final = fallback if fallback is not None else resolved
        found = exists or fallback is not None
        if self._engine.diag is not None:
            self._engine.diag.event(
                "play.resolve", input=original, resolved=final, exists=found,
                fallback=("sppath" if fallback is not None else "none"),
                sppath=self._engine.get_var("sppath") or "",
            )
        return final, found

    def _confine_media(self, file: str) -> str:
        """Confine a pack/dialect sound path to the pack dir or @sppath; "" if it escapes.

        This is the only filesystem reach the sandboxed dialects (native Lua, VIPMud .set) have
        through the sound API, so it must hold (a Windows UNC path also leaks the NTLM hash on
        open). UNC, ``..`` and NUL are always refused. The dialects pre-resolve against
        @sppath/the pack dir and hand us an ABSOLUTE path, which is allowed only when it lands
        inside an allowed root -- so ``\\\\attacker\\share``, ``C:\\Windows``, ``/etc/...`` are
        rejected. A relative path is joined under the pack dir and confined.
        """
        if not file or "\x00" in file or is_unc(file) or is_traversal(file):
            return ""
        # Build the path exactly as before: join a relative name under the pack dir, then collapse
        # the doubled slash MUSHclient makes from GetInfo() (a trailing slash plus a plugin's
        # leading one). NOT os.path.normpath -- on Windows it flips / to \, mangling the
        # forward-slash paths packs use (and breaking exact-path tests).
        if self._base_dir and not is_absolute(file):
            file = os.path.join(self._base_dir, file)
        resolved = re.sub(r"/{2,}", "/", file)
        # The dialects pre-resolve against @sppath / the pack dir and hand us an absolute path;
        # allow it only when it lands inside an allowed root, so \\attacker\share, C:\Windows and
        # /etc/... are rejected. A bare relative name (no pack dir) can't escape (.. already
        # refused above), so it passes through to the backend as before.
        if is_absolute(resolved):
            roots = [root for root in (self._base_dir, self._engine.get_var("sppath")) if root]
            if not any(within(root, resolved) for root in roots):
                return ""
        return resolved

    def _find_in_sounds_dir(self, path: str) -> str | None:
        """Locate a missing sound by basename under the user's Sounds folder (``@sppath``).

        Packs hardcode where their audio lives; this lets the world's Sounds folder point at
        sounds kept elsewhere (e.g. Erion's separate sound repo), regardless of the pack's own
        path assumptions. Indexed once per folder; a basename collision keeps the first match
        (filenames are unique within a soundpack), and the walk only runs on a cache miss.
        """
        sounds_dir = self._engine.get_var("sppath")
        if not sounds_dir or not os.path.isdir(sounds_dir):
            return None
        if self._sounds_index_key != sounds_dir:
            index: dict[str, str] = {}
            for root, _dirs, files in os.walk(sounds_dir):
                for name in files:
                    index.setdefault(name.lower(), os.path.join(root, name))
            self._sounds_index = index
            self._sounds_index_key = sounds_dir
        # Split on both separators: a Windows-authored pack path keeps its backslashes when
        # resolved on Linux, where os.path.basename only honours "/" and would miss the leaf.
        leaf = path.replace("\\", "/").rsplit("/", 1)[-1]
        return self._sounds_index.get(leaf.lower())
