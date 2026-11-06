"""Automation engine: triggers, aliases, keybindings, timers, variables, gags.

This is the dialect-agnostic core. Every scripting front-end (native Lua, the
MUSHclient compat shim, the VIPMud ``.set`` interpreter) compiles down to
registrations against this one engine, so a line is matched once regardless of
which dialect authored the rule. Side effects (send/speak/play/...) go through an
injectable :class:`EngineSink` so the engine is testable without a network, a
voice backend, or a UI.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from genericmud.automation.channels import ChannelRouter
from genericmud.automation.prefilter import required_text
from genericmud.model.buffer import Line
from genericmud.session.hub import SessionHub
from genericmud.sound.bus import SoundBus

if TYPE_CHECKING:
    from genericmud.session.diaglog import DiagnosticLog

# A pack-supplied regex trigger is matched against every incoming line, so a catastrophic-
# backtracking pattern (ReDoS) on a crafted line could hang the engine. The `regex` module is
# a `re` superset that honours a per-match timeout (and interrupts backtracking); fall back to
# stdlib `re` (no timeout) if it's somehow absent, so the engine still imports.
_MATCH_BUDGET_SECONDS = 0.25
try:
    import regex as _matcher

    _MATCH_KWARGS: dict[str, float] = {"timeout": _MATCH_BUDGET_SECONDS}
    _MatchTimeout: type[BaseException] = TimeoutError
except ImportError:  # pragma: no cover - regex is a declared dependency
    import re as _matcher  # type: ignore[no-redef]

    _MATCH_KWARGS = {}

    class _MatchTimeout(Exception):  # never raised by stdlib re; keeps the except clause valid
        ...


class PackWindow(Protocol):
    """What a window does for soundpacks' MUSHclient window calls: its clipboard, command box,
    notepads and the browser. Called on the loop thread."""

    def get_clipboard(self) -> str: ...
    def set_clipboard(self, text: str) -> None: ...
    def get_command(self) -> str: ...
    def set_command(self, text: str) -> bool:
        """Put ``text`` in the command box if it's empty; whether it was."""
    def paste_command(self, text: str) -> str:
        """Replace the command box's selection with ``text``; the text replaced."""
    def select_command(self, first: int, last: int) -> None:
        """Select columns ``first`` to ``last`` (1 is the first, -1 the end)."""
    def open_url(self, url: str) -> bool: ...
    def show_notepad(self, title: str, text: str) -> None:
        """Show a pack's notepad, read-only, without stopping the game."""


class EngineSink:
    """Side-effect surface the engine drives. Real wiring overrides these."""

    def send(self, text: str) -> None: ...
    def send_packet(self, data: bytes) -> None: ...
    def echo(self, text: str, channel: str = "main", *, log: bool = False) -> None:
        """Show ``text`` in the output as a line of the client's own (MUSHclient's echo).

        ``log`` asks for it to be written to the session log too, which is the difference
        between a note and a print: a note is part of the record, a print is not.
        """
    def file_line(self, text: str, channel: str) -> None:
        """Keep ``text`` in the scrollback under ``channel``, without showing or speaking it."""
    def type_text(self, text: str, *, only_if_empty: bool = False) -> None:
        """Put ``text`` in the player's command box, unsent (MUSHclient's "send to command").

        ``only_if_empty`` leaves a half-typed command alone, which is what a rule asks for.
        """

    def log_line(self, text: str) -> None:
        """Write ``text`` to the session log without showing it (MUSHclient's "send to log")."""

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None: ...
    def stop_speech(self) -> None: ...
    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
        times: int = 1,
    ) -> None: ...
    def stop(self, channel: str) -> None: ...
    def music(self, file: str, channel: str = "music") -> None: ...
    def schedule(self, delay: float, callback: Callable[[], None]) -> object | None: ...
    def recent_lines(self, count: int) -> list[str]:
        """The last ``count`` lines of the output, oldest first."""
        return []
    def line_count(self) -> int:
        """How many lines the output holds."""
        return 0
    def buffered_line(self, number: int) -> Line | None:
        """Output line ``number``, 1 being the oldest still held; None outside that."""
        return None
    def simulate(self, text: str) -> None:
        """Take ``text`` as if the MUD had sent it (MUSHclient's Simulate)."""
    def pack_window(self) -> PackWindow | None:
        """The window, for packs' clipboard, command box, notepad and browser calls; None
        when there's no window."""
        return None
    def save_state(self) -> None:
        """Save the packs' variables soon (MUSHclient's SaveState); asks close together
        make one save."""
    def update_packs(self, quiet: bool = False) -> None:
        """Install what this world's packs' repositories have new (a pack's own update
        command); ``quiet`` says nothing when there's nothing new."""
    def check_pack_updates(self) -> None:
        """Say whether this world's packs' repositories have anything new, without
        installing it (a pack's own "update check")."""
    def ask(
        self, request: dict, while_waiting: Callable[[], None] | None = None
    ) -> object | None:
        """Show the player a pack's question and wait for the answer; None means cancelled.

        ``request["kind"]`` is "choice", "choices", "text" or "message"; ScriptApi's ask_*
        methods build each one and check the answer. ``while_waiting`` is called now and
        then until the answer comes. A front end with no dialogs answers None.
        """
        return None


