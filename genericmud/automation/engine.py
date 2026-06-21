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

from genericmud.automation.channels import ChannelRouter
from genericmud.model.buffer import Line


class EngineSink:
    """Side-effect surface the engine drives. Real wiring overrides these."""

    def send(self, text: str) -> None: ...
    def echo(self, text: str, channel: str = "main") -> None: ...
    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None: ...
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
        return re.compile(pattern)
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("(.*)")
        elif ch == "?":
            out.append("(.)")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out))


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
    def __init__(self, sink: EngineSink | None = None) -> None:
        self.sink = sink or EngineSink()
        self._triggers: list[_Rule] = []
        self._aliases: list[_Rule] = []
        self._keys: dict[str, Callback] = {}
        self._vars: dict[str, str] = {}
        self._gvars: dict[str, str] = {}
        self.channels = ChannelRouter()  # output routing/policy, scriptable via ScriptApi

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

    def add_key(self, key: str, callback: Callback) -> None:
        self._keys[key.lower()] = callback

    # --- variables ---

    def get_var(self, name: str) -> str:
        return self._vars.get(name, self._gvars.get(name, ""))

    def set_var(self, name: str, value: object) -> None:
        self._vars[name] = str(value)

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
            match = rule.pattern.search(line.plain_text)
            if match is None:
                continue
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
            match = rule.pattern.match(text)
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
