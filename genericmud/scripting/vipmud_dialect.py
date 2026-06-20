"""VIPMud ``.set`` scripting dialect — clean-room interpreter.

VIPMud's language is macro-substitution plus a small command set, documented
publicly (Order of Chaos wiki). It is not a general-purpose language, so a
dedicated interpreter targeting the shared :class:`AutomationEngine` is small and
sufficient. This lets existing VIPMud soundpacks run alongside Lua packs.

Definition commands (load time):
    #TRIGGER <pat> {body}   /  #TR <pat> {body}   /  #GTRIGGER <pat> {body}
    #ALIAS <pat> {body}     #KEY <key> {body}     #LOAD {file} [noremove]
Action commands (fire time, inside a body):
    #SAY {text}   #PLAY {file} [vol]   #VAR <name> {value}   #STOP
    bare text -> sent to the MUD
Substitution at fire time: ``@var`` -> variable, ``%0..%9`` -> wildcard captures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi

_DEFINITIONS = {"TRIGGER", "TR", "GTRIGGER", "ALIAS", "KEY", "LOAD"}
_VAR_RE = re.compile(r"@(\w+)")
_WILDCARD_RE = re.compile(r"%(\d)")
_DEFAULT_VOLUME = 100


@dataclass
class _Tok:
    braced: bool
    text: str


def _scan_block(source: str, i: int) -> tuple[str, int]:
    """``source[i]`` is ``{``; return (inner text, index after matching ``}``)."""
    depth = 0
    start = i + 1
    j = i
    n = len(source)
    while j < n:
        c = source[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start:j], j + 1
        j += 1
    return source[start:], n  # unbalanced: take the rest


def tokenize_statements(source: str) -> list[tuple[str | None, list[_Tok]]]:
    """Split a .set source (or a command body) into statements.

    Statements are newline/``;`` separated, but a ``{...}`` block may span
    newlines. ``//`` begins a line comment. A statement is ``(command, args)``;
    ``command`` is ``None`` for a bare line of text to send.
    """
    statements: list[tuple[str | None, list[_Tok]]] = []
    i = 0
    n = len(source)
    while i < n:
        while i < n and source[i] in " \t\r\n;":
            i += 1
        if i >= n:
            break
        if source.startswith("//", i):
            while i < n and source[i] != "\n":
                i += 1
            continue
        if source[i] == "#":
            i += 1
            start = i
            while i < n and source[i] not in " \t\r\n{":
                i += 1
            command = source[start:i].upper()
            args: list[_Tok] = []
            while i < n:
                while i < n and source[i] in " \t":
                    i += 1
                if i >= n or source[i] in "\r\n":
                    break
                if source[i] == "{":
                    inner, i = _scan_block(source, i)
                    args.append(_Tok(True, inner))
                else:
                    word_start = i
                    while i < n and source[i] not in " \t\r\n{":
                        i += 1
                    args.append(_Tok(False, source[word_start:i]))
            statements.append((command, args))
        else:
            line_start = i
            while i < n and source[i] not in "\r\n":
                i += 1
            text = source[line_start:i].strip()
            if text:
                statements.append((None, [_Tok(False, text)]))
    return statements


class VipMudPack:
    """A loaded VIPMud soundpack/script, registered against a :class:`ScriptApi`."""

    def __init__(self, api: ScriptApi) -> None:
        self._api = api

    def load_source(self, source: str) -> None:
        for command, args in tokenize_statements(source):
            if command in _DEFINITIONS:
                self._define(command, args)
            else:
                self._execute_statement(command, args, [])

    def _define(self, command: str, args: list[_Tok]) -> None:
        if not args:
            return
        if command in ("TRIGGER", "TR", "GTRIGGER"):
            pattern, body = args[0].text, args[-1].text
            self._api.add_trigger(pattern, self._runner(body), regex=False)
        elif command == "ALIAS":
            pattern, body = args[0].text, args[-1].text
            self._api.add_alias(pattern, self._runner(body), regex=False)
        elif command == "KEY":
            key, body = args[0].text, args[-1].text
            self._api.add_key(key, self._runner(body))
        # #LOAD intentionally deferred: file resolution is wired when packs ship.

    def _runner(self, body: str):
        def run(ctx: MatchContext) -> None:
            self._execute_body(body, ctx.wildcards)

        return run

    def _execute_body(self, body: str, wildcards: list[str]) -> None:
        for command, args in tokenize_statements(body):
            self._execute_statement(command, args, wildcards)

    def _execute_statement(
        self, command: str | None, args: list[_Tok], wildcards: list[str]
    ) -> None:
        if not args:
            return
        if command is None or command == "SEND":
            self._api.send(self._subst(args[-1].text, wildcards))
        elif command == "SAY":
            self._api.speak(self._subst(args[-1].text, wildcards))
        elif command == "PLAY":
            volume = _DEFAULT_VOLUME
            if len(args) > 1 and args[1].text.isdigit():
                volume = int(args[1].text)
            self._api.play(self._subst(args[0].text, wildcards), gain=volume / 100)
        elif command == "VAR" and len(args) > 1:
            self._api.set_var(args[0].text, self._subst(args[1].text, wildcards))
        elif command == "STOP":
            self._api.stop()
        # Unknown #commands are ignored rather than blindly sent to the MUD.

    def _subst(self, text: str, wildcards: list[str]) -> str:
        text = _VAR_RE.sub(lambda m: self._api.get_var(m.group(1)), text)

        def wildcard(match: re.Match[str]) -> str:
            index = int(match.group(1))
            return wildcards[index] if index < len(wildcards) else ""

        return _WILDCARD_RE.sub(wildcard, text)
