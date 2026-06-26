"""VIPMud ``.set`` scripting dialect — clean-room interpreter.

VIPMud's language is macro-substitution plus a small command set, documented
publicly (Order of Chaos wiki). It is not a general-purpose language, so a
dedicated interpreter targeting the shared :class:`AutomationEngine` is small and
sufficient. This lets existing VIPMud soundpacks run alongside Lua packs.

Definition commands (load time):
    #TRIGGER <pat> {body}  / #TR <pat> {body}  / #GTRIGGER <pat> {body}  [{} AnyCase]
    #ALIAS <pat> {body}    #KEY <key> {body}
Action commands (fire time, inside a body):
    #SAY {text} [mode]   #PLAY {file} [vol]   #PLAYLOOP {file} [vol]
    #VAR <name> {value}  #GVAR <name> {value}   #STOP
    #IF {cond} {then} [{else}]
    #PC <handle> stop|volume N|pan N|frequency N
    bare text -> sent to the MUD;  bare ``@name = value`` -> variable assignment

Patterns use VIPMud wildcards: ``*`` (any run), ``?`` (one char), and named
wildcards ``&{name}`` / ``&name`` exposed at fire time as ``@name``.
Substitution at fire time: ``@var`` -> variable, ``%0..%9`` -> positional capture,
``%playhandle`` -> the id of the most recently started cue.

Server-controlled packs (e.g. Cosmic Rage) run through this: the MUD streams a
``$sphook play:path:vol:...`` line, the trigger captures the fields as ``@action``
etc., and ``#if`` dispatches to ``#play``/``#playloop``. Unimplemented commands
(``#math``, the ``%function()`` library, ``#wait``, file I/O, ``#Configure``) are
ignored, so a pack loads partially but its sound core works.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from genericmud.automation.engine import MatchContext
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi

_DEFINITIONS = {"TRIGGER", "TR", "GTRIGGER", "ALIAS", "KEY"}
_VAR_RE = re.compile(r"@(\w+)")
_WILDCARD_RE = re.compile(r"%(\d)")
_FORALL_VAR_RE = re.compile(r"%[Ii]\b")  # #ForAll loop variable (%I / %i), expanded per item
_SOUND_VARIANT_RE = re.compile(r"\*(\d+)(\.\w+)$")  # "name*N.ext": one random variant of 1..N
# Named/positional wildcards inside a .set pattern: &{name}, &name, *, ?
_PATTERN_WILDCARD_RE = re.compile(r"&\{(\w+)\}|&(\w+)|(\*)|(\?)")
# A bare "@name = value" statement is an assignment, not text to send.
_ASSIGN_RE = re.compile(r"^@(\w+)\s*=\s*(.*)$")
# A single comparison for #if: left <op> right (longer operators first).
_CONDITION_RE = re.compile(r"^(.*?)\s*(<=|>=|<|>|=)\s*(.*)$")
_DEFAULT_VOLUME = 100
_MASTER_HANDLE = "0"  # VIPMud handle 0 == all cues / master volume
_PLAY_HANDLE_TOKEN = "%playhandle"


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
            while i < n and source[i] not in " \t\r\n;{":  # ; ends a bare command (e.g. #stop;)
                i += 1
            command = source[start:i].upper()
            args: list[_Tok] = []
            while i < n:
                while i < n and source[i] in " \t":
                    i += 1
                if i >= n or source[i] in "\r\n;":  # ; ends a statement (same as newline)
                    break
                if source[i] == "{":
                    inner, i = _scan_block(source, i)
                    args.append(_Tok(True, inner))
                else:
                    word_start = i
                    while i < n and source[i] not in " \t\r\n;{":
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


def vip_pattern_to_regex(pattern: str) -> str:
    """Convert a VIPMud wildcard pattern to a regex with named/numbered captures.

    ``&{name}``/``&name`` -> a named group (read at fire time as ``@name``); ``*`` ->
    a numbered group; ``?`` -> one char. A wildcard at the very end takes the rest
    (greedy); a bounded one is non-greedy so the following literal delimits it.
    """
    out: list[str] = []
    last = 0
    for match in _PATTERN_WILDCARD_RE.finditer(pattern):
        out.append(re.escape(pattern[last : match.start()]))
        body = ".*" if match.end() == len(pattern) else ".*?"
        name = match.group(1) or match.group(2)
        if name:
            out.append(f"(?P<{name}>{body})")
        elif match.group(3):  # *
            out.append(f"({body})")
        else:  # ?
            out.append("(.)")
        last = match.end()
    out.append(re.escape(pattern[last:]))
    return "".join(out)


def _to_int(value: str, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _as_number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _expand_sound_variant(path: str) -> str:
    """VIPMud plays a random variant of ``name*N.ext`` — one of ``name1.ext``..``nameN.ext``.

    Expand to a concrete variant chosen at random (the source of the pack's sound variety);
    a path without the ``*N`` marker passes through unchanged.
    """
    match = _SOUND_VARIANT_RE.search(path)
    if not match:
        return path
    count = int(match.group(1))
    pick = random.randint(1, count) if count >= 1 else 1
    return path[: match.start()] + str(pick) + match.group(2)


class _Abort(Exception):  # noqa: N818 - a control-flow signal (#Abort), not an error condition
    """Raised by ``#Abort`` to stop the current script body (a trigger fire or a load)."""


class VipMudPack:
    """A loaded VIPMud soundpack/script, registered against a :class:`ScriptApi`."""

    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._base_dir = api.base_dir
        self._loaded: set[str] = set()  # files already #load-ed (cycle/dup guard)
        self._handles: dict[str, str] = {}  # VIPMud play handle -> sound bus channel
        self._next_handle = 1
        self._last_handle = _MASTER_HANDLE
        # Server-controlled packs build sound paths from @sppath; the pack's own
        # settings loader (file I/O, deferred) normally sets it. Default it (and the
        # script path) to the pack dir so @sppath/x.wav resolves to bundled sounds.
        # Default @sppath/@scpath to the pack dir, but don't clobber a value the session
        # pre-set from world.sounds (so a world can point at its own sound folder).
        base = api.base_dir
        if base and not api.get_var("sppath"):
            api.set_var("sppath", base)
        if base and not api.get_var("scpath"):
            api.set_var("scpath", base)

    def load_source(self, source: str) -> None:
        for command, args in tokenize_statements(source):
            if command in _DEFINITIONS:
                self._define(command, args)
            else:
                try:
                    self._execute_statement(command, args, [])
                except _Abort:
                    return  # a top-level #Abort stops loading the rest of this script

    def _define(self, command: str, args: list[_Tok]) -> None:
        if not args:
            return
        body = args[1].text if len(args) > 1 else ""  # {body} is the first block after the pattern
        if command in ("TRIGGER", "TR", "GTRIGGER"):
            flags = "(?i)" if any(a.text.lower() == "anycase" for a in args[2:]) else ""
            self._api.add_trigger(
                flags + vip_pattern_to_regex(args[0].text), self._runner(body), regex=True
            )
        elif command == "ALIAS":
            self._api.add_alias(vip_pattern_to_regex(args[0].text), self._runner(body), regex=True)
        elif command == "KEY":
            self._api.add_key(args[0].text, self._runner(body))

    def _runner(self, body: str):
        def run(ctx: MatchContext) -> None:
            for name, value in ctx.named.items():  # named wildcards become @vars
                self._api.set_var(name, value or "")
            try:
                self._execute_body(body, ctx.wildcards, line=ctx.line)
            except _Abort:
                pass  # #Abort: stop this trigger body early, leaving prior effects in place

        return run

    def _execute_body(self, body: str, wildcards: list[str], line: Line | None = None) -> None:
        for command, args in tokenize_statements(body):
            self._execute_statement(command, args, wildcards, line)

    def _execute_statement(
        self, command: str | None, args: list[_Tok], wildcards: list[str], line: Line | None = None
    ) -> None:
        if command is None or command == "SEND":
            self._bare_or_send(args, wildcards)
        elif command == "SAY" and args:
            self._api.speak(self._subst(args[0].text, wildcards))
        elif command in ("PLAY", "PLAYLOOP") and args:
            self._play(args, wildcards, loop=command == "PLAYLOOP")
        elif command in ("VAR", "GVAR") and len(args) > 1:
            name = self._subst(args[0].text, wildcards)  # @-indirect target supported
            self._api.set_var(name, self._subst(args[1].text, wildcards))
        elif command == "STOP":
            self._api.stop()
        elif command == "IF" and args:
            self._execute_if(args, wildcards, line)
        elif command == "FORALL" and len(args) > 1:
            self._execute_forall(args, wildcards, line)
        elif command == "ALARM" and len(args) > 1:
            self._execute_alarm(args, wildcards)
        elif command == "GAGLINE" and line is not None:
            # "#gagline [count] voice" gags self-voice but keeps the line reviewable; without a
            # "voice" arg ("all", a bare count, or nothing) it removes the line entirely. Packs
            # play a sound in the gagged line's place. (SC writes "voice"; Prometheus "1 Voice".)
            line.gagged = True
            line.display_when_gagged = any(a.text.lower() == "voice" for a in args)
        elif command == "ABORT":
            raise _Abort
        elif command == "PC" and len(args) > 1:
            self._execute_pc(args, wildcards)
        elif command == "LOAD" and args:
            self._load_file(self._subst(args[0].text, wildcards))
        # Unknown #commands are ignored rather than sent to the MUD (Phase 2 features).

    def _load_file(self, reference: str) -> None:
        """``#LOAD {file}`` — load another ``.set`` from the pack so a loader script
        pulls in all the pack's scripts. Confined to the pack dir; each file loads once."""
        if not self._base_dir:
            return
        base = Path(self._base_dir).resolve()
        target = Path(reference.replace("\\", "/"))
        if not target.name:
            return
        candidate = target if target.is_absolute() else base / target
        try:
            real = candidate.resolve()
        except OSError:
            return
        if not (real.is_file() and real.is_relative_to(base)):
            # Layouts vary: match by filename. Escape glob metachars so a literal name like
            # "a[1].set" isn't read as a pattern, and re-confirm each hit stays in the pack
            # (rglob can surface a symlink or directory that escapes base).
            real = None
            wanted = target.name.lower()  # Windows-authored packs are careless about case
            for match in sorted(base.rglob("*")):
                if match.name.lower() != wanted:
                    continue
                try:
                    resolved = match.resolve()
                except OSError:
                    continue
                if resolved.is_file() and resolved.is_relative_to(base):
                    real = resolved
                    break
        if real is None:
            return
        key = str(real)
        if key in self._loaded:  # don't re-load (handles #load cycles + duplicates)
            return
        self._loaded.add(key)
        self.load_source(real.read_text(encoding="latin-1", errors="ignore"))

    def _bare_or_send(self, args: list[_Tok], wildcards: list[str]) -> None:
        if not args:
            return
        text = args[-1].text
        assignment = _ASSIGN_RE.match(text)
        if assignment:  # "@name = value" sets a variable; it isn't sent to the MUD
            self._api.set_var(assignment.group(1), self._subst(assignment.group(2), wildcards))
        else:
            self._api.send(self._subst(text, wildcards))

    def _play(self, args: list[_Tok], wildcards: list[str], *, loop: bool) -> None:
        file = _expand_sound_variant(self._subst(args[0].text, wildcards))
        volume = _DEFAULT_VOLUME
        if len(args) > 1:
            volume = _to_int(self._subst(args[1].text, wildcards), _DEFAULT_VOLUME)
        handle = str(self._next_handle)
        self._next_handle += 1
        channel = f"vip-{handle}"  # one channel per cue so #pc can target it later
        self._handles[handle] = channel
        self._last_handle = handle
        self._api.play(file, channel=channel, gain=volume / 100, loop=loop)

    def _execute_if(self, args: list[_Tok], wildcards: list[str], line: Line | None = None) -> None:
        branch = 1 if self._eval_condition(args[0].text, wildcards) else 2  # then : else
        if len(args) > branch:
            self._execute_body(args[branch].text, wildcards, line)

    def _execute_forall(
        self, args: list[_Tok], wildcards: list[str], line: Line | None = None
    ) -> None:
        """``#ForAll {a|b|c} {body}`` — run ``body`` once per ``|``-separated item with the loop
        token ``%I`` replaced by the item. VIPMud loaders use it to pull in every script, e.g.
        ``#ForAll {combat|ground|...} {#load {Scripts\\%I.set}}``."""
        body = args[1].text
        for item in self._subst(args[0].text, wildcards).split("|"):
            item = item.strip()
            if item:
                expanded = _FORALL_VAR_RE.sub(lambda _m, it=item: it, body)
                self._execute_body(expanded, wildcards, line)

    def _execute_alarm(self, args: list[_Tok], wildcards: list[str]) -> None:
        """``#alarm <delay> {body}`` — run ``body`` after ``delay`` seconds (a VIPMud timer).

        Packs defer loading the rest of the pack until the login line arrives, via
        ``#alarm 0 {#load ...}`` fired from a login trigger. Cancel/named forms (``#alarm -1``,
        ``#unalarm``) aren't modelled. Fires only when an event loop drives the engine's
        scheduler (the live app); under a no-op scheduler the timer simply never runs.
        """
        delay = _as_number(self._subst(args[0].text, wildcards))
        if delay is None or delay < 0:
            return
        body = args[1].text
        self._api.add_timer(delay, lambda: self._execute_body(body, []))

    def _eval_condition(self, condition: str, wildcards: list[str]) -> bool:
        match = _CONDITION_RE.match(self._subst(condition, wildcards).strip())
        if not match:
            return False  # OR/AND/%function conditions are Phase 2 -> treat as false
        left, op, right = _unquote(match.group(1)), match.group(2), _unquote(match.group(3))
        if op == "=":
            return left.lower() == right.lower()
        left_n, right_n = _as_number(left), _as_number(right)
        if left_n is None or right_n is None:
            return False
        return {"<": left_n < right_n, ">": left_n > right_n,
                "<=": left_n <= right_n, ">=": left_n >= right_n}[op]

    def _execute_pc(self, args: list[_Tok], wildcards: list[str]) -> None:
        handle = self._subst(args[0].text, wildcards).strip()
        action = args[1].text.lower()
        value = self._subst(args[2].text, wildcards) if len(args) > 2 else ""
        if handle == _MASTER_HANDLE:  # 0 controls all cues / the master gain
            if action == "stop":
                self._api.flush()
            elif action == "volume":
                self._api.set_master(_to_int(value, _DEFAULT_VOLUME) / 100)
            return
        channel = self._handles.get(handle)
        if channel is None:
            return
        if action == "stop":
            self._api.stop(channel)
        elif action == "volume":
            self._api.set_volume(channel, _to_int(value, _DEFAULT_VOLUME) / 100)
        # pan / frequency: no live per-cue control in the bus yet — accepted, ignored.

    def _subst(self, text: str, wildcards: list[str]) -> str:
        text = _VAR_RE.sub(lambda m: self._api.get_var(m.group(1)), text)
        text = text.replace(_PLAY_HANDLE_TOKEN, self._last_handle)

        def wildcard(match: re.Match[str]) -> str:
            index = int(match.group(1))
            return wildcards[index] if index < len(wildcards) else ""

        return _WILDCARD_RE.sub(wildcard, text)