def wildcard_regex(pattern: str) -> str:
    """The regex a wildcard pattern stands for: ``*`` captures any run, ``?`` one char.

    Each wildcard becomes a numbered capture group; everything else is literal. The
    MUSHclient and VIPMud front-ends translate their own pattern syntax themselves and
    register real regexes, so this is the native (Lua, Automation Manager) convention.
    """
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("(.*)")
        elif ch == "?":
            out.append("(.)")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def compile_pattern(pattern: str, regex: bool, *, whole: bool = False) -> re.Pattern[str]:
    """Compile a trigger/alias pattern.

    ``regex=True`` is used verbatim. Otherwise it's a wildcard pattern (see
    :func:`wildcard_regex`). ``whole=True`` makes a wildcard pattern match the entire
    text rather than a prefix of it: an alias "sc" is the command sc, not every command
    that happens to start with sc ("scan"), and "combo *" still takes the rest as its
    capture.
    """
    if regex:
        return _matcher.compile(pattern)
    body = wildcard_regex(pattern)
    if whole:
        body += r"\Z"
    return _matcher.compile(body)


@dataclass
class MatchContext:
    """Passed to a rule's callback when it fires."""

    line: Line
    wildcards: list[str]  # [0] = full match, [1..] = capture groups
    named: dict[str, str]
    engine: AutomationEngine


Callback = Callable[[MatchContext], None]


@dataclass
class _Rule:
    pattern: re.Pattern[str]
    callback: Callback | None
    priority: int = 0
    name: str = ""
    enabled: bool = True
    gag: bool = False
    gag_but_display: bool = False
    omit_from_log: bool = False
    keep_evaluating: bool = True
    source: str = ""
    channel: str | None = None
    # MUSHclient's group: rules sharing a name can be switched together. Read from the rules
    # when it is asked for rather than kept in a list of its own, so a reload cannot leave a
    # group pointing at rules that no longer exist.
    group: str = ""
    # When a trigger with a stop scope stops evaluation, only the later triggers in the same
    # scope are skipped. MUSHclient stops within one plugin's list, so a pack's trigger can't
    # silence another pack's or the player's own. Without one, a stop ends the whole line.
    stop_scope: str | None = None
    tiebreak: str = ""  # orders rules of equal priority (MUSHclient sorts them by match text)
    timed_out: bool = False  # switched off because its pattern ran past the match budget
    # An alias that takes the command whenever it matches, even when it keeps evaluating
    # (MUSHclient sends what was typed only when no alias matched at all).
    consumes: bool = False
    fallback: bool = False  # an alias that runs only when no alias before it took the command
    # Text every match of the trigger's pattern contains, so a line without it is passed over
    # without running the regex (see prefilter). Worked out for the pattern in ``screened``;
    # a pack that swaps the pattern later (MUSHclient's SetTriggerOption) gets it worked out
    # again. Not part of what makes two rules equal.
    required: str | None = field(default=None, compare=False, repr=False)
    screened: object = field(default=None, compare=False, repr=False)


def _rule_order(rule: _Rule) -> tuple[int, str]:
    """Highest priority first; equal priorities by tiebreak, then in the order added."""
    return -rule.priority, rule.tiebreak


def _wildcards(match: re.Match[str]) -> list[str]:
    return [match.group(0), *(g if g is not None else "" for g in match.groups())]


def _named(match: re.Match[str]) -> dict[str, str]:
    return {name: value if value is not None else "" for name, value in match.groupdict().items()}


# One named timer may not wait less than this, and there may be no more than this many:
# the same floor and ceiling the unnamed timers have had since they existed.
MIN_TIMER_DELAY = 0.01
MAX_NAMED_TIMERS = 1000


