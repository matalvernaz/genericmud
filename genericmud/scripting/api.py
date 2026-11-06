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
import time
from collections.abc import Callable
from pathlib import Path

from genericmud.automation.channels import ChannelPolicy
from genericmud.automation.engine import AutomationEngine, Callback
from genericmud.automation.variables import format_value
from genericmud.safepath import inside, is_absolute, is_traversal, is_unc, real_path

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
_MAX_EXECUTE_DEPTH = 20  # an alias that executes itself must terminate without recursion overflow
_COMMAND_VARIABLE_RE = re.compile(r"\$\{([^{}]+)\}")
_MISSING = object()
_MAX_REAL_ROOTS = 64  # resolved sound roots kept per pack; @sppath can be set to anything

# MUSHclient's msgbox button sets, each with the names its buttons answer with, in order.
MESSAGE_BUTTONS = {
    "ok": ("ok",),
    "okcancel": ("ok", "cancel"),
    "yesno": ("yes", "no"),
    "yesnocancel": ("yes", "no", "cancel"),
    "retrycancel": ("retry", "cancel"),
    "abortretryignore": ("abort", "retry", "ignore"),
}
_MESSAGE_ICONS = frozenset({"!", "?", "i", "."})


def _is_index(value: object, count: int) -> bool:
    """Whether ``value`` is a position in a list of ``count`` items (a bool is not)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < count


class ScriptApi:
    def __init__(
        self, engine: AutomationEngine, *, source: str = "", base_dir: str | None = None
    ) -> None:
        self._engine = engine
        self._source = source
        self._base_dir = base_dir
        self._sounds_index: dict[str, str] = {}  # basename(lower) -> full path under @sppath
        self._sounds_index_key: str | None = None  # the @sppath the index was built for
        self._real_roots: dict[str, Path] = {}  # allowed sound roots, links followed (_real_root)
        self._sounds_root_for: tuple[object, str | None, str | None] | None = None
        self._sounds_root_value: str | None = None  # _sounds_root's answer for that key
        self._active_timers = 0  # pending pack timers, bounded by _MAX_ACTIVE_TIMERS
        self._timer_refusal_noted = False  # the diagnostic log hears about the first refusal
        # Set by the app for a world's rules: why staged text may not go in the command box,
        # asked with the expanded text and with the captures left out. None means "no opinion".
        self.command_text_refusal = None
        self._execute_depth = 0

    @property
    def diag(self):
        """The engine's diagnostic log (None when tracing is off)."""
        return self._engine.diag

    # --- output ---

    def send(self, text: str) -> None:
        self._engine.sink.send(str(text))

    def run_script(self, text: str, *, line: str = "", wildcards=()) -> None:
        """Run rule-authored Lua: a function a script defines, or the text itself.

        Set up by the app, which knows this world's loaded scripts; a ScriptApi without one
        (a pack's, a test's) reports that rather than pretending to run anything.
        """
        runner = getattr(self, "scripts", None)
        if runner is None:
            raise RuntimeError("no scripting runtime is attached to this session")
        runner.run(str(text), line=str(line), wildcards=list(wildcards))

    def execute(self, text: str) -> None:
        """Process ``text`` as if the user typed it (MUSHclient ``Execute``).

        Aliases run first; only what falls through goes to the MUD. Distinct from
        :meth:`send`: Erion's history plugin does ``Execute("history_add all=...")``
        expecting its own alias to consume it -- sending that straight to the server
        gets a spoken "no such command" rejection on every captured line.
        """
        if self._execute_depth >= _MAX_EXECUTE_DEPTH:
            return
        self._execute_depth += 1
        try:
            for out in self._engine.process_input(str(text)):
                self._engine.sink.send(out)
        finally:
            self._execute_depth -= 1

    def expand_command(
        self,
        text: str,
        values: dict[str, object] | None = None,
        *,
        one_line: bool = True,
    ) -> str:
        """Resolve ``${name}`` placeholders from captures, script vars, or MUD data.

        ``values`` contains callback-local values such as wildcard captures. Unscoped names use
        callback values first, then persistent script variables, then GMCP/MSDP/MSSP values.
        ``${script:name}`` and ``${mud:name}`` select a namespace explicitly. Missing values
        raise instead of sending a potentially destructive half-expanded command.

        ``one_line`` refuses a line break in the result, because a command that grew a second
        line is a command stack nobody asked for. Text being stored in a variable is not a
        command yet -- it becomes one where it is used, and that is where it is checked -- so a
        value keeps its line breaks.
        """
        expanded = self._expand(text, values, missing=None)
        if one_line and any(char in expanded for char in ("\r", "\n", "\x00")):
            raise ValueError("expanded command contains a line break or NUL")
        return expanded

    def expand_speech(self, text: str, values: dict[str, object] | None = None) -> str:
        """Resolve ``${...}`` in text to be spoken, the same way as :meth:`expand_command`.

        Speech can't do harm half-expanded, and silence is the worst answer to a keypress
        asking for your health, so a missing value is said ("no value for
        Char.Vitals.hp") instead of stopping everything. That also names the variable, so
        a mistyped one is heard rather than guessed at.
        """
        return self._expand(text, values, missing=lambda name: f"no value for {name}")

    def _expand(
        self,
        text: str,
        values: dict[str, object] | None,
        *,
        missing: Callable[[str], str] | None,
    ) -> str:
        local_values = {str(key): value for key, value in (values or {}).items()}
        script_values = self._engine.all_vars()

        def lookup(mapping: dict[str, object], name: str) -> object:
            if name in mapping:
                return mapping[name]
            return _MISSING

        def replace(match: re.Match[str]) -> str:
            token = match.group(1).strip()
            scope, separator, name = token.partition(":")
            if separator and scope == "script":
                value = lookup(script_values, name)
            elif separator and scope == "mud":
                found, value = self._engine.resolve_mud_var(name)
                if not found:
                    value = _MISSING
            else:
                value = lookup(local_values, token)
                if value is _MISSING:
                    value = lookup(script_values, token)
                if value is _MISSING:
                    found, value = self._engine.resolve_mud_var(token)
                    if not found:
                        value = _MISSING
            if value is _MISSING:
                if missing is None:
                    raise ValueError(f"unknown command variable: {token}")
                return missing(name if separator and scope in ("script", "mud") else token)
            return format_value(value)

        return _COMMAND_VARIABLE_RE.sub(replace, str(text))

    def send_command(self, text: str, values: dict[str, object] | None = None) -> None:
        """Expand and send one command directly to the MUD."""
        command = self.expand_command(text, values)
        if command.strip():
            self.send(command)

    def send_packet(self, data: bytes) -> None:
        """Send a pre-framed telnet packet verbatim (MUSHclient ``SendPkt``).

        The pack builds the full ``IAC SB ... IAC SE`` framing itself (that is
        SendPkt's contract), so no wrapping or escaping happens here.
        """
        self._engine.sink.send_packet(data)

    def echo(self, text: str, channel: str = "main") -> None:
        self._engine.sink.echo(str(text), channel)

    def type_text(self, text: str, *, only_if_empty: bool = False) -> None:
        """Put text in the player's command box, unsent (MUSHclient's "send to command")."""
        self._engine.sink.type_text(str(text), only_if_empty=only_if_empty)

    def log_line(self, text: str) -> None:
        """Write a line to the session log without showing it (MUSHclient's "send to log")."""
        self._engine.sink.log_line(str(text))

    def note(self, text: str, channel: str = "main") -> None:
        """Show a line of the client's own in the output, and in the log (MUSHclient's Note)."""
        self._engine.sink.echo(str(text), channel, log=True)

    def print(self, text: str, channel: str = "main") -> None:  # noqa: A003 - MUSHclient's name
        """Show a line of the client's own in the output, never in the log (MUSHclient's Print)."""
        self._engine.sink.echo(str(text), channel, log=False)

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self._engine.sink.speak(str(text), channel, interrupt)

    def stop_speech(self) -> None:
        """Cut current speech (packs' interrupt-then-announce idiom, e.g. hp reports)."""
        self._engine.sink.stop_speech()

    def line_speech(self) -> bool:
        """Whether incoming lines, and the notes packs print like them, are spoken."""
        return self._engine.line_speech

    def set_line_speech(self, enabled: bool) -> None:
        """Speak incoming lines or keep them quiet (a pack's Do Not Disturb).

        Quiet lines still reach the buffer and the output, and the review keys still read
        them; only the automatic reading as they arrive stops.
        """
        self._engine.line_speech = bool(enabled)

    def file_in_history(self, channel: str, text: str) -> None:
        """File ``text`` under a review channel, as MUSHclient packs' ``history_add`` does.

        Packs file the line that just arrived, so that line is tagged with the channel and
        the channel review keys find it there. Filing under "all" changes nothing: the main
        review already holds every line. Text that isn't the arriving line (one the pack
        built itself) goes into the scrollback under that channel, unspoken and unshown.
        """
        channel = str(channel).strip().lower()
        text = str(text)
        if not channel or channel == "all" or not text.strip():
            return
        line = self._engine.current_line
        if line is not None and text.strip() in line.plain_text:
            if line.channel == "main":
                line.channel = channel
            return
        self._engine.sink.file_line(text, channel)

    def recent_lines(self, count: int) -> list[str]:
        """The last ``count`` lines of the output window, oldest first."""
        return self._engine.sink.recent_lines(max(0, count))

    def line_count(self) -> int:
        """How many lines the output window holds."""
        return self._engine.sink.line_count()

    def buffered_line(self, number: int):
        """Output line ``number`` (a Line), 1 being the oldest held; None outside that."""
        return self._engine.sink.buffered_line(number)

    def simulate(self, text: str) -> None:
        """Take ``text`` as if the MUD had sent it: triggers, the output and speech all get
        it (MUSHclient's Simulate)."""
        self._engine.sink.simulate(str(text))

    def save_state(self) -> None:
        """Save the packs' variables soon, not only when the session closes."""
        self._engine.sink.save_state()

    def update_packs(self, quiet: bool = False) -> None:
        """Install what this world's packs' repositories have new (a pack's own update
        command, answered by genericMud's updater)."""
        self._engine.sink.update_packs(quiet)

    def check_pack_updates(self) -> None:
        """Say whether this world's packs' repositories have anything new."""
        self._engine.sink.check_pack_updates()

    def pack_window(self):
        """The window's clipboard, command box, notepads and browser (a PackWindow), or
        None when there's no window."""
        return self._engine.sink.pack_window()

    def is_connected(self) -> bool:
        return self._engine.connected

    # --- asking the player (each returns None when they cancel or there's no window) ---
    # A script waits for the answer. ``while_waiting`` is called now and then meanwhile, and
    # the time spent waiting doesn't count against any script's time limit.

    @property
    def player_time(self) -> float:
        """Seconds scripts have spent waiting on the player's answers, all packs together."""
        return self._engine.player_time

    def _ask(self, request: dict, while_waiting: Callable[[], None] | None) -> object | None:
        started = time.monotonic()
        try:
            return self._engine.sink.ask(request, while_waiting)
        finally:
            self._engine.player_time += time.monotonic() - started

    def ask_choice(
        self, title: str, prompt: str, items: list[str], selected: int | None = None,
        *, while_waiting: Callable[[], None] | None = None,
    ) -> int | None:
        """Show a list and return the index of the item the player picked. With
        ``selected`` None nothing is selected at first, and OK alone is a cancel."""
        items = [str(item) for item in items]
        if not _is_index(selected, len(items)):
            selected = None
        answer = self._ask({
            "kind": "choice", "title": str(title), "prompt": str(prompt), "items": items,
            "selected": selected,
        }, while_waiting)
        return answer if _is_index(answer, len(items)) else None

    def ask_choices(
        self, title: str, prompt: str, items: list[str], selected: list[int] | None = None,
        *, while_waiting: Callable[[], None] | None = None,
    ) -> list[int] | None:
        """Show a list the player can tick several items in; returns the ticked indexes."""
        items = [str(item) for item in items]
        ticked = sorted({index for index in selected or [] if _is_index(index, len(items))})
        answer = self._ask({
            "kind": "choices", "title": str(title), "prompt": str(prompt), "items": items,
            "selected": ticked,
        }, while_waiting)
        if not isinstance(answer, list):
            return None
        if not all(_is_index(index, len(items)) for index in answer):
            return None
        return sorted(set(answer))

    def ask_text(
        self, title: str, prompt: str, default: str = "", *, multiline: bool = False,
        while_waiting: Callable[[], None] | None = None,
    ) -> str | None:
        """Ask the player to type something; ``default`` is already filled in."""
        answer = self._ask({
            "kind": "text", "title": str(title), "prompt": str(prompt), "default": str(default),
            "multiline": bool(multiline),
        }, while_waiting)
        return answer if isinstance(answer, str) else None

    def ask_message(
        self, title: str, prompt: str, buttons: str = "ok", icon: str = "!", default: int = 1,
        *, while_waiting: Callable[[], None] | None = None,
    ) -> str | None:
        """Show a message with MUSHclient's button sets; returns the pressed button's name.

        ``buttons`` is one of MESSAGE_BUTTONS, ``icon`` is "!", "?", "i" or "." (warning,
        question, information, error), and ``default`` counts the buttons from 1.
        """
        if buttons not in MESSAGE_BUTTONS:
            buttons = "ok"
        if icon not in _MESSAGE_ICONS:
            icon = "!"
        if not (isinstance(default, int) and 1 <= default <= len(MESSAGE_BUTTONS[buttons])):
            default = 1
        answer = self._ask({
            "kind": "message", "title": str(title), "prompt": str(prompt), "buttons": buttons,
            "icon": icon, "default": default,
        }, while_waiting)
        return answer if answer in MESSAGE_BUTTONS[buttons] else None

    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
        times: int = 1,
    ) -> bool:
        """Play a cue (``times`` times when it doesn't loop); returns whether the file resolved
        to something that exists.

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
        if times == 1:  # sinks written before repeats still take a cue
            self._engine.sink.play(resolved, channel, gain, pan, loop)
        else:
            self._engine.sink.play(resolved, channel, gain, pan, loop, times=max(1, times))
        return exists

    def sound_exists(self, file: str) -> bool:
        """Whether ``file`` currently resolves to a real pack-confined sound file."""
        return self._resolve(file)[1]

    def stop(self, channel: str = "sound") -> None:
        self._engine.sink.stop(channel)

    def is_playing(self, channel: str = "sound") -> bool:
        """Whether a cue is still audible on ``channel`` (backend truth, not history)."""
        return self._engine.sound.is_playing(channel)

    def sound_state(self, channel: str = "sound") -> int:
        """The cue's state as BASS numbers it: 0 stopped, 1 playing, 3 paused."""
        return self._engine.sound.state(channel)

    def pause(self, channel: str) -> None:
        self._engine.sound.pause(channel)

    def resume(self, channel: str) -> None:
        self._engine.sound.resume(channel)

    def slide(
        self, channel: str, gain: float | None = None, pan: float | None = None,
        frequency: float | None = None, seconds: float = 0.0,
    ) -> None:
        """Glide a playing cue's volume, pan or pitch over ``seconds`` (a pack's fades); a
        volume below 0 fades it away and stops it."""
        self._engine.sound.slide(channel, gain, pan, frequency=frequency, seconds=seconds)

    def fade_away(self, channel: str, seconds: float) -> None:
        self._engine.sound.fade_away(channel, seconds)

    def sound_position(self, channel: str) -> float:
        """How far into its file the cue on ``channel`` is, in seconds."""
        return self._engine.sound.position(channel)

    def seek(self, channel: str, seconds: float) -> None:
        self._engine.sound.seek(channel, seconds)

    def set_looping(self, channel: str, loop: bool) -> None:
        self._engine.sound.set_looping(channel, loop)

    def source_info(self, file: str):
        """What ``file`` holds (a SourceInfo: length, rate, channels), found the way play()
        finds it, or None when it isn't there or can't be read."""
        resolved, exists = self._resolve(file)
        if not exists:
            return None
        return self._engine.sound.source_info(resolved)

    def add_effect(
        self, channel: str, kind: int, settings: dict[str, float] | None = None,
        priority: int = 0,
    ) -> int:
        """Put one of BASS's DirectX 8 effects (echo, reverb, flanger, gargle and the rest;
        see bass_effects) on the cue playing on ``channel``, with settings by BASS's names.
        Answers a handle for it, or 0 when that can't be had."""
        return self._engine.sound.add_effect(channel, kind, settings, priority)

    def set_effect(self, channel: str, effect: int, settings: dict[str, float]) -> bool:
        return self._engine.sound.set_effect(channel, effect, settings)

    def effect_settings(self, channel: str, effect: int) -> dict[str, float] | None:
        return self._engine.sound.effect_settings(channel, effect)

    def remove_effect(self, channel: str, effect: int) -> None:
        self._engine.sound.remove_effect(channel, effect)

    def reset_effects(self, channel: str, effect: int = 0) -> None:
        self._engine.sound.reset_effects(channel, effect)

    def music(self, file: str, channel: str = "music") -> None:
        if self._engine.diag is not None:
            self._engine.diag.event(
                "play.entry", source=self._source or "?", file=file, channel=channel, kind="music"
            )
        self._engine.sink.music(self._resolve(file)[0], channel)

    def adjust(
        self, channel: str, gain: float | None = None, pan: float | None = None,
        frequency: float | None = None,
    ) -> None:
        """Live volume/pan/pitch change on a playing cue (VIPMud #pc, bass setVol/slideVol).
        ``frequency`` is the rate it plays at in Hz; 0 puts back the file's own."""
        if frequency is None:
            self._engine.sound.adjust(channel, gain, pan)
        else:
            self._engine.sound.adjust(channel, gain, pan, frequency=frequency)

    # --- variables ---

    def get_var(self, name: str, default: str | None = "") -> str | None:
        return self._engine.get_var(name, default)

    def set_var(self, name: str, value: object) -> None:
        if len(str(value)) > _MAX_VAR_VALUE_LEN:
            return  # refuse an oversized value (memory-exhaustion guard)
        self._engine.set_var(name, value)

    def variables(self) -> dict[str, str]:
        """Every script variable, by name (a copy)."""
        return self._engine.all_vars()

    @property
    def vars_version(self) -> int:
        """Goes up whenever a variable changes, so a reader can tell nothing did."""
        return self._engine.vars_version

    def delete_var(self, name: str) -> None:
        """Remove a variable entirely (VIPMud #unvar / MUSHclient DeleteVariable):
        a deleted variable must read as UNSET (nil / %defined 0), not empty-string."""
        self._engine.delete_var(name)

    def get_gvar(self, name: str) -> str:
        return self._engine.get_gvar(name)

    def delete_gvar(self, name: str) -> None:
        """Remove a global variable (VIPMud #GUnVar)."""
        self._engine.delete_gvar(name)

    def set_gvar(self, name: str, value: object) -> None:
        """Set a global (cross-session-persistent-namespace) variable; VIPMud ``#GVAR``.

        Distinct from :meth:`set_var`: globals live in the engine's ``_gvars`` map, which
        ``get_var`` falls back to, so an ``@name`` read still finds a gvar. Same size guard.
        """
        if len(str(value)) > _MAX_VAR_VALUE_LEN:
            return
        self._engine.set_gvar(name, value)

    def get_mud_var(self, name: str, default: object = None) -> object:
        """Latest GMCP/MSDP/MSSP value, including dotted paths into structured data."""
        return self._engine.get_mud_var(str(name), default)

    def mud_vars(self) -> dict[str, object]:
        return self._engine.all_mud_vars()

    # --- registration ---

    def add_trigger(self, pattern: str, callback: Callback, **opts: object):
        """Register a trigger; returns the engine's rule (its ``enabled`` can be switched)."""
        opts.setdefault("source", self._source)
        return self._engine.add_trigger(pattern, callback, **opts)  # type: ignore[arg-type]

    def set_group_enabled(self, group: str, enabled: bool = True) -> int:
        """Turn every rule in a group on or off (MUSHclient's EnableGroup); how many changed."""
        return self._engine.set_group_enabled(str(group), bool(enabled))

    def set_enabled(self, name: str, enabled: bool = True) -> bool:
        """Turn a named rule on or off, as MUSHclient's EnableTrigger/EnableAlias do."""
        return self._engine.set_enabled(str(name), bool(enabled))

    def remove_trigger(self, name: str) -> None:
        """Drop a previously-registered trigger by name (dynamic MUSHclient triggers)."""
        self._engine.remove_trigger(name)

    def add_data_watch(
        self, path: str, callback: Callable[[str, object], None], *, only_changes: bool = False
    ):
        """Hear when the MUD sends data at ``path``; see AutomationEngine.add_data_watch."""
        return self._engine.add_data_watch(
            path, callback, only_changes=only_changes, source=self._source
        )

    def add_alias(self, pattern: str, callback: Callback, **opts: object):
        """Register an alias; returns the engine's rule (its ``enabled`` can be switched)."""
        opts.setdefault("source", self._source)
        return self._engine.add_alias(pattern, callback, **opts)  # type: ignore[arg-type]

    def remove_alias(self, name: str) -> None:
        """Drop a previously-registered alias by name."""
        self._engine.remove_alias(name)

    def add_key(self, key: str, callback: Callback) -> None:
        self._engine.add_key(key, callback, source=self._source)

    def remove_key(self, key: str) -> None:
        """Drop this source's binding of ``key``."""
        self._engine.remove_key(key, source=self._source)

    def add_named_timer(
        self,
        name: str,
        delay: float,
        callback: Callable[[], None],
        *,
        every: float | None = None,
        next_delay: Callable[[], float] | None = None,
        group: str = "",
        enabled: bool = True,
        while_disconnected: bool = False,
    ) -> object:
        """A timer with a name (MUSHclient's AddTimer): findable, switchable, cancellable."""
        return self._engine.add_named_timer(
            str(name),
            delay,
            callback,
            every=every,
            next_delay=next_delay,
            group=str(group),
            source=self._source,
            enabled=enabled,
            while_disconnected=while_disconnected,
        )

    def remove_named_timer(self, name: str) -> bool:
        """MUSHclient's DeleteTimer."""
        return self._engine.remove_named_timer(str(name))

    def set_timer_enabled(self, name: str, enabled: bool = True) -> bool:
        """MUSHclient's EnableTimer."""
        return self._engine.set_timer_enabled(str(name), bool(enabled))

    def add_timer(
        self, delay: float, callback: Callable[[], None], *, while_disconnected: bool = False
    ) -> None:
        """Run ``callback`` after ``delay`` seconds, once.

        ``while_disconnected`` keeps it through a dropped connection (MUSHclient's
        ActiveWhenClosed, for the packs that flag a timer with it); everything else goes when
        the world does.
        """
        if self._active_timers >= _MAX_ACTIVE_TIMERS:
            # Refuse: too many pending timers would starve the event loop (UI freeze). A
            # repeating timer refused here never fires again, so leave a trace of it once.
            if not self._timer_refusal_noted and self._engine.diag is not None:
                self._timer_refusal_noted = True
                self._engine.diag.event(
                    "pack.timer_refused", source=self._source or "?", limit=_MAX_ACTIVE_TIMERS
                )
            return
        self._active_timers += 1
        handle_box: list = []
        released: list[bool] = []

        def release() -> None:
            # Once per timer, whether it fires or a disconnect or reload cancels it, so
            # cancelled timers don't keep counting against the limit.
            if not released:
                released.append(True)
                self._active_timers -= 1

        def wrapped() -> None:
            release()
            if handle_box:
                self._engine.discard_timer(handle_box[0])
            callback()

        # Track the handle on the engine so session teardown cancels any still-pending timer
        # (a fired one is discarded above), rather than letting it run after the tab closes.
        handle_box.append(
            self._engine.schedule_timer(
                max(delay, _MIN_TIMER_DELAY), wrapped, source=self._source, on_cancel=release,
                while_disconnected=while_disconnected,
            )
        )

    def track_pending(self, handle: object) -> None:
        """Cancel ``handle`` (anything with a cancel()) when the session or this pack ends."""
        self._engine.track_pending(handle, source=self._source)

    def forget_pending(self, handle: object) -> None:
        self._engine.discard_timer(handle)

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
            name,
            ChannelPolicy(speak=speak, display=display, interrupt=interrupt, voice=voice),
            source=self._source,
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

    @property
    def source(self) -> str:
        """The registration source (pack id) that rules from this api are tagged with."""
        return self._source

    @property
    def world_name(self) -> str:
        """The name of the world this session plays (VIPMud's %MudName)."""
        return self._engine.session_name

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
        # Windows-authored packs write relative paths with backslashes ("social\hit.wav").
        # POSIX treats that as ONE filename, so the joined path never exists and resolution
        # used to fall through to the basename index -- which picks the wrong file when two
        # directories share a leaf name. Forward slashes work on every platform.
        if not is_absolute(file):
            file = file.replace("\\", "/")
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
            # The requested path is looked at afresh every time; the roots it must lie in are
            # resolved once (_real_root), as a cue that resolved them all again cost a dozen
            # trips to the disk on Windows.
            requested = real_path(resolved)
            allowed = (self._base_dir, self._sounds_root())
            roots = [self._real_root(root) for root in allowed if root]
            if requested is None or not any(
                root is not None and inside(root, requested) for root in roots
            ):
                return ""
        return resolved

    def _real_root(self, root: str) -> Path | None:
        """An allowed sound root with its links followed, remembered once it resolves. The pack
        folder, the player's Sounds folder and @sppath stay put while a session plays, and a
        path checked against a root is still resolved afresh, so a root that did move can only
        turn a cue away, never let one out of the folders that were allowed."""
        known = self._real_roots.get(root)
        if known is None:
            known = real_path(root)
            if known is None:
                return None  # not remembered: the next cue tries again
            if len(self._real_roots) >= _MAX_REAL_ROOTS:
                self._real_roots.clear()
            self._real_roots[root] = known
        return known

    def _sounds_root(self) -> str | None:
        """Where the pack's sounds are: @sppath when it lies inside the pack or the player's
        own sounds folder, else that folder. @sppath is a variable any pack, or MUD text a
        pack runs as script, can set; pointed at "/" it let any file play and a missing
        sound walk the whole disk. Judged once for each @sppath and Sounds folder, since every
        cue asks."""
        sppath = self._engine.get_var("sppath")
        if not sppath or is_unc(sppath):
            # A UNC @sppath isn't even looked at: on Windows, resolving one is enough to send
            # the player's NTLM hash to that host.
            return self._engine.sounds_dir
        key = (sppath, self._base_dir, self._engine.sounds_dir)
        if key != self._sounds_root_for:
            target = real_path(sppath)
            if target is None:
                return self._engine.sounds_dir  # unresolvable just now: judged again next cue
            trusted = [root for root in (self._base_dir, self._engine.sounds_dir) if root]
            allowed = any(
                (real := self._real_root(root)) is not None and inside(real, target)
                for root in trusted
            )
            self._sounds_root_value = sppath if allowed else self._engine.sounds_dir
            self._sounds_root_for = key
        return self._sounds_root_value

    def _find_in_sounds_dir(self, path: str) -> str | None:
        """Locate a missing sound under the user's Sounds folder (``@sppath``).

        Packs hardcode where their audio lives; this lets the world's Sounds folder point at
        sounds kept elsewhere (e.g. Erion's separate sound repo), regardless of the pack's own
        path assumptions. The index maps both the case-folded path relative to the folder and
        the bare basename; the relative form is tried first so ``combat\\hit.wav`` and
        ``social\\hit.wav`` resolve to their own directories instead of whichever ``hit.wav``
        the walk met first. Indexed once per folder; a collision keeps the first match, and
        the walk only runs on a cache miss.
        """
        sounds_dir = self._sounds_root()
        if not sounds_dir or not os.path.isdir(sounds_dir):
            return None
        if self._sounds_index_key != sounds_dir:
            index: dict[str, str] = {}
            for root, _dirs, files in os.walk(sounds_dir):
                for name in files:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, sounds_dir).replace(os.sep, "/").lower()
                    index.setdefault(rel, full)
                    index.setdefault(name.lower(), full)
            self._sounds_index = index
            self._sounds_index_key = sounds_dir
        # Normalize on both separators: a Windows-authored pack path keeps its backslashes
        # when resolved on Linux, where os.path functions only honour "/".
        normalized = path.replace("\\", "/").lower()
        # The dialects pre-resolve against @sppath/the pack dir, so an absolute request is
        # reduced to its known-root-relative form -- a case-mismatched directory ("Comm\Hit.wav"
        # on disk as "comm/hit.wav") then still finds the real file via the relative index.
        for root in (sounds_dir, self._base_dir):
            if not root:
                continue
            prefix = root.replace("\\", "/").lower().rstrip("/") + "/"
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        exact = self._sounds_index.get(normalized)
        if exact is not None:
            return exact
        leaf = normalized.rsplit("/", 1)[-1]
        by_name = self._sounds_index.get(leaf)
        if by_name is not None:
            return by_name
        # A few archived packs contain an accidental doubled extension
        # (``CrossbowFire2.wav.wav``) while their active script requests ``.wav``.
        suffix = Path(leaf).suffix
        return self._sounds_index.get(leaf + suffix) if suffix else None
