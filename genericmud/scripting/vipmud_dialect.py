"""VIPMud ``.set`` scripting dialect: a clean-room interpreter.

VIPMud's language is text substitution plus a command set, documented in the VIPMud 2.0
manual. Statements are ``#commands`` and plain lines of text, separated by newlines or
semicolons; ``{...}`` groups a parameter, and ``~`` makes the next character ordinary.

Before a command runs, its parameters are expanded once: ``@name`` and ``@{name}`` read a
variable (``@{@name}`` reads it indirectly), ``%1``..``%99`` the rule's wildcards,
``%name(...)`` calls a built-in function and ``%name`` reads a system variable. What
substitution inserts is not expanded again in the same pass, which is how packs keep a
literal ``@``: ``~@`` comes through one pass as ``@``, and ``#Exec`` runs its expanded text
as commands, a second pass.

Conditions (``#If``, ``#While``, ``#Math``, ``%Eval``) are expressions with the manual's
operators. A variable or wildcard goes into one as its text, quotes and all, as VIPMud
substitutes before it evaluates; a function's result is one value; other text is a string.

Triggers (``#Trigger {pattern} {commands} [class] [AnyCase]``) match the whole line: ``*``
takes any run and numbers it, ``&{name}`` takes one into a variable, ``?`` is one
character, ``[...]`` a character class. Aliases take the first word typed and keys a key
name. A rule in a class works only while the class is enabled; classes start off.

Server-controlled packs (e.g. Cosmic Rage) run through this: the MUD streams a
``$sphook play:path:vol:...`` line, the trigger captures the fields as ``@action``
etc., and ``#if`` dispatches to ``#play``/``#playloop``.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from genericmud.automation.engine import _MATCH_KWARGS, MatchContext, _matcher, _MatchTimeout
from genericmud.model.buffer import Line
from genericmud.safepath import confine, is_absolute, within
from genericmud.scripting.api import ScriptApi
from genericmud.sound.media import run_in_own_thread

# VIPMud's commands, by full name. A pack may write any start of a name (#Tr, #Exec, #For,
# #Va); where one start fits several, the first in this list wins.
_COMMAND_NAMES = (
    "VARIABLE", "TRIGGER", "ALIAS", "KEY", "IF", "PLAY", "PLAYLOOP", "PC", "SAY", "SUBSTITUTE",
    "MATH", "GAGLINE", "GAGON", "ABORT", "ALARM", "ADDITEM", "AUTOMENU", "BELL", "BREAK",
    "CLASS", "CLOSE", "CLIPBOARD", "CONFIGURE", "CONNECT", "CHARACTER", "DELCLASS", "DELFILE",
    "DIRECTION", "EXECUTE", "EDIT", "ENDRECORD", "ERASE", "FILE", "FIND", "FORALL",
    "GVARIABLE", "GTRIGGER", "GALIAS", "GKEY", "GCLASS", "GMATH", "GALARM", "GADDITEM",
    "GPROMPT", "GREAD", "GUNALIAS", "GUNKEY", "GUNTRIGGER", "GUNVARIABLE", "GUNALARM",
    "IGNORE", "KILLALL", "LOAD", "OFFLINE", "OK", "PASSTHRU", "PAUSE", "PICK", "PROMPT",
    "QUIET", "QUIT", "READ", "RECORD", "RELOAD", "RESET", "SAVE", "SEND", "SETSPEECH", "STOP",
    "SYSKEY", "TAB", "UNALARM", "UNALIAS", "UNCLASS", "UNGAGLINE", "UNGAGON", "UNKEY",
    "UNLOAD", "UNPAUSE", "UNTAB", "UNTIL", "UNTRIGGER", "UNVARIABLE", "WAIT", "WHILE",
    "WINDOW", "WRITE",
)
_COMMAND_NAME_SET = frozenset(_COMMAND_NAMES)
# Definitions and their removals; the G forms are VIPMud's every-session versions, and one
# session is all a pack sees here.
_TRIGGER_COMMANDS = {"TRIGGER", "GTRIGGER"}
_ALIAS_COMMANDS = {"ALIAS", "GALIAS"}
_KEY_COMMANDS = {"KEY", "GKEY"}
_UNTRIGGER_COMMANDS = {"UNTRIGGER", "GUNTRIGGER"}
_UNALIAS_COMMANDS = {"UNALIAS", "GUNALIAS"}
_UNKEY_COMMANDS = {"UNKEY", "GUNKEY"}
_SEQUENTIAL_HANDLES = frozenset("12345")  # #File handles 1-5 are text files; 6-10 records
_SEND_LIMIT = 1000  # lines #Send sends from one file
_PROTECTED_SUFFIXES = (".exe", ".dll", ".ini", ".cfg", ".hlp")  # #DelFile won't (manual)
_WRITES_FILES_RE = re.compile(r"#write\b", re.IGNORECASE)
# Commands that work VIPMud's own menus, windows and keys. genericMud has its own of each and
# ignores these, so a key bound to nothing else stays genericMud's: Cosmic Rage re-binds
# Alt+F, Ctrl+O, Escape and Ctrl+Q to them.
_VIPMUD_INTERFACE_COMMANDS = frozenset(
    {"AUTOMENU", "SYSKEY", "WINDOW", "EDIT", "QUIT", "TAB", "UNTAB", "CONFIGURE"}
)


@lru_cache(maxsize=512)
def _command_name(written: str) -> str:
    """The command a pack means by ``written``: its full name when it's a start of one."""
    if not written or written.isdigit() or written in _COMMAND_NAME_SET:
        return written
    return next((name for name in _COMMAND_NAMES if name.startswith(written)), written)
_CLASS_ON = {"enable", "enabled", "on"}
_CLASS_OFF = {"disable", "disabled", "off"}
_SOUND_VARIANT_RE = re.compile(r"\*(\d+)(\.\w+)$")  # "name*N.ext": one random variant of 1..N
# The wildcards that take any run in a .set pattern (&{name}, &name, *), and the ones that
# take one character each, a run of them making one wildcard.
_PATTERN_WILDCARD_RE = re.compile(r"&\{(\w+)\}|&(\w+)|(\*)")
_SINGLE_WILDCARDS = {"?": ".", "#": "[0-9]", "%": "[A-Za-z]"}
# What makes an alias name a pattern rather than a word.
_ALIAS_PATTERN_RE = re.compile(r"&\{\w+\}|&\w+|[*?]")
# A bare "@name = value" statement is an assignment, not text to send.
_ASSIGN_RE = re.compile(r"^@(\w+)\s*=\s*(.*)$", re.DOTALL)


def _fold(name: str) -> str:
    """Canonicalize a VIPMud variable name: VIPMud is case-insensitive (real packs
    write ``vol`` and read ``@Vol``), but the engine's variable store is a plain dict
    shared with case-sensitive dialects (MUSHclient GetVariable/SetVariable), so the
    folding must happen at this dialect's boundary, on every read and write."""
    return name.lower()


# Expressions refuse anything longer than this, and nest parentheses only this deep.
_EXPRESSION_MAX_LENGTH = 8192
_EXPRESSION_MAX_DEPTH = 64
# A whole number past this many bits from ^ is refused: a hostile MUD can feed operands
# through a captured variable, and 9^9^9 would take the client down.
_POWER_MAX_BITS = 256
_DEFAULT_VOLUME = 100
# A runaway script stops instead of freezing every tab: no loop runs more than this many
# times, and one script (with everything it calls) runs at most this many commands.
_LOOP_LIMIT = 10_000
_STEP_LIMIT = 50_000
# And it gets this many seconds: a rule's script or a resumed #Wait two, a file loading ten.
_SCRIPT_SECONDS = 2.0
_LOAD_SECONDS = 10.0
_REPEAT_LIMIT = 32_767  # #N {commands}: the manual's range is 1 to 32767
# Function calls nest this deep at most; deeper ones are left as written. Packs nest a few
# levels, and text a MUD can get run through #Exec would otherwise cost the client its stack,
# and its time: every level rescans the rest of the line.
_CALL_DEPTH_LIMIT = 32
_MIN_REPEATING_ALARM = 0.1  # seconds; "#alarm -0 {...}" must not spin
_FIND_LINES = 5000  # how far back #Find and %LastLine look in the output
# #Say's voice types other than Incoming (its default) and Output, which #SetSpeech
# Incoming Off silences.
_OTHER_VOICES = {"voiceonly", "entry", "edit", "system"}
_MASTER_HANDLE = "0"  # VIPMud handle 0 == all cues / master volume
_MAX_EXEC_DEPTH = 20  # bare-text alias expansion recursion cap (self-invoking alias)
_VIPMUD_VERSION = "200000"  # %Version: VIPMud 2.0.0 as xyyzzz
# VIPMud key names ("Alt-Shift-PageUp", "control-f11") -> the combo the window builds
# (keyboard._key_combo: ctrl, alt, shift in that order, then the key, joined with "+").
_KEY_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift"}
_KEY_MODIFIER_ORDER = ("ctrl", "alt", "shift")
_KEY_NAMES = {
    "pgup": "pageup", "pgdn": "pagedown", "del": "delete", "ins": "insert",
    "return": "enter", "esc": "escape", "spacebar": "space",
}
# %SwitchDirection's opposites, for MUDs whose packs never declare their own with #Direction.
_OPPOSITE_DIRECTIONS = {
    "n": "s", "s": "n", "e": "w", "w": "e", "u": "d", "d": "u",
    "ne": "sw", "sw": "ne", "nw": "se", "se": "nw",
    "north": "south", "south": "north", "east": "west", "west": "east",
    "up": "down", "down": "up", "northeast": "southwest", "southwest": "northeast",
    "northwest": "southeast", "southeast": "northwest", "in": "out", "out": "in",
}
_REMOTE_SOUND_MAX_BYTES = 32 * 1024 * 1024
# A sound that failed to download is left alone this long before a cue tries it again, so a
# file the server lacks isn't fetched on every cue but one timeout doesn't silence it for good.
_FETCH_RETRY_SECONDS = 300.0


@dataclass
class _Tok:
    braced: bool
    text: str


# VIPMud's escape character: the next character is ordinary, so ~} isn't the end of a
# block, ~; doesn't end a statement, and ~~ is a tilde. Structure only here: the pair stays
# in the text, and the escape comes off when the text is evaluated.
_ESCAPE = "~"


def _scan_block(source: str, i: int) -> tuple[str, int]:
    """``source[i]`` is ``{``; return (inner text, index after matching ``}``)."""
    depth = 0
    start = i + 1
    j = i
    n = len(source)
    while j < n:
        c = source[j]
        if c == _ESCAPE:
            j += 2
            continue
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
        while i < n:
            c = source[i]
            if c in " \t\r\n":
                i += 1
            elif c == ";":
                # A ';' with nothing but whitespace before it on its line begins a
                # comment (VIPMud .set convention, ";core variables"); consumed to EOL.
                # Mid-line it is the statement separator, skipped like whitespace --
                # without the distinction, comment text was SENT to the MUD at load.
                k = i - 1
                while k >= 0 and source[k] in " \t":
                    k -= 1
                if k < 0 or source[k] in "\r\n":
                    while i < n and source[i] != "\n":
                        i += 1
                else:
                    i += 1
            else:
                break
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
                if source.startswith("//", i):  # the rest of the line is a comment
                    while i < n and source[i] != "\n":
                        i += 1
                    break
                if source[i] == "{":
                    inner, i = _scan_block(source, i)
                    args.append(_Tok(True, inner))
                else:
                    word_start = i
                    while i < n and source[i] not in " \t\r\n;":
                        if source[i] == "{":
                            if i == word_start or source[i - 1] not in "@%":
                                break
                            # @{name} and %{1} belong to the word they're in.
                            _inner, i = _scan_block(source, i)
                            continue
                        i += 2 if source[i] == _ESCAPE else 1
                    args.append(_Tok(False, source[word_start:i]))
            statements.append((command, args))
        elif source[i] == ">":
            # ">" sends the rest of the line to the MUD as it is, semicolons and all.
            line_start = i
            while i < n and source[i] not in "\r\n":
                i += 1
            statements.append((None, [_Tok(False, source[line_start:i].rstrip())]))
        else:
            line_start = i
            line_end = None
            # ';' separates statements in bare text too ("makebetter; look" is two
            # commands); without the split the whole line went out as one string and
            # a leading alias name prefix-matched, swallowing the rest.
            while i < n and source[i] not in "\r\n;":
                if source.startswith("//", i) and source[i - 1] in " \t":
                    line_end = i  # a comment to the end of the line (not "http://")
                    while i < n and source[i] != "\n":
                        i += 1
                    break
                if source[i] == "{":
                    # A braced parameter runs to its closing brace, over lines and semicolons:
                    # Miriani's spabout prints a credits block written over eight lines.
                    _inner, i = _scan_block(source, i)
                    continue
                i += 2 if source[i] == _ESCAPE else 1
            text = source[line_start : i if line_end is None else line_end].strip()
            if text:
                statements.append((None, [_Tok(False, text)]))
    return statements


# VIPMud packs from Miriani's and Prometheus's makers ship a scripts folder and a sounds
# folder, each with a batch file that writes its path into the player's Conf.set.
LINK_SCRIPTS = "link the scripts for vip mud 2.0.bat"
LINK_SOUNDS = "link the sounds folder.bat"
LINK_THEMES = "link the themes folder.bat"


def linked_folder(pack_dir: str | Path, batch_name: str) -> Path | None:
    """The folder holding ``batch_name`` (any case), shallowest first."""
    found = [
        path.parent for path in Path(pack_dir).rglob("*.bat") if path.name.lower() == batch_name
    ]
    return min(found, key=lambda folder: (len(folder.parts), str(folder))) if found else None


def linked_vipmud_scripts(pack_dir: str | Path) -> Path | None:
    """A linked pack's real entry, Scripts/Main.set beside its link batch file. Its sounds
    folder has a main.set too, which only says which sounds version it is."""
    folder = linked_folder(pack_dir, LINK_SCRIPTS)
    scripts = folder / "Scripts" if folder is not None else None
    if scripts is None or not scripts.is_dir():
        return None
    mains = [path for path in sorted(scripts.iterdir()) if path.name.lower() == "main.set"]
    return mains[0] if mains else None