class _NamedTimer:
    """One named timer: when to fire, what to do, and whether it is on.

    Kept on the engine rather than only in a handle so that "the timer called tick" can be
    switched off, moved to another group's fate, or deleted by name -- which is what makes a
    timer something a player can manage rather than a callback that runs until the session ends.
    """

    def __init__(
        self,
        engine: AutomationEngine,
        name: str,
        delay: float,
        callback: Callback,
        *,
        every: float | None = None,
        next_delay: Callable[[], float] | None = None,
        group: str = "",
        source: str = "",
        while_disconnected: bool = False,
    ) -> None:
        self._engine = engine
        self.name = name
        self.delay = max(0.0, float(delay))
        self.every = None if every is None else max(0.0, float(every))
        # Asked each time instead of a fixed repeat, for a timer that fires at a time of day:
        # its wait is "until the next 03:00", which changes with the clock.
        self.next_delay = next_delay
        self.callback = callback
        self.group = group
        self.source = source
        # MUSHclient's ActiveWhenClosed: a timer that keeps its clock through a disconnect. Every
        # other timer waits for the world to come back (see AutomationEngine.suspend_timers).
        self.while_disconnected = while_disconnected
        self.enabled = True
        self._pending: object | None = None

    @property
    def armed(self) -> bool:
        """Whether a fire is waiting on the clock right now."""
        return self._pending is not None

    def _schedule(self, delay: float) -> None:
        self._pending = self._engine.schedule_timer(
            delay, self._fire, source=self.source,
            while_disconnected=self.while_disconnected,
        )

    def arm(self) -> None:
        self.disarm()
        if not self.enabled:
            return
        # A time-of-day timer asks the clock when it is armed, not when it was made: armed at
        # 01:00 for 03:00, then paused from 02:30 to 02:45, it has to fire at 03:00.
        delay = self.next_delay() if self.next_delay is not None else self.delay
        self._schedule(delay)

    def disarm(self) -> None:
        if self._pending is not None:
            cancel = getattr(self._pending, "cancel", None)
            if cancel is not None:
                cancel()
            self._engine.discard_timer(self._pending)
            self._pending = None

    def _fire(self) -> None:
        spent, self._pending = self._pending, None
        if spent is not None:
            self._engine.discard_timer(spent)  # it has fired; there is nothing to cancel
        if not self.enabled:
            return
        self.callback()
        if not self.enabled:
            return
        if self.next_delay is not None:  # a time-of-day timer asks the clock each time
            self._schedule(self.next_delay())
        elif self.every is not None:
            self._schedule(self.every)
        else:
                # A one-shot has done its job, so it goes -- which is what MUSHclient does with one.
            # Leaving it in the list would have it answer "on" to the manager and "yes" to
            # is_rule_enabled, and switching its group would start it running again. The name is
            # remembered so the saved rules can lose it too.
            self._engine.note_spent_one_shot(self.name, self.source)
            self._engine.remove_named_timer(self.name)

    def set_enabled(self, enabled: bool) -> bool:
        """Switch the timer on or off; True when that changed something.

        MUSHclient's EnableTimer returns early when nothing changes, and so does this: a prompt
        trigger that switches its group on every ten seconds would otherwise restart every
        timer in it, and a thirty-second timer would never fire.
        """
        if bool(enabled) == self.enabled:
            return False
        self.enabled = bool(enabled)
        if self.enabled:
            self.arm()
        else:
            self.disarm()
        return True


@dataclass
class _KeyBinding:
    """One source's binding of a key combo (see :meth:`AutomationEngine.add_key`).

    ``_keys`` holds the effective binding per combo: the last *enabled* one, so switching a
    group off lets the combo fall back to whatever owned it before -- as if that source had
    never bound it, which is what MUDclient's EnableGroup does to an accelerator.
    """

    key: str
    source: str
    callback: Callback
    group: str = ""
    enabled: bool = True


@dataclass
class _PendingTimer:
    """One scheduled callback the engine holds a handle for, so it can be taken back.

    ``while_disconnected`` marks the work that must survive a dropped connection: a timer the
    player asked to keep going, or a pack's timer flagged ActiveWhenClosed. Everything else is
    cancelled when the world disconnects, because it has nothing left to run against.
    """

    handle: object
    source: str
    on_cancel: Callable[[], None] | None = None
    while_disconnected: bool = False


def _cancel_pending(entry: _PendingTimer) -> None:
    cancel = getattr(entry.handle, "cancel", None)
    if cancel is not None:
        cancel()
    if entry.on_cancel is not None:
        entry.on_cancel()


# A watch may start with one of these to hear only that protocol: gmcp.Char.Vitals.
_DATA_PROTOCOLS = ("gmcp", "msdp", "mssp")
# What a watch's last value is before it has heard one: its first value always counts as a
# change, so a rule that speaks your health says it once you log in.
_UNSEEN = object()


@dataclass
class _DataWatch:
    """One interest in the MUD's data (see :meth:`AutomationEngine.add_data_watch`)."""

    path: str  # as given, minus any protocol prefix: the name a watch inside a value reports
    parts: tuple[str, ...]  # the same path casefolded and split on its dots
    protocol: str | None  # "gmcp", "msdp" or "mssp" to hear only that one; None hears all
    callback: Callable[[str, object], None]
    only_changes: bool = False
    source: str = ""
    last: object = _UNSEEN
    group: str = ""  # switched with the rest of its group (set_group_enabled)
    enabled: bool = True  # a group can turn this watch off without unregistering it


def _watch_key(name: object) -> str:
    """A MUD data name as a watch compares it: no protocol prefix, casefolded, dot-joined."""
    parts = [part for part in str(name or "").split(".") if part]
    if len(parts) > 1 and parts[0].casefold() in _DATA_PROTOCOLS:
        parts = parts[1:]
    return ".".join(part.casefold() for part in parts)


def _walk(value: object, parts: tuple[str, ...]) -> tuple[bool, object]:
    """Follow ``parts`` down into nested data: dictionary keys any case, list indexes from 0."""
    for part in parts:
        if isinstance(value, dict):
            found, value = AutomationEngine._mapping_item(value, part)
            if not found:
                return False, None
        elif isinstance(value, (list, tuple)) and part.isdecimal():
            index = int(part)
            if index >= len(value):
                return False, None
            value = value[index]
        else:
            return False, None
    return True, value


def _heard(
    watch: _DataWatch, message: tuple[str, ...], name: str, value: object
) -> tuple[str, object] | None:
    """What a watch hears of one message, as (name, value), or None when it isn't about it.

    A watch on the message, or on a package above it ("Char" for Char.Vitals), hears the
    whole message. A watch on a path inside it ("Char.Vitals.hp") hears the value found
    there, and nothing when this message doesn't carry it.
    """
    if message[: len(watch.parts)] == watch.parts:
        return name, value
    if watch.parts[: len(message)] == message:
        found, inner = _walk(value, watch.parts[len(message) :])
        if found:
            return watch.path, inner
    return None


