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
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from genericmud.automation.channels import ChannelRouter
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


class EngineSink:
    """Side-effect surface the engine drives. Real wiring overrides these."""

    def send(self, text: str) -> None: ...
    def send_packet(self, data: bytes) -> None: ...
    def echo(self, text: str, channel: str = "main") -> None: ...
    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None: ...
    def stop_speech(self) -> None: ...
    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
    ) -> None: ...
    def stop(self, channel: str) -> None: ...
    def music(self, file: str, channel: str = "music") -> None: ...
    def schedule(self, delay: float, callback: Callable[[], None]) -> None: ...


def compile_pattern(pattern: str, regex: bool) -> re.Pattern[str]:
    """Compile a trigger/alias pattern.

    ``regex=True`` is used verbatim. Otherwise it's a wildcard pattern where
    ``*`` captures any run and ``?`` captures one char (VIPMud/MUSHclient
    convention); each wildcard becomes a numbered capture group.
    """
    if regex:
        return _matcher.compile(pattern)
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("(.*)")
        elif ch == "?":
            out.append("(.)")
        else:
            out.append(re.escape(ch))
    return _matcher.compile("".join(out))


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
    keep_evaluating: bool = True
    source: str = ""
    channel: str | None = None


def _wildcards(match: re.Match[str]) -> list[str]:
    return [match.group(0), *(g if g is not None else "" for g in match.groups())]


class AutomationEngine:
    def __init__(self, sink: EngineSink | None = None, *, sound: SoundBus | None = None) -> None:
        self.sink = sink or EngineSink()
        self._triggers: list[_Rule] = []
        self._aliases: list[_Rule] = []
        self._keys: dict[str, Callback] = {}  # effective binding (last writer wins)
        # (key, source, callback) log: conflict reports + replay-rebuild on remove_source.
        self._key_bindings: list[tuple[str, str, Callback]] = []
        self._vars: dict[str, str] = {}
        self._gvars: dict[str, str] = {}
        self.channels = ChannelRouter()  # output routing/policy, scriptable via ScriptApi
        self.sound = sound or SoundBus()  # per-category audio mixing, scriptable via ScriptApi
        self.hub: SessionHub | None = None  # cross-session bus (set by the app)
        self.session_name = ""  # this session's name, for broadcast-exclude
        self.connected = True  # transport state, maintained by the app (packs read it)
        self.diag: DiagnosticLog | None = None  # sound-path trace (set by the app)

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
        keep_evaluating: bool = True,
        source: str = "",
        channel: str | None = None,
    ) -> None:
        self._triggers.append(
            _Rule(
                compile_pattern(pattern, regex),
                callback,
                priority,
                name,
                True,
                gag,
                gag_but_display,
                keep_evaluating,
                source,
                channel,
            )
        )
        self._triggers.sort(key=lambda r: -r.priority)

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
    ) -> None:
        self._aliases.append(
            _Rule(compile_pattern(pattern, regex), callback, priority, name, True, source=source,
                  keep_evaluating=keep_evaluating)
        )
        self._aliases.sort(key=lambda r: -r.priority)

    def remove_trigger(self, name: str) -> None:
        self._triggers = [rule for rule in self._triggers if rule.name != name]

    def remove_alias(self, name: str) -> None:
        self._aliases = [rule for rule in self._aliases if rule.name != name]

    def add_key(self, key: str, callback: Callback, *, source: str = "") -> None:
        self._keys[key.lower()] = callback
        self._key_bindings.append((key.lower(), source, callback))

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
        for key, source, _callback in self._key_bindings:
            bucket(source)["key"].append(key)
        return reg

    # --- variables ---

    def get_var(self, name: str, default: str | None = "") -> str | None:
        return self._vars.get(name, self._gvars.get(name, default))

    def set_var(self, name: str, value: object) -> None:
        self._vars[name] = str(value)

    def remove_source(self, source: str) -> None:
        """Drop every trigger/alias/key a source registered (live rule-editor reload).

        Keys are rebuilt from the surviving bindings in registration order, preserving
        the last-writer-wins the original registrations produced.
        """
        self._triggers = [rule for rule in self._triggers if rule.source != source]
        self._aliases = [rule for rule in self._aliases if rule.source != source]
        if any(bind_source == source for _key, bind_source, _cb in self._key_bindings):
            self._key_bindings = [b for b in self._key_bindings if b[1] != source]
            # Replay the surviving bindings in order: last writer wins again, and a
            # combo the removed source had shadowed falls back to the earlier owner.
            self._keys = {key: callback for key, _source, callback in self._key_bindings}

    def delete_var(self, name: str) -> None:
        self._vars.pop(name, None)

    def all_vars(self) -> dict[str, str]:
        """A snapshot of session variables (for pack-state persistence)."""
        return dict(self._vars)

    def get_gvar(self, name: str) -> str:
        return self._gvars.get(name, "")

    def set_gvar(self, name: str, value: object) -> None:
        self._gvars[name] = str(value)

    # --- evaluation ---

    def process_line(self, line: Line) -> Line:
        """Run triggers against an incoming line; mutate gag flags in place."""
        for rule in list(self._triggers):
            if not rule.enabled:
                continue
            try:
                match = rule.pattern.search(line.plain_text, **_MATCH_KWARGS)
            except _MatchTimeout:
                rule.enabled = False  # a pattern that backtracks past the budget is disabled
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
            if rule.callback is not None:
                rule.callback(MatchContext(line, _wildcards(match), match.groupdict(), self))
            if not rule.keep_evaluating:
                break
        return line

    def process_input(self, text: str) -> list[str]:
        """Run aliases against a user input line.

        Returns the lines to actually send: ``[text]`` when no alias matches, or
        ``[]`` when an alias consumes the input (its callback performs the sends).
        """
        for rule in list(self._aliases):
            if not rule.enabled:
                continue
            try:
                match = rule.pattern.match(text, **_MATCH_KWARGS)
            except _MatchTimeout:
                rule.enabled = False  # a pattern that backtracks past the budget is disabled
                if self.diag is not None:
                    self.diag.event("alias.timeout_disabled", source=rule.source or "?",
                                    pattern=rule.pattern.pattern, line=text)
                continue
            if match is None:
                continue
            if rule.callback is not None:
                rule.callback(MatchContext(Line(text), _wildcards(match), match.groupdict(), self))
            if not rule.keep_evaluating:
                return []
        return [text]

    def press_key(self, key: str) -> bool:
        callback = self._keys.get(key.lower())
        if callback is None:
            return False
        callback(MatchContext(Line(""), [], {}, self))
        return True