def _read_script(path: Path) -> str:
    """A .set file's text with every line ending CRLF, as VIPMud on Windows reads it. Packs
    split multi-line values on %CRLF (Miriani's option tables), and a pack checked out by
    git on Linux or macOS has bare newlines."""
    text = path.read_bytes().decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _split_parameters(text: str) -> list[str]:
    """What was typed after an alias, as VIPMud numbers it: each word is a parameter, and a
    {braced} one keeps its spaces and loses its braces ("{} {Quiet}" is an empty first
    parameter and "Quiet")."""
    parameters: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
            continue
        if text[i] == "{":
            inner, i = _scan_block(text, i)
            parameters.append(inner)
            continue
        start = i
        while i < n and text[i] not in " \t\r\n":
            if text[i] == "{" and text[i - 1] in "@%":
                _inner, i = _scan_block(text, i)  # @{name} belongs to the word
                continue
            i += 2 if text[i] == _ESCAPE else 1
        parameters.append(text[start:i])
    return parameters


def _scan_char_class(pattern: str, start: int) -> int | None:
    """End index (exclusive) of the ``[...]`` class opening at ``start``, or None if unclosed.

    Follows the regex rule that a ``]`` in the first position (after an optional ``^``) is a
    member rather than the terminator. That rule is the whole point here: it is how packs
    write a literal bracket, ``[[]`` for ``[`` and ``[]]`` for ``]``.
    """
    i = start + 1
    if i < len(pattern) and pattern[i] in "^!":
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1
    while i < len(pattern):
        if pattern[i] == "]":
            return i + 1
        i += 1
    return None


def _char_class(source: str) -> str:
    """Normalise a ``[...]`` group into an equivalent regex character class.

    Bracket members are re-escaped, so ``[[]`` becomes ``[\\[]``: identical in meaning, but
    it keeps Python's "possible nested set" FutureWarning out of the user's console every
    time the pack loads. Falls back to escaping the whole group as a literal if the result
    still will not compile, so a malformed class can never kill a pack load.
    """
    body = source[1:-1]
    negated = body.startswith(("^", "!"))  # VIPMud writes [!a-c]; [^a-c] is kept too
    if negated:
        body = body[1:]
    members: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            members.append(body[i : i + 2])  # an existing escape passes through intact
            i += 2
            continue
        members.append("\\" + body[i] if body[i] in "[]" else body[i])
        i += 1
    candidate = f"[{'^' if negated else ''}{''.join(members)}]"
    try:
        re.compile(candidate)
    except re.error:
        return re.escape(source)
    return candidate


def vip_pattern_to_regex(pattern: str) -> str:
    """Convert a VIPMud pattern to a regex, numbering wildcards for %1 to %99 in order.

    ``*`` takes any run (all the rest at the very end, otherwise up to the next literal);
    ``&{name}``/``&name`` the same into @name; a run of ``?`` (any character), ``#`` (a
    digit) or ``%`` (a letter) is one wildcard; a ``[...]`` class is one character
    (``[!...]`` for "not these"). Miriani's history trigger ``[[]##/##/#### - *:* [ap].m.]
    (*) *`` reads them back as %1 to %8. A class of one character is how packs write a
    wildcard character literally (``[[]``, ``[%]``, ``[#]``), so it takes no number, and
    neither does a character after ``~``.

    ``[...]`` is a character class and passes through, because that is what VIPMud does and
    packs depend on it: channel triggers are written ``[[]*] * *`` to match a line like
    ``[General Communication] Someone transmits, "..."``. Escaping the brackets as literals
    instead produced a regex demanding three characters, ``[[]``, and every channel trigger
    in the Star Conquest pack silently matched nothing.
    """
    out: list[str] = []
    names: set[str] = set()
    literal_start = 0
    i = 0
    end = len(pattern)
    while i < end:
        if pattern[i] == _ESCAPE and i + 1 < end:
            out.append(re.escape(pattern[literal_start:i]))
            out.append(re.escape(pattern[i + 1]))
            i = literal_start = i + 2
            continue
        if pattern[i] == "[":
            close = _scan_char_class(pattern, i)
            if close is None:  # unclosed: an ordinary literal bracket
                i += 1
                continue
            out.append(re.escape(pattern[literal_start:i]))
            group = pattern[i:close]
            if len(group) == 3:  # [[] [%] [#]: one character, written literally
                out.append(re.escape(group[1]))
            else:
                regex = _char_class(group)
                out.append(f"({regex})" if regex.startswith("[") else regex)
            i = literal_start = close
            continue
        if pattern[i] in _SINGLE_WILDCARDS:
            run = i
            while run < end and pattern[run] == pattern[i]:
                run += 1
            out.append(re.escape(pattern[literal_start:i]))
            unit = _SINGLE_WILDCARDS[pattern[i]]
            out.append(f"({unit}{{{run - i}}})" if run - i > 1 else f"({unit})")
            i = literal_start = run
            continue
        match = _PATTERN_WILDCARD_RE.match(pattern, i)
        if match is None:
            i += 1
            continue
        out.append(re.escape(pattern[literal_start : match.start()]))
        body = ".*" if match.end() == end else ".*?"
        name = match.group(1) or match.group(2)
        if name and name.isidentifier() and name not in names:
            names.add(name)
            out.append(f"(?P<{name}>{body})")
        else:  # * (or a name a regex group can't carry)
            out.append(f"({body})")
        i = literal_start = match.end()
    out.append(re.escape(pattern[literal_start:]))
    return "".join(out)


@lru_cache(maxsize=256)
def _wildcard_compare_regex(pattern: str):
    """The ``=*`` operator's pattern: a whole-value match, the way a trigger matches a line,
    on the engine's matcher with its time limit (both sides can come from the MUD)."""
    try:
        return _matcher.compile(f"^{vip_pattern_to_regex(pattern)}$", re.IGNORECASE | re.DOTALL)
    except Exception:  # noqa: BLE001 - re.error or regex.error: a bad pattern matches nothing
        return None


def vip_key_to_combo(spec: str) -> str:
    """A VIPMud #key name as the combo the window sends for that keypress.

    Packs write "alt-f2", "Control-alt-shift-S" and "Shift-Alt-PageUp"; the window sends
    "alt+f2", "ctrl+alt+shift+s" and "alt+shift+pageup". Taken as written, every
    hotkey with a modifier could never fire: 124 of Cosmic Rage's 148, 32 of Star
    Conquest's 40. The key itself may be "-" ("alt--"), so modifiers are peeled off
    the front one at a time rather than split on every dash.
    """
    remaining = "".join(spec.split()).lower()  # "shift-alt + pgdn" spacing varies too
    modifiers: set[str] = set()
    peeled = True
    while peeled:
        peeled = False
        for word, modifier in _KEY_MODIFIERS.items():
            for separator in "-+":
                prefix = word + separator
                if remaining.startswith(prefix) and len(remaining) > len(prefix):
                    modifiers.add(modifier)
                    remaining = remaining[len(prefix):]
                    peeled = True
                    break
            if peeled:
                break
    key = _KEY_NAMES.get(remaining, remaining)
    ordered = [modifier for modifier in _KEY_MODIFIER_ORDER if modifier in modifiers]
    return "+".join([*ordered, key])