class AutomationEngine:
    def __init__(self, sink: EngineSink | None = None, *, sound: SoundBus | None = None) -> None:
        self.sink = sink or EngineSink()
        # named timers, findable by name and group
        self._named_timers: list[_NamedTimer] = []
        # timer name -> source, for one-shots that have fired: the manager drops those from
        # the rules it saves, the way MUSHclient's own timer list loses them.
        self.spent_one_shots: dict[str, str] = {}
        self._triggers: list[_Rule] = []
        self._aliases: list[_Rule] = []
        self._keys: dict[str, Callback] = {}  # effective binding (last enabled writer wins)
        # Every binding, in registration order: conflict reports, replay on remove_source, and
        # the enabled flag a group switches.
        self._key_bindings: list[_KeyBinding] = []
        self._vars: dict[str, str] = {}
        self.vars_version = 0  # goes up whenever a variable changes; readers cache by it
        self._gvars: dict[str, str] = {}
        # Latest structured values supplied by the MUD through GMCP/MSDP/MSSP. These stay
        # separate from script variables: a server update must not overwrite a user's setting,
        # while ScriptApi can still resolve either namespace for command templates.
        self._mud_vars: dict[str, object] = {}
        # Who wants to hear when the MUD sends data, and when each name last arrived, which
        # the MUD variables list can sort by.
        self._data_watches: list[_DataWatch] = []
        self._data_times: dict[str, float] = {}
        self.channels = ChannelRouter()  # output routing/policy, scriptable via ScriptApi
        self.sound = sound or SoundBus()  # per-category audio mixing, scriptable via ScriptApi
        self.hub: SessionHub | None = None  # cross-session bus (set by the app)
        self.session_name = ""  # this session's name, for broadcast-exclude
        self.connected = True  # transport state, maintained by the app (packs read it)
        self.diag: DiagnosticLog | None = None  # sound-path trace (set by the app)
        # The player's own sounds folder for this world (set by the window before packs
        # load). Unlike @sppath, which any pack can set, it is trusted for sound paths.
        self.sounds_dir: str | None = None
        # Seconds scripts have spent waiting on the player's answers to dialogs. Every
        # dialect's time limit leaves them out, whichever pack asked.
        self.player_time = 0.0
        # The server line the triggers are working on, so a pack's script can act on it
        # (MUSHclient packs file it under a channel with history_add).
        self.current_line: Line | None = None
        # False while a pack wants incoming lines kept quiet (Miriani-Next's Do Not Disturb).
        self.line_speech = True
        # Each pending timer or other cancellable job: its handle, the source that made it (so a
        # live reload cancels only the retiring set), what to run if it is cancelled instead of
        # firing (see ScriptApi.add_timer), and whether it outlives a disconnect.
        self._timer_handles: list[_PendingTimer] = []
        # The named timers a disconnect paused, so resume_timers() can start them again.
        self._paused_timers: list[_NamedTimer] = []

    # --- registration ---

    def add_trigger(
        self,
        pattern: str,
        callback: Callback | None = None,
        *,
        regex: bool = False,
        priority: int = 0,
        name: str = "",
        gag: bool = False,
        gag_but_display: bool = False,
        omit_from_log: bool = False,
        keep_evaluating: bool = True,
        source: str = "",
        channel: str | None = None,
        stop_scope: str | None = None,
        tiebreak: str = "",
        group: str = "",
    ) -> _Rule:
        rule = _Rule(
            compile_pattern(pattern, regex),
            callback,
            priority,
            name,
            True,
            gag,
            gag_but_display,
            omit_from_log,
            keep_evaluating,
            source=source,
            channel=channel,
            group=group,
            stop_scope=stop_scope,
            tiebreak=tiebreak,
        )
        self._triggers.append(rule)
        self._triggers.sort(key=_rule_order)
        return rule

    def add_alias(
        self,
        pattern: str,
        callback: Callback,
        *,
        regex: bool = False,
        priority: int = 0,
        name: str = "",
        keep_evaluating: bool = False,
        source: str = "",
        tiebreak: str = "",
        stop_scope: str | None = None,
        consumes: bool = False,
        fallback: bool = False,
        group: str = "",
    ) -> _Rule:
        # process_input anchors every alias at the start; a wildcard alias is anchored at the
        # end too, so it has to be the whole command. Regex aliases keep whatever their
        # author wrote.
        rule = _Rule(compile_pattern(pattern, regex, whole=True), callback, priority, name, True,
                     source=source, keep_evaluating=keep_evaluating, tiebreak=tiebreak,
                     stop_scope=stop_scope, consumes=consumes, fallback=fallback,
                     group=group)
        self._aliases.append(rule)
        self._aliases.sort(key=_rule_order)
        return rule

    def add_named_timer(
        self,
        name: str,
        delay: float,
        callback: Callback,
        *,
        every: float | None = None,
        next_delay: Callable[[], float] | None = None,
        group: str = "",
        source: str = "",
        enabled: bool = True,
        while_disconnected: bool = False,
    ) -> _NamedTimer:
        """A timer with a name, so it can be found, switched and cancelled like a rule.

        ``delay`` is the first wait; ``every`` is the wait after each fire (None makes it a
        one-shot). MUSHclient's timer model: a named thing with a group, an on/off switch and
        a send-to action, which a script or a rule can start and stop. ``while_disconnected``
        is its ActiveWhenClosed: the timer keeps its clock through a dropped connection.
        """
        if len(self._named_timers) >= MAX_NAMED_TIMERS and self.named_timer(name) is None:
            # The same ceiling the unnamed timers have: thousands of pending timers starve the
            # event loop, and a script that adds one per line would get there.
            if self.diag is not None:
                self.diag.event(
                    "timer.refused", source=source or "?", limit=MAX_NAMED_TIMERS
                )
            return None
        timer = _NamedTimer(
            self, name, delay, callback, every=every, next_delay=next_delay,
            group=group, source=source, while_disconnected=while_disconnected,
        )
        timer.delay = max(MIN_TIMER_DELAY, float(delay))
        # Replacing a timer by name has to stop the one it replaces: otherwise the old handle
        # keeps firing and nothing can reach it, and mud.add_timer("tick", ...) three times
        # leaves three timers running.
        replaced = self.named_timer(name)
        if replaced is not None:
            replaced.disarm()
        self._named_timers = [t for t in self._named_timers if t.name != name]
        self._named_timers.append(timer)
        timer.enabled = bool(enabled)
        if timer.enabled:
            timer.arm()
        return timer

    def note_spent_one_shot(self, name: str, source: str) -> None:
        """Remember that a one-shot has fired, so the rules it came from can drop it.

        MUSHclient deletes a one-shot timer when it fires, and its world file is written from the
        live list, so the timer is gone from that file at the next save. This is that memory: the
        manager takes the names for its own rules and saves the file without them.
        """
        self.spent_one_shots[str(name)] = str(source)

    def named_timer(self, name: str) -> _NamedTimer | None:
        for timer in self._named_timers:
            if timer.name == name:
                return timer
        return None

    def remove_named_timer(self, name: str) -> bool:
        """Cancel and forget one named timer (MUSHclient's DeleteTimer)."""
        timer = self.named_timer(name)
        if timer is None:
            return False
        timer.disarm()
        self._named_timers = [t for t in self._named_timers if t is not timer]
        return True

    def set_timer_enabled(self, name: str, enabled: bool) -> bool:
        """Switch one named timer on or off (MUSHclient's EnableTimer)."""
        timer = self.named_timer(name)
        if timer is None:
            return False
        timer.set_enabled(enabled)
        return True

    def set_group_enabled(self, group: str, enabled: bool) -> int:
        """Turn every rule in a named group on or off; returns how many changed.

        MUSHclient's EnableGroup, and the reason to put a name on a set of rules: "all the
        combat triggers off" is one call rather than a list the player has to keep in step with
        the rules. A group nothing belongs to is not an error -- it is a group waiting.
        """
        if not group:
            return 0
        changed = 0
        for rule in [*self._triggers, *self._aliases]:
            if rule.group == group:
                rule.enabled = enabled
                changed += 1
        for timer in self._named_timers:
            if timer.group == group and timer.set_enabled(enabled):
                changed += 1
        for binding in self._key_bindings:
            if binding.group == group:
                binding.enabled = enabled
                changed += 1
        self._rebuild_keys()  # a combo whose binding went off falls back to an earlier owner
        for watch in self._data_watches:
            if watch.group == group:
                watch.enabled = enabled
                changed += 1
        return changed

    def is_rule_enabled(self, kind: str, key: object, *, source: str = "") -> bool | None:
        """Whether the engine has this rule switched on now; None if it has no such rule.

        The manager asks this before showing a rule, so a script's EnableTrigger, EnableGroup or
        EnableTimer reaches the place the rule is edited. A switch that only changes what runs is
        half a switch: MUSHclient's dialogs show the same state the engine holds.

        ``kind`` is the manager's word for the rule -- "trigger", "alias", "timer", "key" or
        "data" -- and ``key`` is what identifies it there: a label, a timer's name, a key combo or
        a MUD data name. ``source`` narrows a combo or a data name to one owner's binding, since
        a pack can bind the same one.
        """
        name = str(key or "").strip()
        if not name:
            return None  # unnamed: nothing a script could have switched, so the file is the truth
        if kind in ("trigger", "alias"):
            found = None
            for rule in (self._triggers if kind == "trigger" else self._aliases):
                if rule.name == name:
                    found = rule.enabled  # a later rule of that name wins, as set_enabled does
            return found
        if kind == "timer":
            timer = self.named_timer(name)
            return None if timer is None else timer.enabled
        if kind == "key":
            wanted = name.lower()
            found = None
            for binding in self._key_bindings:
                if binding.key == wanted and (not source or binding.source == source):
                    found = binding.enabled
            return found
        if kind == "data":
            wanted = _watch_key(name)
            found = None
            for watch in self._data_watches:
                if ".".join(watch.parts) == wanted and (
                    not source or watch.source == source
                ):
                    found = watch.enabled
            return found
        return None

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Turn a named trigger, alias or key on or off; True if one had that name.

        MUSHclient's EnableTrigger/EnableAlias/EnableKey, which act by name because a script has
        no other handle on a rule the player made. A rule without a name can only be switched
        from the manager, which is what makes naming one worth doing.
        """
        if not name:
            return False
        found = False
        # Hotkeys are not in this sweep: a binding has no name to be switched by, and
        # iterating the effective dict would hand ``rule.name`` a plain string.
        for rule in [*self._triggers, *self._aliases]:
            if rule.name == name:
                rule.enabled = enabled
                found = True
        if self.set_timer_enabled(name, enabled):
            found = True
        return found

    def remove_trigger(self, name: str) -> None:
        self._triggers = [rule for rule in self._triggers if rule.name != name]

    def remove_alias(self, name: str) -> None:
        self._aliases = [rule for rule in self._aliases if rule.name != name]

    def add_key(
        self, key: str, callback: Callback, *, source: str = "", group: str = ""
    ) -> None:
        """Bind a combo. ``group`` lets one EnableGroup switch it with the other rules."""
        self._key_bindings.append(_KeyBinding(key.lower(), source, callback, group))
        self._rebuild_keys()

    def _rebuild_keys(self) -> None:
        """The effective binding per combo: the last enabled one, in registration order."""
        keys: dict[str, Callback] = {}
        for binding in self._key_bindings:
            if binding.enabled:
                keys[binding.key] = binding.callback
        self._keys = keys  # replaced, not mutated: has_key reads it from another thread

    def remove_key(self, key: str, *, source: str = "") -> None:
        """Drop one source's bindings of ``key``; an earlier owner's binding comes back."""
        key = key.lower()
        self._key_bindings = [
            binding for binding in self._key_bindings
            if (binding.key, binding.source) != (key, source)
        ]
        self._rebuild_keys()

    def registrations_by_source(self) -> dict[str, dict[str, list[str]]]:
        """Every trigger/alias/key grouped by the pack that registered it.

        Tokens are the regex/pattern string (triggers/aliases) or the key combo.
        Drives cross-pack conflict detection; read-only.
        """
        reg: dict[str, dict[str, list[str]]] = {}

        def bucket(source: str) -> dict[str, list[str]]:
            return reg.setdefault(source, {"trigger": [], "alias": [], "key": []})

        for rule in self._triggers:
            bucket(rule.source)["trigger"].append(rule.pattern.pattern)
        for rule in self._aliases:
            bucket(rule.source)["alias"].append(rule.pattern.pattern)
        for binding in self._key_bindings:
            bucket(binding.source)["key"].append(binding.key)
        return reg

    # --- variables ---

    def get_var(self, name: str, default: str | None = "") -> str | None:
        return self._vars.get(name, self._gvars.get(name, default))

    def set_var(self, name: str, value: object) -> None:
        self._vars[name] = str(value)
        self.vars_version += 1

    def remove_source(self, source: str) -> None:
        """Drop every rule and channel policy registered by one reloading source.

        Keys are rebuilt from the surviving bindings in registration order, preserving
        the last-writer-wins the original registrations produced.
        """
        self._triggers = [rule for rule in self._triggers if rule.source != source]
        self._aliases = [rule for rule in self._aliases if rule.source != source]
        if any(binding.source == source for binding in self._key_bindings):
            self._key_bindings = [
                binding for binding in self._key_bindings if binding.source != source
            ]
            # Replay the surviving bindings in order: last writer wins again, and a
            # combo the removed source had shadowed falls back to the earlier owner.
            self._rebuild_keys()
        self._data_watches = [watch for watch in self._data_watches if watch.source != source]
        self.channels.remove_source(source)
        # A named timer is a rule of the reloading source too: cancel it and forget it, or the
        # old one stays in the list looking armed (its handle was cancelled behind its back) and
        # answers "is this rule on?" as if it were still running.
        # What this source's one-shots have already done is no longer news once its rules are
        # written and reloaded.
        self.spent_one_shots = {
            name: origin for name, origin in self.spent_one_shots.items() if origin != source
        }
        retired = [timer for timer in self._named_timers if timer.source == source]
        self._named_timers = [t for t in self._named_timers if t.source != source]
        for timer in retired:
            timer.disarm()
        self.cancel_source_timers(source)

    def delete_var(self, name: str) -> None:
        self._vars.pop(name, None)
        self.vars_version += 1

    def all_vars(self) -> dict[str, str]:
        """A snapshot of session variables (for pack-state persistence)."""
        return dict(self._vars)

    def get_gvar(self, name: str) -> str:
        return self._gvars.get(name, "")

    def delete_gvar(self, name: str) -> None:
        self._gvars.pop(name, None)
        self.vars_version += 1

    def set_gvar(self, name: str, value: object) -> None:
        self._gvars[name] = str(value)
        self.vars_version += 1

    # --- structured MUD variables ---

    @staticmethod
    def _mapping_item(mapping: dict, name: str) -> tuple[bool, object]:
        """Case-insensitive mapping lookup while preserving the server's original keys."""
        if name in mapping:
            return True, mapping[name]
        folded = name.casefold()
        for key, value in mapping.items():
            if str(key).casefold() == folded:
                return True, value
        return False, None

    def set_mud_var(self, name: str, value: object) -> None:
        """Record one latest GMCP/MSDP/MSSP value under its protocol-provided name."""
        if not name:
            return
        found, _value = self._mapping_item(self._mud_vars, name)
        if found:
            # Replace the existing spelling instead of accumulating HEALTH/health duplicates.
            folded = name.casefold()
            old_name = next(key for key in self._mud_vars if key.casefold() == folded)
            self._mud_vars[old_name] = value
        else:
            self._mud_vars[name] = value

    def resolve_mud_var(self, name: str) -> tuple[bool, object]:
        """Resolve ``HEALTH`` or a dotted path such as ``Char.Vitals.hp``.

        GMCP package names already contain dots, so resolution finds the longest matching
        top-level key first, then walks dictionaries (and zero-based list indexes) below it.
        """
        parts = str(name).split(".") if name else []
        for boundary in range(len(parts), 0, -1):
            found, value = self._mapping_item(self._mud_vars, ".".join(parts[:boundary]))
            if not found:
                continue
            for part in parts[boundary:]:
                if isinstance(value, dict):
                    found, value = self._mapping_item(value, part)
                    if not found:
                        return False, None
                elif isinstance(value, (list, tuple)) and part.isdecimal():
                    index = int(part)
                    if index >= len(value):
                        return False, None
                    value = value[index]
                else:
                    return False, None
            return True, value
        return False, None

    def get_mud_var(self, name: str, default: object = None) -> object:
        found, value = self.resolve_mud_var(name)
        return value if found else default

    def all_mud_vars(self) -> dict[str, object]:
        """A shallow snapshot of the latest structured server values."""
        return dict(self._mud_vars)

    # --- data events ---

    def add_data_watch(
        self,
        path: str,
        callback: Callable[[str, object], None],
        *,
        only_changes: bool = False,
        source: str = "",
        group: str = "",
    ) -> _DataWatch:
        """Call ``callback(name, value)`` whenever the MUD sends data at ``path``.

        Modelled on Mudlet's GMCP events. ``path`` is a GMCP package, an MSDP or MSSP
        variable, or a path inside one, in any case; starting it with ``gmcp.``, ``msdp.`` or
        ``mssp.`` hears only that protocol. A package hears every package below it, as
        Mudlet raises gmcp.Char for Char.Vitals. ``only_changes`` skips a value equal to the
        last one this watch heard. Called after the value is stored, so ${mud:...} reads it.
        """
        parts = [part for part in str(path).split(".") if part]
        protocol = None
        if len(parts) > 1 and parts[0].casefold() in _DATA_PROTOCOLS:
            protocol = parts[0].casefold()
            parts = parts[1:]
        if not parts:
            raise ValueError("no MUD data name to watch")
        watch = _DataWatch(
            ".".join(parts), tuple(part.casefold() for part in parts), protocol, callback,
            only_changes, source, group=str(group),
        )
        self._data_watches.append(watch)
        return watch

    def data_arrived(self, protocol: str, name: str, value: object) -> None:
        """Tell the watches that the MUD sent ``name`` over ``protocol``; it's stored already."""
        if not name:
            return
        message = tuple(part.casefold() for part in name.split("."))
        self._data_times[name.casefold()] = time.monotonic()
        for watch in list(self._data_watches):
            if not watch.enabled:
                continue  # its group is switched off for now
            if watch.protocol is not None and watch.protocol != protocol.casefold():
                continue
            heard = _heard(watch, message, name, value)
            if heard is None:
                continue
            heard_name, heard_value = heard
            if watch.only_changes:
                if watch.last is not _UNSEEN and watch.last == heard_value:
                    continue
                watch.last = heard_value
            try:
                watch.callback(heard_name, heard_value)
            except Exception as error:  # noqa: BLE001 - one watch's mistake mustn't stop the rest
                if self.diag is not None:
                    self.diag.event(
                        "data_watch.error", source=watch.source or "?", path=watch.path,
                        error=f"{type(error).__name__}: {error}",
                    )

    def data_arrival_times(self) -> dict[str, float]:
        """When each name last arrived, by its casefolded name, in time.monotonic() seconds."""
        return dict(self._data_times)

    # --- timers ---

    def schedule_timer(
        self,
        delay: float,
        callback: Callable[[], None],
        *,
        source: str = "",
        on_cancel: Callable[[], None] | None = None,
        while_disconnected: bool = False,
    ) -> object | None:
        """Schedule a timer through the sink and track its handle.

        Tracked so :meth:`cancel_timers` can drop everything still pending at session close;
        a one-shot cue timer that outlives its tab would otherwise fire and start audio with no
        session left to stop it. ``on_cancel`` runs if it's cancelled instead of firing, and
        ``while_disconnected`` keeps it through a dropped connection.
        """
        handle = self.sink.schedule(delay, callback)
        if handle is not None:
            self._timer_handles.append(
                _PendingTimer(handle, source, on_cancel, while_disconnected)
            )
        return handle

    def track_pending(self, handle: object, *, source: str = "") -> None:
        """Hold other cancellable pending work (a sound still downloading) with the timers,
        so session teardown and a source's reload cancel it the same way."""
        self._timer_handles.append(_PendingTimer(handle, source))

    def discard_timer(self, handle: object) -> None:
        """Forget a handle once it has fired, so the tracking list can't grow unbounded."""
        self._timer_handles = [entry for entry in self._timer_handles if entry.handle is not handle]

    def cancel_source_timers(self, source: str) -> None:
        """Cancel pending timers registered by one retiring script/pack source."""
        cancelled = [entry for entry in self._timer_handles if entry.source == source]
        self._timer_handles = [entry for entry in self._timer_handles if entry.source != source]
        for entry in cancelled:
            _cancel_pending(entry)

    def cancel_timers(self) -> None:
        """Cancel every still-pending timer (session teardown)."""
        cancelled, self._timer_handles = self._timer_handles, []
        self._paused_timers = []
        for entry in cancelled:
            _cancel_pending(entry)

    def suspend_timers(self) -> None:
        """A dropped connection: stop the clocks, except the ones asked to keep going.

        A timer that did not ask to outlive the connection is paused -- dropped now, armed again
        by :meth:`resume_timers` -- so its schedule survives the drop instead of vanishing with
        the socket. Everything anonymous (a one-shot ``mud.timer``, a sound still downloading)
        has nothing to resume into, so it is cancelled as it always was.
        """
        newly_paused = [
            timer for timer in self._named_timers
            if timer.enabled and timer.armed and not timer.while_disconnected
        ]
        for timer in newly_paused:
            timer.disarm()  # takes its handle out of the tracking list too
        # Added, never replaced: a world that says it dropped twice must not lose the timers the
        # first disconnect paused.
        self._paused_timers.extend(newly_paused)
        cancelled = [entry for entry in self._timer_handles if not entry.while_disconnected]
        self._timer_handles = [entry for entry in self._timer_handles if entry.while_disconnected]
        for entry in cancelled:
            _cancel_pending(entry)

    def resume_timers(self) -> None:
        """The world is back: start the paused timers again, each with its wait from now."""
        paused, self._paused_timers = self._paused_timers, []
        for timer in paused:
            timer.arm()

    # --- evaluation ---

    def process_line(self, line: Line) -> Line:
        """Run triggers against an incoming line; mutate gag flags in place."""
        previous, self.current_line = self.current_line, line
        try:
            return self._run_triggers(line)
        finally:
            self.current_line = previous

    def _run_triggers(self, line: Line) -> Line:
        stopped: set[str] = set()  # scopes a trigger has stopped evaluating for this line
        for rule in list(self._triggers):
            if not rule.enabled or rule.stop_scope in stopped:
                continue
            pattern = rule.pattern
            if rule.screened is not pattern:
                rule.required = required_text(pattern)
                rule.screened = pattern
            if rule.required is not None and rule.required not in line.plain_text:
                continue  # it can't match: the line lacks text every match has
            try:
                match = pattern.search(line.plain_text, **_MATCH_KWARGS)
            except _MatchTimeout:
                rule.enabled = False  # a pattern that backtracks past the budget is disabled
                rule.timed_out = True
                if self.diag is not None:
                    # A trigger silently disabling itself is a soundpack going quiet -- trace it.
                    self.diag.event("trigger.timeout_disabled", source=rule.source or "?",
                                    pattern=rule.pattern.pattern, line=line.plain_text)
                continue
            if match is None:
                continue
            if self.diag is not None:
                self.diag.event(
                    "trigger.fire", source=rule.source or "?", pattern=rule.pattern.pattern,
                    line=line.plain_text,
                )
            if rule.channel is not None:
                line.channel = rule.channel
            if rule.gag or rule.gag_but_display:
                line.gagged = True
                line.display_when_gagged = rule.gag_but_display
            if rule.omit_from_log:
                line.omit_from_log = True
            if rule.callback is not None:
                rule.callback(MatchContext(line, _wildcards(match), _named(match), self))
            if not rule.keep_evaluating:
                if rule.stop_scope is None:
                    break
                stopped.add(rule.stop_scope)
        return line

    def process_input(self, text: str) -> list[str]:
        """Run aliases against a user input line.

        Returns the lines to actually send: ``[text]`` when no alias matches, or
        ``[]`` when an alias consumes the input (its callback performs the sends).
        """
        stopped: set[str] = set()  # scopes an alias has stopped (see _Rule.stop_scope)
        consumed = False
        for rule in list(self._aliases):
            if not rule.enabled or rule.stop_scope in stopped or (rule.fallback and consumed):
                continue
            try:
                match = rule.pattern.match(text, **_MATCH_KWARGS)
            except _MatchTimeout:
                rule.enabled = False  # a pattern that backtracks past the budget is disabled
                rule.timed_out = True
                if self.diag is not None:
                    self.diag.event("alias.timeout_disabled", source=rule.source or "?",
                                    pattern=rule.pattern.pattern, line=text)
                continue
            if match is None:
                continue
            if rule.callback is not None:
                rule.callback(MatchContext(Line(text), _wildcards(match), _named(match), self))
            consumed = consumed or rule.consumes
            if not rule.keep_evaluating:
                if rule.stop_scope is None:
                    return []
                stopped.add(rule.stop_scope)
                consumed = True
        return [] if consumed else [text]

    def press_key(self, key: str) -> bool:
        if not self.has_key(key):
            return False
        self._keys[key.lower()](MatchContext(Line(""), [], {}, self))
        return True

    def has_key(self, key: str) -> bool:
        """Whether a macro is bound to this combo.

        The UI asks this BEFORE swallowing a keypress: an unbound combo must fall
        through to the platform (menu access keys, accelerators) instead of dying
        in the macro path. Read cross-thread; dict membership is safe under the GIL.
        """
        callback = self._keys.get(key.lower())
        # A binding can be switched off for now (a VIPMud key whose class is disabled), and
        # then the keypress belongs to the platform again.
        return callback is not None and getattr(callback, "active", lambda: True)()