def _to_int(value: str, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


_INTEGER_RE = re.compile(r"^[+-]?\d{1,30}$")
_DECIMAL_RE = re.compile(r"^[+-]?(?:\d{1,30}\.\d{0,30}|\.\d{1,30})$")


def _number(text: str) -> int | float | None:
    """``text`` as a VIPMud number, or None when it isn't one.

    Only plain decimal notation counts: Python's float() would also take "inf", "nan",
    "1e5" and "1_000", none of which a VIPMud script means as a number.
    """
    text = text.strip()
    if _INTEGER_RE.match(text):
        return int(text)
    if _DECIMAL_RE.match(text):
        return float(text)
    return None


def _format_number(value: int | float) -> str:
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return format(value, ".10g")
    return str(value)


def _truthy(text: str) -> bool:
    """VIPMud's truth: a number is true unless zero; text is true unless empty."""
    number = _number(text)
    if number is not None:
        return number != 0
    return text.strip() != ""


def _unquote(text: str) -> str:
    """Text without the double quotes around it, if it is exactly one quoted string."""
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        return stripped[1:-1]
    return text


def _skip_quoted(text: str, i: int) -> int:
    """``text[i]`` is ``"``; the index after its closing quote (or the end of the text)."""
    j = i + 1
    while j < len(text):
        if text[j] == _ESCAPE:
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        j += 1
    return len(text)


def _scan_call(text: str, open_index: int) -> tuple[list[str], int] | None:
    """Split ``%name(a,b,...)`` at ``text[open_index] == "("`` into its raw arguments.

    Commas inside quotes, braces or nested parentheses belong to their argument, and ``~,``
    is an ordinary comma. Returns the arguments and the index after ``)``, or None when the
    call is never closed.
    """
    depth = 0
    start = open_index + 1
    args: list[str] = []
    i = open_index
    n = len(text)
    while i < n:
        c = text[i]
        if c == _ESCAPE:
            i += 2
            continue
        if c == '"':
            i = _skip_quoted(text, i)
            continue
        if c == "{":
            _inner, i = _scan_block(text, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                args.append(text[start:i])
                return args, i + 1
        elif c == "," and depth == 1:
            args.append(text[start:i])
            start = i + 1
        i += 1
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


# --- expressions ---

# Longest first, so "<=" isn't read as "<" then "=".
_EXPRESSION_SYMBOLS = ("=*", "<=", ">=", "<>", "!=", "=", "<", ">", "+", "-", "*", "/", "\\", "^")
_EXPRESSION_WORDS = frozenset({"and", "or", "xor", "not"})
_WORD_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
)
_COMPARISONS = ("=*", "<=", ">=", "<>", "!=", "=", "<", ">")


@dataclass
class _ExpressionToken:
    kind: str  # "op", "(", ")", "value" or "space"
    text: str  # the operator (word operators lowercased), or the value's text
    quoted: bool = False  # a "..." string

    @property
    def source(self) -> str:
        return f'"{self.text}"' if self.quoted else self.text


class _ExpressionError(Exception):
    """The text isn't an expression this interpreter can evaluate."""


@dataclass
class _Value:
    text: str
    start: int  # the token span it came from, for operators that fall back to plain text
    end: int
    missing: bool = False  # no operand was written here ("= 1", or between two operators)


class _ExpressionParser:
    """Evaluate expression tokens with the manual's operators, lowest precedence first:
    XOR, OR, AND, the comparisons, + and -, *, / and \\ (modulus), ^, then NOT and signs.

    Arithmetic on something that isn't a number gives back the text it was written as, so
    "gold-plated" compared with a captured "gold-plated" is still equal.
    """

    def __init__(self, tokens: list[_ExpressionToken]) -> None:
        self._tokens = tokens
        self._position = 0

    def parse(self) -> str:
        value = self._xor()
        self._skip_spaces()
        if self._position < len(self._tokens):
            raise _ExpressionError(f"unexpected {self._tokens[self._position].text!r}")
        return value.text

    # --- token helpers ---

    def _skip_spaces(self) -> None:
        while self._position < len(self._tokens) and self._tokens[self._position].kind == "space":
            self._position += 1

    def _operator(self, choices: tuple[str, ...]) -> str | None:
        self._skip_spaces()
        if self._position < len(self._tokens):
            token = self._tokens[self._position]
            if token.kind == "op" and token.text in choices:
                self._position += 1
                return token.text
        return None

    def _source(self, start: int, end: int) -> str:
        return "".join(token.source for token in self._tokens[start:end]).strip()

    def _binary(self, operand, operators: tuple[str, ...], apply) -> _Value:
        left = operand()
        while True:
            operator = self._operator(operators)
            if operator is None:
                return left
            right = operand()
            text = apply(operator, left, right)
            left = _Value(text, left.start, right.end)

    # --- precedence levels ---

    def _xor(self) -> _Value:
        return self._binary(self._or, ("xor",), self._logical)

    def _or(self) -> _Value:
        return self._binary(self._and, ("or",), self._logical)

    def _and(self) -> _Value:
        return self._binary(self._comparison, ("and",), self._logical)

    def _comparison(self) -> _Value:
        return self._binary(self._additive, _COMPARISONS, self._compare)

    def _additive(self) -> _Value:
        return self._binary(self._multiplicative, ("+", "-"), self._arithmetic)

    def _multiplicative(self) -> _Value:
        return self._binary(self._power, ("*", "/", "\\"), self._arithmetic)

    def _power(self) -> _Value:
        return self._binary(self._unary, ("^",), self._arithmetic)

    def _unary(self) -> _Value:
        start = self._position
        operator = self._operator(("not", "-", "+"))
        if operator is None:
            return self._primary()
        operand = self._unary()
        if operand.missing:
            raise _ExpressionError(f"nothing after {operator!r}")
        if operator == "not":
            return _Value("0" if _truthy(operand.text) else "1", start, operand.end)
        number = 0 if not operand.text.strip() else _number(operand.text)
        if number is None:
            return _Value(self._source(start, operand.end), start, operand.end)
        return _Value(_format_number(-number if operator == "-" else number), start, operand.end)

    def _primary(self) -> _Value:
        self._skip_spaces()
        start = self._position
        if start >= len(self._tokens) or self._tokens[start].kind in ("op", ")"):
            return _Value("", start, start, missing=True)
        if self._tokens[start].kind == "(":
            self._position += 1
            value = self._xor()
            self._skip_spaces()
            if self._position >= len(self._tokens) or self._tokens[self._position].kind != ")":
                raise _ExpressionError("unclosed parenthesis")
            self._position += 1
            return _Value(value.text, start, self._position)
        # An operand is every value and space up to the next operator or parenthesis.
        end = start
        while end < len(self._tokens) and self._tokens[end].kind in ("value", "space"):
            end += 1
        while end > start and self._tokens[end - 1].kind == "space":
            end -= 1
        self._position = end
        tokens = self._tokens[start:end]
        if len(tokens) == 1:
            return _Value(tokens[0].text, start, end)
        return _Value("".join(token.text for token in tokens).strip(), start, end)

    # --- operators ---

    @staticmethod
    def _logical(operator: str, left: _Value, right: _Value) -> str:
        if left.missing or right.missing:
            raise _ExpressionError(f"{operator!r} needs a value on both sides")
        a, b = _truthy(left.text), _truthy(right.text)
        result = {"and": a and b, "or": a or b, "xor": a != b}[operator]
        return "1" if result else "0"

    @staticmethod
    def _compare(operator: str, left: _Value, right: _Value) -> str:
        if operator == "=*":
            regex = _wildcard_compare_regex(right.text)
            try:
                matched = regex is not None and regex.match(left.text, **_MATCH_KWARGS)
            except _MatchTimeout:
                matched = False
            return "1" if matched else "0"
        a, b = _number(left.text), _number(right.text)
        # An unset variable is zero next to a number, as with VIPMud's Explicit off: the
        # manual's prompt example compares @endurFlag with 0 before ever setting it.
        if a is None and not left.text.strip() and b is not None:
            a = 0
        if b is None and not right.text.strip() and a is not None:
            b = 0
        if a is not None and b is not None:
            x: object = a
            y: object = b
        else:
            x, y = left.text.casefold(), right.text.casefold()
        result = {
            "=": x == y, "<>": x != y, "!=": x != y,
            "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y,  # type: ignore[operator]
        }[operator]
        return "1" if result else "0"

    def _arithmetic(self, operator: str, left: _Value, right: _Value) -> str:
        if left.missing or right.missing:
            raise _ExpressionError(f"{operator!r} needs a value on both sides")
        a = 0 if not left.text.strip() else _number(left.text)
        b = 0 if not right.text.strip() else _number(right.text)
        if a is None or b is None:
            return self._source(left.start, right.end)  # not numbers: it was text
        result = _calculate(operator, a, b)
        if result is None:
            return self._source(left.start, right.end)
        return _format_number(result)


def _calculate(operator: str, a: int | float, b: int | float) -> int | float | None:
    """One arithmetic step. VIPMud works in whole numbers, so whole operands divide whole."""
    whole = isinstance(a, int) and isinstance(b, int)
    if operator == "+":
        return a + b
    if operator == "-":
        return a - b
    if operator == "*":
        return a * b
    if operator == "/":
        if b == 0:
            return 0
        if whole:
            quotient = abs(a) // abs(b)  # type: ignore[operator]
            return quotient if (a >= 0) == (b >= 0) else -quotient
        return a / b
    if operator == "\\":
        if b == 0:
            return 0
        if whole:
            quotient = abs(a) // abs(b)  # type: ignore[operator]
            quotient = quotient if (a >= 0) == (b >= 0) else -quotient
            return a - b * quotient
        return math.fmod(a, b)
    if operator == "^":
        if whole and b >= 0:
            if abs(a) > 1 and b * math.log2(abs(a)) > _POWER_MAX_BITS:
                return None
            return a**b
        try:
            result = float(a) ** float(b)
        except (OverflowError, ZeroDivisionError):
            return None
        return None if isinstance(result, complex) or math.isinf(result) else result
    return None


_SETTINGS_RECORD = 128  # VIPMud settings files store one value per fixed 128-byte record


def _parse_vip_settings(data: bytes) -> list[str]:
    """Decode a VIPMud settings file into its values, one per record.

    The format is fixed 128-byte records, each a uint16 little-endian length then that many
    ASCII bytes, null-padded. Reading it (via #file/#Read) is what flips a pack's sound-enable
    flags on — they otherwise keep the conservative defaults set before the read.
    """
    values: list[str] = []
    for off in range(0, len(data), _SETTINGS_RECORD):
        record = data[off : off + _SETTINGS_RECORD]
        if len(record) < 2:
            break
        length = int.from_bytes(record[0:2], "little")
        values.append(record[2 : 2 + length].decode("latin-1", "ignore"))
    return values


def _serialize_vip_settings(values: list[str]) -> bytes:
    """Re-encode values into the 128-byte-record format (for #Write -> #Close persistence)."""
    out = bytearray()
    for value in values:
        encoded = value.encode("latin-1", "ignore")[: _SETTINGS_RECORD - 2]
        out += (len(encoded).to_bytes(2, "little") + encoded).ljust(_SETTINGS_RECORD, b"\x00")
    return bytes(out)


def _lexical(path: Path) -> Path:
    """``path`` made absolute and normalised as text, without asking the disk."""
    return Path(os.path.normpath(os.path.abspath(path)))


def _stays_inside(path: Path, root: Path) -> bool:
    """Whether ``path`` is inside ``root`` once links are followed.

    Only folders that already exist are resolved, strictly: a folder that appears while
    resolve() runs (another sound's download making it) can change what resolve() returns
    on Windows, and an existing path's answer can't change underneath it.
    """
    def existing(candidate: Path) -> Path:
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate

    try:
        real = existing(_lexical(path)).resolve(strict=True)
        real_root = existing(_lexical(root)).resolve(strict=True)
    except OSError:
        return False
    return real.is_relative_to(real_root)


@dataclass
class _PendingCue:
    """A cue waiting for its sound to download; #pc volume, pan and frequency land here until
    it plays."""

    file: str
    gain: float
    loop: bool
    pan: float = 0.0
    frequency: float | None = None


@dataclass
class _VipFile:
    path: Path | None  # where to flush on #Close (None if unresolved)
    values: list[str]  # one entry per record, addressed 1-based by #Read/#Write
    dirty: bool = False


@dataclass
class _TextFile:
    """A sequential #File (handles 1-5): read a line at a time, written by appending."""

    reference: str  # as the pack named it; resolved when first read or written
    lines: list[str] | None = None  # read on the first #Read
    position: int = 0


@dataclass
class _Scope:
    """What a running script can see: the wildcards of the rule that started it, the line
    it fired on, and the #ForAll item it is working through (%i)."""

    wildcards: list[str]
    line: Line | None = None
    loops: list[tuple[str, int]] = field(default_factory=list)  # (%i, %repeatnum), innermost last

    def looping(self, item: str, number: int) -> _Scope:
        return _Scope(self.wildcards, self.line, [*self.loops, (item, number)])


@dataclass
class _VipRule:
    """A trigger, alias or key as the pack defined it, kept so a redefinition or an #Un...
    command can find and replace it, and so its classes can switch it on and off."""

    body: str
    classes: tuple[str, ...] = ()  # all of them have to be enabled for the rule to work
    engine_name: str = ""  # the engine rule's name, for removing it
    engine_rule: object | None = None
    origin: str | None = None  # the file whose loading defined it, for #UnLoad


@lru_cache(maxsize=256)
def _only_works_vipmud(body: str) -> bool:
    """Whether a script does nothing but work VIPMud's own menus, windows and keys."""
    statements = tokenize_statements(body)
    return bool(statements) and all(
        command is not None and _command_name(command) in _VIPMUD_INTERFACE_COMMANDS
        for command, _args in statements
    )


class _KeyBinding:
    """A pack key as the engine sees it. It is active while one of its definitions is, so a
    key whose class is off falls through to genericMud instead of doing nothing, and so does
    one that only opens VIPMud's own menus and windows."""

    def __init__(self, pack: VipMudPack, combo: str) -> None:
        self._pack = pack
        self._combo = combo

    def active(self) -> bool:
        # The window asks from its own thread while the pack may be changing its keys.
        try:
            rule = self._pack._key_rule(self._combo)
        except RuntimeError:
            return True
        return rule is not None and not _only_works_vipmud(rule.body)

    def __call__(self, ctx: MatchContext) -> None:
        rule = self._pack._key_rule(self._combo)
        if rule is not None:
            self._pack._run_rule(rule.body, _Scope([], ctx.line))


def _split_trigger_name(pattern: str) -> tuple[str, str]:
    """A ``<name>`` at the start of a trigger pattern names the trigger (manual 5.2.54).

    Only when real pattern text follows and the name has no wildcards in it: COGG's
    ``<Sanity -*>`` is its prompt, and read as a name it would leave nothing to match.
    """
    if pattern.startswith("<"):
        close = pattern.find(">")
        name, rest = pattern[1:close], pattern[close + 1 :]
        if close > 1 and rest.strip() and not any(c in name for c in "*?&[]~"):
            return name, rest
    return "", pattern


class _AbortLevel(Exception):  # noqa: N818 - a control-flow signal (#Abort), not an error
    """``#Abort`` with no value: ends the current level, a loop's pass or the whole script
    when it isn't in a loop (manual 5.2.1)."""


class _AbortScript(Exception):  # noqa: N818 - a control-flow signal (#Abort 1), not an error
    """``#Abort`` with a value: ends the whole script, loops and all."""


_ABORTS = (_AbortLevel, _AbortScript)


@dataclass
class _Alarm:
    body: str
    seconds: float
    repeating: bool
    generation: int = 0  # bumped on every reschedule, so a superseded timer does nothing
    due: float = 0.0  # when it fires next (time.monotonic), so a dialog's wait can run it


class VipMudPack:
    """A loaded VIPMud soundpack/script, registered against a :class:`ScriptApi`."""

    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._base_dir = api.base_dir
        self._loaded: set[str] = set()  # files #Load has run, by resolved path
        self._loading: list[str] = []  # files loading right now, innermost last
        self._files: dict[str, _VipFile | _TextFile] = {}  # by #File handle
        self._handles: dict[str, str] = {}  # VIPMud play handle -> sound bus channel
        self._next_handle = 1
        self._exec_depth = 0  # bare-text -> alias-pipeline recursion guard
        self._last_handle = _MASTER_HANDLE
        self._last_loop_handle = _MASTER_HANDLE  # %MP3Loop: the newest looping cue
        self._master_volume = _DEFAULT_VOLUME  # %Volume, set by #pc 0 volume
        self._clipboard = ""  # #Clipboard / %Clipboard
        self._directions = dict(_OPPOSITE_DIRECTIONS)  # #Direction adds a pack's own
        # The pack's rules by what identifies them in VIPMud: a trigger by its <name> or its
        # pattern, an alias by its name and classes, a key by its combo and then classes (a
        # class's F7 and the plain F7 are both kept). Defining one again replaces it.
        self._triggers: dict[str, _VipRule] = {}
        self._aliases: dict[tuple[str, tuple[str, ...]], _VipRule] = {}
        self._keys: dict[str, dict[tuple[str, ...], _VipRule]] = {}
        self._rule_count = 0
        self._alarms: dict[int, _Alarm] = {}  # by the number #Alarm hands its variable
        self._alarm_count = 0
        self._run_depth = 0  # scripts running inside one another (an alias calling an alias)
        self._deadline = 0.0  # when the outermost running script runs out of time
        self._budget_noted = False  # the diagnostic log has heard about this script stopping
        self._call_depth = 0  # %functions being evaluated inside one another
        self._found_line = ""  # %FoundLine, from #Find
        self._find_index: int | None = None  # where #Find last found, for "next"
        self._gagging_all = False  # between #GagOn All and #UnGagOn
        self._gag_rule: object | None = None
        # "#GagLine 4 All" gags the line and the three after it.
        self._gag_left = 0
        self._gag_voice_only = False
        self._gag_from: Line | None = None  # the line that asked, which isn't one of them
        self._steps = 0  # commands the outermost running script has used of _STEP_LIMIT
        # Classes start switched off; a rule in a class works only while it's enabled.
        self._enabled_classes: set[str] = set()
        self._current_classes: tuple[str, ...] = ()  # set by #Class with just a name
        self._remote_sound_base: str | None = None
        self._remote_sound_root: Path | None = None
        # Sounds fetched in the background (see _fetch_in_background), by URL: the download
        # in flight, the cue channels waiting on it, and URLs that already failed this session.
        self._fetching: dict[str, asyncio.Future] = {}
        self._waiting: dict[str, list[str]] = {}
        self._pending_cues: dict[str, _PendingCue] = {}  # channel -> cue waiting on a download
        self._failed_fetches: dict[str, float] = {}  # url -> when it failed (time.monotonic)
        # Server-controlled packs build sound paths from @sppath; the pack's own
        # settings loader (file I/O, deferred) normally sets it. Default it (and the
        # script path) to the pack dir so @sppath/x.wav resolves to bundled sounds.
        # Default @sppath/@scpath to the pack dir, but don't clobber a value the session
        # pre-set from world.sounds (so a world can point at its own sound folder).
        # Names this pack has set, and whether the pack saves what it wants in files of its
        # own. VIPMud starts a session without variables unless the player keeps them, so the
        # app leaves out a pack's variables when it saves its own settings (Miriani warns
        # when variables were kept) and keeps them when it doesn't (COGG's volume).
        self.written_variables: set[str] = set()
        self.saves_its_own_settings = False
        self._entry: Path | None = None  # the start-up script, for #Reload
        base = api.base_dir
        if base:
            self._discover_remote_sounds(Path(base))
        self._wire_paths()

    def _wire_paths(self) -> None:
        """Tell the pack where it lives. Server-controlled packs build sound paths from
        @sppath and script paths from @scpath; linked packs read ScriptPath and SoundsPath."""
        base = self._base_dir
        if not base:
            return
        self._link_folders(Path(base))
        if not self._api.get_var("sppath"):
            self._set_var("sppath", str(self._remote_sound_root or base))
        if not self._api.get_var("scpath"):
            self._set_var("scpath", base)

    def _set_var(self, name: str, value: object) -> None:
        self.written_variables.add(name)
        self._api.set_var(name, value)

    def _link_folders(self, base: Path) -> None:
        """Do what a linked pack's setup scripts do: tell it where its scripts, sounds and
        themes are (ScriptPath, SoundsPath, ThemesPath). The paths belong to this install,
        so they replace whatever an earlier session had."""
        scripts = linked_vipmud_scripts(base)
        sounds = linked_folder(base, LINK_SOUNDS)
        themes = linked_folder(base, LINK_THEMES)
        for name, folder in (
            ("scriptpath", scripts.parent if scripts is not None else None),
            ("soundspath", sounds),
            ("themespath", themes),
        ):
            if folder is not None:
                self._set_var(name, str(folder))

    def _discover_remote_sounds(self, base: Path) -> None:
        """Read an official VIPMud installer's lazy sound-repository declaration."""
        installers = sorted(base.rglob("installer.bat"))
        for installer in installers:
            try:
                source = installer.read_text(encoding="latin-1", errors="ignore")
            except OSError:
                continue
            repo = re.search(r'set\s+"REPO_URL=([^"]+)"', source, re.IGNORECASE)
            subfolder = re.search(
                r'set\s+"REPO_SUBFOLDER=([^"]+)"', source, re.IGNORECASE
            )
            helper = installer.parent / "SoundSync.exe"
            if repo is None or subfolder is None or not helper.is_file():
                continue
            repo_url = re.sub(r"(?<!:)//+", "/", repo.group(1).strip()).rstrip("/")
            subtree = subfolder.group(1).strip().strip("/\\")
            if not repo_url.startswith(("http://", "https://")) or not subtree:
                continue
            self._remote_sound_base = f"{repo_url}/raw/branch/main/{quote(subtree)}/"
            self._remote_sound_root = installer.parent / "sounds" / subtree
            return

    def _remote_sound_request(self, file: str) -> tuple[Path, str] | None:
        root = self._remote_sound_root
        if root is None or self._remote_sound_base is None:
            return None
        requested = Path(file.replace("\\", "/"))
        candidate = requested if requested.is_absolute() else root / requested
        # Decided on the path alone. resolve() here turned down every missing sound asked
        # for while another one's download was creating the sounds folder on Windows, so
        # they were played as misses instead of fetched. _fetch_remote_sound follows links
        # before it writes anything.
        destination = _lexical(candidate)
        root = _lexical(root)
        if not destination.is_relative_to(root) or destination.suffix.casefold() != ".wav":
            return None
        relative = destination.relative_to(root).as_posix()
        url = self._remote_sound_base + quote(relative, safe="/")
        return destination, url

    def can_fetch_sound(self, file: str) -> bool:
        """Whether this pack declares a confined online source for the requested WAV."""
        return self._remote_sound_request(file) is not None

    def _fetch_remote_sound(self, file: str) -> bool:
        request = self._remote_sound_request(file)
        if request is None:
            return False
        destination, url = request
        root = self._remote_sound_root
        assert root is not None
        relative = destination.relative_to(_lexical(root)).as_posix()
        temporary: Path | None = None
        try:
            # A link inside the pack must not send the download, or the folders made for
            # it, anywhere outside the pack's sounds folder.
            if not _stays_inside(destination.parent, root):
                raise PermissionError("the sound's folder leads outside the pack's sounds folder")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".genericmud-sound-", delete=False
            ) as handle:
                temporary = Path(handle.name)
            from genericmud.packs import vault

            vault.download(url, temporary, max_bytes=_REMOTE_SOUND_MAX_BYTES)
            if temporary.stat().st_size == 0:
                return False
            temporary.replace(destination)
            if self._api.diag is not None:
                self._api.diag.event(
                    "pack.sound.fetch", source=self._api.source or "?",
                    file=relative, status="downloaded",
                )
            return True
        except Exception as error:  # noqa: BLE001 - the pack handles a failed play normally
            if self._api.diag is not None:
                self._api.diag.event(
                    "pack.sound.fetch", source=self._api.source or "?",
                    file=relative, status="failed", error=f"{type(error).__name__}: {error}",
                )
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load_source(self, source: str) -> None:
        self._run_rule(source, _Scope([]))  # an #Abort stops loading the rest of the file

    def _engine_name(self) -> str:
        self._rule_count += 1
        return f"vipmud:{self._api.source}:{self._rule_count}"

    def _rule_classes(self, arg: _Tok | None, scope: _Scope) -> tuple[str, ...]:
        """The classes a rule belongs to: its class parameter (several joined with |, all of
        which have to be on), else the class #Class made current. Class 0 is no class."""
        text = self._expand(arg.text, scope) if arg is not None else ""
        names = (part.strip().lower() for part in text.split("|"))
        classes = tuple(dict.fromkeys(n for n in names if n and n not in ("0", "anycase")))
        return classes or self._current_classes

    def _class_active(self, classes: tuple[str, ...]) -> bool:
        return all(name in self._enabled_classes for name in classes)

    def _switch_rules(self) -> None:
        """Turn each trigger and alias on or off to match its classes."""
        for rule in [*self._triggers.values(), *self._aliases.values()]:
            if rule.engine_rule is not None:
                rule.engine_rule.enabled = self._class_active(rule.classes)

    def _execute_class(self, command: str, args: list[_Tok], scope: _Scope) -> None:
        names = [
            name for name in (
                part.strip().lower() for part in self._expand(args[0].text, scope).split("|")
            ) if name
        ]
        state = self._expand(args[1].text, scope).strip().lower() if len(args) > 1 else ""
        if command in ("UNCLASS", "DELCLASS"):
            self._drop_classes(set(names), delete=command == "DELCLASS")
        elif state in _CLASS_ON:
            self._enabled_classes.update(names)
        elif state in _CLASS_OFF:
            self._enabled_classes.difference_update(names)
        elif not state:
            self._current_classes = tuple(n for n in names if n != "0")
        self._switch_rules()

    def _drop_classes(self, names: set[str], *, delete: bool) -> None:
        """#UnClass frees a class's rules to work all the time; #DelClass deletes them."""
        for key, rule in list(self._triggers.items()):
            if names & set(rule.classes):
                if delete:
                    self._remove_trigger(key)
                else:
                    rule.classes = tuple(c for c in rule.classes if c not in names)
        for key, rule in list(self._aliases.items()):
            if names & set(rule.classes):
                self._remove_alias(key)
                if not delete:
                    remaining = tuple(c for c in rule.classes if c not in names)
                    self._register_alias(key[0], rule.body, remaining)
        for combo, definitions in list(self._keys.items()):
            for classes in [c for c in definitions if names & set(c)]:
                rule = definitions.pop(classes)
                if not delete:
                    remaining = tuple(c for c in classes if c not in names)
                    definitions[remaining] = _VipRule(rule.body, remaining)
            if not definitions:
                self._remove_key(combo)

    def _define_trigger(self, args: list[_Tok], scope: _Scope) -> None:
        """``#Trigger {pattern} {commands} [class] [AnyCase]``: the pattern has to match the
        whole line (manual 2.4.2: "you must include a leading or trailing asterisk if there
        is any text expected before or after"). Spaces a MUD leaves at the end of a line
        don't count; a screen reader never says them, so no pack author knew to allow for
        them."""
        name, pattern = _split_trigger_name(args[0].text)
        body = args[1].text if len(args) > 1 else ""
        flags = "(?i)" if any(arg.text.strip().lower() == "anycase" for arg in args[2:]) else ""
        key = name.lower() if name else pattern
        classes = self._rule_classes(args[2] if len(args) > 2 else None, scope)
        self._remove_trigger(key)
        engine_name = self._engine_name()
        rule = self._api.add_trigger(
            f"{flags}^{vip_pattern_to_regex(pattern)}[ \t]*$", self._runner(body),
            regex=True, name=engine_name,
        )
        rule.enabled = self._class_active(classes)
        self._triggers[key] = _VipRule(body, classes, engine_name, rule, self._origin())

    def _remove_trigger(self, key: str, class_name: str = "") -> None:
        old = self._triggers.get(key)
        if old is not None and (not class_name or class_name in old.classes):
            del self._triggers[key]
            self._api.remove_trigger(old.engine_name)

    def _define_key(self, args: list[_Tok], scope: _Scope) -> None:
        combo = vip_key_to_combo(self._expand(args[0].text, scope))
        classes = self._rule_classes(args[2] if len(args) > 2 else None, scope)
        definitions = self._keys.setdefault(combo, {})
        definitions.pop(classes, None)  # the newest definition goes last
        definitions[classes] = _VipRule(
            args[1].text if len(args) > 1 else "", classes, origin=self._origin()
        )
        self._api.add_key(combo, _KeyBinding(self, combo))

    def _key_rule(self, combo: str) -> _VipRule | None:
        """What a key does now: the newest definition in an enabled class, else its plain
        one ("the keys would return to their global assignments", manual 2.4.8)."""
        definitions = self._keys.get(combo, {})
        in_classes = [
            rule for classes, rule in list(definitions.items())
            if classes and self._class_active(classes)
        ]
        return in_classes[-1] if in_classes else definitions.get(())

    def _remove_key(self, combo: str, class_name: str = "") -> None:
        definitions = self._keys.get(combo)
        if definitions is None:
            return
        for classes in list(definitions):
            if not class_name or class_name in classes:
                del definitions[classes]
        if not definitions:
            del self._keys[combo]
            self._api.remove_key(combo)

    def _remove_alias(self, key: tuple[str, tuple[str, ...]]) -> None:
        old = self._aliases.pop(key, None)
        if old is not None:
            self._api.remove_alias(old.engine_name)

    def _unalias(self, name: str, class_name: str = "") -> None:
        for key in [k for k in self._aliases if k[0] == name]:
            if not class_name or class_name in key[1]:
                self._remove_alias(key)

    def connection_changed(self, connected: bool) -> None:
        """Run the pack's OnConnect or OnDisconnect alias, as VIPMud does when the
        connection to the MUD opens or closes (manual 2.4.1)."""
        name = "onconnect" if connected else "ondisconnect"
        candidates = [
            rule for (alias, classes), rule in self._aliases.items()
            if alias == name and self._class_active(classes)
        ]
        if candidates:
            self._run_rule(candidates[-1].body, _Scope([""]))

    def _run_rule(self, body: str, scope: _Scope) -> None:
        """Run commands as one script (a rule firing, an alarm, a file loading): an #Abort
        of either kind ends it here."""
        if self._run_depth == 0:
            self._start_budget(_SCRIPT_SECONDS)  # covers this script and everything it calls
        self._run_depth += 1
        try:
            self._execute_body(body, scope)
        except _ABORTS:
            pass
        except Exception as error:  # noqa: BLE001 - one broken script mustn't stop the MUD's lines
            if self._api.diag is not None:
                self._api.diag.event(
                    "script.error", source=self._api.source or "?",
                    error=f"{type(error).__name__}: {error}"[:300],
                )
        finally:
            self._run_depth -= 1

    def _define_alias(self, args: list[_Tok], scope: _Scope) -> None:
        name = args[0].text.strip()
        body = args[1].text if len(args) > 1 else ""
        classes = self._rule_classes(args[2] if len(args) > 2 else None, scope)
        self._remove_alias((name.lower(), classes))
        self._register_alias(name, body, classes)

    def _register_alias(self, name: str, body: str, classes: tuple[str, ...]) -> None:
        """``#ALIAS``: the first word of what the player types names the alias.

        Real packs are written that way: ``#alias {dk} {dirtkick %1}`` expects "dk goblin" to
        dirtkick the goblin, and Cogg's ``#Alias {reply} {tell @tellname %0}`` expects %0 to be
        the whole message. So the alias matches only when its name is the first word, ``%0`` is
        everything after it and ``%1``..``%9`` are those words one at a time. Matching a bare
        prefix instead let Prometheus's one-letter fly-mode aliases ("s", "n", "e") swallow
        "say", "north" and "exits", and dropped every argument. VIPMud is case-insensitive
        elsewhere (variable names), so alias names are too. A name that carries wildcards
        ("gt *") is a pattern instead, and has to match the whole command.
        """
        engine_name = self._engine_name()
        if _ALIAS_PATTERN_RE.search(name) or name.split() != [name]:
            pattern = f"(?is)^{vip_pattern_to_regex(name)}$"
            rule = self._api.add_alias(pattern, self._runner(body), regex=True, name=engine_name)
        else:
            pattern = rf"(?is)^{re.escape(name)}(?:\s+(.*?))?\s*$"
            rule = self._api.add_alias(
                pattern, self._alias_runner(body), regex=True, name=engine_name
            )
        rule.enabled = self._class_active(classes)
        self._aliases[(name.lower(), classes)] = _VipRule(
            body, classes, engine_name, rule, self._origin()
        )

    def _alias_runner(self, body: str):
        def run(ctx: MatchContext) -> None:
            parameters = ctx.wildcards[1] if len(ctx.wildcards) > 1 else ""
            # %0 is everything typed after the alias name; %1.. are its parameters in turn.
            self._run_rule(body, _Scope([parameters, *_split_parameters(parameters)], ctx.line))

        return run

    def _runner(self, body: str):
        def run(ctx: MatchContext) -> None:
            for name, value in ctx.named.items():  # named wildcards become @vars
                self._set_var(_fold(name), value or "")
            self._run_rule(body, _Scope(ctx.wildcards, ctx.line))

        return run

    def _execute_body(self, body: str, scope: _Scope) -> None:
        self._run_statements(tokenize_statements(body), 0, scope)

    def _start_budget(self, seconds: float) -> None:
        self._steps = 0
        self._deadline = self._script_time() + seconds
        self._budget_noted = False

    def _script_time(self) -> float:
        """The clock budgets run on. It leaves out every wait for the player's answer to a
        dialog, whichever pack asked, so the scripts waiting on one keep their time."""
        return time.monotonic() - self._api.player_time

    def _tick(self) -> None:
        """Count a command or a loop's pass against the running script's budget. Text a pack
        runs with #Exec can come from the MUD, so no script gets to keep the client busy."""
        self._steps += 1
        if self._steps <= _STEP_LIMIT and self._script_time() <= self._deadline:
            return
        if not self._budget_noted and self._api.diag is not None:
            self._budget_noted = True
            self._api.diag.event("vipmud.script_stopped", source=self._api.source,
                                 reason="ran too long", commands=self._steps)
        raise _AbortScript

    def _run_statements(
        self, statements: list[tuple[str | None, list[_Tok]]], start: int, scope: _Scope
    ) -> None:
        for index in range(start, len(statements)):
            command, args = statements[index]
            self._tick()
            if command == "WAIT":
                # #Wait holds the rest of these commands for a while. Only this list waits:
                # whatever called it carries on, which is all the packs' #Waits need.
                delay = self._delay(args, scope)
                if delay is not None:
                    self._api.add_timer(
                        delay, lambda index=index: self._resume(statements, index + 1, scope)
                    )
                    return
                continue
            self._execute_statement(command, args, scope)

    def _resume(
        self, statements: list[tuple[str | None, list[_Tok]]], start: int, scope: _Scope
    ) -> None:
        """The rest of a script after its #Wait: a script of its own, with its own budget,
        which whatever it calls shares."""
        self._start_budget(_SCRIPT_SECONDS)
        self._run_depth += 1
        try:
            self._run_statements(statements, start, scope)
        except _ABORTS:
            pass
        except Exception as error:  # noqa: BLE001 - as in _run_rule
            if self._api.diag is not None:
                self._api.diag.event(
                    "script.error", source=self._api.source or "?",
                    error=f"{type(error).__name__}: {error}"[:300],
                )
        finally:
            self._run_depth -= 1

    def _delay(self, args: list[_Tok], scope: _Scope) -> float | None:
        """Seconds for #Wait and #Alarm, worked out once from what the script wrote (a time
        from the MUD stays text). Packs build them with the decimal separator their system
        uses ("0,50"), so a comma is a decimal point here; "(0.1)" works too."""
        if not args:
            return None
        value = self._evaluate(args[0].text, scope)
        if value is None:
            return None
        number = _number(value)
        if number is None:
            number = _number(value.replace(",", "."))
        return float(number) if number is not None else None

    def _execute_statement(self, command: str | None, args: list[_Tok], scope: _Scope) -> None:
        if command is None:
            self._bare_or_send(args, scope)
            return
        command = _command_name(command)
        if command in _TRIGGER_COMMANDS and args:
            self._define_trigger(args, scope)
        elif command in _ALIAS_COMMANDS and args:
            self._define_alias(args, scope)
        elif command in _KEY_COMMANDS and args:
            self._define_key(args, scope)
        elif command in (_UNTRIGGER_COMMANDS | _UNALIAS_COMMANDS | _UNKEY_COMMANDS) and args:
            target = self._expand(args[0].text, scope)
            class_name = self._expand(args[1].text, scope).strip().lower() if len(args) > 1 else ""
            if command in _UNTRIGGER_COMMANDS:
                name, pattern = _split_trigger_name(target)
                self._remove_trigger(name.lower() if name else pattern, class_name)
                self._remove_trigger(pattern.lower(), class_name)  # or the trigger's name
            elif command in _UNALIAS_COMMANDS:
                self._unalias(target.strip().lower(), class_name)
            else:
                self._remove_key(vip_key_to_combo(target), class_name)
        elif command in ("CLASS", "GCLASS", "UNCLASS", "DELCLASS") and args:
            self._execute_class(command, args, scope)
        elif command == "SAY" and args:
            self._execute_say(args, scope)
        elif command == "SUBSTITUTE" and args and scope.line is not None:
            # Shown and spoken in place of the line; triggers after this one still see the
            # line as the MUD sent it, so the packs' sounds on it still play.
            scope.line.substitute = self._expand(args[0].text, scope)
        elif command == "SETSPEECH" and len(args) > 1:
            self._execute_setspeech(args, scope)
        elif command == "GAGON":
            self._execute_gagon(args, scope)
        elif command == "UNGAGON":
            self._gagging_all = False
            self._api.set_line_speech(True)
        elif command == "UNGAGLINE" and scope.line is not None:
            scope.line.gagged = False
            scope.line.display_when_gagged = False
            self._gag_left = 0  # and no more of the lines a #GagLine count still had to go
        elif command == "FIND" and args:
            self._execute_find(args, scope)
        elif command in ("PLAY", "PLAYLOOP") and args:
            self._play(args, scope, loop=command == "PLAYLOOP")
        elif command in ("VARIABLE", "GVARIABLE") and args:
            # #Var name [value]: no value still defines the variable, empty.
            name = _fold(self._expand(args[0].text, scope))  # @-indirect target supported
            value = self._expand(args[1].text, scope) if len(args) > 1 else ""
            if command == "GVARIABLE":  # global namespace; @-reads still resolve it
                self._api.set_gvar(name, value)
            else:
                self._set_var(name, value)
        elif command == "UNVARIABLE" and args:
            self._api.delete_var(_fold(self._expand(args[0].text, scope)))
        elif command == "GUNVARIABLE" and args:
            self._api.delete_gvar(_fold(self._expand(args[0].text, scope)))
        elif command in ("MATH", "GMATH") and len(args) > 1:
            self._execute_math(args, scope)
        elif command in ("ADDITEM", "GADDITEM") and len(args) > 1:
            self._execute_additem(args, scope)
        elif command == "STOP":
            # VIPMud #Stop halts every playing sound. The api default channel ("sound")
            # is one this dialect never plays on -- every cue goes to a per-cue vip-N
            # channel -- so a plain stop() was a no-op against our own loops.
            self._pending_cues.clear()
            self._api.flush()
        elif command == "IF" and args:
            self._execute_if(args, scope)
        elif command == "EXECUTE" and args:
            # #Exec runs its parameter, expanded once more, as commands of its own.
            self._execute_body(self._expand(args[0].text, scope), scope)
        elif command == "FORALL" and len(args) > 1:
            self._execute_forall(args, scope)
        elif command in ("ALARM", "GALARM") and len(args) > 1:
            self._execute_alarm(args, scope)
        elif command == "GAGLINE" and scope.line is not None:
            # "#gagline [count] voice" gags self-voice but keeps the line reviewable; without a
            # "voice" arg ("all", a bare count, or nothing) it removes the line entirely. Packs
            # play a sound in the gagged line's place. (SC writes "voice"; Prometheus "1 Voice".)
            voice = any(a.text.lower() == "voice" for a in args)
            scope.line.gagged = True
            scope.line.display_when_gagged = voice
            counts = [_number(self._expand(a.text, scope)) for a in args]
            count = next((int(n) for n in counts if n is not None), 1)
            if count > 1:
                self._gag_left, self._gag_voice_only = count - 1, voice
                self._gag_from = scope.line
                self._add_gag_rule()
        elif command == "ABORT":
            raise _AbortScript if args else _AbortLevel
        elif command.isdigit():
            self._execute_repeat(int(command), args, scope)
        elif command in ("WHILE", "UNTIL") and len(args) > 1:
            self._execute_loop(command == "UNTIL", args, scope)
        elif command in ("UNALARM", "GUNALARM") and args:
            self._execute_unalarm(args, scope)
        elif command == "PC" and len(args) > 1:
            self._execute_pc(args, scope)
        elif command == "LOAD" and args:
            flags = self._options(args[1:], scope)
            if "noexecute" not in flags:
                self._load_file(self._expand(args[0].text, scope), keep="noremove" in flags)
        elif command == "UNLOAD" and args:
            real = self._resolve_in_pack(self._expand(args[0].text, scope), must_exist=True)
            if real is not None:
                self._unload(str(real))
        elif command == "FILE" and len(args) > 1:
            self._open_file(args, scope)
        elif command in ("READ", "GREAD") and len(args) > 1:
            self._read_file(args, scope)
        elif command == "WRITE" and len(args) > 1:
            self._write_file(args, scope)
        elif command == "CLOSE" and args:
            self._close_file(self._expand(args[0].text, scope).strip())
        elif command == "DELFILE" and args:
            self._delete_file(self._expand(args[0].text, scope).strip())
        elif command == "SEND" and args:
            self._send_file(args, scope)
        elif command == "CLIPBOARD" and args:
            self._clipboard = self._expand(args[0].text, scope)
        elif command == "RELOAD":
            self._reload()
        elif command == "DIRECTION" and len(args) > 1:
            way = self._expand(args[0].text, scope).strip().lower()
            back = self._expand(args[1].text, scope).strip()
            if way and back:
                self._directions[way] = back
        elif command == "PICK" and args:
            self._execute_pick(args, scope)
        elif command in ("PROMPT", "GPROMPT") and args:
            self._execute_prompt(command == "GPROMPT", args, scope)
        # Other #commands are ignored rather than sent to the MUD: the ones for VIPMud's own
        # windows and menus (#Edit, #Window, #AutoMenu, #SysKey, #Tab) have genericMud
        # equivalents.

    def _options(self, args: list[_Tok], scope: _Scope) -> set[str]:
        """Option words like "NoDisplay|Current", lowercased, from every parameter given."""
        return {
            word.strip().lower()
            for arg in args for word in self._expand(arg.text, scope).split("|") if word.strip()
        }

    def _execute_say(self, args: list[_Tok], scope: _Scope) -> None:
        """``#Say text [VoiceOnly|NoDisplay|NoVoice|...]``: speak the text and show it in the
        output, unless an option says otherwise (manual 5.2.47)."""
        text = self._expand(args[0].text, scope)
        if not text.strip():
            return
        options = self._options(args[1:], scope)
        incoming = not options & _OTHER_VOICES  # the Incoming voice, which #SetSpeech can mute
        if "novoice" not in options and (not incoming or self._api.line_speech()):
            self._api.speak(text)
        if not options & {"nodisplay", "voiceonly"}:
            self._api.echo(text)

    def _execute_setspeech(self, args: list[_Tok], scope: _Scope) -> None:
        """``#SetSpeech voice-type On|Off|Stop``. The incoming voice reads the MUD's lines,
        so switching it off and on is the speech mute; Stop flushes speech. Voices and rates
        are genericMud's to choose, so the SAPI settings are ignored."""
        voice = self._expand(args[0].text, scope).strip().lower()
        mode = self._expand(args[1].text, scope).strip().lower()
        if mode == "stop":
            self._api.stop_speech()
        elif voice == "incoming" and mode in ("off", "none"):
            self._api.set_line_speech(False)
        elif voice == "incoming" and mode in ("on", "sapi", "screenreader"):
            self._api.set_line_speech(True)

    def _execute_gagon(self, args: list[_Tok], scope: _Scope) -> None:
        """``#GagOn [Voice|Display|All]``: keep the MUD's lines from being spoken (Voice) or
        from being shown and spoken (All, the default) until #UnGagOn. A line can't be hidden
        from the screen and still spoken here, so Display alone changes nothing."""
        mode = (self._options(args, scope) & {"voice", "display", "all"} or {"all"}).pop()
        if mode == "display":
            return
        self._api.set_line_speech(False)
        if mode == "all":
            self._gagging_all = True
            self._add_gag_rule()

    def _add_gag_rule(self) -> None:
        """The trigger behind #GagOn All and #GagLine's count. It runs after every other
        trigger, so an #UnGagOn or #UnGagLine on the same line wins."""
        if self._gag_rule is None:
            self._gag_rule = self._api.add_trigger(
                r".", self._gag_everything, regex=True, priority=-1, name=self._engine_name(),
            )

    def _gag_everything(self, ctx: MatchContext) -> None:
        line = ctx.line
        if self._gag_left > 0 and line is not self._gag_from:
            self._gag_left -= 1
            line.gagged = True
            line.display_when_gagged = line.display_when_gagged or self._gag_voice_only
        if self._gagging_all:
            line.gagged = True
            line.display_when_gagged = False

    def _execute_find(self, args: list[_Tok], scope: _Scope) -> None:
        """``#Find text [First|Next|Forward|Reverse]``: look through the output for a line
        with the text in it and put the line in %FoundLine (empty when there's none)."""
        needle = self._expand(args[0].text, scope).strip().casefold()
        options = self._options(args[1:], scope)
        lines = self._api.recent_lines(_FIND_LINES)
        reverse = "reverse" in options
        again = "next" in options and self._find_index is not None
        if reverse:
            start = self._find_index - 1 if again else len(lines) - 1
            order = range(min(start, len(lines) - 1), -1, -1)
        else:
            order = range(self._find_index + 1 if again else 0, len(lines))
        self._found_line, self._find_index = "", None
        for index in order:
            if needle and needle in lines[index].casefold():
                self._found_line, self._find_index = lines[index], index
                return

    def _execute_pick(self, args: list[_Tok], scope: _Scope) -> None:
        """``#Pick list`` (manual 5.2.36): show the list and run what the player picks as if
        they had typed it, inside this script, so a picked ``#Abort`` ends it. Cancelling
        runs nothing and the script goes on."""
        picked = self._ask_pick(args[0].text, scope)
        if picked is not None:
            self._execute_body(picked[0], scope)

    def _ask_pick(self, text: str, scope: _Scope) -> tuple[str, str] | None:
        """Show a #Pick list. Returns the picked item's commands, for #Pick to run, and its
        value, for %Pick to return; None when the player cancels."""
        listing = self._pick_list(text, scope)
        if not listing.captions:
            return None
        index = self._api.ask_choice(
            listing.title, listing.title or "Choose an item", listing.captions,
            listing.selected if listing.selected is not None else 0,
            while_waiting=self._run_due_alarms,
        )
        if index is None:
            return None
        return listing.commands[index], listing.results[index]

    def _pick_list(self, text: str, scope: _Scope) -> _PickList:
        """Read a #Pick list (manual 5.2.36): items between ``|``, each ``caption:value`` or
        one text that is both. A ``P`` caption makes its value the list's title, and a ``*``
        before a caption starts the cursor there.

        Packs write values that are whole scripts, so a ``|`` or ``:`` inside braces belongs
        to its item, ``~|`` is a pipe in an item, and the first ``:`` splits (Cosmic Rage's
        titles end in one). An item written in the list is expanded part by part when the
        list is shown: the unofficial Prometheus options menu words each toggle's message by
        the value it had then, and a colon in a variable stays in its caption (Cosmic Rage
        shows the line being gagged, "Bob says: *"). An item without a caption can be a
        variable holding a whole list of its own (Star Conquest's ``|@CurrentBufferList``,
        Miriani's ``#Pick {@SCGCTmp3}``), whose items are written the same way.
        """
        listing = _PickList()
        for item in _split_top_level(text, "|"):
            caption, value = _split_caption(item)
            if value is not None:
                value = self._expand(value, scope)
                listing.add(self._expand(caption, scope), value, value, titled=True)
                continue
            for inner in _split_top_level(self._expand(item, scope), "|"):
                caption, value = _split_caption(inner)
                titled = value is not None
                if value is None:
                    value = caption.strip().removeprefix("*")
                listing.add(_unescape(caption), value, _unescape(value), titled=titled)
        return listing

    def _execute_prompt(self, is_global: bool, args: list[_Tok], scope: _Scope) -> None:
        """``#Prompt variable text`` (manual 5.2.39): ask the player to type the variable's
        value. Cancelling leaves the variable as it was. #GPrompt sets a global one."""
        name = _fold(self._expand(args[0].text, scope).strip())
        if not name:
            return
        text = self._expand(args[1].text, scope).strip() if len(args) > 1 else ""
        answer = self._api.ask_text(text, text or name, while_waiting=self._run_due_alarms)
        if answer is None:
            return
        if is_global:
            self._api.set_gvar(name, answer)
        else:
            self._set_var(name, answer)

    def _resolve_in_pack(self, reference: str, *, must_exist: bool) -> Path | None:
        """Resolve a pack-relative path: an exact file under the pack dir, else the first match
        by basename (case-insensitive — Windows-authored packs are careless about case and the
        in-pack layout rarely matches a script's hardcoded path). When ``must_exist`` is False
        and nothing matches, return ``base/target`` so a new file (e.g. saved settings) can be
        created there. Always confined to the pack dir (rglob can surface escaping symlinks)."""
        if not self._base_dir:
            return None
        base = Path(self._base_dir).resolve()
        target = Path(reference.replace("\\", "/"))
        if not target.name:
            return None
        candidate = target if target.is_absolute() else base / target
        try:
            real = candidate.resolve()
        except OSError:
            real = None
        if real is not None and real.is_file() and real.is_relative_to(base):
            return real
        wanted = target.name.lower()
        for match in sorted(base.rglob("*")):
            if match.name.lower() != wanted:
                continue
            try:
                resolved = match.resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved.is_relative_to(base):
                return resolved
        if must_exist:
            return None
        # Creating a new file (a settings file #Write will flush): confine it strictly to the
        # pack dir. Refuse an absolute/UNC/.. reference or anything resolving outside the pack,
        # so #file/#write can't create or overwrite a file elsewhere on disk.
        return confine(base, reference)

    def load_entry(self, path: str | Path) -> None:
        """Load the pack's own load script, remembered as a file like any #Load'ed one. An
        unreadable entry raises, so activation reports the pack as failed. A linked pack
        always starts at its scripts' Main.set, as its setup scripts arrange; installs before
        that was understood recorded its sounds folder's main.set."""
        linked = linked_vipmud_scripts(self._base_dir) if self._base_dir else None
        real = (linked or Path(path)).resolve()
        self._entry = real
        self._load_path(real, keep=False, source=_read_script(real))

    def _reload(self) -> None:
        """``#Reload`` (manual 5.2.44): remove every trigger, alias, key and variable the
        pack has, then run its start-up script again. Packs' "reload the soundpack" commands
        end with it."""
        if self._entry is None:
            return
        for key in list(self._triggers):
            self._remove_trigger(key)
        for alias_key in list(self._aliases):
            self._remove_alias(alias_key)
        for combo in list(self._keys):
            self._remove_key(combo)
        for handle in list(self._files):
            self._close_file(handle)
        for name in self.written_variables:
            self._api.delete_var(name)
        self.written_variables.clear()
        self._alarms.clear()
        self._enabled_classes.clear()
        self._current_classes = ()
        self._loaded.clear()
        self._gagging_all, self._gag_left = False, 0
        self._wire_paths()
        self.load_entry(self._entry)

    def _load_file(self, reference: str, *, keep: bool = False) -> None:
        """``#LOAD {file} [NoRemove]``: run another ``.set`` from the pack, so a loader script
        pulls in all the pack's scripts. Confined to the pack dir."""
        real = self._resolve_in_pack(reference, must_exist=True)
        if real is not None:
            self._load_path(real, keep=keep)

    def _load_path(self, real: Path, *, keep: bool, source: str | None = None) -> None:
        """Loading a file again replaces what it defined last time, unless NoRemove says to
        keep it, then runs it again (Prometheus loads its whole pack on every login). A file
        loading itself, directly or through others, is skipped."""
        key = str(real)
        if key in self._loading:
            return
        if source is None:
            try:
                source = _read_script(real)
            except OSError:
                return
        if _WRITES_FILES_RE.search(source):
            self.saves_its_own_settings = True
        if key in self._loaded and not keep:
            self._unload(key)
        self._loaded.add(key)
        self._loading.append(key)
        budget = (self._steps, self._deadline, self._budget_noted)
        self._start_budget(_LOAD_SECONDS)  # each file has its own: a big pack is many commands
        self._run_depth += 1  # so the file's own script doesn't start a smaller budget
        try:
            self._run_rule(source, _Scope([]))
        finally:
            self._run_depth -= 1
            self._loading.pop()
            self._steps, self._deadline, self._budget_noted = budget

    def _unload(self, key: str) -> None:
        """``#UnLoad``: remove the triggers, aliases and keys a file defined."""
        self._loaded.discard(key)
        for name, rule in list(self._triggers.items()):
            if rule.origin == key:
                self._remove_trigger(name)
        for alias_key, rule in list(self._aliases.items()):
            if rule.origin == key:
                self._remove_alias(alias_key)
        for combo, definitions in list(self._keys.items()):
            for classes in [c for c, rule in definitions.items() if rule.origin == key]:
                del definitions[classes]
            if not definitions:
                self._remove_key(combo)

    def _origin(self) -> str | None:
        return self._loading[-1] if self._loading else None

    def _execute_additem(self, args: list[_Tok], scope: _Scope) -> None:
        """``#AddItem list item [NoDuplicates]``: add an item to the end of a | list."""
        name = _fold(self._expand(args[0].text, scope).strip())
        item = self._expand(args[1].text, scope)
        current = self._api.get_var(name, "") or ""
        items = _list_items(current)
        if "noduplicates" in self._options(args[2:], scope) and any(
            existing.casefold() == item.casefold() for existing in items
        ):
            return
        self._set_var(name, f"{current}|{item}" if current else item)

    def _open_file(self, args: list[_Tok], scope: _Scope) -> None:
        """``#File handle {path} [record-length]``. Handles 1 to 5 are text files, read a
        line at a time or written by appending; 6 to 10 are record files. A record file is
        read now: reading the pack's shipped settings is what turns its sound-enable flags
        on (Star Conquest gates every #Play on @silent=1, which lives in Settings.set).
        Giving a handle a new file closes the old one first."""
        handle = self._expand(args[0].text, scope).strip()
        reference = self._expand(args[1].text, scope)
        self._close_file(handle)
        if handle in _SEQUENTIAL_HANDLES:
            self._files[handle] = _TextFile(reference)
            return
        path = self._resolve_in_pack(reference, must_exist=False)
        values: list[str] = []
        if path is not None and path.is_file():
            try:
                values = _parse_vip_settings(path.read_bytes())
            except OSError:
                values = []
        self._files[handle] = _VipFile(path=path, values=values)

    def _text_lines(self, text_file: _TextFile) -> list[str]:
        if text_file.lines is None:
            path = self._resolve_in_pack(text_file.reference, must_exist=True)
            try:
                content = path.read_text(encoding="latin-1") if path is not None else ""
            except OSError:
                content = ""
            text_file.lines = content.splitlines()
        return text_file.lines

    def _read_file(self, args: list[_Tok], scope: _Scope) -> None:
        """``#Read handle variable [record]``: the next line of a text file ("[EOF]" past the
        end), or the 1-based record of a record file."""
        opened = self._files.get(self._expand(args[0].text, scope).strip())
        name = _fold(self._expand(args[1].text, scope).strip())
        if isinstance(opened, _TextFile):
            lines = self._text_lines(opened)
            value = lines[opened.position] if opened.position < len(lines) else "[EOF]"
            opened.position += 1
            self._set_var(name, value)
        elif isinstance(opened, _VipFile) and len(args) > 2:
            index = _to_int(self._expand(args[2].text, scope), 0) - 1
            if 0 <= index < len(opened.values):
                self._set_var(name, opened.values[index])

    def _write_file(self, args: list[_Tok], scope: _Scope) -> None:
        """``#Write handle value [record|NoReturn]``: append a line to a text file (with no
        line ending after NoReturn), or set a 1-based record (flushed on #Close)."""
        opened = self._files.get(self._expand(args[0].text, scope).strip())
        value = self._expand(args[1].text, scope)
        if isinstance(opened, _TextFile):
            ending = "" if "noreturn" in self._options(args[2:], scope) else "\r\n"
            path = self._writable(opened.reference)
            if path is None:
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="latin-1", errors="replace", newline="") as handle:
                    handle.write(value + ending)
            except OSError:
                pass
        elif isinstance(opened, _VipFile) and len(args) > 2:
            index = _to_int(self._expand(args[2].text, scope), 0) - 1
            if index < 0:
                return
            while len(opened.values) <= index:
                opened.values.append("")
            opened.values[index] = value
            opened.dirty = True

    def _writable(self, reference: str) -> Path | None:
        """Where a text file the pack writes goes: exactly the path it named, inside the pack.
        Not a same-named file somewhere else in the pack, which reading can fall back on."""
        if not self._base_dir:
            return None
        base = Path(self._base_dir).resolve()
        reference = reference.strip()
        if is_absolute(reference):  # built from %HomeFolder, say
            path = Path(reference.replace("\\", "/") if os.sep == "/" else reference)
            return path.resolve() if within(base, path) and path.resolve() != base else None
        return confine(base, reference)

    def _close_file(self, handle: str) -> None:
        """``#Close handle``: flush a written record file back to disk and drop the handle."""
        opened = self._files.pop(handle, None)
        if isinstance(opened, _VipFile) and opened.dirty and opened.path is not None:
            try:
                opened.path.parent.mkdir(parents=True, exist_ok=True)
                opened.path.write_bytes(_serialize_vip_settings(opened.values))
            except OSError:
                pass

    def _delete_file(self, reference: str) -> None:
        """``#DelFile path``: delete that file in the pack, if it's there. Never a file
        outside the pack, and never a program or settings file (manual 5.2.15)."""
        path = self._writable(reference)
        if path is None or path.suffix.lower() in _PROTECTED_SUFFIXES:
            return
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass

    def _send_file(self, args: list[_Tok], scope: _Scope) -> None:
        """``#Send file [prefix] [suffix]``: send each line of a text file in the pack to the
        MUD as written, with the prefix and suffix around it."""
        path = self._resolve_in_pack(self._expand(args[0].text, scope), must_exist=True)
        if path is None:
            return
        prefix = self._expand(args[1].text, scope) if len(args) > 1 else ""
        suffix = self._expand(args[2].text, scope) if len(args) > 2 else ""
        suffix = suffix.replace("\r", "").replace("\n", "")  # each line is sent as a line anyway
        try:
            lines = path.read_text(encoding="latin-1").splitlines()
        except OSError:
            return
        for line in lines[:_SEND_LIMIT]:
            self._api.send(prefix + line + suffix)

    def _bare_or_send(self, args: list[_Tok], scope: _Scope) -> None:
        if not args:
            return
        text = args[-1].text
        if text.startswith(">"):
            # The manual's pass-through: the rest of the line goes to the MUD as written.
            self._api.send(text[1:])
            return
        assignment = _ASSIGN_RE.match(text)
        if assignment:  # "@name = value" sets a variable; it isn't sent to the MUD
            self._set_var(_fold(assignment.group(1)), self._expand(assignment.group(2), scope))
            return
        # Bare text runs "as if typed": VIPMud expands aliases in script bodies (packs
        # call their own aliases by name, e.g. CR's `makebetter`), so route through the
        # alias pipeline; unmatched text still reaches the wire. Depth-capped: an alias
        # whose body re-invokes itself would otherwise recurse without bound.
        if self._exec_depth >= _MAX_EXEC_DEPTH:
            return
        self._exec_depth += 1
        try:
            self._api.execute(self._expand(text, scope))
        finally:
            self._exec_depth -= 1

    def _play(self, args: list[_Tok], scope: _Scope, *, loop: bool) -> None:
        file = _expand_sound_variant(self._expand(args[0].text, scope))
        volume = _DEFAULT_VOLUME
        if len(args) > 1:
            volume = _to_int(self._expand(args[1].text, scope), _DEFAULT_VOLUME)
        handle = str(self._next_handle)
        self._next_handle += 1
        channel = f"vip-{handle}"  # one channel per cue so #pc can target it later
        self._handles[handle] = channel
        if loop:
            self._last_loop_handle = handle
        # Only a pack with an online sound source asks first whether the file is here: for the
        # rest the answer changes nothing, and finding a file twice doubled each cue's cost.
        if (
            self.can_fetch_sound(file)
            and not self._api.sound_exists(file)
            and self._fetch_in_background(file, channel, volume, loop)
        ):
            self._last_handle = handle  # it will play once the file lands
            return
        played = self._api.play(file, channel=channel, gain=volume / 100, loop=loop)
        # VIPMud reports a failed play as handle 0, and packs branch on it -- Cosmic
        # Rage speaks the failure and auto-downloads the missing file.
        self._last_handle = handle if played else "0"

    def _fetch_in_background(self, file: str, channel: str, volume: int, loop: bool) -> bool:
        """Start (or join) a download of a missing sound; True if the cue will play later.

        The pack's installer names an online sound tree (Cosmic Rage's VIPMud pack: several GB
        on its gitea), so a missing .wav is fetched the first time a cue wants it. That used to
        happen right here, inside line processing, on the event loop every tab and all speech
        share, with no timeout: each new sound froze the client while it downloaded, a
        stalled server froze it for good, and a file the server lacked was fetched again on
        every cue. Now the download runs on a worker thread and the cue plays when it lands,
        unless #pc stopped it first; a URL that failed isn't tried again this session.
        """
        request = self._remote_sound_request(file)
        if request is None:
            return False
        _destination, url = request
        failed_at = self._failed_fetches.get(url)
        if failed_at is not None and time.monotonic() - failed_at < _FETCH_RETRY_SECONDS:
            return False  # play() then reports the miss as handle 0, which packs handle
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (a headless load): fetch in place, as before, and play straight away.
            if not self._fetch_remote_sound(file):
                self._failed_fetches[url] = time.monotonic()
            return False
        self._pending_cues[channel] = _PendingCue(file, gain=volume / 100, loop=loop)
        self._waiting.setdefault(url, []).append(channel)
        if url not in self._fetching:
            # Its own thread, not the loop's default executor: asyncio looks up every connect's
            # host name there, so downloads stalled on a slow server held up connecting.
            future = run_in_own_thread(lambda file=file: self._fetch_remote_sound(file))
            self._fetching[url] = future
            self._api.track_pending(future)  # closing the session cancels the late play
            future.add_done_callback(lambda done, url=url: self._fetched(url, done))
        return True

    def _fetched(self, url: str, future: asyncio.Future) -> None:
        """A background download finished (on the event loop): play the cues still waiting."""
        self._fetching.pop(url, None)
        self._api.forget_pending(future)
        channels = self._waiting.pop(url, [])
        landed = not future.cancelled() and future.exception() is None and bool(future.result())
        if not landed:
            if not future.cancelled():
                self._failed_fetches[url] = time.monotonic()
            for channel in channels:
                self._pending_cues.pop(channel, None)
            return
        for channel in channels:
            cue = self._pending_cues.pop(channel, None)
            if cue is None:
                continue  # stopped while it was downloading
            self._api.play(cue.file, channel=channel, gain=cue.gain, pan=cue.pan, loop=cue.loop)
            if cue.frequency is not None:
                self._api.adjust(channel, frequency=cue.frequency)

    def _execute_if(self, args: list[_Tok], scope: _Scope) -> None:
        branch = 1 if self._condition(args[0].text, scope) else 2  # then : else
        if len(args) > branch:
            self._execute_body(args[branch].text, scope)

    def _execute_forall(self, args: list[_Tok], scope: _Scope) -> None:
        """``#ForAll {a|b|c} {body}`` — run ``body`` once per ``|``-separated item, with the
        item in ``%i``. VIPMud loaders use it to pull in every script, e.g.
        ``#ForAll {combat|ground|...} {#load {Scripts\\%I.set}}``."""
        body = args[1].text
        items = [item.strip() for item in self._expand(args[0].text, scope).split("|")]
        for number, item in enumerate([item for item in items if item][:_LOOP_LIMIT], 1):
            self._tick()
            try:
                self._execute_body(body, scope.looping(item, number))
            except _AbortLevel:
                continue  # a plain #Abort ends this pass; the loop goes on

    def _execute_repeat(self, count: int, args: list[_Tok], scope: _Scope) -> None:
        """``#10 {commands}``: run them that many times, with the pass number in %repeatnum
        (and %i)."""
        if not args:
            return
        for number in range(1, min(count, _REPEAT_LIMIT) + 1):
            self._tick()
            try:
                self._execute_body(args[0].text, scope.looping(str(number), number))
            except _AbortLevel:
                continue

    def _execute_loop(self, until: bool, args: list[_Tok], scope: _Scope) -> None:
        """``#While {condition} {commands}`` runs while the condition holds; ``#Until``
        runs the commands first and stops once it holds."""
        for _ in range(_LOOP_LIMIT):
            self._tick()
            if not until and not self._condition(args[0].text, scope):
                return
            try:
                self._execute_body(args[1].text, scope)
            except _AbortLevel:
                pass
            if until and self._condition(args[0].text, scope):
                return
        if self._api.diag is not None:
            self._api.diag.event("vipmud.loop_stopped", source=self._api.source,
                                 limit=_LOOP_LIMIT)

    def _execute_alarm(self, args: list[_Tok], scope: _Scope) -> None:
        """``#Alarm [variable] [-]seconds [commands]`` (manual 5.2.3): run the commands after
        that many seconds, or every that many seconds when negative. The variable gets the
        alarm's number for #UnAlarm; given a variable and a time but no commands, it sets a
        new time for that alarm.

        Packs defer loading the rest of the pack until the login line arrives with
        ``#alarm 0 {#load ...}``. Alarms fire only when an event loop drives the engine's
        scheduler (the live app); under a no-op scheduler they never run.
        """
        name = ""
        delay = self._delay(args[:1], scope)
        body: _Tok | None = args[1] if len(args) > 1 else None
        if delay is None:  # the first parameter names the variable
            name = _fold(self._expand(args[0].text, scope).strip())
            delay = self._delay(args[1:2], scope)
            body = args[2] if len(args) > 2 else None
        if delay is None:
            return
        existing = _to_int(self._api.get_var(name, "") or "", 0) if name else 0
        if body is None:
            alarm = self._alarms.get(existing)
            if alarm is not None:
                alarm.seconds, alarm.repeating = abs(delay), delay < 0
                self._schedule_alarm(existing, alarm)
            return
        self._alarms.pop(existing, None)  # setting a named alarm again replaces it
        self._alarm_count += 1
        alarm = _Alarm(body.text, abs(delay), delay < 0)
        self._alarms[self._alarm_count] = alarm
        if name:
            self._set_var(name, str(self._alarm_count))
        self._schedule_alarm(self._alarm_count, alarm)

    def _schedule_alarm(self, number: int, alarm: _Alarm) -> None:
        alarm.generation += 1
        generation = alarm.generation
        seconds = max(alarm.seconds, _MIN_REPEATING_ALARM) if alarm.repeating else alarm.seconds
        alarm.due = time.monotonic() + seconds
        self._api.add_timer(seconds, lambda: self._fire_alarm(number, generation))

    def _run_due_alarms(self) -> None:
        """Run this pack's alarms that come due while a dialog waits for the player, as
        VIPMud's do: Cosmic Rage speaks a menu's instructions from an alarm it sets just
        before showing it. Each runs as a script of its own, and the script waiting on the
        player carries on afterwards as it was. Their timers find them already run."""
        now = time.monotonic()
        due = [
            (number, alarm.generation) for number, alarm in self._alarms.items()
            if alarm.due <= now
        ]
        if not due:
            return
        waiting = (
            self._steps, self._deadline, self._budget_noted, self._run_depth,
            self._exec_depth, self._call_depth,
        )
        self._run_depth = self._exec_depth = self._call_depth = 0
        try:
            for number, generation in due:
                self._fire_alarm(number, generation)
        finally:
            (
                self._steps, self._deadline, self._budget_noted, self._run_depth,
                self._exec_depth, self._call_depth,
            ) = waiting

    def _fire_alarm(self, number: int, generation: int) -> None:
        alarm = self._alarms.get(number)
        if alarm is None or alarm.generation != generation:
            return  # cancelled, or set to a new time since
        if alarm.repeating:
            self._schedule_alarm(number, alarm)
        else:
            del self._alarms[number]
        # It runs on its own: the wildcards of whatever set it are gone.
        self._run_rule(alarm.body, _Scope([]))

    def _execute_unalarm(self, args: list[_Tok], scope: _Scope) -> None:
        """``#UnAlarm @variable`` (or its name, or a number) stops that alarm; a negative
        number stops them all."""
        text = self._expand(args[0].text, scope).strip()
        number = _number(text)
        if number is None:
            number = _number(self._api.get_var(_fold(text), "") or "")
        if number is None:
            return
        if number < 0:
            self._alarms.clear()
        else:
            self._alarms.pop(int(number), None)

    def _execute_math(self, args: list[_Tok], scope: _Scope) -> None:
        """``#math <var> {expr}`` — arithmetic into a variable. Cosmic Rage computes its
        stereo pan with it (``#math pan {@pan * 50}``), so without it every directional
        cue collapsed to center. A result that isn't a number leaves the variable alone."""
        expression = " ".join(arg.text for arg in args[1:])
        result = self._evaluate(expression, scope)
        if result is None or _number(result) is None:
            return
        self._set_var(_fold(self._expand(args[0].text, scope)), result)

    def _execute_pc(self, args: list[_Tok], scope: _Scope) -> None:
        handle = self._expand(args[0].text, scope).strip()
        action = self._expand(args[1].text, scope).strip().lower()
        value = self._expand(args[2].text, scope) if len(args) > 2 else ""
        number = _number(handle)
        if number is not None and number <= 0:  # zero or less: every cue, and the master
            if action == "stop":
                self._pending_cues.clear()
                self._api.flush()
            elif action == "volume":
                self._master_volume = max(0, min(100, _to_int(value, _DEFAULT_VOLUME)))
                self._api.set_master(self._master_volume / 100)
            return
        channel = self._handles.get(handle)
        if action == "isplaying":
            # "#PC handle IsPlaying variable": non-zero while the sound plays. One still
            # downloading counts; it starts as soon as it lands.
            playing = channel is not None and (
                channel in self._pending_cues or self._api.is_playing(channel)
            )
            if value.strip():
                self._set_var(_fold(value.strip()), "1" if playing else "0")
            return
        if channel is None:
            return
        if action == "stop":
            self._pending_cues.pop(channel, None)  # still downloading: don't start it later
            self._api.stop(channel)
        elif action == "volume":
            # Live change on the playing cue (a per-cue channel's future is one cue, so
            # the old category-policy set_volume adjusted nothing anyone would hear).
            gain = _to_int(value, _DEFAULT_VOLUME) / 100
            if channel in self._pending_cues:
                self._pending_cues[channel].gain = gain  # still downloading: start at this
            else:
                self._api.adjust(channel, gain=gain)
        elif action == "pan":
            # VIPMud pan runs from -5000 (left) to 5000 (right); the bus wants -1..1.
            pan = max(-1.0, min(1.0, _to_int(value, 0) / 5000))
            if channel in self._pending_cues:
                self._pending_cues[channel].pan = pan
            else:
                self._api.adjust(channel, pan=pan)
        elif action == "frequency":
            # The rate the sound plays at, in Hz (VIPMud's examples run 5000 to 44100): higher
            # is higher and faster. 0 puts back the file's own.
            frequency = _number(value)
            if frequency is None or frequency < 0:
                return
            if channel in self._pending_cues:
                self._pending_cues[channel].frequency = frequency
            else:
                self._api.adjust(channel, frequency=frequency)

    # --- substitution ---

    def _expand(self, text: str, scope: _Scope) -> str:
        """``text`` with one pass of substitution: escapes removed, variables, wildcards,
        functions and system variables replaced. What goes in isn't scanned again."""
        if "~" not in text and "@" not in text and "%" not in text:
            return text
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            if c == _ESCAPE and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            found = None
            if c == "@":
                found = self._scan_variable(text, i, scope)
            elif c == "%":
                found = self._scan_percent(text, i, scope)
            if found is not None:
                value, i = found
                out.append(value)
                continue
            out.append(c)
            i += 1
        return "".join(out)

    def _scan_variable(self, text: str, i: int, scope: _Scope) -> tuple[str, int] | None:
        """The variable reference at ``text[i] == "@"``: (its value, the index after it).

        ``@{name}`` takes the name from the braces (so it can run into text, and ``@{@x}``
        names the variable indirectly); otherwise the name is a letter and then letters and
        digits. An ``@`` before anything else is an ordinary character.
        """
        j = i + 1
        n = len(text)
        if j < n and text[j] == "{":
            inner, end = _scan_block(text, j)
            return self._variable(self._expand(inner, scope).strip()), end
        if j < n and text[j].isascii() and text[j].isalpha():
            k = j + 1
            while k < n and text[k].isascii() and text[k].isalnum():
                k += 1
            return self._variable(text[j:k]), k
        return None

    def _variable(self, name: str) -> str:
        return self._api.get_var(_fold(name), "") or ""

    def _scan_percent(self, text: str, i: int, scope: _Scope) -> tuple[str, int] | None:
        """What a ``%`` at ``text[i]`` stands for: a wildcard (``%1``, ``%{1}``), a function
        call or a system variable. None when it is an ordinary percent sign."""
        j = i + 1
        n = len(text)
        if j >= n:
            return None
        c = text[j]
        if c == "{":
            inner, end = _scan_block(text, j)
            key = self._expand(inner, scope).strip()  # %{@n}: the number from a variable
            if key.isdigit():
                return self._wildcard(scope, int(key)), end
            value = self._system_variable(key.lower(), scope)
            return (value, end) if value is not None else None
        if c.isdigit():
            k = j + 1
            if k < n and text[k].isdigit():  # %0 to %99
                k += 1
            return self._wildcard(scope, int(text[j:k])), k
        if not (c.isascii() and c.isalpha()):
            return None
        k = j + 1
        while k < n and text[k].isascii() and text[k].isalnum():
            k += 1
        name = text[j:k].lower()
        if k < n and text[k] == "(":
            if self._call_depth >= _CALL_DEPTH_LIMIT:
                return None
            call = _scan_call(text, k)
            if call is None:
                return None
            raw_args, end = call
            self._call_depth += 1
            try:
                result = self._call_function(name, raw_args, scope)
            finally:
                self._call_depth -= 1
            # A function VIPMud doesn't have stays as written rather than vanishing.
            return (result if result is not None else text[i:end]), end
        value = self._system_variable(name, scope)
        return (value, k) if value is not None else None

    @staticmethod
    def _wildcard(scope: _Scope, index: int) -> str:
        return scope.wildcards[index] if index < len(scope.wildcards) else ""

    def _system_variable(self, name: str, scope: _Scope) -> str | None:
        if name in ("i", "repeatnum"):
            if not scope.loops:
                return ""
            item, number = scope.loops[-1]
            return item if name == "i" else str(number)
        if name == "playhandle":
            return self._last_handle
        if name == "mp3loop":
            return self._last_loop_handle
        if name in ("crlf", "cr", "lf"):
            return {"crlf": "\r\n", "cr": "\r", "lf": "\n"}[name]
        if name == "version":
            return _VIPMUD_VERSION
        if name in ("homefolder", "installfolder", "mudfolder"):
            return self._folder(self._base_dir)
        if name == "soundsfolder":
            return self._folder(self._remote_sound_root or self._base_dir)
        if name in ("mudname", "hosttitle"):
            return self._api.world_name
        if name in ("char", "reference", "password", "logsfolder"):
            return ""  # never the password: a pack has no business holding it
        if name == "connected":
            return "1" if self._api.is_connected() else "0"
        if name == "clipboard":
            return self._clipboard
        if name == "foundline":
            return self._found_line
        if name == "volume":
            return str(self._master_volume)
        if name == "socket":
            return "0"
        return None

    @staticmethod
    def _folder(path: str | Path | None) -> str:
        """A folder as VIPMud gives one, ending in a separator, so {%HomeFolder}Logs works."""
        if not path:
            return ""
        return str(path).rstrip("\\/") + os.sep

    # --- expressions ---

    def _evaluate(self, expression: str, scope: _Scope, *, as_text: bool = False) -> str | None:
        """The value of a VIPMud expression, or None when it can't be evaluated; with
        ``as_text``, its expanded text instead of None. Either way it's worked out once:
        expanding it again would run its functions a second time."""
        if len(expression) > _EXPRESSION_MAX_LENGTH:
            return None
        try:
            tokens = self._expression_tokens(expression, scope)
        except (_ExpressionError, RecursionError):
            return None
        try:
            return _ExpressionParser(tokens).parse()
        except (_ExpressionError, RecursionError):
            return "".join(token.source for token in tokens).strip() if as_text else None

    def _condition(self, expression: str, scope: _Scope) -> bool:
        value = self._evaluate(expression, scope)
        return value is not None and _truthy(value)

    def _expression_tokens(self, text: str, scope: _Scope) -> list[_ExpressionToken]:
        """Tokens of an expression. A variable, wildcard or function is one value whatever
        it holds, so MUD text with "and", "or" or parentheses in it can't change what a
        condition means (Miriani compares camera feeds like "(Bridge) (Bob) says hi" with
        =*). A value that is all one quoted string counts without its quotes: Cosmic Rage
        stores its settings with them and compares @intromusic = "enabled"."""
        tokens: list[_ExpressionToken] = []
        depth = 0
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            if c in " \t\r\n":
                j = i
                while j < n and text[j] in " \t\r\n":
                    j += 1
                tokens.append(_ExpressionToken("space", text[i:j]))
                i = j
                continue
            if c == _ESCAPE and i + 1 < n:
                tokens.append(_ExpressionToken("value", text[i + 1]))
                i += 2
                continue
            if c == '"':
                end = _skip_quoted(text, i)
                closed = end - 1 > i and text[end - 1] == '"'
                inner = text[i + 1 : end - 1] if closed else text[i + 1 :]
                tokens.append(_ExpressionToken("value", self._expand(inner, scope), quoted=True))
                i = end
                continue
            found = None
            if c == "@":
                found = self._scan_variable(text, i, scope)
            elif c == "%":
                found = self._scan_percent(text, i, scope)
            if found is not None:
                value, i = found
                stripped = value.strip()
                quoted = len(stripped) >= 2 and stripped[0] == stripped[-1] == '"'
                tokens.append(
                    _ExpressionToken("value", stripped[1:-1] if quoted else stripped, quoted)
                )
                continue
            if c in "()":
                depth = depth + 1 if c == "(" else max(0, depth - 1)
                if depth > _EXPRESSION_MAX_DEPTH:
                    raise _ExpressionError("parentheses nested too deep")
                tokens.append(_ExpressionToken(c, c))
                i += 1
                continue
            symbol = next((s for s in _EXPRESSION_SYMBOLS if text.startswith(s, i)), None)
            if symbol is not None:
                tokens.append(_ExpressionToken("op", symbol))
                i += len(symbol)
                continue
            if c in _WORD_CHARACTERS:
                j = i
                while j < n and text[j] in _WORD_CHARACTERS:
                    j += 1
                word = text[i:j]
                if word.lower() in _EXPRESSION_WORDS:
                    tokens.append(_ExpressionToken("op", word.lower()))
                else:
                    tokens.append(_ExpressionToken("value", word))
                i = j
                continue
            tokens.append(_ExpressionToken("value", c))
            i += 1
        return tokens

    # --- functions (manual 5.3) ---

    def _text_argument(self, args: list[str], index: int, scope: _Scope, default: str = "") -> str:
        """An argument used as text: expanded once, and unquoted when it is one quoted string."""
        if index >= len(args):
            return default
        return _unquote(self._expand(args[index], scope))

    def _number_argument(
        self, args: list[str], index: int, scope: _Scope, default: int | float = 0
    ) -> int | float:
        """An argument used as a number: evaluated as an expression, so %pos(...)-1 works."""
        if index >= len(args) or not args[index].strip():
            return default
        value = self._evaluate(args[index], scope)
        number = _number(value) if value is not None else None
        return number if number is not None else default

    def _call_function(self, name: str, args: list[str], scope: _Scope) -> str | None:
        handler = getattr(self, f"_function_{name}", None)
        if handler is None:
            return None
        return handler(args, scope)

    def _function_case(self, args: list[str], scope: _Scope) -> str:
        index = int(self._number_argument(args, 0, scope))
        if 1 <= index < len(args):
            return self._text_argument(args, index, scope)
        return ""

    def _function_charinfo(self, args: list[str], scope: _Scope) -> str:
        what = self._text_argument(args, 0, scope).strip().lower()
        if what == "mudname":
            return self._api.world_name
        if what == "connected":
            return "1" if self._api.is_connected() else "0"
        return ""  # name, reference and password aren't the pack's to have

    def _function_chr(self, args: list[str], scope: _Scope) -> str:
        code = int(self._number_argument(args, 0, scope))
        return chr(code) if 1 <= code <= 255 else ""

    def _function_clip(self, args: list[str], scope: _Scope) -> str:
        return self._clipboard  # VIPMud 1's %Clip(), replaced by %Clipboard in 2.0

    def _function_connected(self, args: list[str], scope: _Scope) -> str:
        return "1" if self._api.is_connected() else "0"

    def _function_copy(self, args: list[str], scope: _Scope) -> str:
        text = self._text_argument(args, 0, scope)
        start = max(1, int(self._number_argument(args, 1, scope, 1)))
        count = int(self._number_argument(args, 2, scope, len(text)))
        return text[start - 1 : start - 1 + max(0, count)]

    def _function_defined(self, args: list[str], scope: _Scope) -> str:
        name = self._text_argument(args, 0, scope).strip()
        return "1" if name and self._api.get_var(_fold(name), None) is not None else "0"

    def _function_delete(self, args: list[str], scope: _Scope) -> str:
        text = self._text_argument(args, 0, scope)
        start = max(1, int(self._number_argument(args, 1, scope, 1)))
        count = max(0, int(self._number_argument(args, 2, scope, 0)))
        return text[: start - 1] + text[start - 1 + count :]

    def _function_delitem(self, args: list[str], scope: _Scope) -> str:
        items = _list_items(self._text_argument(args, 0, scope))
        wanted = self._text_argument(args, 1, scope).casefold()
        # The manual deletes the first item that contains the text. An item that is exactly
        # the text goes first, so deleting "boot" doesn't take out "boots" listed before it.
        exact = [k for k, item in enumerate(items) if item.casefold() == wanted]
        partial = [k for k, item in enumerate(items) if wanted and wanted in item.casefold()]
        matches = exact or partial
        if matches:
            del items[matches[0]]
        return "|".join(items)

    def _function_delnitem(self, args: list[str], scope: _Scope) -> str:
        items = _list_items(self._text_argument(args, 0, scope))
        index = int(self._number_argument(args, 1, scope))
        if 1 <= index <= len(items):
            del items[index - 1]
        return "|".join(items)

    def _function_endoffile(self, args: list[str], scope: _Scope) -> str:
        opened = self._files.get(self._text_argument(args, 0, scope).strip())
        if not isinstance(opened, _TextFile):
            return "1"
        return "1" if opened.position >= len(self._text_lines(opened)) else "0"

    def _function_eval(self, args: list[str], scope: _Scope) -> str:
        return self._evaluate(",".join(args), scope, as_text=True) or ""

    def _function_expand(self, args: list[str], scope: _Scope) -> str:
        return self._expand(",".join(args), scope)

    def _function_if(self, args: list[str], scope: _Scope) -> str:
        chosen = 1 if args and self._condition(args[0], scope) else 2
        return self._text_argument(args, chosen, scope)

    def _function_ifword(self, args: list[str], scope: _Scope) -> str:
        needle = self._text_argument(args, 0, scope).casefold()
        words = self._text_argument(args, 1, scope).casefold()
        delimiter = self._text_argument(args, 2, scope, " ").casefold()
        if not needle:
            return "0"
        if not delimiter:
            return "1" if needle == words else "0"
        return "1" if f"{delimiter}{needle}{delimiter}" in f"{delimiter}{words}{delimiter}" else "0"

    def _function_isnumber(self, args: list[str], scope: _Scope) -> str:
        return "1" if _number(self._text_argument(args, 0, scope)) is not None else "0"

    def _function_lastline(self, args: list[str], scope: _Scope) -> str:
        count = int(self._number_argument(args, 0, scope, 1))
        if not 1 <= count <= _FIND_LINES:
            return ""
        lines = self._api.recent_lines(count)
        return lines[0] if len(lines) == count else ""  # the count-th line from the bottom

    def _function_left(self, args: list[str], scope: _Scope) -> str:
        count = max(0, int(self._number_argument(args, 1, scope, 0)))
        return self._text_argument(args, 0, scope)[:count]

    def _function_length(self, args: list[str], scope: _Scope) -> str:
        return str(len(self._text_argument(args, 0, scope)))

    def _function_ltrim(self, args: list[str], scope: _Scope) -> str:
        return self._text_argument(args, 0, scope).lstrip()

    def _function_max(self, args: list[str], scope: _Scope) -> str:
        values = [self._number_argument(args, k, scope) for k in range(len(args))]
        return _format_number(max(values)) if values else ""

    def _function_min(self, args: list[str], scope: _Scope) -> str:
        values = [self._number_argument(args, k, scope) for k in range(len(args))]
        return _format_number(min(values)) if values else ""

    def _function_numwords(self, args: list[str], scope: _Scope) -> str:
        text = self._text_argument(args, 0, scope)
        delimiter = self._text_argument(args, 1, scope, " ")
        return str(len(_words(text, delimiter)))

    def _function_pick(self, args: list[str], scope: _Scope) -> str:
        """``%Pick(list)`` (manual 5.3.23): the value of the item the player picks, or
        nothing when they cancel. Commas split a function's arguments, not a list, so the
        list is all of them."""
        picked = self._ask_pick(_unquote(",".join(args)), scope)
        return picked[1] if picked is not None else ""

    def _function_pop(self, args: list[str], scope: _Scope) -> str:
        name = _fold(self._text_argument(args, 0, scope).strip())
        if not name:
            return ""
        first, _separator, rest = (self._api.get_var(name, "") or "").partition("|")
        self._set_var(name, rest)
        return first

    def _function_pos(self, args: list[str], scope: _Scope) -> str:
        needle = self._text_argument(args, 0, scope).casefold()
        haystack = self._text_argument(args, 1, scope).casefold()
        return str(haystack.find(needle) + 1) if needle else "0"

    def _function_proper(self, args: list[str], scope: _Scope) -> str:
        text = self._text_argument(args, 0, scope)
        return text[:1].upper() + text[1:].lower()

    def _function_push(self, args: list[str], scope: _Scope) -> str:
        items = self._text_argument(args, 0, scope)
        item = self._text_argument(args, 1, scope)
        return f"{item}|{items}" if items else item

    def _function_random(self, args: list[str], scope: _Scope) -> str:
        low = int(self._number_argument(args, 0, scope, 0))
        high = int(self._number_argument(args, 1, scope, low))
        if low > high:
            low, high = high, low
        return str(random.randint(low, high))

    def _function_replace(self, args: list[str], scope: _Scope) -> str:
        text = self._text_argument(args, 0, scope)
        old = self._text_argument(args, 1, scope)
        new = self._text_argument(args, 2, scope)
        return text.replace(old, new) if old else text

    def _function_reverselist(self, args: list[str], scope: _Scope) -> str:
        return "|".join(reversed(_list_items(self._text_argument(args, 0, scope))))

    def _function_right(self, args: list[str], scope: _Scope) -> str:
        count = max(0, int(self._number_argument(args, 1, scope, 0)))
        text = self._text_argument(args, 0, scope)
        return text[len(text) - count :] if count else ""

    def _function_rtrim(self, args: list[str], scope: _Scope) -> str:
        return self._text_argument(args, 0, scope).rstrip()

    def _function_sort(self, args: list[str], scope: _Scope) -> str:
        items = _list_items(self._text_argument(args, 0, scope))
        descending = self._number_argument(args, 1, scope, 0) != 0
        return "|".join(sorted(items, key=str.casefold, reverse=descending))

    def _function_switchdirection(self, args: list[str], scope: _Scope) -> str:
        way = self._text_argument(args, 0, scope).strip()
        back = self._directions.get(way.lower())
        if back is None:
            return way
        return back.upper() if way.isupper() else back

    def _function_trim(self, args: list[str], scope: _Scope) -> str:
        return self._text_argument(args, 0, scope).strip()

    def _function_var(self, args: list[str], scope: _Scope) -> str:
        return self._variable(self._text_argument(args, 0, scope).strip())

    def _function_word(self, args: list[str], scope: _Scope) -> str:
        text = self._text_argument(args, 0, scope)
        words = _words(text, self._text_argument(args, 1, scope, " "))
        if not words:
            return ""
        index = int(self._number_argument(args, 2, scope, 1))
        return words[min(max(index, 1), len(words)) - 1]  # past the end: the last word


def _list_items(text: str) -> list[str]:
    return text.split("|") if text else []


def _unescape(text: str) -> str:
    """``text`` with its ``~`` escapes taken off, as shown to the player."""
    if _ESCAPE not in text:
        return text
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == _ESCAPE and i + 1 < len(text):
            i += 1
        out.append(text[i])
        i += 1
    return "".join(out)


def _top_level(text: str, separator: str) -> list[int]:
    """Where ``separator`` occurs in ``text`` outside braces and not after a ``~``."""
    positions: list[int] = []
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c == _ESCAPE:
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}" and depth:
            depth -= 1
        elif c == separator and not depth:
            positions.append(i)
        i += 1
    return positions


def _split_top_level(text: str, separator: str) -> list[str]:
    """``text`` split at each ``separator`` outside braces and not after a ``~``."""
    pieces: list[str] = []
    start = 0
    for position in _top_level(text, separator):
        pieces.append(text[start:position])
        start = position + 1
    pieces.append(text[start:])
    return pieces


def _split_caption(item: str) -> tuple[str, str | None]:
    """A #Pick item's caption and value, split at its first ``:`` outside braces. The value
    is None when there's no colon."""
    colons = _top_level(item, ":")
    if not colons:
        return item, None
    return item[:colons[0]], item[colons[0] + 1:]


@dataclass
class _PickList:
    """A #Pick list as shown: its title, the captions, for each one the commands #Pick runs
    and the value %Pick returns, and the item the cursor starts on."""

    title: str = ""
    captions: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    selected: int | None = None

    def add(self, caption: str, command: str, result: str, *, titled: bool) -> None:
        """Add one item. ``titled`` says it was written ``caption:value``, so a caption of
        ``P`` makes the value the title."""
        caption = caption.strip()
        if titled and caption.lower() == "p":
            self.title = result.strip()
            return
        if caption.startswith("*"):
            caption = caption[1:].strip()
            if self.selected is None:
                self.selected = len(self.captions)
        if not caption and not command.strip():
            return
        self.captions.append(" ".join(caption.split()))
        self.commands.append(command)
        self.results.append(result.strip())


def _words(text: str, delimiter: str) -> list[str]:
    """``text`` split into VIPMud words: on runs of spaces, or on ``delimiter`` exactly."""
    if not delimiter or delimiter == " ":
        return text.split()
    return text.split(delimiter) if text else []
