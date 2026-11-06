"""MUSHclient compatibility: load `<muclient>` XML worlds/plugins + run their Lua.

Parses MUSHclient triggers/aliases and executes their `<script>` CDATA against a
sandboxed Lua runtime whose globals are MUSHclient's API (``Send``, ``Sound``,
``GetInfo``, ``DoAfterSpecial``, ``ColourNote``, ``GetVariable``...), backed by the
shared :class:`ScriptApi`. The same functions are also mirrored onto a ``world``
table, since real packs call both bare (``Sound(...)``) and through the world
object (``world.Sound(...)``). This lets Matt's existing plugins (e.g.
``/home/matt/erion/erion_gathering.xml``) and MUSHclient soundpacks run on the
genericMud engine unchanged.

Scope: covers the API surface real soundpacks use, audio included — ``Sound`` (the
BASS-backed call mudsoundpack.com packs use, with its ``volume=``/``pan=`` control
strings) and ``GetInfo`` directory codes (so ``GetInfo(67).."/sounds/x.ogg"``
resolves against the pack dir). Out of scope: the full plugin-suite surface
(LuaSocket, GUI windows, VBScript) and a few simplified semantics — notably
``DoAfterSpecial`` always runs its deferred text as Lua (the soundpack-standard
"send to script" case) rather than honouring every sendto code.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import math
import random
import re
import time
import xml.etree.ElementTree as ET
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

from genericmud.automation.engine import MatchContext, compile_pattern
from genericmud.navigation import expand_speedwalk
from genericmud.render.ansi import strip_ansi
from genericmud.safepath import is_absolute
from genericmud.scripting.api import MESSAGE_BUTTONS, ScriptApi
from genericmud.scripting.guard import ScriptGuard
from genericmud.scripting.lua_runtime import install_pack_require, make_sandboxed_runtime
from genericmud.scripting.mushclient_audio import AudioDll
from genericmud.scripting.mushclient_bass import BassWrapper
from genericmud.scripting.mushclient_rules import (
    ALIAS,
    ANSI_BOLD,
    ANSI_NORMAL,
    E_ALIAS_ALREADY_EXISTS,
    E_ALIAS_CANNOT_BE_EMPTY,
    E_ALIAS_NOT_FOUND,
    E_ARRAY_ALREADY_EXISTS,
    E_ARRAY_DOES_NOT_EXIST,
    E_ARRAY_NOT_EVEN_NUMBER_OF_VALUES,
    E_BAD_DELIMITER,
    E_BAD_REGULAR_EXPRESSION,
    E_ERROR_CALLING_PLUGIN_ROUTINE,
    E_IMPORTED_WITH_DUPLICATES,
    E_KEY_DOES_NOT_EXIST,
    E_NO_SUCH_COMMAND,
    E_NO_SUCH_PLUGIN,
    E_NO_SUCH_ROUTINE,
    E_OK,
    E_OPTION_OUT_OF_RANGE,
    E_PLUGIN_CANNOT_SET_OPTION,
    E_PLUGIN_DISABLED,
    E_SET_REPLACING_EXISTING_VALUE,
    E_TIME_INVALID,
    E_TIMER_ALREADY_EXISTS,
    E_TIMER_NOT_FOUND,
    E_TRIGGER_ALREADY_EXISTS,
    E_TRIGGER_CANNOT_BE_EMPTY,
    E_TRIGGER_NOT_FOUND,
    E_UNKNOWN_OPTION,
    ERROR_CODES,
    ERROR_DESCRIPTIONS,
    TRIGGER,
    PackRule,
    colour_from_name,
    colour_to_name,
    evaluate_speedwalk,
    fill_text,
    fixup_escape_sequences,
    get_option,
    make_regular_expression,
    option_number,
    parse_option,
    rule_info,
    set_option,
    sha0_hex,
    style_runs,
)
from genericmud.scripting.sqlite_compat import make_sqlite_bridge

_SEND_TO_SCRIPT = "12"
# MUSHclient's send_to codes (its OtherTypes.h eSendTo*): what a trigger, alias, timer or
# DoAfterSpecial does with its text. The others (1 command window, 3 status line, 4, 5 and 7
# notepads, 6 log file, 15 pane) are MUSHclient windows genericMud doesn't have, so that text
# is dropped. It used to go to the MUD as if the player had typed it, Lua source included.
_SEND_TO_WORLD_CODE = 0
# world, command queue, immediate
_SEND_TO_QUEUE_CODE = 8
_SEND_TO_WORLD_CODES = frozenset({_SEND_TO_WORLD_CODE, _SEND_TO_QUEUE_CODE, 13})
_SEND_TO_OUTPUT_CODE = 2
_SEND_TO_VARIABLE_CODE = 9
_SEND_TO_EXECUTE_CODE = 10
_SEND_TO_SPEEDWALK_CODE = 11
_SEND_TO_SCRIPT_CODES = frozenset({12, 14})  # script, and script after omitting the line
_SEND_TO_LAST_CODE = 14
_APPEND_TO_NOTEPAD = 5
_SEND_TO_NOTEPAD_CODES = frozenset({4, _APPEND_TO_NOTEPAD, 7})  # new, append, replace
_NOTEPAD_LIMIT = 1_000_000  # characters a notepad keeps; the oldest go first
_BROWSER_SCHEMES = ("http://", "https://", "mailto:")  # all OpenBrowser will open
# Lua's sendto table: every destination by MUSHclient's name for it.
_SEND_TO_NAMES = {
    "world": 0, "command": 1, "output": 2, "status": 3, "notepad": 4, "notepadappend": 5,
    "logfile": 6, "notepadreplace": 7, "commandqueue": 8, "variable": 9, "execute": 10,
    "speedwalk": 11, "script": 12, "immediate": 13, "scriptafteromit": 14,
}
_DEFAULT_SEQUENCE = 100
_MAX_SEQUENCE = 10000
_FILLED_IN = re.compile(r"%[0-9<%NnCc]")  # what a match fills into send text
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^\[>]*(?:\[.*?\])?\s*>", re.DOTALL | re.IGNORECASE)
_NO_SOUND = "(No sound)"  # what MUSHclient writes in a trigger's sound attribute when it has none
# Rule options packs can read back that genericMud keeps but doesn't act on.
_KEPT_FLAGS = {
    TRIGGER: ("multi_line", "repeat", "sound_if_inactive"),
    ALIAS: ("menu", "echo_alias", "omit_from_command_history"),
}
_KEPT_NUMBERS = {
    TRIGGER: (
        "clipboard_arg", "colour_change_type", "custom_colour", "lines_to_match",
        "match_style", "new_style",
    ),
    ALIAS: (),
}
# What ImportXML's text has to start with to be imported at all (MUSHclient's own list).
_IMPORT_STARTS = (
    "<?xml", "<!--", "<!doctype", "<muclient", "<world", "<triggers", "<aliases", "<timers",
    "<macros", "<variables", "<colours", "<keypad", "<printing", "<comment", "<include",
    "<plugin", "<script",
)
# A repeating timer can't fire faster than this, so a zero or tiny interval from a pack can't
# turn into a busy loop on the event loop every session shares.
_MIN_TIMER_INTERVAL = 0.1
# MUSHclient answers a bad or missing timer with a nonzero code; packs rarely check it.
_TIMER_ERROR = 1

_SOUND_CHANNEL = "sound"  # MUSHclient Sound() is a single-voice channel
_SOUND_BUFFERS = 10  # MUSHclient PlaySound offers buffers 1..10; 0 = first free
_FREE_BUFFER_CHOICES = 5  # PlaySound's buffer 0 takes a free one among the first five
_QUIETEST_DECIBELS = 100.0  # PlaySound's volume runs 0 to -100 dB, its pan -100 to 100
_VOLUME_MAX = 100.0  # Sound("volume=x") is a percentage, as the LuaAudio packs send it
_PAN_MAX = 100.0  # and Sound("pan=x") one from -100 to 100; the SoundBus wants -1..1
_HTTP_CHUNK_BYTES = 16 * 1024
_HTTP_MAX_BYTES = 32 * 1024 * 1024
_HTTP_TIMEOUT_SECONDS = 20


def _open_http(url: str):
    """Open a public HTTP(S) URL with the pack downloader's redirect/SSRF checks."""
    from genericmud.packs.vault import _secure_urlopen, _validate_url

    _validate_url(url)
    request = Request(url, headers={"User-Agent": "genericMud-soundpack-runtime"})
    return _secure_urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS)


def _buffer_channel(number: int) -> str:
    """The logical sound-bus channel for a MUSHclient PlaySound buffer."""
    return f"mush-{number}"


def _decibel_gain(volume: object) -> float:
    """PlaySound's volume as the amplitude the sound bus takes: decibels, 0 full and -100
    silent. Anything outside that plays at full, as MUSHclient forces it to 0."""
    decibels = _to_float(volume)
    if decibels is None or not -_QUIETEST_DECIBELS <= decibels <= 0:
        decibels = 0.0
    return 10 ** (decibels / 20)


def _decibel_pan(pan: object) -> float:
    """PlaySound's pan as the sound bus takes it. MUSHclient's is DirectSound's: the decibels
    taken off the other side, -100 hard left and 100 hard right, and anything outside that
    centred. The bus pans by balance, keeping the near side full and lowering the other in
    proportion (measured on BASS, and so does the pygame backend), so lowering the other side
    by d decibels is a balance of 1 - 10^(-d/20) toward the near side."""
    decibels = _to_float(pan)
    if decibels is None or not -_QUIETEST_DECIBELS <= decibels <= _QUIETEST_DECIBELS:
        return 0.0
    other_side = 10 ** (-abs(decibels) / 20)
    return math.copysign(1 - other_side, decibels)

# Lifecycle entry points MUSHclient calls on each plugin. All plugins share one _G here,
# so each plugin's hooks are captured (and the globals cleared) right after its script
# runs -- otherwise a later plugin would inherit or silently overwrite an earlier one's.
# Every known name is captured for isolation; dispatch() is only wired up for the
# sound-critical ones today (install/connect + the telnet pair that carries MSDP).
_LIFECYCLE_HOOKS = (
    "OnPluginInstall",
    "OnPluginConnect",
    "OnPluginDisconnect",
    "OnPluginClose",
    "OnPluginEnable",
    "OnPluginDisable",
    "OnPluginSaveState",
    "OnPluginTick",
    "OnPluginLineReceived",
    "OnPluginPartialLine",
    "OnPluginBroadcast",
    "OnPluginListChanged",
    "OnPluginCommandEntered",
    "OnPluginCommand",
    "OnPluginSend",
    "OnPluginSent",
    "OnPluginPacketReceived",
    "OnPluginPlaySound",
    "OnPluginTelnetOption",
    "OnPluginScreendraw",
    "OnPluginWorldOutputResized",
    "OnPluginGetFocus",
    "OnPluginLoseFocus",
    "OnPluginTabComplete",
    "OnPluginTelnetRequest",
    "OnPluginTelnetSubnegotiation",
)


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # lupa hands Lua numbers over as int/float; nil arrives as None
    except (TypeError, ValueError):
        return None


# What a msgbox answers when the player backs out of it, or there's no window to show it.
_MSGBOX_BACKED_OUT = {
    "ok": "ok",
    "okcancel": "cancel",
    "yesno": "no",
    "yesnocancel": "cancel",
    "retrycancel": "cancel",
    "abortretryignore": "abort",
}


def _lua_true(value: object) -> bool:
    """Whether Lua counts a value as true: everything but nil and false, 0 and "" included.
    A function's first result decides (``return false, "why"`` is false)."""
    if isinstance(value, tuple):
        value = value[0] if value else None
    return value is not None and value is not False


def _lua_flag(value: object, default: bool = True) -> bool:
    """A yes/no argument the way MUSHclient reads one from Lua: a boolean as itself, a number
    as true unless 0, and nil as the default (EnableTrigger(name) switches it on)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    number = _to_float(value)
    return number != 0 if number is not None else default


def _lua_bytes(value: object) -> bytes:
    """A Lua string's bytes: lupa hands over text it could read as UTF-8, bytes otherwise."""
    if isinstance(value, bytes):
        return value
    return _dialog_text(value).encode("utf-8")


def _table_bytes(table: object) -> bytes:
    """The bytes of a Lua string that came over as a table of byte values."""
    return bytes(bytearray(int(table[index]) for index in range(1, len(table) + 1)))


def _base64_encode(data: bytes, lines: object = False) -> str:
    """MUSHclient's Base64Encode, with CR LF after every 76 characters when asked."""
    encoded = base64.b64encode(data).decode("ascii")
    if not _lua_true(lines):
        return encoded
    return "\r\n".join(encoded[index:index + 76] for index in range(0, len(encoded), 76))


def _base64_decode(value: object) -> bytes | None:
    try:
        return base64.b64decode(b"".join(_lua_bytes(value).split()), validate=True)
    except (binascii.Error, ValueError):
        return None


def _edit_distance(first: str, second: str) -> int:
    """The Levenshtein distance: how many single-character edits turn one into the other."""
    previous = list(range(len(second) + 1))
    for row, left in enumerate(first, 1):
        current = [row]
        for column, right in enumerate(second, 1):
            current.append(min(
                previous[column] + 1, current[column - 1] + 1,
                previous[column - 1] + (left != right),
            ))
        previous = current
    return previous[-1]


def _dialog_text(value: object, default: str = "") -> str:
    """A dialog argument as text: nil is the default, 5.0 is "5", and CR LF a line break."""
    if value is None:
        return default
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r\n", "\n")


def _sorted_choices(choices: object) -> tuple[list[object], list[str]]:
    """A listbox table's keys and captions, sorted by caption ignoring case, the way a
    Windows list box sorts them."""
    if not hasattr(choices, "items"):
        return [], []
    pairs = sorted(
        ((key, _dialog_text(value)) for key, value in choices.items()),
        key=lambda pair: pair[1].casefold(),
    )
    return [key for key, _caption in pairs], [caption for _key, caption in pairs]
_MUSHCLIENT_VERSION = "5.06"
_CLIENT_STARTED = time.time()  # GetInfo(305): near enough when genericMud started

# MUSHclient's AddTrigger/AddTriggerEx flag bits (exposed to packs as `trigger_flag`).
_TRIGGER_FLAGS = {
    "Enabled": 1,
    "OmitFromLog": 2,
    "OmitFromOutput": 4,
    "KeepEvaluating": 8,
    "IgnoreCase": 16,
    "RegularExpression": 32,
    "ExpandVariables": 512,
    "Replace": 1024,
    "LowercaseWildcard": 2048,
    "Temporary": 16384,
    "OneShot": 32768,
}
# MUSHclient's AddTimer flag bits (exposed to packs as `timer_flag`).
_TIMER_FLAGS = {
    "Enabled": 1,
    "AtTime": 2,
    "OneShot": 4,
    "TimerSpeedWalk": 8,
    "TimerNote": 16,
    "ActiveWhenClosed": 32,
    "Replace": 1024,
    "Temporary": 16384,
}
# MUSHclient's AddAlias flag bits (exposed to packs as `alias_flag`).
_ALIAS_FLAGS = {
    "Enabled": 1,
    "KeepEvaluating": 8,
    "IgnoreAliasCase": 32,
    "OmitFromLogFile": 64,
    "RegularExpression": 128,
    "ExpandVariables": 512,
    "Replace": 1024,
    "AliasSpeedWalk": 2048,
    "AliasQueue": 4096,
    "AliasMenu": 8192,
    "Temporary": 16384,
    "OneShot": 32768,
}
# Get/SetTimerOption names, from MUSHclient's timer option table.
_TIMER_TEXT_OPTIONS = ("group", "script", "send", "variable")
_TIMER_FLAG_OPTIONS = (
    "enabled", "at_time", "one_shot", "omit_from_output", "omit_from_log", "active_closed",
    "temporary",
)
_TIMER_NUMBER_OPTIONS = {
    "hour": (0, 23), "minute": (0, 59), "second": (0, 59.9999), "offset_hour": (0, 23),
    "offset_minute": (0, 59), "offset_second": (0, 59.9999), "send_to": (0, 14),
    "user": (-(2**31), 2**31 - 1),
}
_SCRIPT_ERROR_OK = 0  # MUSHclient eOK; nonzero = failure (packs rarely check)
_MOD_ORDER = ("ctrl", "alt", "shift")  # canonical combo order, matching the UI's _key_combo

# Client-shell plugins whose job genericMud already owns. Loading them runs Windows UI,
# editor, or self-update code that cannot contribute a sound cue here. They are represented as
# enabled virtual plugins so dependency managers remain satisfied, and the skip is reported.
_HOST_PLUGIN_REPLACEMENTS = {
    "current_output_window.xml": "genericMud owns the output window",
    "capture2dworld.xml": "genericMud owns output review windows",
    "capture2notepads.xml": "genericMud owns output review windows",
    "localedit.xml": "genericMud owns command editing",
    "log_manager.xml": "genericMud owns session logging",
    "mushclient_help.xml": "genericMud provides its own help",
    "mm_gmcp_mapper_gmcp.xml": "genericMud does not load MUSHclient miniwindow mappers",
    "output_functions.xml": "genericMud owns output review windows",
    "copy_output.xml": "genericMud owns copying from the output window",
    "plugins_updater_v2.xml": "genericMud owns soundpack updates",
    "trace_to_notepad.xml": "genericMud provides diagnostics instead of Notepad tracing",
    "updater.xml": "genericMud owns soundpack updates",
    "update_watcher.xml": "genericMud owns soundpack updates",
    "version_check.xml": "genericMud owns update checks and client preferences",
    "lua_sapi.xml": "genericMud owns automatic speech output",
    "mushreader.xml": "genericMud owns automatic speech output",
    "sapi_speaker.xml": "genericMud owns automatic speech output",
    "text_to_speech.xml": "genericMud owns automatic speech output",
    "universal_text_to_speech.xml": "genericMud owns automatic speech output",
}
_NON_LUA_SCRIPT_SUFFIXES = frozenset({".js", ".pl", ".pys", ".vbs"})
# The speech plugins above also give packs a small command language, which packs drive with
# Execute or send_to 10: Erion's channel history and output review say everything through
# "tts_interrupt <text>", Cosmic Rage reads its $buffer lines that way, and Materia Magica its
# health with "sapi interrupt <text>". Skipping the plugin without answering those commands
# sent them to the MUD. (regex, interrupt first, speak the capture)
_SPEECH_PLUGINS = frozenset({
    "lua_sapi.xml", "mushreader.xml", "sapi_speaker.xml", "text_to_speech.xml",
    "universal_text_to_speech.xml",
})
_SPEECH_COMMANDS = (
    (r"^tts_interrupt(?: (.*))?$", True, True),  # bare tts_interrupt just stops speech
    (r"^tts_note(?: (.*))?$", False, True),
    (r"^tts_stop$", True, False),
    (r"^sapi interrupt (.*)$", True, True),
    (r"^sapi say (.*)$", False, True),
    (r"^sapi (?:stop|skip|clear)$", True, False),
)
_SPEECH_COMMAND_PRIORITY = -1_000_000  # after every alias of the pack's own
_GMCP_HANDLER_ID = "3e7dedbe37e44942dd46d264"  # GMCP_handler_NJG broadcast identity
# Miriani-Next's and Toastush's updater plugins (each an updater.xml) take an "update" command,
# and the packs send it themselves: at login when the MUD says the pack is out of date and the
# player turned automatic updates on, and hourly while idle when they turned that on. Skipping
# the plugin sent those to the MUD. genericMud's own updater does the plugin's job, so it
# answers the command; when it has installed something it tells the pack the way the plugin
# did, so the pack plays its update sound and shows its changelog if its player asked for it.
_UPDATER_PLUGIN = "updater.xml"
_MIRIANI_NEXT_UPDATER = "706cd88f4d441a81439bd421"  # tells the pack with message 777 (or 999)
_TOASTUSH_UPDATER = "508bd88f4d441f81466bf471"  # tells the pack with message 1
_UPDATE_QUIET_WORDS = frozenset({"quietly", "-q", "--quiet"})
_UPDATE_CHECK_WORDS = frozenset({"check", "-d", "--dry-run"})
_UPDATE_HELP_WORDS = frozenset({"help", "-h", "--help"})
# Toastush's other switches set how its own downloads ran; genericMud installs the same way
# whichever it's given. Anything else (Miriani-Next's install, reinstall, switch and version,
# which drive its Windows launcher) gets a line saying genericMud does the updating.
_UPDATE_INSTALL_WORDS = frozenset({
    "-c", "--clean-run", "-e", "--error-suppress", "-f", "--confirm", "-s", "--safe-run",
    "--verbose",
})
_UPDATE_HELP = (
    "genericMud keeps this soundpack up to date. Type update to install what's new, update "
    "check to hear whether there's anything new, or update quietly to install without a word "
    "when there's nothing."
)
_UPDATE_ELSEWHERE = (
    "genericMud updates this soundpack itself, so there's no separate updater to install, "
    "switch or ask for its version. Type update to install what's new, or update check to "
    "hear whether there's anything new."
)
_UPDATE_INDEX = (
    "Making an update index is for the pack's maintainers, who publish one for MUSHclient's "
    "updater. genericMud updates this soundpack from its repository, so there's no index to "
    "make here."
)


_XML_TRUE = frozenset({"y", "yes", "1", "-1", "true", "t"})
_PLUGIN_SEQUENCE = 5000  # MUSHclient's default plugin sequence
# World file event handlers genericMud runs (on_world_<event>). MUSHclient's get_focus and
# lose_focus, for the window, have no equivalent here yet.
_WORLD_EVENTS = ("open", "connect", "disconnect", "close", "get_focus", "lose_focus")
_PLUGIN_DATE = re.compile(r"^(\d+)-(\d+)-(\d+)(?:\s+(\d+)(?::(\d+))?(?::(\d+))?)?$")


def _plugin_date(text: str | None) -> int | None:
    """A plugin's date_written/date_modified as Lua gets it from MUSHclient: Unix seconds
    for "YYYY-MM-DD [HH:MM:SS]" in local time, or nil (MUSHclient refuses a malformed one;
    genericMud just leaves it out)."""
    found = _PLUGIN_DATE.match((text or "").strip())
    if found is None:
        return None
    try:
        parts = [int(part) for part in found.groups(default="0")]
        return int(datetime.datetime(*parts).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _plugin_number(text: str | None, default: float) -> float:
    try:
        return float(text) if text is not None else default
    except ValueError:
        return default


def _plugin_details(plugin: ET.Element) -> dict:
    """What GetPluginInfo answers about a plugin, read the way MUSHclient reads it."""
    description = plugin.find("description")
    text = ""
    if description is not None:
        text = description.text or ""
        if _xml_flag(description.attrib, "trim"):
            text = text.strip("\n\r\t ")
    return {
        "author": plugin.get("author", ""),
        "purpose": plugin.get("purpose", ""),
        "language": plugin.get("language", ""),
        "description": text,
        "date_written": _plugin_date(plugin.get("date_written")),
        "date_modified": _plugin_date(plugin.get("date_modified")),
        "requires": _plugin_number(plugin.get("requires"), 0.0),
        "version": _plugin_number(plugin.get("version"), 0.0),
        "sequence": int(_plugin_number(plugin.get("sequence"), _PLUGIN_SEQUENCE)),
        "installed": int(time.time()),
    }


def _xml_flag(attrs: dict[str, str], name: str, default: bool = False) -> bool:
    """A boolean attribute the way MUSHclient's xmlparse.cpp reads it: y, yes, 1, true..."""
    value = attrs.get(name)
    if value is None:
        return default
    return value.strip().lower() in _XML_TRUE


def _palette_colour(palette: tuple[int, ...], which: object) -> int:
    """GetNormalColour / GetBoldColour: ANSI colour 1 to 8 (black to white), else 0."""
    number = int(_to_float(which) or 0)
    return palette[number - 1] if 1 <= number <= len(palette) else 0


def _reduce_ints(args: tuple, op) -> int:
    values = [int(_to_float(a) or 0) for a in args]
    result = values[0] if values else 0
    for value in values[1:]:
        result = op(result, value)
    return result


def mushclient_pattern(text: str) -> str:
    """The regex MUSHclient matches a plain (non-regex) trigger or alias against.

    MUSHclient never matches the text as written: ``ConvertToRegularExpression`` (its
    Utilities.cpp, called with ``bWholeLine`` true for every trigger and alias it loads)
    wraps it in ``^...$`` so it has to be the whole line or the whole command, turns ``*``
    into a non-greedy ``(.*?)``, and treats everything else as a literal, ``?`` included.
    Packs are written for that: a plain trigger on "You are hungry." must not fire on
    someone saying the words, and an alias "vol" must not also catch "voldown".
    """
    parts = ["^"]
    for char in text:
        parts.append("(.*?)" if char == "*" else re.escape(char))
    parts.append("$")
    return "".join(parts)


def _normalize_key_spec(spec: object) -> str:
    """MUSHclient accelerator syntax -> a keymap combo, or "" when unusable.

    Real packs are inconsistent: "shift+f1", "ctrl +0", "alt + pageup",
    "shift+alt + left". Lowercase everything, strip spaces, and emit modifiers in
    the ctrl/alt/shift order the UI's _key_combo builds, key last.
    """
    parts = [part.strip().lower() for part in str(spec or "").split("+")]
    parts = [part for part in parts if part]
    keys = [part for part in parts if part not in _MOD_ORDER]
    if len(keys) != 1:
        return ""  # no key, or something like "ctrl+alt" alone
    mods = [mod for mod in _MOD_ORDER if mod in parts]
    return "+".join([*mods, keys[0]])


class MushclientPack:
    def __init__(self, api: ScriptApi, *, full_stdlib: bool = False) -> None:
        self._api = api
        self._base_dir = api.base_dir
        # Default @sppath to the pack dir when the session didn't pre-set it from world.sounds, so
        # _find_in_sounds_dir can locate bundled audio by basename when the pack's own
        # GetInfo()-anchored paths miss (e.g. Erion's split sounds/ + worlds/sounds/
        # layout). Mirrors the VIPMud default; the guard keeps a session-set
        # world.sounds path from being clobbered.
        if self._base_dir and not api.get_var("sppath"):
            api.set_var("sppath", self._base_dir)
        self._world_dir: str | None = None  # dir of the loaded world file; anchors GetInfo() paths
        self._world_path: Path | None = None
        self._client_dir = Path(self._base_dir).resolve() if self._base_dir else None
        self._world_id = uuid4().hex[:24]
        self._world_name = ""
        self._world_address = ""
        self._world_port = 0
        # The world file's own event handlers (on_world_open="OnWorldOpen" and so on).
        self._world_events: dict[str, str] = {}
        self._world_object = None  # what GetWorld answers for this world (see _get_world)
        self._exposed: dict[str, dict] = {}  # ppi: plugin id -> {exposed name -> Lua fn}
        self._current_plugin = "world"  # whose script is loading now (for ppi.Expose)
        self._current_document: Path | None = None
        self._loaded_includes: set[Path] = set()  # resolved paths, so each file loads once
        self._include_errors: list[tuple[str, str]] = []  # plugins that failed to load (name, why)
        self._skipped_plugins: list[tuple[str, str]] = []  # optional/missing plugins (name, why)
        self._rule_errors: list[tuple[str, str]] = []  # malformed rules skipped in isolation
        self._external_script_errors: list[tuple[str, str]] = []
        self._module_errors: list[tuple[str, str]] = []
        self._hook_errors: list[tuple[str, str]] = []
        self._plugin_info: dict[str, dict[str, object]] = {}
        self._plugin_file_by_id: dict[str, Path] | None = None
        self._file_index: dict[str, list[Path]] | None = None
        self._hooks: dict[str, dict[str, object]] = {}  # plugin id -> {hook name -> Lua fn}
        self._arrays: dict[str, dict[str, str]] = {}  # MUSHclient Array* API backing store
        self._timers: dict[str, dict] = {}  # AddTimer / XML <timer> state, by timer name
        self._speech_commands_installed = False
        self._updater_id: str | None = None  # the updater plugin genericMud stands in for
        self._plain_include_depth = 0  # >0 while a non-plugin <include> loads
        # Tell/ColourTell text waiting for the note that ends its line
        self._pending_tell = ""
        self._gmcp_values: dict[str, object] = {}
        self._options: dict[str, object] = {
            "enable_aliases": 1,
            "enable_scripts": 1,
            "enable_timers": 1,
            "enable_triggers": 1,
        }
        self._alpha_options: dict[str, str] = {}
        # Every trigger and alias, however it arrived (a world or plugin file, ImportXML,
        # AddTriggerEx, AddAlias), by kind then (plugin, label). Each has one engine rule that
        # goes when the rule does, so replaced and spent rules don't pile up being scanned.
        self._rules: dict[str, dict[tuple[str, str], PackRule]] = {TRIGGER: {}, ALIAS: {}}
        self._expanding: set[PackRule] = set()  # triggers whose match has @variables in it
        self._expanded_version = -1  # the variables' version those matches were filled from
        self._rule_counter = 0  # engine names and internal labels for rules
        self._unique_number = 0
        self._random = random.Random()  # MtRand's own stream, so MtSrand can't touch others
        self._note_colours = [ANSI_NORMAL[7], ANSI_NORMAL[0]]  # Get/SetNoteColourFore/Back
        self._opened_at = time.time()  # GetInfo(306): when this world was opened
        self._connected_at: float | None = None  # GetInfo(301) and GetConnectDuration
        self._saving_state = False  # SaveState can't run inside OnPluginSaveState
        self._in_sound_hook = False  # a plugin's OnPluginPlaySound is running
        self._held_buffers: set[int] = set()  # PlaySound buffers not freed yet
        self._notepads: dict[str, str] = {}  # MUSHclient notepads by title
        self._loading_plugins: dict[str, int] = {}  # plugins whose files are loading now
        self._plugin_files: dict[str, set[Path]] = {}  # plain includes each plugin loaded
        self._lifecycle_started = False  # OnPluginInstall has gone round once
        self._opening = False  # the session's first OnPluginInstall round is running
        # Each plugin's own globals (see _env_for) and package.loaded (its modules built
        # with module()). The world's are the Lua state's own.
        self._plugin_envs: dict[str, object] = {}
        self._plugin_loaded: dict[str, object] = {}
        self._headless_window = _HeadlessWindow()  # the window's calls when there's no window
        # MUSHclient targets Lua 5.1; trusted packs keep the full stdlib their
        # libraries assume (os/io/loadstring + the module(..., package.seeall) idiom).
        self._script_error_spoken = False  # speak the first fire-time script fault, trace the rest
        self._lua, install_hook = make_sandboxed_runtime(lua51=True, full_stdlib=full_stdlib)
        if full_stdlib:
            self._install_io_compat()
            self._install_os_compat()
        # Untrusted packs fail closed if the runaway-loop guard can't be installed; a trusted
        # pack is user-vouched arbitrary code, so a missing hook is acceptable there.
        # The guard's clock leaves out time a script spent waiting on the player's answer to a
        # dialog, and time genericMud spent doing work the script handed it (ImportXML).
        self._host_time = 0.0
        self._guard = ScriptGuard(
            install_hook, require_hook=not full_stdlib, report=self._report_error,
            clock=lambda: time.monotonic() - api.player_time - self._host_time,
        )
        # Hands a Python function a Lua string as a table of its byte values (see utils.md5).
        self._byte_args = self._lua.eval(
            "function(fn) return function(s, ...) s = tostring(s or '')"
            " local bytes = {} for i = 1, #s do bytes[i] = string.byte(s, i) end"
            " return fn(bytes, ...) end end"
        )
        self._install_api()
        # MushReader and the channel_history plugin are often installed globally in
        # MUSHclient rather than shipped with a pack, so packs call their commands without
        # loading them (Stellarmush, Mystavaria, Empire). Answer them for every pack, after
        # the pack's own aliases, or "tts_interrupt <text>" goes to the MUD instead of aloud.
        self._install_speech_commands()
        self._install_sendpkt()
        self._install_audio()
        self._install_nvda()
        self._install_utils()
        # Calls fn(option, <lua byte-string>) entirely on the Lua side: an MSDP payload
        # is rarely valid UTF-8, so it crosses the lupa boundary as a table of byte
        # values, never as a string (lupa's string conversion would raise or mangle it).
        self._payload_caller = self._lua.eval(
            "function(fn, option, t)\n"
            "  local parts = {}\n"
            "  for i = 1, #t do parts[i] = string.char(t[i]) end\n"
            "  return fn(option, table.concat(parts))\n"
            "end"
        )
        # The function a rule or timer names. The first part of the name is read raw: every
        # unknown capitalised global reads as the do-nothing stand-in (set up below), and a
        # handler isn't one until the pack defines it.
        # A plugin's own name is looked for in its own globals first, then the shared ones
        # (host functions, the world's script, libraries built with module()).
        self._find_function = self._lua.eval(
            "function(name, env)\n"
            "  local target = nil\n"
            "  for part in string.gmatch(name, '[^%.]+') do\n"
            "    if target == nil then\n"
            "      if env ~= nil then target = rawget(env, part) end\n"
            "      if target == nil then target = rawget(_G, part) end\n"
            "    elseif type(target) == 'table' then target = target[part]\n"
            "    else return nil end\n"
            "    if target == nil then return nil end\n"
            "  end\n"
            "  if type(target) == 'function' then return target end\n"
            "  return nil\n"
            "end"
        )
        self._new_env = None  # made at the end of setup, from genericMud's own globals
        self._setfenv = self._lua.eval("setfenv")
        self._rawget = self._lua.eval("rawget")
        # A chunk loadstring, load or loadfile makes gets the Lua state's globals, so a
        # plugin restoring a saved table with loadstring would write it where it can't see
        # it. They get the plugin's own globals instead. (Only trusted packs have them.)
        self._lua.eval(
            "function(current_env)\n"
            "  local function adopt(original)\n"
            "    if original == nil then return nil end\n"
            "    return function(...)\n"
            "      local chunk, problem = original(...)\n"
            "      if chunk ~= nil then\n"
            "        local env = current_env()\n"
            "        if env ~= nil then setfenv(chunk, env) end\n"
            "      end\n"
            "      return chunk, problem\n"
            "    end\n"
            "  end\n"
            "  loadstring = adopt(rawget(_G, 'loadstring'))\n"
            "  load = adopt(rawget(_G, 'load'))\n"
            "  loadfile = adopt(rawget(_G, 'loadfile'))\n"
            "end"
        )(lambda: self._env_for(self._current_plugin))
        # Hand each plugin our own ppi (its bundled ppi.lua needs package.seeall, which the
        # sandbox strips). Then make any still-unimplemented host name a "black hole" that is
        # callable AND indexable (returns itself) -- so Window/InfoBox/etc. we don't implement
        # no-op (even Foo.bar.baz()) and the plugin loads + its sound path runs.
        # A black hole: callable AND self-indexing (returns itself), so Window/InfoBox/socket
        # and any other host API we don't implement no-op, even Foo.bar.baz(). It backs both an
        # unresolved require (native/external modules) and any unknown global, so a plugin loads
        # + its sound path runs regardless of the peripheral features it reaches for.
        black_hole = self._lua.eval(
            "setmetatable({}, {__call=function(t) return t end, __index=function(t) return t end})"
        )
        bass_bridge = self._make_bass_bridge()
        json_bridge = self._make_json_bridge()
        lfs_bridge = self._make_lfs_bridge()
        socket_bridge = self._make_socket_bridge()
        http_bridge, ltn12_bridge = self._make_http_bridges()
        socket_bridge.http = http_bridge
        sqlite_bridge = make_sqlite_bridge(
            self._lua, self._base_dir, lambda: self._client_dir
        )
        self._lua.globals().json = json_bridge
        self._lua.globals().ltn12 = ltn12_bridge
        self._lua.globals().socket = socket_bridge
        self._lua.globals().sqlite3 = sqlite_bridge
        self._forget_scope = install_pack_require(
            self._lua,
            self._base_dir,
            builtins={
                "ppi": self._make_ppi(),
                # The ffi-based BASS wrapper packs ship: Miriani-Next and Toastush under the
                # Miriani name, Chatpack and the yugioh pack as "audio.bass". It needs
                # LuaJIT and bass.dll; this plays through genericMud's sound instead.
                "miriani.lib.audio.bass": bass_bridge,
                "miriani/lib/audio/bass": bass_bridge,
                "audio.bass": bass_bridge,
                "audio/bass": bass_bridge,
                "json": json_bridge,
                "json.decode": json_bridge.decode,
                "json.encode": json_bridge.encode,
                "lfs": lfs_bridge,
                "ltn12": ltn12_bridge,
                "socket": socket_bridge,
                "socket.http": http_bridge,
                "sqlite3": sqlite_bridge,
            },
            fallback=black_hole,
            on_error=self._module_failed,
            # A module that defines plain globals runs once in each plugin that requires
            # it, as each plugin has its own Lua state in MUSHclient.
            scope=lambda: (
                self._current_plugin,
                self._env_for(self._current_plugin),
                self._loaded_for(self._current_plugin),
            ),
        )
        # Lua 5.1's module() and package.seeall put a module in the Lua state's globals and
        # package.loaded; MUSHclient plugins each have their own, so these put it in the
        # requiring plugin's (Mush-Z's serialize.save finds the plugin's tables through _G).
        self._lua.eval(
            "function(current_env, current_loaded)\n"
            "  if rawget(_G, 'module') == nil or rawget(_G, 'package') == nil then return end\n"
            "  local shared_loaded = package.loaded\n"
            "  module = function(modname, ...)\n"
            "    local env = current_env()\n"
            "    local loaded = shared_loaded\n"
            "    if env ~= nil then loaded = current_loaded() end\n"
            "    local M = loaded[modname]\n"
            "    if type(M) ~= 'table' then\n"
            "      M = env or _G\n"
            "      for part in string.gmatch(modname, '[^%.]+') do\n"
            "        local child = rawget(M, part)\n"
            "        if child == nil then\n"
            "          child = {}\n"
            "          rawset(M, part, child)\n"
            "        elseif type(child) ~= 'table' then\n"
            "          error(\"name conflict for module '\" .. modname .. \"'\", 2)\n"
            "        end\n"
            "        M = child\n"
            "      end\n"
            "      loaded[modname] = M\n"
            "    end\n"
            "    if rawget(M, '_NAME') == nil then\n"
            "      M._M = M\n"
            "      M._NAME = modname\n"
            "      M._PACKAGE = string.match(modname, '^(.*%.)[^%.]+$') or ''\n"
            "    end\n"
            "    setfenv(2, M)\n"
            "    for index = 1, select('#', ...) do (select(index, ...))(M) end\n"
            "  end\n"
            "  package.seeall = function(M)\n"
            "    local meta = getmetatable(M)\n"
            "    if meta == nil then\n"
            "      meta = {}\n"
            "      setmetatable(M, meta)\n"
            "    end\n"
            "    meta.__index = current_env() or _G\n"
            "  end\n"
            "end"
        )(
            lambda: self._env_for(self._current_plugin),
            lambda: self._loaded_for(self._current_plugin),
        )
        # Only API-shaped names fall into the black hole: MUSHclient functions are CapWords
        # (Sound, WindowCreate, BroadcastPlugin...) plus a few lowercase host libraries
        # (utils/rex/serialize). `bit` and `nvda` used to be here but are real shims now
        # (AddTriggerEx flag math; the pack speech object). A plain script variable (var,
        # dir, roomName) must read
        # back as nil -- assigning nil to a global DELETES it, so an unconditional fallback
        # made `if var ~= nil` true right after `var = nil` and Erion's OnPluginInstall
        # stored the black hole into every sound toggle instead of defaulting them to 1.
        self._lua.eval(
            "function(bh)\n"
            "  local hosted = {utils=true, rex=true, serialize=true}\n"
            "  setmetatable(_G, {__index = function(_, key)\n"
            "    if type(key) == 'string' and (string.match(key, '^%u') or hosted[key]) then\n"
            "      return bh\n"
            "    end\n"
            "    return nil\n"
            "  end})\n"
            "end"
        )(black_hole)
        # A plugin's globals: its own table, starting with genericMud's own globals (the host
        # functions and Lua's libraries, as they stand now, before any pack code runs) and
        # reading through to the shared ones for the rest (the world's script, unknown host
        # names). _G names the plugin's table, as in a MUSHclient plugin's own Lua state, so
        # rawget(_G, "unpack") (Penlight), pairs(_G) and strict.lua see what they expect.
        self._new_env = self._lua.eval(
            "function()\n"
            "  local host = {}\n"
            "  for name, value in pairs(_G) do host[name] = value end\n"
            "  return function()\n"
            "    local env = {}\n"
            "    for name, value in pairs(host) do env[name] = value end\n"
            "    env._G = env\n"
            "    return setmetatable(env, {__index = _G})\n"
            "  end\n"
            "end"
        )()
        # Its own package.loaded behind package (module() registers there), the rest of
        # package shared. Trusted packs only; the others have no package.
        self._package_for = self._lua.eval(
            "function(loaded)\n"
            "  local shared = rawget(_G, 'package')\n"
            "  if shared == nil then return nil end\n"
            "  return setmetatable({loaded = loaded}, {__index = shared})\n"
            "end"
        )

    def _install_io_compat(self) -> None:
        """Match MUSHclient's pack-relative file paths and legacy read modes."""
        original_open = self._lua.globals().io.open

        def open_file(
            filename: object = "", mode: object = "r", *_args: object
        ) -> object:
            name = str(filename or "")
            normalized = name.replace("\\", "/")
            is_windows_absolute = re.match(r"^[A-Za-z]:/", normalized) is not None
            if self._base_dir and not Path(normalized).is_absolute() and not is_windows_absolute:
                target = self._resolve_pack_directory(name)
                if target is None:
                    return None, "relative file path escapes the soundpack"
                name = target.as_posix()
            else:
                # Packs build Windows paths from GetInfo's folders: Krinnzenn opens its help
                # pages as GetInfo(66) .. "\docs\...", which only Windows reads as written.
                name = normalized
            return original_open(name, str(mode or "r"))

        self._lua.globals().io.open = open_file
        self._lua.execute(
            "local methods = getmetatable(io.stdin).__index\n"
            "local original_read = methods.read\n"
            "methods.read = function(handle, ...)\n"
            "  local count, modes = select('#', ...), {...}\n"
            "  for i = 1, count do\n"
            "    if type(modes[i]) == 'string' then\n"
            "      local mode = string.match(modes[i], '^([alLn])%*$')\n"
            "      if mode then modes[i] = '*' .. mode end\n"
            "    end\n"
            "  end\n"
            "  return original_read(handle, unpack(modes, 1, count))\n"
            "end"
        )

    def _install_os_compat(self) -> None:
        """Handle Windows ``mkdir`` commands inside the installed pack on any host OS."""
        original_execute = self._lua.globals().os.execute

        def execute(command: object = "", *_args: object):
            text = str(command or "")
            match = re.fullmatch(
                r'\s*(?:md|mkdir)\s+"([^"]+)"(?:\s+2>\s*nul)?\s*', text, re.IGNORECASE
            )
            if match:
                path = self._resolve_pack_directory(match.group(1))
                if path is None:
                    return 1
                path.mkdir(parents=True, exist_ok=True)
                return 0
            return original_execute(text)

        self._lua.globals().os.execute = execute

    def _make_ppi(self):
        """A minimal in-process ppi (plugin-to-plugin interface): Expose registers a
        function under the loading plugin; Load returns a plugin's exposed functions."""
        ppi = self._lua.table()
        ppi.Expose = self._ppi_expose
        ppi.Load = self._ppi_load
        ppi.init = lambda *_a: None
        ppi.unload = lambda *_a: None
        return ppi

    def _make_bass_bridge(self):
        """The BASS wrapper class packs ship, as genericMud's sound bus (see mushclient_bass)."""
        self._bass_wrapper = BassWrapper(lua=self._lua, api=self._api, to_number=_to_float)
        return self._bass_wrapper.build()

    def _make_json_bridge(self):
        """JSON codec for packs whose bundled implementation depends on native LPeg.

        Shaped like LuaJSON, the library those packs ship: json.decode and json.encode are
        modules that can be called as functions, with decode.decode and encode.encode inside.
        Packs use both ways, and require "json.decode" and "json.encode" on their own.
        """

        def from_lua(value: object):
            if not hasattr(value, "items"):
                return value
            items = list(value.items())
            integer_keys = [key for key, _item in items if isinstance(key, int)]
            if len(integer_keys) == len(items) and sorted(integer_keys) == list(
                range(1, len(items) + 1)
            ):
                by_key = dict(items)
                return [from_lua(by_key[index]) for index in range(1, len(items) + 1)]
            return {str(key): from_lua(item) for key, item in items}

        def decode(value="", *_a):
            return self._to_lua_value(json.loads(str(value)))

        def encode(value=None, *_a):
            return json.dumps(from_lua(value), ensure_ascii=False, separators=(",", ":"))

        luajson_shape = self._lua.eval(
            "function(decode, encode) "
            "local function module(fields, run) "
            "return setmetatable(fields, {__call = function(_self, ...) return run(...) end}) "
            "end "
            "local decoder = module({decode = decode}, decode) "
            "decoder.getDecoder = function() return decode end "
            "local encoder = module({encode = encode}, encode) "
            "encoder.getEncoder = function() return encode end "
            "return {decode = decoder, encode = encoder} "
            "end"
        )
        return luajson_shape(decode, encode)

    def _make_socket_bridge(self):
        """Non-network LuaSocket subset used by packs for a sub-second clock."""
        bridge = self._lua.table()
        bridge.gettime = lambda *_a: time.time()
        return bridge

    def _make_http_bridges(self):
        """Bounded LuaSocket HTTP GET plus the ``ltn12.sink.table`` contract."""
        deliver_bytes = self._lua.eval(
            "function(sink, bytes) local out = {} "
            "for i = 1, #bytes do out[i] = string.char(bytes[i]) end "
            "return sink(table.concat(out)) end"
        )
        make_table_sink = self._lua.eval(
            "function(target) return function(chunk) "
            "if chunk ~= nil then target[#target + 1] = chunk end return 1 end end"
        )

        def headers_table(headers: object):
            try:
                values = {str(key): str(value) for key, value in headers.items()}
            except AttributeError:
                values = {}
            return self._lua.table_from(values)

        def raw_request(options: object = "", *_args: object):
            if hasattr(options, "items"):
                url = str(options["url"] or "")
                sink = options["sink"]
            else:
                url = str(options or "")
                sink = None
            if not url:
                return None, 0, self._lua.table(), "missing URL"
            if sink is None:
                return None, 0, self._lua.table(), "missing response sink"
            try:
                with _open_http(url) as response:
                    status = int(getattr(response, "status", response.getcode()))
                    headers = headers_table(response.headers)
                    reason = str(getattr(response, "reason", ""))
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) > _HTTP_MAX_BYTES:
                        return None, 413, headers, "response exceeds sound download limit"
                    total = 0
                    while True:
                        chunk = response.read(_HTTP_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _HTTP_MAX_BYTES:
                            return None, 413, headers, "response exceeds sound download limit"
                        deliver_bytes(sink, self._lua.table_from(list(chunk)))
                    sink(None)
                    return 1, status, headers, reason
            except HTTPError as error:
                return None, error.code, headers_table(error.headers), str(error.reason)
            except Exception as error:  # noqa: BLE001 - LuaSocket reports transport errors
                return None, 0, self._lua.table(), f"{type(error).__name__}: {error}"

        http = self._lua.table()
        http.request = self._lua.eval(
            "function(raw_request, table_sink) return function(options) "
            "if type(options) == 'table' and options.sink ~= nil then "
            "return raw_request(options) end "
            "local body, request = {}, {} "
            "if type(options) == 'table' then "
            "for key, value in pairs(options) do request[key] = value end "
            "else request.url = options end "
            "request.sink = table_sink(body) "
            "local ok, status, headers, reason = raw_request(request) "
            "if not ok then return nil, status, headers, reason end "
            "return table.concat(body), status, headers, reason end end"
        )(raw_request, make_table_sink)
        sink = self._lua.table()
        sink.table = make_table_sink
        ltn12 = self._lua.table()
        ltn12.sink = sink
        return http, ltn12

    def _make_lfs_bridge(self):
        """LuaFileSystem reads confined to the installed pack."""
        base = Path(self._base_dir).resolve() if self._base_dir else None
        make_iterator = self._lua.eval(
            "function(items) local i = 0; return function() "
            "i = i + 1; return items[i] end end"
        )

        def resolve(value: object) -> Path | None:
            if base is None:
                return None
            normalized = str(value or "").replace("\\", "/")
            requested = Path(normalized)
            anchor = self._client_dir or base
            candidate = requested if requested.is_absolute() else anchor / requested
            try:
                candidate = candidate.resolve()
            except OSError:
                return None
            return candidate if candidate.is_relative_to(base) else None

        def attributes(value: object = "", key: object = None, *_args: object):
            path = resolve(value)
            if path is None or not path.exists():
                return None
            try:
                stat = path.stat()
            except OSError:
                return None
            values = {
                "access": stat.st_atime,
                "change": stat.st_ctime,
                "mode": "directory" if path.is_dir() else "file",
                "modification": stat.st_mtime,
                "size": stat.st_size,
            }
            if key is not None:
                return values.get(str(key))
            return self._lua.table_from(values)

        def directory(value: object = "", *_args: object):
            path = resolve(value)
            if path is None or not path.is_dir():
                return make_iterator(self._lua.table())
            try:
                names = [".", "..", *sorted(item.name for item in path.iterdir())]
            except OSError:
                names = []
            return make_iterator(self._lua.table_from(names))

        lfs = self._lua.table()
        lfs.attributes = attributes
        lfs.symlinkattributes = attributes
        lfs.currentdir = lambda *_a: (self._client_dir or base).as_posix() if base else ""
        lfs.dir = directory
        lfs.mkdir = lambda *_a: None
        lfs.rmdir = lambda *_a: None
        lfs.chdir = lambda *_a: None
        return lfs

    def _ppi_expose(self, name: object = "", fn: object = None) -> None:
        key = str(name)
        functions = self._exposed.setdefault(self._current_plugin, {})
        functions[key] = fn if fn is not None else self._lua_function(key)

    def _ppi_load(self, plugin_id: object = None):
        key = str(plugin_id or "")
        if key not in self._plugin_info:
            target = self._find_plugin_by_id(key)
            if target is not None and target not in self._loaded_includes:
                self._loaded_includes.add(target)
                reason = self._plugin_skip_reason(target)
                if reason:
                    self._mark_virtual_plugin(target)
                    self._skip_plugin(target.name, reason)
                else:
                    try:
                        self._load_path(target)
                    except Exception as exc:  # noqa: BLE001 - dependency remains isolated
                        self._include_errors.append(
                            (target.name, f"{type(exc).__name__}: {exc}")
                        )
                    else:
                        self._install_late(target)
        functions = self._exposed.get(key)
        if not functions:
            return None
        # Each runs as the plugin that exposed it, as MUSHclient's ppi (CallPlugin
        # underneath) runs it, so its EnableTrigger and GetPluginID are that plugin's. And,
        # as through CallPlugin, the function is found when it's called, so a reloaded
        # plugin's new code answers.
        return self._lua.table_from({name: self._as_plugin(key, name) for name in functions})

    def _as_plugin(self, plugin_id: str, name: str):
        def call(*args: object):
            function = self._exposed.get(plugin_id, {}).get(name)
            if function is None:
                return None  # the plugin was unloaded, or no longer exposes it
            previous = self._current_plugin
            info = self._plugin_info.get(plugin_id, {})
            previous_caller = info.get("calling", "")
            info["calling"] = previous if previous in self._plugin_info else ""
            self._current_plugin = plugin_id
            try:
                return function(*args)
            finally:
                self._current_plugin = previous
                info["calling"] = previous_caller

        return call

    def _gmcp_value(self, path: object = ""):
        value = self._gmcp_values.get(str(path or "").casefold())
        return self._to_lua_value(value)

    def _to_lua_value(self, value: object):
        if isinstance(value, dict):
            table = self._lua.table()
            for key, item in value.items():
                table[str(key)] = self._to_lua_value(item)
            return table
        if isinstance(value, list):
            table = self._lua.table()
            for index, item in enumerate(value, 1):
                table[index] = self._to_lua_value(item)
            return table
        return value

    def _broadcast_plugin(self, message: object = 0, text: object = "", *_args: object) -> int:
        """MUSHclient ``BroadcastPlugin``: every other enabled plugin's OnPluginBroadcast
        hears it (never the sender, which could loop); answers how many did."""
        return self._broadcast_from(self._current_plugin, message, text)

    def _broadcast_from(self, sender: str, message: object, text: object) -> int:
        sender_id = "" if sender == "world" else sender
        sender_name = str(self._plugin_info.get(sender, {}).get("name", ""))
        heard = 0
        for plugin_id, hooks in list(self._hooks.items()):
            if plugin_id == sender or "OnPluginBroadcast" not in hooks:
                continue
            if not self._plugin_enabled(plugin_id):
                continue
            heard += 1
            self._call_hook(
                plugin_id, "OnPluginBroadcast", int(_to_float(message) or 0), sender_id,
                sender_name, _dialog_text(text),
            )
        return heard

    def _get_plugin_id(self, *_args: object) -> str:
        return "" if self._current_plugin == "world" else self._current_plugin

    def _get_plugin_name(self, *_args: object) -> str:
        info = self._plugin_info.get(self._current_plugin, {})
        return str(info.get("name", ""))

    def _is_plugin_installed(self, plugin_id: object = "", *_args: object) -> bool:
        return str(plugin_id or "") in self._plugin_info

    def _get_plugin_info(self, plugin_id: object = "", info_type: object = 0, *_args: object):
        """MUSHclient ``GetPluginInfo``, every type its methods_plugins.cpp answers. Materia
        Magica's plugins print their help from 3 (the description) and 14 (a date)."""
        found = self._find_plugin(plugin_id)
        if found is None:
            return None
        info = self._plugin_info[found]
        path = info.get("path")
        code = int(_to_float(info_type) or 0)
        if code in (9, 10):
            kind = TRIGGER if code == 9 else ALIAS
            return sum(1 for plugin, _label in self._rules[kind] if plugin == found)
        if code == 11:
            return sum(1 for plugin, _label in self._timers if plugin == found)
        if code == 16:  # MUSHclient starts a script engine for script, or send-to-script rules
            return bool(info.get("script")) or any(
                rule.send_to in _SEND_TO_SCRIPT_CODES
                for rules in self._rules.values()
                for (plugin, _label), rule in rules.items() if plugin == found
            )
        if code == 21:  # place in the plugin list, which MUSHclient keeps in sequence order
            order = sorted(
                enumerate(self._plugin_info.items()),
                key=lambda item: (int(item[1][1].get("sequence", _PLUGIN_SEQUENCE)), item[0]),
            )
            return next(place for place, (_index, (key, _info)) in enumerate(order, 1)
                        if key == found)
        values = {
            1: info.get("name", ""),
            2: info.get("author", ""),
            3: info.get("description", ""),
            4: info.get("script", ""),
            5: info.get("language", ""),
            6: str(path) if isinstance(path, Path) else "",
            7: found,
            8: info.get("purpose", ""),
            12: len(self._api.variables()),
            13: info.get("date_written"),  # nil when the plugin gives none
            14: info.get("date_modified"),
            15: bool(info.get("save_state")),
            17: bool(info["enabled"]),
            18: float(info.get("requires", 0.0)),
            19: float(info.get("version", 0.0)),
            # The plugin's directory, including the trailing separator.
            20: path.parent.as_posix() + "/" if isinstance(path, Path) else "",
            22: info.get("installed"),
            23: info.get("calling", ""),  # the plugin calling it through CallPlugin, if any
            24: 0.0,  # time spent in its scripts, which genericMud doesn't keep
            25: int(info.get("sequence", _PLUGIN_SEQUENCE)),
        }
        return values.get(code)

    def _enable_plugin(self, plugin_id: object = "", enabled: object = True, *_args: object) -> int:
        found = self._find_plugin(plugin_id)
        if found is None:
            return E_NO_SUCH_PLUGIN
        info = self._plugin_info[found]
        wanted = _lua_flag(enabled)
        if bool(info.get("enabled", True)) == wanted:
            return _SCRIPT_ERROR_OK
        info["enabled"] = wanted
        for rules in self._rules.values():
            for (plugin, _label), rule in rules.items():
                if plugin == found:
                    self._sync_engine(rule)
        # As MUSHclient does, the plugin hears it was switched on or off.
        self._call_hook(found, "OnPluginEnable" if wanted else "OnPluginDisable")
        return _SCRIPT_ERROR_OK

    def _load_plugin_api(self, filename: object = "", *_args: object) -> int:
        name = str(filename or "")
        target = self._resolve_pack_file(name)
        if target is None:
            self._skip_plugin(name, "required plugin is not bundled with the pack")
            return ERROR_CODES["ePluginFileNotFound"]
        if target in self._loaded_includes:
            return _SCRIPT_ERROR_OK
        self._loaded_includes.add(target)
        reason = self._plugin_skip_reason(target)
        if reason:
            self._mark_virtual_plugin(target)
            self._skip_plugin(target.name, reason)
            return _SCRIPT_ERROR_OK
        try:
            self._load_path(target)
        except Exception as exc:  # noqa: BLE001 - dependency failure stays local to that plugin
            self._include_errors.append((name, f"{type(exc).__name__}: {exc}"))
            return ERROR_CODES["eProblemsLoadingPlugin"]
        self._install_late(target)
        return _SCRIPT_ERROR_OK

    def _install_late(self, path: Path) -> None:
        """A plugin loaded while playing hears OnPluginInstall then, as MUSHclient installs
        one it loads at any time. It doesn't hear OnPluginConnect: MUSHclient calls that only
        when the world connects, and plugins that need it call it themselves when
        IsConnected() (Erion's MSDP handler does)."""
        if not self._lifecycle_started:
            return  # the session's own install round will reach it
        plugin_id, _name, _language = self._plugin_metadata(path)
        if plugin_id in self._plugin_info:
            self._call_hook(plugin_id, "OnPluginInstall")

    # --- plugins: their variables, routines, state and lifecycle ---

    def _plugin_enabled(self, plugin_id: str) -> bool:
        """Whether a plugin is switched on; the world always is. A disabled plugin's
        triggers, aliases, timers and hooks stay quiet, as in MUSHclient."""
        info = self._plugin_info.get(plugin_id)
        return info is None or bool(info.get("enabled", True))

    def _find_plugin(self, plugin_id: object) -> str | None:
        """A plugin's id from its id or, as MUSHclient also accepts, its name."""
        key = str(plugin_id or "")
        if key in self._plugin_info:
            return key
        folded = key.casefold()
        return next(
            (
                found for found, info in self._plugin_info.items()
                if str(info.get("name", "")).casefold() == folded and folded
            ),
            None,
        )

    def _get_plugin_variable(self, plugin_id: object = "", name: object = "", *_args: object):
        """GetPluginVariable: nil for a plugin that isn't installed or a variable it hasn't
        set. Every plugin shares one set of variables here, "" naming the world's."""
        if str(plugin_id or "") and self._find_plugin(plugin_id) is None:
            return None
        return self._api.get_var(str(name or ""), None)

    def _get_plugin_variable_list(self, plugin_id: object = "", *_args: object):
        if str(plugin_id or "") and self._find_plugin(plugin_id) is None:
            return None
        return self._lua.table_from(self._api.variables())

    def _plugin_routine(self, plugin_id: str, routine: str):
        """The function ``routine`` of a plugin: one it exposed, one of its hooks, or a
        global function of that name."""
        return (
            self._exposed.get(plugin_id, {}).get(routine)
            or self._hooks.get(plugin_id, {}).get(routine)
            or self._lua_function(routine, plugin_id)
        )

    def _plugin_supports(self, plugin_id: object = "", routine: object = "", *_args: object) -> int:
        """PluginSupports: eOK when the plugin has that function (Mush-Z asks before each
        CallPlugin), else eNoSuchPlugin or eNoSuchRoutine."""
        found = self._find_plugin(plugin_id)
        if found is None:
            return E_NO_SUCH_PLUGIN
        name = str(routine or "")
        if not name or self._plugin_routine(found, name) is None:
            return E_NO_SUCH_ROUTINE
        return E_OK

    def _call_hook(self, plugin_id: str, name: str, *args: object) -> None:
        """Run one plugin's own lifecycle hook, as that plugin; a fault is reported, not raised."""
        fn = self._hooks.get(plugin_id, {}).get(name)
        if fn is None:
            return
        previous = self._current_plugin
        self._current_plugin = plugin_id
        try:
            self._guard.run(fn, *args)
        finally:
            self._current_plugin = previous

    def _save_state(self, *_args: object) -> int:
        """MUSHclient ``SaveState``: the plugin's OnPluginSaveState puts its settings in its
        variables (Miriani-Next's channel history saves its database there), then genericMud
        saves the variables. The world isn't a plugin, as MUSHclient answers."""
        plugin_id = self._current_plugin
        if plugin_id == "world":
            return ERROR_CODES["eNotAPlugin"]
        if self._saving_state:
            return ERROR_CODES["ePluginCouldNotSaveState"]
        self._saving_state = True
        try:
            self._call_hook(plugin_id, "OnPluginSaveState")
        finally:
            self._saving_state = False
        self._api.save_state()
        return E_OK

    def _forget_plugin(self, plugin_id: str) -> None:
        """Drop a plugin's triggers, aliases, timers, hooks and exposed functions."""
        for kind in (TRIGGER, ALIAS):
            for (plugin, _label), rule in list(self._rules[kind].items()):
                if plugin == plugin_id:
                    self._retire_rule(rule)
        for key, state in list(self._timers.items()):
            if key[0] == plugin_id:
                del self._timers[key]
                state["generation"] += 1
        self._hooks.pop(plugin_id, None)
        self._exposed.pop(plugin_id, None)
        # Loaded again, it starts with fresh globals and runs its modules again, as a new
        # Lua state does in MUSHclient.
        self._plugin_envs.pop(plugin_id, None)
        self._plugin_loaded.pop(plugin_id, None)
        self._forget_modules(plugin_id)
        # Its files load afresh next time: the plugin file and the plain includes it pulled in.
        for path in self._plugin_files.pop(plugin_id, set()):
            self._loaded_includes.discard(path)
        info_path = self._plugin_info.get(plugin_id, {}).get("path")
        if isinstance(info_path, Path):
            self._loaded_includes.discard(info_path)

    def _is_connected(self) -> bool:
        """IsConnected and GetInfo(106): connected, except while the session's plugins are
        first installed (see dispatch_install)."""
        return not self._opening and self._api.is_connected()

    def _module_failed(self, name: str, path: Path, error: Exception) -> None:
        """A bundled module that failed to load, noted once however many plugins need it."""
        item = (f"require({name}) [{path.name}]", f"{type(error).__name__}: {error}")
        if item not in self._module_errors:
            self._module_errors.append(item)

    def _forget_modules(self, plugin_id: str) -> None:
        if self._forget_scope is not None:
            self._forget_scope(plugin_id)

    def _reload_plugin(self, plugin_id: object = "", *_args: object) -> int:
        """MUSHclient ``ReloadPlugin``: load a plugin's file again from scratch and install
        it. A plugin can't reload itself (Mush-Z does it from a DoAfterSpecial instead)."""
        found = self._find_plugin(plugin_id)
        if found is None:
            return E_NO_SUCH_PLUGIN
        if found == self._current_plugin:
            return ERROR_CODES["eBadParameter"]
        info = self._plugin_info[found]
        path = info.get("path")
        if info.get("virtual") or not isinstance(path, Path):
            return E_OK  # genericMud does that plugin's job; there is nothing to reload
        self._call_hook(found, "OnPluginClose")
        self._forget_plugin(found)
        self._loaded_includes.add(path)
        try:
            self._load_path(path)
        except Exception as exc:  # noqa: BLE001 - the plugin stays unloaded, the pack goes on
            self._include_errors.append((path.name, f"{type(exc).__name__}: {exc}"))
            return ERROR_CODES["eProblemsLoadingPlugin"]
        # As MUSHclient loads any plugin: OnPluginInstall, and no OnPluginConnect.
        self._call_hook(found, "OnPluginInstall")
        return E_OK

    def _unload_plugin(self, plugin_id: object = "", *_args: object) -> int:
        found = self._find_plugin(plugin_id)
        if found is None:
            return E_NO_SUCH_PLUGIN
        if found == self._current_plugin:
            return ERROR_CODES["eBadParameter"]
        self._call_hook(found, "OnPluginClose")
        self._forget_plugin(found)
        del self._plugin_info[found]
        return E_OK

    def _replace(
        self, source: object = "", search: object = "", replacement: object = "",
        multiple: object = True, *_args: object,
    ) -> str:
        """MUSHclient ``Replace``: every match, or only the first when Multiple is false.
        Chatpack builds its sound file names with it."""
        text = _dialog_text(source)
        old = _dialog_text(search)
        if not old:
            return text
        return text.replace(old, _dialog_text(replacement), -1 if _lua_flag(multiple) else 1)

    def _connect_duration(self, *_args: object) -> int:
        """Seconds connected this time, 0 while disconnected."""
        if self._connected_at is None:
            return 0
        return int(time.time() - self._connected_at)

    def _set_note_colour_fore(self, colour: object = 0, *_args: object) -> None:
        number = _to_float(colour)
        if number is not None:
            self._note_colours[0] = int(number) & 0xFFFFFF

    def _set_note_colour_back(self, colour: object = 0, *_args: object) -> None:
        number = _to_float(colour)
        if number is not None:
            self._note_colours[1] = int(number) & 0xFFFFFF

    # --- reading the output back ---

    def _get_line_info(self, number: object = 0, info_type: object = None, *_args: object):
        """MUSHclient ``GetLineInfo``: one field of an output line, or all of them as a table
        when no type is given (Copy_Output reads ``GetLineInfo(n).text``)."""
        line = self._api.buffered_line(int(_to_float(number) or 0))
        if line is None:
            return None
        runs = style_runs(line.plain_text, line.spans)
        fields = {
            "text": line.plain_text,
            "length": len(line.plain_text),
            "newline": True,
            "note": False,
            "user": False,
            "log": True,
            "bookmark": False,
            "hr": False,
            "time": int(line.ts),
            "line": line.seq + 1 if line.seq >= 0 else int(_to_float(number) or 0),
            "styles": len(runs),
            "ticks": line.ts,
            "elapsed": max(0.0, line.ts - self._opened_at),
        }
        if info_type is None or not _to_float(info_type):
            return self._lua.table_from(fields)  # 0 or none: every field, as MUSHclient answers
        order = (
            "text", "length", "newline", "note", "user", "log", "bookmark", "hr", "time",
            "line", "styles", "ticks", "elapsed",
        )
        index = int(_to_float(info_type) or 0)
        return fields[order[index - 1]] if 1 <= index <= len(order) else None

    def _get_style_info(
        self, number: object = 0, style: object = None, info_type: object = None,
        *_args: object,
    ):
        """MUSHclient ``GetStyleInfo``: one field of one colour run of an output line, every
        field of a run as a table, or every run when only the line is given."""
        line = self._api.buffered_line(int(_to_float(number) or 0))
        if line is None:
            return None
        runs = style_runs(line.plain_text, line.spans)
        column = 1
        tables = []
        for run in runs:
            run = {**run, "column": column}
            column += int(run["length"])
            tables.append(run)

        def fields_of(run: dict) -> dict:
            style = int(run["style"])
            return {
                "text": run["text"], "length": run["length"], "column": run["column"],
                "actiontype": 0, "action": "", "hint": "", "variable": "",
                "bold": bool(style & 1), "ul": False, "blink": False, "inverse": False,
                "changed": False, "starttag": False, "textcolour": run["textcolour"],
                "backcolour": run["backcolour"],
            }

        if style is None:
            result = self._lua.table()
            for index, run in enumerate(tables, 1):
                result[index] = self._lua.table_from(fields_of(run))
            return result
        position = int(_to_float(style) or 0)
        if not 1 <= position <= len(tables):
            return None
        run = tables[position - 1]
        if info_type is None:
            return self._lua.table_from(fields_of(run))
        values = {
            # 4 is the MXP action's type (0, none), 5 to 7 its action, hint and variable.
            1: run["text"], 2: run["length"], 3: run["column"], 4: 0, 5: "", 6: "", 7: "",
            8: bool(int(run["style"]) & 1), 9: False, 10: False, 11: False, 12: False,
            13: False, 14: run["textcolour"], 15: run["backcolour"],
        }
        return values.get(int(_to_float(info_type) or 0))

    def _dofile(self, filename: object = "", *_args: object):
        """Pack-confined `dofile`; a missing optional support file is a recorded no-op."""
        name = str(filename or "")
        target = self._resolve_pack_file(name)
        if target is None:
            self._skip_plugin(name, "Lua support file is not bundled with the pack")
            return None
        try:
            return self._load_path(target)
        except Exception as exc:  # noqa: BLE001 - keep the containing sound plugin usable
            self._external_script_errors.append((name, f"{type(exc).__name__}: {exc}"))
            return None

    # --- MUSHclient global API ---

    def _install_api(self) -> None:
        api = self._api

        def set_variable(name: object = "", value: object = "", *_args: object) -> int:
            if value is None:
                # MUSHclient raises here; a pack clearing a setting means "no value", and
                # storing the word "None" would read back as a real one.
                api.delete_var(str(name))
            else:
                api.set_var(str(name), value)
            return _SCRIPT_ERROR_OK

        def delete_variable(name: object = "", *_args: object) -> int:
            api.delete_var(str(name))
            return _SCRIPT_ERROR_OK

        # MUSHclient's Note, Send, SendNoEcho and Tell take any number of arguments and join
        # them with Lua's tostring (lua_methods.cpp concatArgs), so Note("HP: ", hp) is one
        # line. Built in Lua so the joining is Lua's own tostring, nil and numbers included.
        joined = self._lua.eval(
            "function(fn, separator)\n"
            "  return function(...)\n"
            "    local parts = {}\n"
            "    for index = 1, select('#', ...) do\n"
            "      parts[index] = tostring((select(index, ...)))\n"
            "    end\n"
            "    return fn(table.concat(parts, separator))\n"
            "  end\n"
            "end"
        )

        # AppendToNotepad and its kin join everything after the title the same way
        # (Mush-Z passes the line ending as a separate argument).
        titled = self._lua.eval(
            "function(fn)\n"
            "  return function(title, ...)\n"
            "    local parts = {}\n"
            "    for index = 1, select('#', ...) do\n"
            "      parts[index] = tostring((select(index, ...)))\n"
            "    end\n"
            "    return fn(title, table.concat(parts))\n"
            "  end\n"
            "end"
        )

        funcs = {
            "Send": joined(self._send_text, ""),
            "SendNoEcho": joined(self._send_text, ""),
            # Execute = "as if typed": aliases match first (Erion's historyadd Executes
            # history_add, which its own channel_history alias consumes); send-through only
            # for what no alias claims.
            "Execute": api.execute,
            "Note": joined(self._note, ""),
            "Tell": joined(self._tell, ""),
            "ColourNote": self._colour_note,
            "ColourTell": self._colour_tell,
            # nil (not "") for an unset variable -- MUSHclient semantics. Erion's
            # OnPluginInstall does `if GetVariable(...) ~= nil` to keep saved toggle
            # settings; an ""-for-unset answer makes it adopt "" and every toggle-gated
            # sound stays off.
            "GetVariable": lambda name="": api.get_var(str(name), None),
            "SetVariable": set_variable,
            "GetOption": self._get_option,
            "SetOption": self._set_option,
            "GetAlphaOption": self._get_alpha_option,
            "SetAlphaOption": self._set_alpha_option,
            "GetAlphaOptionList": self._get_alpha_option_list,
            "GetGlobalOption": self._get_global_option,
            "GetGlobalOptionList": self._get_global_option_list,
            # The Array* family MSDP packs keep state in (room name etc.). Real bindings,
            # not black-holed: a black-holed ArrayGet returns a table, and concatenating
            # that raises inside the plugin's subnegotiation handler.
            "ArrayCreate": self._array_create,
            "ArraySet": self._array_set,
            "ArrayGet": self._array_get,
            "ArrayDelete": self._array_delete,
            "ArrayClear": self._array_clear,
            "ArrayExists": lambda name="", *_a: str(name) in self._arrays,
            "ArrayCount": lambda *_a: len(self._arrays),
            "ArraySize": lambda name="", *_a: len(self._arrays.get(str(name), {})),
            "ArrayKeyExists": lambda name="", key="", *_a: str(key) in self._arrays.get(
                str(name), {}
            ),
            "ArrayDeleteKey": self._array_delete_key,
            "ArrayListAll": lambda *_a: self._lua_list(sorted(self._arrays)),
            "ArrayListKeys": lambda name="", *_a: self._array_listing(name, keys=True),
            "ArrayListValues": lambda name="", *_a: self._array_listing(name, keys=False),
            "ArrayGetFirstKey": lambda name="", *_a: self._array_end_key(name, last=False),
            "ArrayGetLastKey": lambda name="", *_a: self._array_end_key(name, last=True),
            "ArrayImport": self._array_import,
            "ArrayExport": lambda name="", delimiter="", *_a: self._array_export(
                name, delimiter, keys_only=False
            ),
            "ArrayExportKeys": lambda name="", delimiter="", *_a: self._array_export(
                name, delimiter, keys_only=True
            ),
            "GetVariableList": lambda *_a: self._lua.table_from(api.variables()),
            "GetPluginVariable": self._get_plugin_variable,
            "GetPluginVariableList": self._get_plugin_variable_list,
            "PluginSupports": self._plugin_supports,
            "SaveState": self._save_state,
            "ReloadPlugin": self._reload_plugin,
            "UnloadPlugin": self._unload_plugin,
            "Simulate": joined(api.simulate, ""),
            "Replace": self._replace,
            "StripANSI": lambda text="", *_a: strip_ansi(str(text or "")),
            "MakeRegularExpression": lambda text="", *_a: make_regular_expression(
                str(text or "")
            ),
            "FixupEscapeSequences": lambda text="", *_a: fixup_escape_sequences(str(text or "")),
            "EvaluateSpeedwalk": lambda text="", *_a: evaluate_speedwalk(
                expand_speedwalk(str(text or ""))
            ),
            "ErrorDesc": lambda code=0, *_a: ERROR_DESCRIPTIONS.get(
                int(_to_float(code) or 0), str(int(_to_float(code) or 0))
            ),
            "MtRand": lambda *_a: self._random.random(),
            "MtSrand": lambda seed=0, *_a: self._random.seed(int(_to_float(seed) or 0)),
            "GetConnectDuration": self._connect_duration,
            "GetLinesInBufferCount": lambda *_a: api.line_count(),
            "GetLineInfo": self._get_line_info,
            "GetStyleInfo": self._get_style_info,
            "AnsiNote": lambda text="", *_a: self._note(strip_ansi(str(text or ""))),
            "SetClipboard": lambda text="", *_a: self._window().set_clipboard(
                _dialog_text(text)
            ),
            "GetClipboard": lambda *_a: self._window().get_clipboard(),
            "GetCommand": lambda *_a: self._window().get_command(),
            "SetCommand": self._set_command,
            "PasteCommand": lambda text="", *_a: self._window().paste_command(
                _dialog_text(text)
            ),
            "SetCommandSelection": self._set_command_selection,
            "OpenBrowser": self._open_browser,
            "AppendToNotepad": titled(
                lambda title, text: self._notepad_write(title, text, replace=False)
            ),
            "ReplaceNotepad": titled(
                lambda title, text: self._notepad_write(title, text, replace=True)
            ),
            "SendToNotepad": titled(
                lambda title, text: self._notepad_write(title, text, replace=True)
            ),
            "ActivateNotepad": self._activate_notepad,
            "CloseNotepad": self._close_notepad,
            "GetNotepadList": lambda *_a: self._lua_list(list(self._notepads)),
            "GetNotepadText": lambda title="", *_a: self._notepads.get(
                self._notepad_title(title), ""
            ),
            "GetNotepadLength": lambda title="", *_a: len(
                self._notepads.get(self._notepad_title(title), "")
            ),
            "NotepadReadOnly": self._notepad_exists,
            "NotepadSaveMethod": self._notepad_exists,
            "NotepadColour": self._notepad_exists,
            "NotepadFont": self._notepad_exists,
            "MoveNotepadWindow": self._notepad_exists,
            "Hash": self._byte_args(lambda data, *_a: sha0_hex(_table_bytes(data))),
            "Base64Encode": self._byte_args(
                lambda data, lines=False, *_a: _base64_encode(_table_bytes(data), lines)
            ),
            "Base64Decode": lambda text="", *_a: _base64_decode(text),
            "EditDistance": lambda first="", second="", *_a: _edit_distance(
                _dialog_text(first), _dialog_text(second)
            ),
            # MUSHclient windows and settings genericMud has no equivalent of: the status line,
            # the info bar, its own menu commands, note styling and redrawing. They answer
            # like MUSHclient so a pack carries on, and do nothing.
            "SetStatus": lambda *_a: None,
            "DoCommand": lambda *_a: E_NO_SUCH_COMMAND,
            "NoteStyle": lambda *_a: None,
            "GetNoteStyle": lambda *_a: 0,
            "SetNoteColourFore": self._set_note_colour_fore,
            "SetNoteColourBack": self._set_note_colour_back,
            "GetNoteColourFore": lambda *_a: self._note_colours[0],
            "GetNoteColourBack": lambda *_a: self._note_colours[1],
            "Repaint": lambda *_a: None,
            "Redraw": lambda *_a: None,
            "SetTrace": lambda *_a: None,
            "Info": lambda *_a: None,
            "InfoClear": lambda *_a: None,
            "InfoColour": lambda *_a: None,
            "InfoBackground": lambda *_a: None,
            # Truly delete: MUSHclient's GetVariable answers nil after a delete, and
            # packs distinguish nil ("use my default") from "" (a saved empty value).
            "DeleteVariable": delete_variable,
            # Real transport state: Erion's OnPluginTick branches on it to run its
            # music engine vs. stop everything (the black hole answered truthy-table).
            "IsConnected": lambda *_a: self._is_connected(),
            "Version": lambda *_a: _MUSHCLIENT_VERSION,
            "GetUniqueID": lambda *_a: uuid4().hex[:24],
            "GetUniqueNumber": self._get_unique_number,
            "GetWorldID": lambda *_a: self._world_id,
            "GetWorld": self._get_world,
            "GetWorldById": self._get_world_by_id,
            "GetWorldIdList": lambda *_a: self._lua.table_from([self._world_id]),
            "GetWorldList": lambda *_a: self._lua.table_from(
                [self._world_name] if self._world_name else []
            ),
            "WorldName": lambda *_a: self._world_name,
            "WorldAddress": lambda *_a: self._world_address,
            "WorldPort": lambda *_a: self._world_port,
            "GetPluginList": lambda *_a: self._lua.table_from(list(self._plugin_info)),
            "Trim": lambda value="", *_a: str(value).strip(),
            "GetPluginID": self._get_plugin_id,
            "GetPluginName": self._get_plugin_name,
            "GetPluginInfo": self._get_plugin_info,
            "IsPluginInstalled": self._is_plugin_installed,
            "LoadPlugin": self._load_plugin_api,
            "EnablePlugin": self._enable_plugin,
            "dofile": self._dofile,
            "gmcp": self._gmcp_value,
            "BroadcastPlugin": self._broadcast_plugin,
            "Accelerator": self._accelerator,
            "AcceleratorTo": self._accelerator_to,
            "AddTrigger": self._add_trigger_api,
            "AddTriggerEx": self._add_trigger_ex,
            "DeleteTrigger": self._delete_trigger,
            "CallPlugin": self._call_plugin,
            "EnableTrigger": self._enable_trigger,
            "EnableTriggerGroup": self._enable_trigger_group,
            "DeleteTriggerGroup": lambda group="", *_a: self._delete_rule_group(TRIGGER, group),
            "DeleteTemporaryTriggers": lambda *_a: self._delete_temporary_rules(TRIGGER),
            "GetTriggerList": lambda *_a: self._rule_list(TRIGGER),
            "IsTrigger": lambda name="", *_a: self._is_rule(TRIGGER, name),
            "GetTriggerInfo": lambda name="", number=0, *_a: self._rule_info(
                TRIGGER, name, number
            ),
            "GetTriggerOption": lambda name="", option="", *_a: self._get_rule_option(
                TRIGGER, name, option
            ),
            "SetTriggerOption": lambda name="", option="", value="", *_a: self._set_rule_option(
                TRIGGER, name, option, value
            ),
            "AddAlias": self._add_alias_api,
            "DeleteAlias": self._delete_alias,
            "EnableAlias": self._enable_alias,
            "EnableAliasGroup": self._enable_alias_group,
            "DeleteAliasGroup": lambda group="", *_a: self._delete_rule_group(ALIAS, group),
            "DeleteTemporaryAliases": lambda *_a: self._delete_temporary_rules(ALIAS),
            "GetAliasList": lambda *_a: self._rule_list(ALIAS),
            "IsAlias": lambda name="", *_a: self._is_rule(ALIAS, name),
            "GetAliasInfo": lambda name="", number=0, *_a: self._rule_info(ALIAS, name, number),
            "GetAliasOption": lambda name="", option="", *_a: self._get_rule_option(
                ALIAS, name, option
            ),
            "SetAliasOption": lambda name="", option="", value="", *_a: self._set_rule_option(
                ALIAS, name, option, value
            ),
            "EnableGroup": self._enable_group,
            "DeleteGroup": self._delete_group,
            "ImportXML": self._import_xml,
            "AddTimer": self._add_timer_api,
            "EnableTimer": self._enable_timer,
            "DeleteTimer": self._delete_timer,
            "ResetTimer": self._reset_timer,
            "EnableTimerGroup": self._enable_timer_group,
            "DeleteTimerGroup": self._delete_timer_group,
            "DeleteTemporaryTimers": self._delete_temporary_timers,
            "GetTimerList": self._timer_list,
            "IsTimer": self._is_timer,
            "GetTimerInfo": self._timer_info,
            "GetTimerOption": self._timer_option,
            "SetTimerOption": self._set_timer_option,
            "RGBColourToName": lambda colour=None, *_a: colour_to_name(colour),
            "ColourNameToRGB": lambda name="", *_a: colour_from_name(name),
            "GetNormalColour": lambda which=0, *_a: _palette_colour(ANSI_NORMAL, which),
            "GetBoldColour": lambda which=0, *_a: _palette_colour(ANSI_BOLD, which),
            "Hyperlink": lambda *_a: None,
            "GetSoundKeyword": lambda *_a: "",
            "PlaySound": self._play_sound,
            "StopSound": self._stop_sound,
            "GetSoundStatus": self._get_sound_status,
            "Sound": self._sound,
            "GetInfo": self._get_info,
            "DoAfter": self._do_after,
            "DoAfterNote": self._do_after_note,
            "DoAfterSpeedWalk": self._do_after_speedwalk,
            "DoAfterSpecial": self._do_after_special,
        }
        g = self._lua.globals()
        for name, fn in funcs.items():
            g[name] = fn
        # MUSHclient points Lua's print at the output window, joining its arguments with
        # spaces. Packs rely on it: Empire's "omit and reprint" triggers hide a line and
        # print a reworded copy, which would otherwise go to a console nobody can see.
        g["print"] = joined(self._note, " ")
        # Packs call both bare (Sound(...)) and through the world object
        # (world.Sound(...), world.getvariable(...)). Mirror funcs onto a world
        # table, with lowercase aliases for the world.lowercase() callers.
        world = self._lua.table()
        for name, fn in funcs.items():
            world[name] = fn
            world[name.lower()] = fn
        g.world = world
        # Real `bit` ops and the AddTrigger flag/colour constants. These were black-holed,
        # which broke every runtime registration: bit.bor returned a table and
        # trigger_flag.Enabled was nil, so AddTriggerEx got garbage flags (Erion's F-key
        # report triggers are all registered this way).
        bit = self._lua.table()
        bit.bor = lambda *a: _reduce_ints(a, lambda x, y: x | y)
        bit.band = lambda *a: _reduce_ints(a, lambda x, y: x & y)
        bit.bxor = lambda *a: _reduce_ints(a, lambda x, y: x ^ y)
        bit.bnot = lambda x=0: ~(int(_to_float(x) or 0)) & 0xFFFFFFFF
        bit.lshift = (
            lambda x=0, n=0: (int(_to_float(x) or 0) << int(_to_float(n) or 0)) & 0xFFFFFFFF
        )
        bit.rshift = (
            lambda x=0, n=0: (int(_to_float(x) or 0) & 0xFFFFFFFF)
            >> int(_to_float(n) or 0)
        )
        g.bit = bit
        trigger_flag = self._lua.table()
        for flag_name, value in _TRIGGER_FLAGS.items():
            trigger_flag[flag_name] = value
        g.trigger_flag = trigger_flag
        g.sendto = self._lua.table_from(_SEND_TO_NAMES)
        g.timer_flag = self._lua.table_from(_TIMER_FLAGS)
        g.alias_flag = self._lua.table_from(_ALIAS_FLAGS)
        # MUSHclient's own return codes and their descriptions, so a pack comparing a result
        # with error_code.eTriggerNotFound, or check.lua reporting error_desc[result], works.
        g.error_code = self._lua.table_from(ERROR_CODES)
        g.error_desc = self._lua.table_from(ERROR_DESCRIPTIONS)
        custom_colour = self._lua.table()
        custom_colour.NoChange = -1
        for i in range(1, 17):
            custom_colour[f"Custom{i}"] = i - 1
        g.custom_colour = custom_colour

    def _install_audio(self) -> None:
        """Provide the ``audio`` global that the LuaAudio packs (Erion, Empire, Mystavaria,
        Cosmic Rage and others) play every sound through: audio.dll's functions, played on the
        sound bus (see mushclient_audio)."""
        self._audio_dll = AudioDll(lua=self._lua, api=self._api, to_number=_to_float)
        self._lua.globals().audio = self._audio_dll.build()

    def _install_nvda(self) -> None:
        """Provide the ``nvda`` object MushReader.dll would have installed.

        The pack's whole speech surface funnels through it: ``tts_interrupt`` (how the
        F-key hp/mana reports and the history browser talk) does ``nvda.stop(); say(...)``
        with ``say`` wrapping ``nvda.say``. As a black hole those were silent no-ops --
        the report chain ran perfectly and said nothing. ``say`` speaks through the
        engine (own channel, so channel policy can tune it), ``stop`` cuts current
        speech (the interrupt half of the idiom). Every OTHER method must be callable
        but return nil -- the pack probes ``nvda.jaws_running()`` and a truthy
        black-hole answer sent speech to a JAWS object that doesn't exist.
        """
        api = self._api
        nvda = self._lua.eval(
            "setmetatable({}, {__index = function() return function() return nil end end})"
        )
        nvda.say = lambda text="", *_a: api.speak(str(text).strip(), channel="pack")
        nvda.stop = lambda *_a: api.stop_speech()
        self._lua.globals().nvda = nvda

    def _install_utils(self) -> None:
        """Provide MUSHclient's scalar/string and confined directory utilities."""

        def split(value: object = "", delimiter: object = "", maximum: object = 0, *_args):
            text = str(value)
            separator = str(delimiter)
            limit = int(_to_float(maximum) or 0)
            if not separator:
                parts = [text]
            else:
                parts = text.split(separator, limit if limit > 0 else -1)
            return self._lua.table_from(parts)

        def readdir(specification: object = "", *_args):
            if self._base_dir is None:
                return None
            base = Path(self._base_dir).resolve()
            anchor = self._client_dir or base
            normalized = str(specification or "").replace("\\", "/")
            requested = Path(normalized)
            target = requested if requested.is_absolute() else anchor / requested
            if target.is_dir():
                directory, wildcard = target, "*"
            else:
                directory, wildcard = target.parent, target.name or "*"
            try:
                directory = directory.resolve()
            except OSError:
                return None
            if not directory.is_dir() or not directory.is_relative_to(base):
                return None
            if wildcard == "*.*":  # Windows treats *.* as every entry.
                wildcard = "*"
            result = self._lua.table()
            matched = False
            try:
                children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
            except OSError:
                return None
            for child in children:
                if not fnmatchcase(child.name.casefold(), wildcard.casefold()):
                    continue
                try:
                    resolved = child.resolve()
                    stat = resolved.stat()
                except OSError:
                    continue
                if not resolved.is_relative_to(base):
                    continue
                metadata = self._lua.table()
                metadata.size = stat.st_size
                metadata.directory = resolved.is_dir()
                metadata.hidden = child.name.startswith(".")
                metadata.readonly = not bool(stat.st_mode & 0o200)
                metadata.normal = not metadata.directory
                metadata.write_time = stat.st_mtime
                result[child.name] = metadata
                matched = True
            return result if matched else None

        utils = self._lua.eval(
            "setmetatable({}, {__index = function() "
            "return function() return nil end end})"
        )
        utils.split = split
        utils.readdir = readdir
        # MUSHclient's hashing and encoding helpers. md5 and sha256 answer the raw digest
        # (packs pass it to tohex), hash answers SHA-1 in hex, as MUSHclient's do. They take
        # any bytes, which reach Python as a table of byte values: lupa can't hand over a
        # Lua string that isn't UTF-8 text, and a digest never is.
        utils.timer = lambda *_a: time.perf_counter()
        utils.tohex = self._byte_args(lambda data, *_a: _table_bytes(data).hex().upper())
        utils.md5 = self._byte_args(
            lambda data, *_a: hashlib.md5(_table_bytes(data), usedforsecurity=False).digest()
        )
        utils.sha256 = self._byte_args(
            lambda data, *_a: hashlib.sha256(_table_bytes(data)).digest()
        )
        utils.hash = self._byte_args(lambda data, *_a: sha0_hex(_table_bytes(data)))
        utils.base64encode = self._byte_args(
            lambda data, lines=False, *_a: _base64_encode(_table_bytes(data), lines)
        )
        utils.base64decode = lambda value="", *_a: _base64_decode(value)
        utils.edit_distance = lambda first="", second="", *_a: _edit_distance(
            _dialog_text(first), _dialog_text(second)
        )
        utils.getfontfamilies = lambda *_a: self._lua.table()
        utils.appendtonotepad = lambda title="", text="", replace=False, *_a: (
            self._notepad_write(title, _dialog_text(text), replace=_lua_true(replace))
        )
        utils.activatenotepad = self._activate_notepad

        def shellexecute(target: object = "", *_args: object):
            # MUSHclient runs anything here; genericMud only opens web and mail links.
            address = _dialog_text(target).strip()
            if address.lower().startswith(_BROWSER_SCHEMES) and self._window().open_url(address):
                return True
            return None, "genericMud doesn't run programs or open files for soundpacks"

        utils.shellexecute = shellexecute
        self._install_dialogs(utils)
        self._lua.globals().utils = utils

    def _install_dialogs(self, utils) -> None:
        """MUSHclient's utils dialogs, shown by the window and waited on like MUSHclient does.

        The time the player takes doesn't count against the script's time limit. Without a
        window to show them, they answer as if the player backed out.
        """
        api = self._api

        def msgbox(message=None, title=None, buttons=None, icon=None, default=None, *_args):
            kind = _dialog_text(buttons, "ok").lower()
            if kind not in MESSAGE_BUTTONS:
                kind = "ok"
            answer = api.ask_message(
                _dialog_text(title, "MUSHclient"), _dialog_text(message), kind,
                _dialog_text(icon, "!"), int(_to_float(default) or 1),
            )
            # MUSHclient's msgbox always names a button, and packs compare the answer or
            # call string methods on it.
            return answer if answer is not None else _MSGBOX_BACKED_OUT[kind]

        def ask_text(message, title, default, extras, *, multiline: bool):
            validate = extras["validate"] if hasattr(extras, "items") else None
            limit = int(_to_float(extras["max_length"]) or 0) if hasattr(extras, "items") else 0
            text = _dialog_text(default)
            while True:
                answer = api.ask_text(
                    _dialog_text(title, "MUSHclient"), _dialog_text(message), text,
                    multiline=multiline,
                )
                if answer is None:
                    return None
                if limit > 0:
                    answer = answer[:limit]
                if multiline:  # a Windows edit box's lines end in CR LF, and packs split on it
                    answer = answer.replace("\r\n", "\n").replace("\n", "\r\n")
                # MUSHclient keeps the box open until the pack's validate function accepts
                # the text; it usually explains what's wrong in a msgbox of its own first.
                if not callable(validate) or _lua_true(validate(answer)):
                    return answer
                text = answer

        def inputbox(message=None, title=None, default=None, _font=None, _size=None,
                     extras=None, *_args):
            return ask_text(message, title, default, extras, multiline=False)

        def editbox(message=None, title=None, default=None, _font=None, _size=None,
                    extras=None, *_args):
            return ask_text(message, title, default, extras, multiline=True)

        def listbox(message=None, title=None, choices=None, default=None, *_args):
            keys, captions = _sorted_choices(choices)
            if not keys:
                return None
            # Nothing is selected unless the pack names an item, and OK alone is then nil:
            # the mapper's "Choose exit to delete" deletes whatever comes back.
            start = next((index for index, key in enumerate(keys) if key == default), None)
            index = api.ask_choice(
                _dialog_text(title, "MUSHclient"), _dialog_text(message), captions, start
            )
            return keys[index] if index is not None else None

        def multilistbox(message=None, title=None, choices=None, defaults=None, *_args):
            keys, captions = _sorted_choices(choices)
            if not keys:
                return None
            ticked = [
                index for index, key in enumerate(keys)
                if hasattr(defaults, "items") and _lua_true(defaults[key])
            ]
            indexes = api.ask_choices(
                _dialog_text(title, "MUSHclient"), _dialog_text(message), captions, ticked
            )
            if indexes is None:
                return None
            chosen = self._lua.table()
            for index in indexes:
                chosen[keys[index]] = True
            return chosen

        utils.msgbox = msgbox
        utils.umsgbox = msgbox
        utils.inputbox = inputbox
        utils.editbox = editbox
        utils.listbox = listbox
        utils.choose = listbox  # a drop-down list in MUSHclient; the same question here
        utils.multilistbox = multilistbox

    def _send_text(self, text: object = "") -> int:
        self._api.send(str(text))
        return _SCRIPT_ERROR_OK  # MUSHclient's Send answers eOK, and some packs Note() it

    def _note(self, text: object = "") -> None:
        """A finished output line: whatever Tell/ColourTell started, then ``text``.

        Spoken as well as shown. genericMud stands in for MushReader, the screen-reader plugin
        these packs are written for, and its OnPluginScreendraw reads a note (type 1) just as
        it reads the MUD's own lines (type 0), blank ones excepted. Without that, a pack that
        hides a line and prints a reworded copy (Empire's death message) would go silent.
        """
        line = self._pending_tell + str(text)
        self._pending_tell = ""
        self._api.echo(line)
        if line.strip() and self._api.line_speech():
            self._api.speak(line)

    def _tell(self, text: object = "") -> None:
        """MUSHclient Tell: output with no line break; the next note finishes the line."""
        self._pending_tell += str(text)

    def _flush_tell(self) -> None:
        if self._pending_tell:
            self._note()

    def _colour_note(self, *args: object) -> None:
        # ColourNote(fg, bg, text [, fg, bg, text]...) — concatenate the text parts.
        texts = [str(args[i]) for i in range(2, len(args), 3)]
        self._note("".join(texts))

    def _colour_tell(self, *args: object) -> None:
        # ColourTell(fg, bg, text [, fg, bg, text]...): Materia Magica's helper plugins build
        # whole report lines out of these and finish them with ColourNote or Note.
        texts = [str(args[i]) for i in range(2, len(args), 3)]
        self._tell("".join(texts))

    def _deliver(
        self, send_to: int, text: str, *, variable: str = "", notepad: str = ""
    ) -> None:
        """Do with ``text`` what MUSHclient's send_to code says (see _SEND_TO_*). The
        notepad codes write to the notepad ``notepad`` names, as MUSHclient titles it."""
        if send_to in _SEND_TO_SCRIPT_CODES:
            # Compiled here, inside the guard, so a syntax error is reported, not raised
            # into whatever line or timer happened to fire it.
            self._guard.run(lambda: self._compile(text)())
        elif send_to in _SEND_TO_WORLD_CODES:
            for command in _command_lines(text):
                self._api.send(command)
        elif send_to == _SEND_TO_OUTPUT_CODE:
            for line in text.splitlines() or [""]:
                self._note(line)
        elif send_to == _SEND_TO_EXECUTE_CODE:
            for command in _command_lines(text):
                self._api.execute(command)
        elif send_to == _SEND_TO_VARIABLE_CODE:
            if variable:
                self._api.set_var(variable, text)
        elif send_to == _SEND_TO_SPEEDWALK_CODE:
            steps = expand_speedwalk(text.replace(" ", ""))
            for command in steps or _command_lines(text):
                self._api.send(command)
        elif send_to in _SEND_TO_NOTEPAD_CODES and notepad:
            self._notepad_write(notepad, text + "\r\n", replace=send_to != _APPEND_TO_NOTEPAD)

    # --- the window: clipboard, command box, browser and notepads ---

    def _window(self):
        """The session's window, or the stand-in kept here when there isn't one."""
        return self._api.pack_window() or self._headless_window

    def _set_command(self, text: object = "", *_args: object) -> int:
        """MUSHclient ``SetCommand``: fills the command box only when it's empty."""
        if self._window().set_command(_dialog_text(text)):
            return E_OK
        return ERROR_CODES["eCommandNotEmpty"]

    def _set_command_selection(self, first: object = 1, last: object = -1, *_args) -> int:
        number = _to_float(last)
        self._window().select_command(
            int(_to_float(first) or 1), int(number) if number is not None else -1
        )
        return E_OK

    def _open_browser(self, url: object = "", *_args: object) -> int:
        """MUSHclient ``OpenBrowser``: web and mail links only, as MUSHclient allows, so a
        pack can't start a program or open a file this way."""
        address = _dialog_text(url).strip()
        if not address.lower().startswith(_BROWSER_SCHEMES):
            return ERROR_CODES["eBadParameter"]
        if self._window().open_url(address):
            return E_OK
        return ERROR_CODES["eCouldNotOpenFile"]

    def _notepad_title(self, title: object) -> str:
        """The title a pack's notepad is kept under: an existing one's, whatever the case.
        MUSHclient's FindNotepad compares titles ignoring case, and Toastush fills its
        "Changelog" notepad and then opens "changelog"."""
        name = _dialog_text(title)
        wanted = name.casefold()
        for existing in self._notepads:
            if existing.casefold() == wanted:
                return existing
        return name

    def _notepad_write(self, title: object, text: str, *, replace: bool) -> bool:
        """Add to (or replace) a pack's notepad. Nothing is shown: Mush-Z appends every
        channel message to one, and only ActivateNotepad puts a notepad in front."""
        name = self._notepad_title(title)
        combined = text if replace else self._notepads.get(name, "") + text
        self._notepads[name] = combined[-_NOTEPAD_LIMIT:]
        return True

    def notepads(self) -> dict[str, str]:
        """This pack's notepads by title, for the window's list of them (a copy)."""
        return dict(self._notepads)

    def _activate_notepad(self, title: object = "", *_args: object) -> bool:
        name = self._notepad_title(title)
        if name not in self._notepads:
            return False
        self._window().show_notepad(name, self._notepads[name])
        return True

    def _close_notepad(self, title: object = "", *_args: object) -> int:
        return 1 if self._notepads.pop(self._notepad_title(title), None) is not None else 0

    def _notepad_exists(self, title: object = "", *_args: object) -> int:
        """Read-only, save method, colour, font and window position: genericMud's notepad
        window has none of these to set, so they answer whether the notepad exists."""
        return 1 if self._notepad_title(title) in self._notepads else 0

    # --- arrays: MUSHclient's named string maps, kept in key order like its std::map ---

    def _array_create(self, name: object = "", *_args: object) -> int:
        if str(name) in self._arrays:
            return E_ARRAY_ALREADY_EXISTS
        self._arrays[str(name)] = {}
        return E_OK

    def _array_set(
        self, name: object = "", key: object = "", value: object = "", *_args: object
    ) -> int:
        array = self._arrays.get(str(name))
        if array is None:
            return E_ARRAY_DOES_NOT_EXIST
        replacing = str(key) in array
        array[str(key)] = _dialog_text(value)
        return E_SET_REPLACING_EXISTING_VALUE if replacing else E_OK

    def _array_get(self, name: object = "", key: object = "", *_args: object) -> str | None:
        return self._arrays.get(str(name), {}).get(str(key))  # nil when absent (MUSHclient)

    def _array_delete(self, name: object = "", *_args: object) -> int:
        if self._arrays.pop(str(name), None) is None:
            return E_ARRAY_DOES_NOT_EXIST
        return E_OK

    def _array_clear(self, name: object = "", *_args: object) -> int:
        array = self._arrays.get(str(name))
        if array is None:
            return E_ARRAY_DOES_NOT_EXIST
        array.clear()
        return E_OK

    def _array_delete_key(self, name: object = "", key: object = "", *_args: object) -> int:
        array = self._arrays.get(str(name))
        if array is None:
            return E_ARRAY_DOES_NOT_EXIST
        if array.pop(str(key), None) is None:
            return E_KEY_DOES_NOT_EXIST
        return E_OK

    def _lua_list(self, items: list[str]):
        """A list as a Lua array, or nil when it's empty (MUSHclient's empty variant)."""
        return self._lua.table_from(items) if items else None

    def _array_listing(self, name: object, *, keys: bool):
        array = self._arrays.get(str(name))
        if array is None:
            return None
        ordered = sorted(array)
        return self._lua_list(ordered if keys else [array[key] for key in ordered])

    def _array_end_key(self, name: object, *, last: bool) -> str | None:
        array = self._arrays.get(str(name))
        if not array:
            return None
        ordered = sorted(array)
        return ordered[-1] if last else ordered[0]

    def _array_import(
        self, name: object = "", values: object = "", delimiter: object = "", *_args: object
    ) -> int:
        """ArrayImport: key, value pairs split on a one-character delimiter, which a
        backslash escapes inside a key or value (and \\\\ is a backslash)."""
        separator = str(delimiter or "")
        if len(separator) != 1 or separator == "\\":
            return E_BAD_DELIMITER
        array = self._arrays.get(str(name))
        if array is None:
            return E_ARRAY_DOES_NOT_EXIST
        items = _split_escaped(str(values or ""), separator)
        if len(items) % 2:
            return E_ARRAY_NOT_EVEN_NUMBER_OF_VALUES
        duplicates = False
        for index in range(0, len(items), 2):
            duplicates = duplicates or items[index] in array
            array[items[index]] = items[index + 1]
        return E_IMPORTED_WITH_DUPLICATES if duplicates else E_OK

    def _array_export(self, name: object, delimiter: object, *, keys_only: bool):
        """ArrayExport(Keys): the array in key order, joined by the delimiter, with the
        delimiter and backslashes inside keys and values escaped by a backslash."""
        array = self._arrays.get(str(name))
        if array is None:
            return E_ARRAY_DOES_NOT_EXIST
        if not array:
            return ""
        separator = str(delimiter or "")
        if len(separator) != 1 or separator == "\\":
            return E_BAD_DELIMITER

        def escape(text: str) -> str:
            return text.replace("\\", "\\\\").replace(separator, "\\" + separator)

        parts: list[str] = []
        for key in sorted(array):
            parts.append(escape(key))
            if not keys_only:
                parts.append(escape(array[key]))
        return separator.join(parts)

    def _get_option(self, name: object = "", *_args: object) -> object:
        return self._options.get(str(name), 0)

    def _get_unique_number(self, *_args: object) -> int:
        self._unique_number += 1
        return self._unique_number

    # --- timers ---

    def _add_timer_api(
        self,
        name: object = "",
        hours: object = 0,
        minutes: object = 0,
        seconds: object = 0,
        response: object = "",
        flags: object = 0,
        script_name: object = "",
        *_args: object,
    ) -> int:
        """MUSHclient ``AddTimer``: a timer repeats every interval unless it is OneShot.

        Toastush's one-second heartbeat (roundtime and stun countdowns, the roundtime sound)
        and wait.lua's one-shot resumes both come through here or through an XML <timer>.
        """
        flag_bits = int(_to_float(flags) or 0)
        if flag_bits & _TIMER_FLAGS["TimerNote"]:
            send_to = _SEND_TO_OUTPUT_CODE
        elif flag_bits & _TIMER_FLAGS["TimerSpeedWalk"]:
            send_to = _SEND_TO_SPEEDWALK_CODE
        else:
            send_to = _SEND_TO_WORLD_CODE
        return self._define_timer(
            {
                "name": str(name or "").strip(),
                "hour": _to_float(hours) or 0.0,
                "minute": _to_float(minutes) or 0.0,
                "second": _to_float(seconds) or 0.0,
                "text": str(response or ""),
                "send_to": send_to,
                "script": str(script_name or "").strip(),
                "enabled": bool(flag_bits & _TIMER_FLAGS["Enabled"]),
                "one_shot": bool(flag_bits & _TIMER_FLAGS["OneShot"]),
                "at_time": bool(flag_bits & _TIMER_FLAGS["AtTime"]),
                "active_closed": bool(flag_bits & _TIMER_FLAGS["ActiveWhenClosed"]),
                "temporary": bool(flag_bits & _TIMER_FLAGS["Temporary"]),
            },
            replace=bool(flag_bits & _TIMER_FLAGS["Replace"]),
        )

    def _register_timer(self, element: ET.Element, *, defaults: bool = False) -> int:
        """An XML <timer> from a world or plugin file, or from ImportXML, defined like AddTimer."""
        attrs = element.attrib
        send_element = element.find("send")
        return self._define_timer(
            {
                "name": attrs.get("name", "").strip(),
                "hour": _to_float(attrs.get("hour")) or 0.0,
                "minute": _to_float(attrs.get("minute")) or 0.0,
                "second": _to_float(attrs.get("second")) or 0.0,
                "text": (send_element.text or "") if send_element is not None else "",
                "send_to": int(_to_float(attrs.get("send_to")) or 0),
                "script": attrs.get("script", "").strip(),
                "variable": attrs.get("variable", "").strip(),
                "group": attrs.get("group", "").strip(),
                "enabled": _xml_flag(attrs, "enabled", defaults),
                "one_shot": _xml_flag(attrs, "one_shot"),
                "at_time": _xml_flag(attrs, "at_time"),
                "active_closed": _xml_flag(attrs, "active_closed"),
                "temporary": _xml_flag(attrs, "temporary"),
                "omit_from_output": _xml_flag(attrs, "omit_from_output"),
                "omit_from_log": _xml_flag(attrs, "omit_from_log"),
                "user": int(_to_float(attrs.get("user")) or 0),
            },
            replace=True,
        )

    def _define_timer(self, options: dict[str, object], *, replace: bool) -> int:
        """Add a timer under its lower-case label in the current plugin; MUSHclient's code."""
        name = str(options["name"])
        if options.get("at_time"):
            # A timer that fires at a time of day: one place in all the vault's packs uses
            # it. Firing it every N hours instead would be wrong, so it isn't scheduled.
            if self._api.diag is not None:
                self._api.diag.event(
                    "pack.timer", source=self._api.source or "?", name=name,
                    status="skipped", reason="time-of-day timers aren't supported",
                )
            return _TIMER_ERROR
        interval = (
            float(options["hour"]) * 3600 + float(options["minute"]) * 60
            + float(options["second"])
        )
        if interval <= 0:
            return E_TIME_INVALID
        label = name.lower()
        if not label or label.startswith("*"):
            name, label = "", f"*timer{self._get_unique_number()}"
        key = (self._current_plugin, label)
        previous = self._timers.get(key)
        if previous is not None and not replace:
            return E_TIMER_ALREADY_EXISTS
        state: dict[str, object] = {
            "variable": "", "group": "", "temporary": False, "omit_from_output": False,
            "omit_from_log": False, "user": 0, "active_closed": False,
            **options,
            "name": name,
            "label": label,
            "plugin": self._current_plugin,
            "interval": max(interval, _MIN_TIMER_INTERVAL),
            "generation": 0,
            "invocations": 0,
            "fired": 0,
            "last_fired": 0.0,
            "next_fire": 0.0,
        }
        if previous is not None:
            previous["generation"] += 1  # a replaced timer's pending fire sees it isn't current
        self._timers[key] = state
        if state["enabled"]:
            self._schedule_timer(key, state)
        return E_OK

    def _timer_is_current(self, key: tuple[str, str], state: dict, generation: int) -> bool:
        """Whether a scheduled fire still belongs to a live, enabled, unreset timer."""
        return (
            self._timers.get(key) is state
            and state["generation"] == generation
            and state["enabled"]
        )

    def _schedule_timer(self, key: tuple[str, str], state: dict) -> None:
        generation = state["generation"]
        state["next_fire"] = time.time() + state["interval"]

        def fire() -> None:
            if not self._timer_is_current(key, state, generation):
                return  # deleted, replaced, reset or disabled since this was scheduled
            if state["one_shot"]:
                del self._timers[key]  # before running, so the script can re-add the name
            if (state["active_closed"] or self._api.is_connected()) and self._plugin_enabled(
                str(state["plugin"])
            ):
                self._run_timer(state)
            if not state["one_shot"] and self._timer_is_current(key, state, generation):
                self._schedule_timer(key, state)

        self._api.add_timer(state["interval"], fire)

    def _run_timer(self, state: dict) -> None:
        state["fired"] += 1
        state["last_fired"] = time.time()
        previous = self._current_plugin
        self._current_plugin = str(state["plugin"])
        try:
            if state["text"]:
                self._deliver(
                    state["send_to"], state["text"], variable=state["variable"],
                    notepad=f"Timer: {state['name'] or state['label']}",
                )
            if not state["script"]:
                return
            handler = self._lua_function(str(state["script"]))
            if handler is None:
                return
            state["invocations"] += 1
            self._guard.run(handler, state["name"] or state["label"])
        finally:
            self._current_plugin = previous

    def _find_timer(self, name: object) -> tuple[tuple[str, str], dict] | tuple[None, None]:
        """The timer a plugin names: its own, as in MUSHclient."""
        label = str(name or "").strip().lower()
        if not label:
            return None, None
        key = (self._current_plugin, label)
        if key in self._timers:
            return key, self._timers[key]
        return None, None

    def _set_timer_enabled(self, key: tuple[str, str], state: dict, wanted: bool) -> None:
        if wanted != state["enabled"]:
            state["enabled"] = wanted
            state["generation"] += 1  # a pending fire from before the change no-ops
            if wanted:
                self._schedule_timer(key, state)

    def _enable_timer(self, name: object = "", enabled: object = True, *_args: object) -> int:
        key, state = self._find_timer(name)
        if state is None:
            return E_TIMER_NOT_FOUND
        self._set_timer_enabled(key, state, _lua_flag(enabled))
        return E_OK

    def _delete_timer(self, name: object = "", *_args: object) -> int:
        key, state = self._find_timer(name)
        if state is None:
            return E_TIMER_NOT_FOUND
        del self._timers[key]
        state["generation"] += 1
        return E_OK

    def _reset_timer(self, name: object = "", *_args: object) -> int:
        """Start the timer's interval over from now."""
        key, state = self._find_timer(name)
        if state is None:
            return E_TIMER_NOT_FOUND
        state["generation"] += 1
        if state["enabled"]:
            self._schedule_timer(key, state)
        return E_OK

    def _timers_in_group(self, group: object) -> list[tuple[tuple[str, str], dict]]:
        name = str(group or "")
        if not name:
            return []
        return [
            (key, state) for key, state in self._timers.items()
            if state["group"] == name and key[0] == self._current_plugin
        ]

    def _enable_timer_group(self, group: object = "", enabled: object = True, *_args) -> int:
        timers = self._timers_in_group(group)
        for key, state in timers:
            self._set_timer_enabled(key, state, _lua_flag(enabled))
        return len(timers)

    def _delete_timer_group(self, group: object = "", *_args: object) -> int:
        timers = self._timers_in_group(group)
        for key, state in timers:
            del self._timers[key]
            state["generation"] += 1
        return len(timers)

    def _delete_temporary_timers(self, *_args: object) -> int:
        timers = [
            (key, state) for key, state in self._timers.items()
            if state["temporary"] and key[0] == self._current_plugin
        ]
        for key, state in timers:
            del self._timers[key]
            state["generation"] += 1
        return len(timers)

    def _timer_list(self, *_args: object):
        labels = [label for plugin, label in self._timers if plugin == self._current_plugin]
        return self._lua.table_from(labels) if labels else None

    def _is_timer(self, name: object = "", *_args: object) -> int:
        _key, state = self._find_timer(name)
        return E_OK if state is not None else E_TIMER_NOT_FOUND

    def _timer_info(self, name: object = "", info_type: object = 0, *_args: object):
        """MUSHclient ``GetTimerInfo``; nil for a timer or number it doesn't have."""
        _key, state = self._find_timer(name)
        number = _to_float(info_type)
        if state is None or number is None:
            return None
        hour, minute, second = state["hour"], state["minute"], state["second"]
        values = {
            1: int(hour), 2: int(minute), 3: float(second), 4: state["text"],
            5: state["script"], 6: bool(state["enabled"]), 7: bool(state["one_shot"]),
            8: False, 9: state["invocations"], 10: state["fired"],
            11: state["last_fired"] or None, 12: state["next_fire"] or None,
            13: max(0.0, state["next_fire"] - time.time()) if state["enabled"] else 0.0,
            14: bool(state["temporary"]),
            15: state["send_to"] == _SEND_TO_SPEEDWALK_CODE,
            16: state["send_to"] == _SEND_TO_OUTPUT_CODE,
            17: bool(state["active_closed"]), 18: False, 19: state["group"],
            20: state["send_to"], 21: state["user"], 22: state["name"],
            23: bool(state["omit_from_output"]), 24: bool(state["omit_from_log"]),
            25: False,
            26: bool(state["script"]) and self._lua_function(str(state["script"])) is not None,
        }
        return values.get(int(number))

    def _timer_option(self, name: object = "", option: object = "", *_args: object):
        """MUSHclient ``GetTimerOption``: numbers and yes/no as numbers, text as text."""
        _key, state = self._find_timer(name)
        if state is None:
            return None
        option_name = str(option or "").strip().lower()
        if option_name in _TIMER_TEXT_OPTIONS:
            return str(state["text" if option_name == "send" else option_name])
        if option_name in _TIMER_NUMBER_OPTIONS or option_name in _TIMER_FLAG_OPTIONS:
            value = state.get(option_name, 0)
            return value if isinstance(value, float) and not value.is_integer() else int(value)
        return None

    def _set_timer_option(
        self, name: object = "", option: object = "", value: object = "", *_args: object
    ) -> int:
        """MUSHclient ``SetTimerOption``, with its checks and return codes."""
        key, state = self._find_timer(name)
        if state is None:
            return E_TIMER_NOT_FOUND
        option_name = str(option or "").strip().lower()
        text = option_number(value)
        if option_name in _TIMER_TEXT_OPTIONS:
            state["text" if option_name == "send" else option_name] = text
            return E_OK
        if option_name in _TIMER_FLAG_OPTIONS:
            if text in ("y", "Y", "n", "N"):
                text = "1" if text in ("y", "Y") else "0"
            if not re.fullmatch(r"[+-]?\d+", text.strip()):
                return E_OPTION_OUT_OF_RANGE
            wanted = int(text) != 0
            if option_name == "enabled":
                self._set_timer_enabled(key, state, wanted)
            elif option_name == "at_time":
                return E_OK if not wanted else E_PLUGIN_CANNOT_SET_OPTION
            else:
                state[option_name] = wanted
            return E_OK
        if option_name not in _TIMER_NUMBER_OPTIONS:
            return E_UNKNOWN_OPTION
        number = _to_float(text)
        low, high = _TIMER_NUMBER_OPTIONS[option_name]
        if number is None or not low <= number <= high:
            return E_OPTION_OUT_OF_RANGE
        value = number if option_name in ("second", "offset_second") else int(number)
        if option_name not in ("hour", "minute", "second"):
            state[option_name] = value
            return E_OK
        timing = {"hour": state["hour"], "minute": state["minute"], "second": state["second"]}
        timing[option_name] = value
        interval = timing["hour"] * 3600 + timing["minute"] * 60 + timing["second"]
        if interval <= 0:
            return E_TIME_INVALID
        state[option_name] = value
        state["interval"] = max(interval, _MIN_TIMER_INTERVAL)
        state["generation"] += 1
        if state["enabled"]:
            self._schedule_timer(key, state)
        return E_OK

    def _set_option(self, name: object = "", value: object = 0, *_args: object) -> int:
        self._options[str(name)] = value
        return _SCRIPT_ERROR_OK

    def _get_alpha_option(self, name: object = "", *_args: object) -> str:
        return self._alpha_options.get(str(name), "")

    def _set_alpha_option(self, name: object = "", value: object = "", *_args: object) -> int:
        self._alpha_options[str(name)] = str(value)
        return _SCRIPT_ERROR_OK

    def _get_alpha_option_list(self, *_args: object):
        return self._lua.table_from(sorted(self._alpha_options))

    def _get_global_option(self, name: object = "", *_args: object) -> object:
        defaults = {
            "ConfirmBeforeClosingMushclient": 1,
            "ConfirmBeforeSavingVariables": 0,
            "F1macro": 1,
            "OpenActivityWindow": 0,
            "SmoothScrolling": 0,
            "SmootherScrolling": 0,
        }
        return defaults.get(str(name), 0)

    def _get_global_option_list(self, *_args: object):
        return self._lua.table()

    def _install_sendpkt(self) -> None:
        """Bind ``SendPkt`` via a Lua-side byte-table trampoline.

        The packet is a pre-framed telnet sequence (IAC SB ... IAC SE) full of bytes
        that are invalid UTF-8, so the Lua string must never cross the lupa boundary
        directly -- the runtime's string conversion would raise. The trampoline
        explodes it into a table of byte values; Python reassembles and sends verbatim
        (no re-framing: SendPkt's contract is that the caller built the framing).
        """
        make_sendpkt = self._lua.eval(
            "function(deliver)\n"
            "  return function(data)\n"
            "    data = tostring(data or '')\n"
            "    local bytes = {}\n"
            "    for i = 1, #data do bytes[i] = string.byte(data, i) end\n"
            "    deliver(bytes)\n"
            "  end\n"
            "end"
        )
        sendpkt = make_sendpkt(self._deliver_packet)
        globals_ = self._lua.globals()
        globals_["SendPkt"] = sendpkt
        globals_.world["SendPkt"] = sendpkt  # packs also call through the world object

    def _report_error(self, error: Exception) -> None:
        """A fire-time hook/timer fault is contained by the guard; trace every one and speak the
        first, so a broken pack callback (a missing accessibility cue) isn't silently swallowed."""
        if self._api.diag is not None:
            self._api.diag.event(
                "script.error", source=self._api.source or "?",
                error=f"{type(error).__name__}: {error}",
            )
        if not self._script_error_spoken:
            self._script_error_spoken = True
            self._api.speak(
                f"A soundpack script error occurred: {type(error).__name__}", channel="system"
            )

    def _deliver_packet(self, table: object) -> None:
        data = bytes(bytearray(table[i] for i in range(1, len(table) + 1)))
        self._api.send_packet(data)

    def _play_sound(
        self,
        buffer: object = 0,
        file: object = "",
        loop: object = False,
        volume: object = 0,
        pan: object = 0,
        *_rest: object,
    ) -> int:
        """MUSHclient ``PlaySound(buffer, file, loop, volume, pan)``, by the rules in its source.

        Ten buffers play at once, one sound each. The volume is in decibels, 0 full and -100
        silent, and the pan is the decibels taken off the other side, -100 hard left and 100
        hard right; anything outside those plays at full and centred, as MUSHclient forces it.
        An empty file name re-sets the volume, pan and looping of the sound playing in that
        buffer. Buffer 0 takes a free buffer among the first five, or buffer 1. A file that
        isn't there leaves the buffer playing what it was, and answers eFileNotFound.
        """
        self._let_go_of_ended_buffers()  # as MUSHclient does first, every time
        number = int(_to_float(buffer) or 0)
        name = "" if file is None else str(file)
        gain = _decibel_gain(volume)
        balance = _decibel_pan(pan)
        looping = _lua_flag(loop, default=False)
        if not name and 1 <= number <= _SOUND_BUFFERS:
            if number not in self._held_buffers:
                return ERROR_CODES["eBadParameter"]  # nothing there to change
            channel = _buffer_channel(number)
            self._api.adjust(channel, gain=gain, pan=balance)
            self._api.set_looping(channel, looping)
            return E_OK
        if len(name) < 2:
            return ERROR_CODES["eBadParameter"]
        if number == 0:
            free = (
                each for each in range(1, _FREE_BUFFER_CHOICES + 1)
                if each not in self._held_buffers
            )
            number = next(free, 1)
        if not 1 <= number <= _SOUND_BUFFERS:
            return ERROR_CODES["eBadParameter"]
        target = self._sound_buffer_file(name)
        if not self._api.sound_exists(target):
            return ERROR_CODES["eFileNotFound"]
        self._held_buffers.add(number)
        self._api.play(
            target, channel=_buffer_channel(number), gain=gain, pan=balance, loop=looping
        )
        return E_OK

    def _sound_buffer_file(self, name: str) -> str:
        """Where PlaySound finds ``name``: a name that isn't a full path is in the sounds folder
        beside MUSHclient, as MUSHclient looks for it, or failing that wherever genericMud finds
        a pack's sounds."""
        if self._client_dir is None or is_absolute(name):
            return name
        beside = (self._client_dir / "sounds" / name.replace("\\", "/")).as_posix()
        return beside if self._api.sound_exists(beside) else name

    def _let_go_of_ended_buffers(self) -> None:
        """Free the buffers whose sounds have ended, as each PlaySound does before anything
        else: buffer 0 can take them again, and GetSoundStatus says they're free."""
        for number in list(self._held_buffers):
            if not self._api.is_playing(_buffer_channel(number)):
                self._held_buffers.discard(number)

    def _stop_sound(self, buffer: object = 0, *_rest: object) -> int:
        """MUSHclient ``StopSound(buffer)``: stop one of the ten buffers and free it, or all of
        them for 0. The stop is sent whether or not anything plays there, since genericMud's
        web page plays sounds where nothing here can tell whether they're still going."""
        number = int(_to_float(buffer) or 0)
        if number == 0:
            for each in range(1, _SOUND_BUFFERS + 1):
                self._api.stop(_buffer_channel(each))
            self._held_buffers.clear()
            return E_OK
        if not 1 <= number <= _SOUND_BUFFERS:
            return ERROR_CODES["eBadParameter"]
        self._api.stop(_buffer_channel(number))
        self._held_buffers.discard(number)
        return E_OK

    def _get_sound_status(self, buffer: object = 0, *_rest: object) -> int:
        """MUSHclient ``GetSoundStatus(buffer)``: -1 for no such buffer, -2 for a free one, 1
        while its sound plays (a looping one too, as MUSHclient answers), and 0 once the sound
        has ended but the buffer hasn't been freed yet."""
        number = int(_to_float(buffer) or 0)
        if not 1 <= number <= _SOUND_BUFFERS:
            return -1
        if number not in self._held_buffers:
            return -2
        return 1 if self._api.is_playing(_buffer_channel(number)) else 0

    def _sound(self, arg: object = "", *_rest: object) -> int:
        """MUSHclient ``Sound``: a plugin that plays sounds its own way gets it first (see
        _hand_sound_to_plugin). Otherwise a path plays it, and a ``key=value`` string is a
        control directive (``volume=``/``pan=``/``freq=``) for the current cue."""
        text = str(arg)
        if not text:
            return ERROR_CODES["eNoNameSpecified"]
        if self._hand_sound_to_plugin(text):
            return E_OK
        if "=" in text:
            self._sound_control(text)
        elif not self._api.play(text, channel=_SOUND_CHANNEL):
            return ERROR_CODES["eCannotPlaySound"]
        return E_OK

    def _hand_sound_to_plugin(self, name: str) -> bool:
        """MUSHclient's PlaySoundFile, for Sound() and triggers' sounds: the first enabled
        plugin with an OnPluginPlaySound plays ``name`` its own way, and nothing else does. The
        LuaAudio packs play every sound through audio.dll like this, their volume keys'
        Sound("volume=x") included. A sound played inside that hook isn't handed on again, so
        the hook can't call itself. Answers whether a plugin took it."""
        if self._in_sound_hook:
            return False
        for plugin_id, hooks in list(self._hooks.items()):
            if plugin_id == "world" or hooks.get("OnPluginPlaySound") is None:
                continue  # MUSHclient asks plugins only, never the world's script
            if not self._plugin_enabled(plugin_id):
                continue
            self._in_sound_hook = True
            try:
                self._call_hook(plugin_id, "OnPluginPlaySound", name)
            finally:
                self._in_sound_hook = False
            return True
        return False

    def _sound_control(self, directive: str) -> None:
        key, _, raw = directive.partition("=")
        key = key.strip().lower()
        try:
            level = float(raw)
        except ValueError:
            return
        if key == "pan":
            # Live pan on the playing cue -- directional cues are how a pack tells a blind
            # player WHERE something happened, so dropping this loses real information.
            self._api.adjust(_SOUND_CHANNEL, pan=max(-1.0, min(1.0, level / _PAN_MAX)))
        elif key != "volume":
            return  # freq: no live pitch control in the bus — accept, ignore
        elif level <= 0:
            self._api.stop(_SOUND_CHANNEL)  # "volume=0" is the soundpack idiom for stop
        else:
            # adjust() re-levels the PLAYING cue; set_volume would permanently drop the whole
            # sound category (every future cue) and wouldn't even change the running loop.
            self._api.adjust(_SOUND_CHANNEL, gain=level / _VOLUME_MAX)

    def _get_info(self, code: object = 0):
        """MUSHclient ``GetInfo``: folders, names and versions as text, and the yes/no,
        counter and time codes as Lua booleans and numbers, as MUSHclient answers them.

        A code genericMud has nothing for answers its kind's empty value ("" for text, false,
        0) rather than raising, and one MUSHclient doesn't have answers nil. Times are Unix
        seconds, which is what MUSHclient hands Lua for a date.
        """
        try:
            number = int(code)
        except (TypeError, ValueError):
            return ""
        if number >= 100:
            return self._get_info_value(number)

        client = self._client_dir
        world_dir = Path(self._world_dir) if self._world_dir else client

        def directory(path: Path | None) -> str:
            return path.as_posix().rstrip("/") + "/" if path is not None else ""

        if number == 1:  # current world address
            return self._world_address
        if number == 2:  # current world name
            return self._world_name
        if number == 54:  # current world file pathname
            return self._world_path.as_posix() if self._world_path is not None else ""
        if number == 56:  # MUSHclient application path, with trailing separator
            return directory(client)
        if number == 57:  # default world directory
            return directory(client / "worlds" if client is not None else world_dir)
        if number == 58:  # default log directory
            return directory(client / "logs" if client is not None else None)
        if number == 59:  # default script directory
            return directory(client / "scripts" if client is not None else None)
        if number == 60:  # default plugin directory
            return directory(client / "worlds" / "plugins" if client is not None else None)
        if number in (64, 66, 68):  # current, application, and startup directories
            return directory(client)
        if number == 67:  # directory containing the current world file
            return directory(world_dir)
        if number == 72:
            return _MUSHCLIENT_VERSION
        if number == 74:  # default sounds directory
            return directory(client / "sounds" if client is not None else None)
        if number == 82:  # preferences DB pathname; host-owned and never opened by packs
            return (client / "MUSHclient_prefs.sqlite").as_posix() if client is not None else ""
        return ""

    def _get_info_value(self, number: int):
        """GetInfo's yes/no (100s), counter (200s) and date (300s) codes."""
        connected = self._is_connected()
        flags = {
            106: not connected,  # not connected
            107: False,  # connecting
            113: True,  # this world's window is the active one (it has the player's keys)
            114: False,  # output paused
        }
        if number in flags:
            return flags[number]
        if number < 200:
            return False if number <= 125 else None
        if number == 232:  # a high-resolution timer, in seconds
            return time.perf_counter()
        if number < 300:
            return 0 if number <= 299 else None
        dates = {
            301: self._connected_at,  # when this connection was made (nil if never)
            304: time.time(),  # now
            305: _CLIENT_STARTED,  # when the client started
            306: self._opened_at,  # when this world was opened
        }
        if number in dates:
            value = dates[number]
            return int(value) if value is not None else None
        if number in (302, 303):  # last log flush, script file changed: nothing to say
            return None
        if number == 310:  # newlines received
            return 0
        return None

    def _accelerator(self, key: object = "", send: object = "", *_rest: object) -> int:
        """MUSHclient ``Accelerator(key, send)``: bind a hotkey that runs ``send`` as if
        typed. This is how a pack ships its keyboard UI -- Erion binds 54 of these
        (F1 hp report, F7 recall, the whole alt+arrows history browser) -- so a black
        hole here meant a pack loaded with zero working hotkeys (``pack.counts keys=0``).
        """
        return self._accelerator_to(key, send, _SEND_TO_EXECUTE_CODE)

    def _accelerator_to(
        self,
        key: object = "",
        send: object = "",
        destination: object = 0,
        *_rest: object,
    ) -> int:
        """Bind a key to Lua when ``sendto.script`` is requested, else typed input.

        Answers as MUSHclient's methods_accelerators.cpp does: eOK, eBadParameter for a key
        it can't read, eOptionOutOfRange for a send-to code that doesn't exist. Materia
        Magica's accelerators plugin reports every key it loads as failed unless it hears 0.
        Nothing to send takes the key's binding away, as in MUSHclient.
        """
        target = int(_to_float(destination) or 0)
        if not 0 <= target <= _SEND_TO_LAST_CODE:
            return ERROR_CODES["eOptionOutOfRange"]
        combo = _normalize_key_spec(key)
        if not combo:
            return ERROR_CODES["eBadParameter"]
        command = str(send or "")
        if not command:
            self._api.remove_key(combo)
            return _SCRIPT_ERROR_OK
        if target == int(_SEND_TO_SCRIPT):
            # Compiled when pressed, as MUSHclient finds the plugin by its id then, so after
            # a ReloadPlugin the key runs the plugin's new code in its new globals.
            plugin = self._current_plugin
            script = int(_SEND_TO_SCRIPT)

            def press(_ctx: MatchContext) -> None:
                if not self._plugin_gone(plugin):  # MUSHclient finds no plugin, does nothing
                    self._in_plugin(plugin, self._deliver, script, command)

            self._api.add_key(combo, press)
        else:
            self._api.add_key(combo, lambda _ctx, cmd=command: self._api.execute(cmd))
        return _SCRIPT_ERROR_OK

    # --- triggers and aliases ---

    def _next_rule_number(self) -> int:
        self._rule_counter += 1
        return self._rule_counter

    def _rule_from_xml(self, element: ET.Element, kind: str, *, defaults: bool) -> PackRule:
        """A <trigger> or <alias> read the way MUSHclient's world loader reads it.

        MUSHclient writes a yes/no attribute only when it's yes, so a missing one is no; with
        the defaults switch on (see _uses_defaults) a missing ``enabled`` is yes instead.
        """
        attrs = element.attrib

        def number(name: str, default: int, low: int, high: int) -> int:
            value = _to_float(attrs.get(name))
            if value is None:
                return default
            return max(low, min(high, int(value)))

        send_element = element.find("send")
        sound = attrs.get("sound", "").strip()
        rule = PackRule(
            kind=kind,
            name=attrs.get("name", "").strip(),
            plugin=self._current_plugin,
            match=attrs.get("match", ""),
            send=(send_element.text or "") if send_element is not None else "",
            send_to=number("send_to", _SEND_TO_WORLD_CODE, 0, _SEND_TO_LAST_CODE),
            script=attrs.get("script", "").strip(),
            sound="" if sound == _NO_SOUND else sound,
            group=attrs.get("group", "").strip(),
            variable=attrs.get("variable", "").strip(),
            enabled=_xml_flag(attrs, "enabled", defaults),
            regexp=_xml_flag(attrs, "regexp"),
            ignore_case=_xml_flag(attrs, "ignore_case"),
            keep_evaluating=_xml_flag(attrs, "keep_evaluating"),
            omit_from_output=_xml_flag(attrs, "omit_from_output"),
            omit_from_log=_xml_flag(attrs, "omit_from_log"),
            expand_variables=_xml_flag(attrs, "expand_variables"),
            lowercase_wildcard=_xml_flag(attrs, "lowercase_wildcard"),
            one_shot=_xml_flag(attrs, "one_shot"),
            temporary=_xml_flag(attrs, "temporary"),
            sequence=number("sequence", _DEFAULT_SEQUENCE, 0, _MAX_SEQUENCE),
            user=number("user", 0, -(2**31), 2**31 - 1),
        )
        for option in _KEPT_FLAGS[kind]:  # packs read these back; genericMud doesn't act on them
            if option in attrs:
                rule.extra[option] = int(_xml_flag(attrs, option))
        for option in _KEPT_NUMBERS[kind]:
            if option in attrs:
                rule.extra[option] = number(option, 0, -(2**31), 2**31 - 1)
        if rule.send_to == _SEND_TO_VARIABLE_CODE and not rule.variable:
            rule.variable = rule.name  # older MUSHclient files send to the rule's own name
        return rule

    def _compile_fixed_script(self, rule: PackRule) -> None:
        """Compile a rule's script text now when nothing in it changes from match to match,
        so a pack's broken rule is reported when the pack loads; raises if it won't compile."""
        if rule.send_to not in _SEND_TO_SCRIPT_CODES or not rule.send.strip():
            return
        text = fixup_escape_sequences(rule.send)
        if _FILLED_IN.search(text) or (rule.expand_variables and "@" in text):
            return
        rule.compiled = self._compile(text)
        rule.compiled_text = text

    def _add_rule(self, rule: PackRule, *, replace: bool, skip_repeats: bool) -> int | None:
        """Add a trigger or alias under its label; MUSHclient's return code.

        A pack names its rules in any case, and MUSHclient goes by the lower-case label. One
        it didn't name gets an internal label ("*trigger12") and, with ``skip_repeats``, is
        left out when the same unnamed rule already exists (answering None), as MUSHclient
        does when a file or ImportXML adds it twice. A bad pattern raises; the existing rule
        of that label stays until the new one has compiled.
        """
        if not rule.match:
            return E_TRIGGER_CANNOT_BE_EMPTY if rule.kind == TRIGGER else E_ALIAS_CANNOT_BE_EMPTY
        rules = self._rules[rule.kind]
        label = rule.name.lower()
        if not label or label.startswith("*"):
            rule.name = ""
            if skip_repeats:
                definition = rule.definition()
                for (plugin, key), existing in rules.items():
                    if (
                        plugin == rule.plugin and key.startswith("*")
                        and existing.definition() == definition
                    ):
                        return None
            label = f"*{rule.kind}{self._next_rule_number()}"
        rule.label = label
        existing = rules.get((rule.plugin, label))
        if existing is not None and not replace:
            return E_TRIGGER_ALREADY_EXISTS if rule.kind == TRIGGER else E_ALIAS_ALREADY_EXISTS
        self._register_rule(rule)
        if existing is not None:
            self._retire_rule(existing)
        rules[(rule.plugin, label)] = rule
        if rule.script and not self._loading_plugins.get(rule.plugin):
            # Added while playing: the plugin's functions exist now. One added while its plugin
            # loads is bound when the plugin's script has run (_bind_handlers), so a function
            # of the same name another plugin left in the shared script space isn't taken.
            rule.handler = self._lua_function(rule.script)
        return E_OK

    def _register_rule(self, rule: PackRule) -> None:
        """Give the rule an engine rule compiled from its match as it stands now."""
        expands = rule.kind == TRIGGER and rule.expand_variables and "@" in rule.match
        pattern = self._rule_pattern(rule, self._variable_lookup() if expands else None)
        engine_name = f"mush-{rule.kind}-{self._next_rule_number()}"
        callback = self._rule_callback(rule)
        if rule.kind == TRIGGER:
            # A trigger that doesn't keep evaluating stops only its own plugin's later triggers,
            # as in MUSHclient, so it can't silence other packs' triggers or the player's.
            engine_rule = self._api.add_trigger(
                pattern, callback, regex=True, priority=-rule.sequence, name=engine_name,
                keep_evaluating=rule.keep_evaluating,
                stop_scope=f"mushclient:{self._api.source}:{rule.plugin}", tiebreak=rule.match,
            )
        else:
            # The same for aliases, and any alias that matches takes the command, as in
            # MUSHclient: Mush-Z's cast alias keeps evaluating and sends the cast itself.
            engine_rule = self._api.add_alias(
                pattern, callback, regex=True, priority=-rule.sequence, name=engine_name,
                keep_evaluating=rule.keep_evaluating,
                stop_scope=f"mushclient:{self._api.source}:{rule.plugin}", consumes=True,
                tiebreak=rule.match,
            )
        rule.engine_rule, rule.engine_name, rule.pattern = engine_rule, engine_name, pattern
        self._sync_engine(rule)
        if rule.kind == TRIGGER and rule.expand_variables and "@" in rule.match:
            self._expanding.add(rule)
        else:
            self._expanding.discard(rule)

    def _rebuild_rule(self, rule: PackRule) -> None:
        """Compile the rule afresh after its match or sequence changed; raises on a bad one."""
        old_engine_rule, old_engine_name = rule.engine_rule, rule.engine_name
        self._register_rule(rule)
        if old_engine_rule is not None:
            old_engine_rule.enabled = False  # a line already being matched passes it by
            self._remove_engine_rule(rule.kind, old_engine_name)

    def _remove_engine_rule(self, kind: str, engine_name: str) -> None:
        if kind == TRIGGER:
            self._api.remove_trigger(engine_name)
        else:
            self._api.remove_alias(engine_name)

    def _retire_rule(self, rule: PackRule) -> None:
        """Delete a rule: it stops matching at once and its engine rule goes with it."""
        rule.deleted = True
        rule.enabled = False
        if rule.engine_rule is not None:
            rule.engine_rule.enabled = False  # a line already being matched passes it by
        self._expanding.discard(rule)
        rules = self._rules[rule.kind]
        if rules.get((rule.plugin, rule.label)) is rule:
            del rules[(rule.plugin, rule.label)]
        if rule.engine_rule is not None:
            self._remove_engine_rule(rule.kind, rule.engine_name)
            rule.engine_rule = None

    def _rule_pattern(self, rule: PackRule, variables) -> str:
        """The regular expression a rule matches with, made MUSHclient's way: a trigger that
        expands variables has them filled in, then a plain match becomes a whole-line one."""
        text = rule.match
        if rule.kind == TRIGGER and rule.expand_variables and "@" in text:
            text = fill_text(text, variables=variables, for_match=True, regexp=rule.regexp)
        pattern = text if rule.regexp else mushclient_pattern(text)
        return "(?i)" + pattern if rule.ignore_case else pattern

    def _variable_lookup(self):
        """Read a variable for @name, the name in any case, as MUSHclient's are."""
        values = self._api.variables()
        folded = {name.casefold(): value for name, value in values.items()}

        def lookup(name: str) -> str | None:
            value = values.get(name)
            return value if value is not None else folded.get(name.casefold())

        return lookup

    def _refresh_expanding(self) -> None:
        """Re-fill the @variables in triggers' matches before a line is matched, as MUSHclient
        does on every line, so a pack changing a variable changes what its triggers catch."""
        if not self._expanding:
            return
        version = self._api.vars_version
        if version == self._expanded_version:
            return  # no variable changed since the last line: the patterns stand
        self._expanded_version = version
        variables = self._variable_lookup()
        for rule in list(self._expanding):
            pattern = self._rule_pattern(rule, variables)
            if pattern == rule.pattern or rule.engine_rule is None:
                continue
            try:
                rule.engine_rule.pattern = compile_pattern(pattern, True)
            except Exception:  # noqa: BLE001 - a variable can make a bad pattern; keep the last
                continue
            rule.pattern = pattern

    def _rule_callback(self, rule: PackRule):
        """What a matching trigger or alias does, in MUSHclient's order: hide the line if it
        omits it, play its sound, do what its send_to says with its send text, then call its
        script function. Its plugin is the current one meanwhile, as in MUSHclient."""

        def fire(ctx: MatchContext) -> None:
            if rule.deleted or not rule.enabled or not self._plugin_enabled(rule.plugin):
                return
            rule.matched += 1
            rule.last_matched = time.time()
            rule.last_text = ctx.line.plain_text
            rule.last_wildcards = list(ctx.wildcards)
            if rule.kind == TRIGGER and rule.omit_from_output:
                ctx.line.gagged = True
                ctx.line.display_when_gagged = False
            if rule.kind == TRIGGER and rule.omit_from_log:
                ctx.line.omit_from_log = True
            if rule.one_shot:
                self._retire_rule(rule)  # first, so its script can add the name again
            previous = self._current_plugin
            self._current_plugin = rule.plugin
            try:
                if rule.sound and not self._hand_sound_to_plugin(rule.sound):
                    self._api.play(rule.sound)
                if rule.send:
                    self._send_rule_text(rule, ctx)
                if rule.script:
                    self._run_rule_script(rule, ctx)
            except Exception as error:  # noqa: BLE001 - a fault here mustn't cost the player
                self._report_error(error)  # the line it fired on, or the rest of the packet
            finally:
                self._current_plugin = previous

        return fire

    def _send_rule_text(self, rule: PackRule, ctx: MatchContext) -> None:
        to_script = rule.send_to in _SEND_TO_SCRIPT_CODES
        text = fixup_escape_sequences(rule.send)
        text = fill_text(
            text,
            variables=self._variable_lookup() if rule.expand_variables and "@" in text else None,
            wildcards=ctx.wildcards, named=ctx.named, label=rule.name or rule.label,
            to_script=to_script, lower_wildcards=rule.lowercase_wildcard,
        )
        if not to_script:
            self._deliver(
                rule.send_to, text, variable=rule.variable,
                notepad=f"{rule.kind.capitalize()}: {rule.name or rule.label}",
            )
            return
        if rule.kind == TRIGGER:
            # Scripts reprint lines in their colours from this (Mush-Z's channel triggers).
            env = self._env_for(rule.plugin)
            target = self._lua.globals() if env is None else env
            target.TriggerStyleRuns = self._style_table(ctx.line)
        if rule.compiled_text != text:
            # Compiled inside the guard, so a syntax error is reported, not raised into
            # whatever line happened to fire it. Kept while the text stays the same.
            rule.compiled = self._guard.run(self._compile, text)
            rule.compiled_text = text if rule.compiled is not None else None
        if rule.compiled is not None:
            self._guard.run(rule.compiled)

    def _run_rule_script(self, rule: PackRule, ctx: MatchContext) -> None:
        if rule.handler is None:
            rule.handler = self._lua_function(rule.script)
            if rule.handler is None:
                return
        rule.invocations += 1
        wildcards = self._wildcard_table(ctx)
        label = rule.name or rule.label
        if rule.kind == TRIGGER:
            self._guard.run(
                rule.handler, label, ctx.line.plain_text, wildcards, self._style_table(ctx.line)
            )
        else:
            self._guard.run(rule.handler, label, ctx.line.plain_text, wildcards)

    def _wildcard_table(self, ctx: MatchContext):
        """The wildcards a script gets: the whole match at 0, then each capture, and the named
        captures by name too."""
        table = self._lua.table()
        for index, value in enumerate(ctx.wildcards):
            table[index] = value
        for name, value in ctx.named.items():
            table[name] = value
        return table

    def _style_table(self, line):
        """The line's colour runs as MUSHclient's scripts get them, never empty."""
        runs = self._lua.table()
        for index, run in enumerate(style_runs(line.plain_text, line.spans), 1):
            runs[index] = self._lua.table_from(run)
        return runs

    def _lua_function(self, name: str, plugin: str | None = None):
        """The Lua function a rule or timer names, or None while there isn't one: the
        plugin's own (``plugin``, else the one running now), else a shared one.

        Dotted names reach into tables (wait.lua's "wait.timer_resume"). The first part is
        read raw, so a capitalised name that isn't defined doesn't come back as the
        do-nothing stand-in every unknown capitalised global reads as.
        """
        if not name:
            return None
        env = self._env_for(self._current_plugin if plugin is None else plugin)
        try:
            return self._find_function(name, env)
        except Exception:  # noqa: BLE001 - a table that errors when read has no function
            return None

    def _loaded_for(self, plugin_id: str):
        """A plugin's own package.loaded, for its modules built with module(); None for the
        world's, which is Lua's."""
        if not plugin_id or plugin_id == "world":
            return None
        loaded = self._plugin_loaded.get(plugin_id)
        if loaded is None:
            loaded = self._plugin_loaded[plugin_id] = self._lua.table()
        return loaded

    def _env_for(self, plugin_id: str):
        """The table a plugin's globals live in, or None for the world's, which are the Lua
        state's own. MUSHclient gives every plugin a Lua state of its own; here they share
        one, and each plugin gets a table of its own that reads through to the shared
        globals for host functions and anything else it doesn't define."""
        if not plugin_id or plugin_id == "world":
            return None
        env = self._plugin_envs.get(plugin_id)
        if env is None:
            env = self._plugin_envs[plugin_id] = self._new_env()
            package = self._package_for(self._loaded_for(plugin_id))
            if package is not None:
                env.package = package
        return env

    def _find_rule(self, kind: str, name: object) -> PackRule | None:
        """The rule a plugin names: its own, as in MUSHclient. Two plugins may well both have
        a "temp" trigger, and deleting or switching off yours mustn't touch the other's."""
        label = str(name or "").strip().lower()
        if not label:
            return None
        return self._rules[kind].get((self._current_plugin, label))

    def _rules_in_group(self, kind: str, group: object) -> list[PackRule]:
        """A group's rules in the current plugin, as in MUSHclient."""
        name = str(group or "")
        if not name:
            return []
        return [
            rule for rule in self._rules[kind].values()
            if rule.group == name and rule.plugin == self._current_plugin
        ]

    def _set_rule_enabled(self, rule: PackRule, enabled: bool) -> None:
        rule.enabled = enabled
        self._sync_engine(rule)

    def _sync_engine(self, rule: PackRule) -> None:
        """The engine matches a rule only while it's on, not deleted, and its plugin is on,
        so a switched-off alias or a disabled plugin's alias lets the command through. Never
        back on if the engine stopped it for running past its match budget."""
        engine_rule = rule.engine_rule
        if engine_rule is not None:
            engine_rule.enabled = (
                rule.enabled and not rule.deleted and self._plugin_enabled(rule.plugin)
                and not engine_rule.timed_out
            )

    def _not_found(self, kind: str) -> int:
        return E_TRIGGER_NOT_FOUND if kind == TRIGGER else E_ALIAS_NOT_FOUND

    def _add_trigger_api(
        self, name: object = "", match: object = "", response: object = "", flags: object = 0,
        colour: object = 0, wildcard: object = 0, sound_file: object = "",
        script_name: object = "", *_rest: object,
    ) -> int:
        """MUSHclient ``AddTrigger``: AddTriggerEx sending to the world at sequence 100."""
        return self._add_trigger_ex(
            name, match, response, flags, colour, wildcard, sound_file, script_name,
            _SEND_TO_WORLD_CODE, _DEFAULT_SEQUENCE,
        )

    def _add_trigger_ex(
        self, name: object = "", match: object = "", response: object = "", flags: object = 0,
        colour: object = 0, wildcard: object = 0, sound_file: object = "",
        script_name: object = "", send_to: object = 0, sequence: object = _DEFAULT_SEQUENCE,
        *_rest: object,
    ) -> int:
        """MUSHclient ``AddTriggerEx``: add a trigger at runtime.

        Erion's F-key report chain hangs off this: the hotkey alias sends "score hp" and adds
        a OneShot+Replace trigger to speak the reply. A name that's taken is replaced only
        with the Replace flag, as in MUSHclient.
        """
        bits = int(_to_float(flags) or 0)
        order = _to_float(sequence)
        rule = PackRule(
            kind=TRIGGER,
            name=str(name or "").strip(),
            plugin=self._current_plugin,
            match=str(match or ""),
            send=str(response or ""),
            send_to=int(_to_float(send_to) or 0),
            script=str(script_name or "").strip(),
            sound=str(sound_file or "").strip(),
            enabled=bool(bits & _TRIGGER_FLAGS["Enabled"]),
            omit_from_log=bool(bits & _TRIGGER_FLAGS["OmitFromLog"]),
            omit_from_output=bool(bits & _TRIGGER_FLAGS["OmitFromOutput"]),
            keep_evaluating=bool(bits & _TRIGGER_FLAGS["KeepEvaluating"]),
            ignore_case=bool(bits & _TRIGGER_FLAGS["IgnoreCase"]),
            regexp=bool(bits & _TRIGGER_FLAGS["RegularExpression"]),
            expand_variables=bool(bits & _TRIGGER_FLAGS["ExpandVariables"]),
            lowercase_wildcard=bool(bits & _TRIGGER_FLAGS["LowercaseWildcard"]),
            temporary=bool(bits & _TRIGGER_FLAGS["Temporary"]),
            one_shot=bool(bits & _TRIGGER_FLAGS["OneShot"]),
            sequence=int(order) if order is not None else _DEFAULT_SEQUENCE,
        )
        rule.extra["custom_colour"] = int(_to_float(colour) or 0)
        rule.extra["clipboard_arg"] = int(_to_float(wildcard) or 0)
        if rule.send_to == _SEND_TO_VARIABLE_CODE:
            rule.variable = rule.name  # MUSHclient's own "kludge": the variable is the name
        try:
            code = self._add_rule(
                rule, replace=bool(bits & _TRIGGER_FLAGS["Replace"]), skip_repeats=False
            )
        except Exception:  # noqa: BLE001 - the pattern didn't compile
            return E_BAD_REGULAR_EXPRESSION
        return E_OK if code is None else code

    def _add_alias_api(
        self, name: object = "", match: object = "", response: object = "", flags: object = 0,
        script_name: object = "", *_rest: object,
    ) -> int:
        """MUSHclient ``AddAlias`` (Toastush makes its aliases this way)."""
        bits = int(_to_float(flags) or 0)
        if bits & _ALIAS_FLAGS["AliasSpeedWalk"]:
            send_to = _SEND_TO_SPEEDWALK_CODE
        elif bits & _ALIAS_FLAGS["AliasQueue"]:
            send_to = _SEND_TO_QUEUE_CODE
        else:
            send_to = _SEND_TO_WORLD_CODE
        rule = PackRule(
            kind=ALIAS,
            name=str(name or "").strip(),
            plugin=self._current_plugin,
            match=str(match or ""),
            send=str(response or ""),
            send_to=send_to,
            script=str(script_name or "").strip(),
            enabled=bool(bits & _ALIAS_FLAGS["Enabled"]),
            keep_evaluating=bool(bits & _ALIAS_FLAGS["KeepEvaluating"]),
            ignore_case=bool(bits & _ALIAS_FLAGS["IgnoreAliasCase"]),
            omit_from_log=bool(bits & _ALIAS_FLAGS["OmitFromLogFile"]),
            regexp=bool(bits & _ALIAS_FLAGS["RegularExpression"]),
            expand_variables=bool(bits & _ALIAS_FLAGS["ExpandVariables"]),
            temporary=bool(bits & _ALIAS_FLAGS["Temporary"]),
            one_shot=bool(bits & _ALIAS_FLAGS["OneShot"]),
        )
        rule.extra["menu"] = int(bool(bits & _ALIAS_FLAGS["AliasMenu"]))
        try:
            code = self._add_rule(
                rule, replace=bool(bits & _ALIAS_FLAGS["Replace"]), skip_repeats=False
            )
        except Exception:  # noqa: BLE001 - the pattern didn't compile
            return E_BAD_REGULAR_EXPRESSION
        return E_OK if code is None else code

    def _delete_rule(self, kind: str, name: object) -> int:
        rule = self._find_rule(kind, name)
        if rule is None:
            return self._not_found(kind)
        self._retire_rule(rule)
        return E_OK

    def _delete_trigger(self, name: object = "", *_rest: object) -> int:
        return self._delete_rule(TRIGGER, name)

    def _delete_alias(self, name: object = "", *_rest: object) -> int:
        return self._delete_rule(ALIAS, name)

    def _enable_rule(self, kind: str, name: object, enabled: object) -> int:
        rule = self._find_rule(kind, name)
        if rule is None:
            return self._not_found(kind)
        self._set_rule_enabled(rule, _lua_flag(enabled))
        return E_OK

    def _enable_trigger(self, name: object = "", enabled: object = True, *_rest: object) -> int:
        return self._enable_rule(TRIGGER, name, enabled)

    def _enable_alias(self, name: object = "", enabled: object = True, *_rest: object) -> int:
        return self._enable_rule(ALIAS, name, enabled)

    def _enable_rule_group(self, kind: str, group: object, enabled: object) -> int:
        """Switch a group's rules on or off; how many there were, as MUSHclient answers."""
        rules = self._rules_in_group(kind, group)
        for rule in rules:
            self._set_rule_enabled(rule, _lua_flag(enabled))
        return len(rules)

    def _enable_trigger_group(
        self, group: object = "", enabled: object = True, *_rest: object
    ) -> int:
        return self._enable_rule_group(TRIGGER, group, enabled)

    def _enable_alias_group(
        self, group: object = "", enabled: object = True, *_rest: object
    ) -> int:
        return self._enable_rule_group(ALIAS, group, enabled)

    def _enable_group(self, group: object = "", enabled: object = True, *_rest: object) -> int:
        return (
            self._enable_rule_group(TRIGGER, group, enabled)
            + self._enable_rule_group(ALIAS, group, enabled)
            + self._enable_timer_group(group, enabled)
        )

    def _delete_rule_group(self, kind: str, group: object) -> int:
        rules = self._rules_in_group(kind, group)
        for rule in rules:
            self._retire_rule(rule)
        return len(rules)

    def _delete_group(self, group: object = "", *_rest: object) -> int:
        return (
            self._delete_rule_group(TRIGGER, group)
            + self._delete_rule_group(ALIAS, group)
            + self._delete_timer_group(group)
        )

    def _delete_temporary_rules(self, kind: str) -> int:
        rules = [
            rule for rule in self._rules[kind].values()
            if rule.temporary and rule.plugin == self._current_plugin
        ]
        for rule in rules:
            self._retire_rule(rule)
        return len(rules)

    def _rule_list(self, kind: str):
        """Get(Trigger|Alias)List: the current plugin's labels, nil when it has none."""
        labels = [label for plugin, label in self._rules[kind] if plugin == self._current_plugin]
        return self._lua.table_from(labels) if labels else None

    def _is_rule(self, kind: str, name: object) -> int:
        return E_OK if self._find_rule(kind, name) is not None else self._not_found(kind)

    def _rule_info(self, kind: str, name: object, info_type: object):
        rule = self._find_rule(kind, name)
        number = _to_float(info_type)
        if rule is None or number is None:
            return None
        script_valid = bool(rule.script) and (
            rule.handler is not None or self._lua_function(rule.script) is not None
        )
        return rule_info(rule, int(number), script_valid=script_valid)

    def _get_rule_option(self, kind: str, name: object, option: object):
        rule = self._find_rule(kind, name)
        if rule is None:
            return None
        return get_option(rule, str(option or ""))

    def _set_rule_option(self, kind: str, name: object, option: object, value: object) -> int:
        """Set(Trigger|Alias)Option, with MUSHclient's checks and return codes."""
        rule = self._find_rule(kind, name)
        if rule is None:
            return self._not_found(kind)
        option_name = str(option or "").strip().lower()
        code, parsed = parse_option(kind, option_name, value)
        if code != E_OK:
            return code
        if option_name in ("match", "sequence", "expand_variables"):
            if option_name == "match" and not parsed:
                return E_TRIGGER_CANNOT_BE_EMPTY if kind == TRIGGER else E_ALIAS_CANNOT_BE_EMPTY
            previous = getattr(rule, option_name)
            setattr(rule, option_name, parsed)
            try:
                self._rebuild_rule(rule)
            except Exception:  # noqa: BLE001 - the new pattern didn't compile; keep the old one
                setattr(rule, option_name, previous)
                return E_BAD_REGULAR_EXPRESSION
            return E_OK
        set_option(rule, option_name, parsed)
        if option_name == "enabled":
            self._set_rule_enabled(rule, bool(parsed))
        elif option_name == "keep_evaluating" and rule.engine_rule is not None:
            rule.engine_rule.keep_evaluating = bool(parsed)
        elif option_name == "script":
            rule.handler = self._lua_function(rule.script)
        elif option_name in ("send", "send_to"):
            rule.compiled, rule.compiled_text = None, None
        return E_OK

    def _import_xml(self, xml: object = "", *_rest: object) -> int:
        """MUSHclient ``ImportXML``: add the triggers, aliases, timers and variables in an XML
        fragment, replacing any of the same name. Mush-Z brings in its 857 sound triggers this
        way. Answers how many were added, or -1 if it isn't XML or doesn't parse (MUSHclient's
        methods_xml.cpp answers -1 for both)."""
        started = time.monotonic()
        try:
            return self._import_xml_text(str(xml or ""))
        finally:
            # Mush-Z imports 857 triggers in one call, which takes seconds on a slow machine:
            # the module that asked mustn't run out of script time because of it.
            self._host_time += time.monotonic() - started

    def _import_xml_text(self, text: str) -> int:
        start = text.lstrip()[:12].lower()
        if not start.startswith(_IMPORT_STARTS):
            return -1
        text = re.sub(r"<\?xml[^>]*\?>", "", text)
        if "<muclient" not in text.lower():
            # Wrap the fragment so it has one root, after any DOCTYPE, which must come first.
            doctype = _DOCTYPE_RE.search(text)
            split = doctype.end() if doctype is not None else 0
            text = f"{text[:split]}<muclient>{text[split:]}</muclient>"
        try:
            root = ET.fromstring(_sanitize_attr_markup(text))
        except ET.ParseError as error:
            # MUSHclient shows the parse error in a window; here it goes with the pack's other
            # load problems, named by the file that called ImportXML.
            where = self._current_document.name if self._current_document else "ImportXML"
            self._rule_errors.append((f"ImportXML in {where}", f"ParseError: {error}"))
            return -1
        count = 0
        for kind, group_tag in ((TRIGGER, "triggers"), (ALIAS, "aliases")):
            for container in root.iter(group_tag):
                for element in container.iter(kind):
                    defaults = _xml_flag(
                        element.attrib, "defaults", _xml_flag(container.attrib, "defaults")
                    )
                    try:
                        rule = self._rule_from_xml(element, kind, defaults=defaults)
                        self._compile_fixed_script(rule)
                        code = self._add_rule(rule, replace=True, skip_repeats=True)
                    except Exception as exc:  # noqa: BLE001 - one bad rule is not the import
                        label = element.get("name") or element.get("match") or "(unnamed)"
                        self._rule_errors.append((label, f"{type(exc).__name__}: {exc}"))
                        continue
                    if code == E_OK:
                        count += 1
        for container in root.iter("timers"):
            for element in container.iter("timer"):
                defaults = _xml_flag(
                    element.attrib, "defaults", _xml_flag(container.attrib, "defaults")
                )
                if self._register_timer(element, defaults=defaults) == E_OK:
                    count += 1
        for container in root.iter("variables"):
            for element in container.iter("variable"):
                name = element.get("name", "").strip()
                if name:
                    self._api.set_var(name, element.text or "")
                    count += 1
        return count

    def _call_plugin(self, plugin_id: object = "", func: object = "", *args: object):
        """MUSHclient ``CallPlugin``: run a function of another plugin, as that plugin.

        Answers eOK and then whatever the function returned (Mush-Z's PluginFunction reads
        ``rc, a = CallPlugin(...)``), or an error code and why. The function is the one the
        plugin exposed through ppi, one of its OnPlugin hooks, or a global of that name.
        Erion only CallPlugins its cosmetic messages-window plugin, which may not be loaded.
        """
        target = str(plugin_id or "")
        routine = str(func or "")
        if not routine:
            return E_NO_SUCH_ROUTINE, "No function name supplied"
        info = self._plugin_info.get(target)
        if info is None:
            return E_NO_SUCH_PLUGIN, f"Plugin ID ({target}) is not installed"
        name = str(info.get("name", ""))
        if not info.get("enabled", True):
            return E_PLUGIN_DISABLED, f"Plugin '{name}' ({target}) disabled"
        fn = self._plugin_routine(target, routine)
        if fn is None:
            return E_NO_SUCH_ROUTINE, f"No function '{routine}' in plugin '{name}' ({target})"
        previous = self._current_plugin
        previous_caller = info.get("calling", "")
        info["calling"] = previous if previous in self._plugin_info else ""
        self._current_plugin = target
        try:
            results = self._guard.run_strict(fn, *args)
        except Exception as error:  # noqa: BLE001 - reported, and the caller gets the reason
            self._report_error(error)
            return E_ERROR_CALLING_PLUGIN_ROUTINE, f"{type(error).__name__}: {error}"
        finally:
            self._current_plugin = previous
            info["calling"] = previous_caller
        if isinstance(results, tuple):
            return (E_OK, *results)
        return E_OK, results

    def _do_after_special(
        self, seconds: object = 0, text: object = "", _sendto: object = _SEND_TO_SCRIPT,
        *_rest: object,
    ) -> int:
        """MUSHclient ``DoAfterSpecial``, simplified on purpose: the text always runs as Lua.

        That's the soundpack-standard send-to-script case (see the module docstring), and
        plugins written against genericMud lean on it: the Erion gathering plugin passes
        sendto 1 and expects its Send(...) to run.
        """
        return self._do_after_as(seconds, text, int(_SEND_TO_SCRIPT))

    def _do_after(self, seconds: object = 0, text: object = "", *_rest: object) -> int:
        """MUSHclient ``DoAfter``: send ``text`` to the MUD after ``seconds``."""
        return self._do_after_as(seconds, text, _SEND_TO_WORLD_CODE)

    def _do_after_note(self, seconds: object = 0, text: object = "", *_rest: object) -> int:
        return self._do_after_as(seconds, text, _SEND_TO_OUTPUT_CODE)

    def _do_after_speedwalk(self, seconds: object = 0, text: object = "", *_rest: object) -> int:
        return self._do_after_as(seconds, text, _SEND_TO_SPEEDWALK_CODE)

    def _do_after_as(self, seconds: object, text: object, send_to: int) -> int:
        delay = _to_float(seconds)
        if delay is None or delay < 0:
            return E_TIME_INVALID
        content = str(text or "")
        plugin = self._current_plugin

        def fire() -> None:
            if self._plugin_gone(plugin):
                return  # its plugin was unloaded, which in MUSHclient takes its timers too
            # Compiled when it fires, inside the guard (see _deliver), so a syntax error in
            # the deferred text is reported then instead of raising into the script that
            # queued it.
            self._in_plugin(plugin, self._deliver, send_to, content)

        self._api.add_timer(delay, fire)
        return E_OK

    def _get_world(self, name: object = "", *_args: object):
        """MUSHclient ``GetWorld``: this world's object by its name, nil for any other.
        genericMud's sessions can't reach each other, and in MUSHclient nil means that world
        isn't open: Materia Magica's GMCP handler checks for it before it sends the room to
        a mapping world, and sent the do-nothing stand-in's text to the MUD instead."""
        wanted = str(name or "").casefold()
        if not wanted or wanted != self._world_name.casefold():
            return None
        return self._this_world()

    def _get_world_by_id(self, world_id: object = "", *_args: object):
        return self._this_world() if str(world_id or "") == self._world_id else None

    def _this_world(self):
        """This world as an object, its functions called with a colon (w:Note("hi"))."""
        if self._world_object is None:
            self._world_object = self._lua.eval(
                "function(world)\n"
                "  return setmetatable({}, {__index = function(_, name)\n"
                "    local value = world[name]\n"
                "    if type(value) ~= 'function' then return value end\n"
                "    return function(_, ...) return value(...) end\n"
                "  end})\n"
                "end"
            )(self._lua.globals().world)
        return self._world_object

    def _plugin_gone(self, plugin: str) -> bool:
        """Whether the plugin that set something up has since been unloaded."""
        return plugin != "world" and plugin not in self._plugin_info

    def _in_plugin(self, plugin: str, action, *args: object) -> None:
        """Run ``action`` as the plugin that set it up, the way MUSHclient runs a plugin's
        timers and hotkeys: GetPluginID, GetTriggerList and the like then answer for it."""
        previous = self._current_plugin
        self._current_plugin = plugin
        try:
            action(*args)
        finally:
            self._current_plugin = previous

    def _compile(self, code: str):
        """Host-side compile of a Lua chunk into a zero-arg callable that runs in the
        current plugin's globals."""
        chunk = self._lua.eval(f"function()\n{code}\nend")
        env = self._env_for(self._current_plugin)
        if env is not None:
            self._setfenv(chunk, env)
        return chunk

    # --- loading ---

    def load_file(self, path: str) -> None:
        # The world file's directory anchors GetInfo() sound paths: sounds sit beside the
        # world (often nested below the pack root that require/ resolves against).
        entry = Path(path).resolve()
        self._world_path = entry
        self._world_dir = entry.parent.as_posix()
        self._client_dir = Path(self._base_dir).resolve() if self._base_dir else entry.parent
        for parent in entry.parents:
            if parent.name.casefold() == "worlds":
                self._client_dir = parent.parent
                break
        self._load_path(entry)
        # MUSHclient raises this after its initial plugin list settles. Dependency-manager
        # plugins use it to load their declared requirements (Miriani-Next); optional plugins
        # are deliberately not loaded and are recorded below.
        self.dispatch("OnPluginListChanged")
        self._record_optional_plugins()

    def load_source(self, xml: str) -> None:
        # Strip only the XML declaration -- ElementTree rejects an encoding decl on a str.
        # Keep the DOCTYPE: MUSHclient plugins declare config entities in its internal
        # subset (<!ENTITY foo "...">) and reference them as &foo;, which ET expands. (An
        # earlier strip of the whole DOCTYPE corrupted that subset -> ParseError.)
        xml = re.sub(r"<\?xml[^>]*\?>", "", xml)
        xml = _sanitize_attr_markup(xml)  # MUSHclient regex attrs carry raw < (named groups)
        self._load_plugin(ET.fromstring(xml))

    def _load_path(self, path: Path) -> None:
        """Load one XML/Lua include while retaining its directory for relative children."""
        previous = self._current_document
        self._current_document = path
        try:
            text = path.read_text(encoding="latin-1", errors="ignore")
            # MUSHclient's `constants.lua` is often an XML <script> wrapper despite its
            # suffix. Sniff the content rather than trusting the extension.
            if text.lstrip().startswith("<"):
                self.load_source(text)
            else:
                self._guard.run_strict(self._execute_lua, text)
        finally:
            self._current_document = previous

    def _execute_lua(self, source: str) -> object:
        """Run a script file or block in the current plugin's globals."""
        name = "@" + self._current_document.as_posix() if self._current_document else "mushclient"
        chunk = self._lua.compile(source, name=name)
        env = self._env_for(self._current_plugin)
        if env is not None:
            self._setfenv(chunk, env)
        return chunk()

    def _index_pack_files(self) -> dict[str, list[Path]]:
        if self._file_index is not None:
            return self._file_index
        index: dict[str, list[Path]] = {}
        if self._base_dir:
            base = Path(self._base_dir).resolve()
            for path in sorted(base.rglob("*")):
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved.is_file() and resolved.is_relative_to(base):
                    index.setdefault(resolved.name.casefold(), []).append(resolved)
        self._file_index = index
        return index

    def _resolve_pack_file(self, filename: str) -> Path | None:
        """Resolve Windows-style, case-insensitive pack paths without escaping the pack."""
        if not self._base_dir or not filename:
            return None
        base = Path(self._base_dir).resolve()
        normalized = filename.replace("\\", "/")
        normalized = re.sub(r"^[A-Za-z]:", "", normalized)
        requested = Path(normalized)
        direct: list[Path] = []
        if requested.is_absolute():
            direct.append(requested)
        if self._current_document is not None:
            direct.append(self._current_document.parent / normalized)
        direct.append(base / normalized.lstrip("/"))
        for candidate in direct:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved.is_relative_to(base):
                return resolved

        basename = Path(normalized).name.casefold()
        matches = self._index_pack_files().get(basename, [])
        wanted = tuple(part.casefold() for part in Path(normalized).parts if part not in ("/", "."))

        def rank(path: Path) -> tuple[int, str]:
            rel = tuple(part.casefold() for part in path.relative_to(base).parts)
            suffix_match = bool(wanted) and rel[-len(wanted):] == wanted
            return (0 if suffix_match else 1, path.as_posix())

        return min(matches, key=rank) if matches else None

    def _resolve_pack_directory(self, value: str) -> Path | None:
        """Resolve a possibly-not-yet-created directory while confining it to the pack."""
        if not self._base_dir or not value:
            return None
        base = Path(self._base_dir).resolve()
        normalized = re.sub(r"^[A-Za-z]:", "", value.replace("\\", "/"))
        requested = Path(normalized)
        candidate = requested if requested.is_absolute() else (self._client_dir or base) / requested
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        return resolved if resolved.is_relative_to(base) else None

    def _plugin_metadata(self, path: Path) -> tuple[str, str, str]:
        """Return (id, name, language) from a plugin XML file, or empty values."""
        try:
            xml = path.read_text(encoding="latin-1", errors="ignore")
            xml = re.sub(r"<\?xml[^>]*\?>", "", xml)
            root = ET.fromstring(_sanitize_attr_markup(xml))
        except (OSError, ET.ParseError):
            return "", "", ""
        plugin = next(root.iter("plugin"), None)
        if plugin is None:
            return "", "", ""
        return (
            plugin.get("id", ""),
            plugin.get("name", "") or path.stem,
            plugin.get("language", ""),
        )

    def _find_plugin_by_id(self, plugin_id: str) -> Path | None:
        if self._plugin_file_by_id is None:
            index: dict[str, Path] = {}
            seen: set[Path] = set()
            for paths in self._index_pack_files().values():
                for path in paths:
                    if path in seen or path.suffix.casefold() != ".xml":
                        continue
                    seen.add(path)
                    candidate_id, _name, _language = self._plugin_metadata(path)
                    if candidate_id:
                        index.setdefault(candidate_id, path)
            self._plugin_file_by_id = index
        return self._plugin_file_by_id.get(plugin_id)

    def _plugin_skip_reason(self, path: Path) -> str | None:
        replacement = _HOST_PLUGIN_REPLACEMENTS.get(path.name.casefold())
        if replacement:
            return replacement
        if path.suffix.lower() in _NON_LUA_SCRIPT_SUFFIXES:
            return f"requires unsupported {path.suffix[1:].upper()} scripting"
        _plugin_id, _name, language = self._plugin_metadata(path)
        if language and language.casefold() != "lua":
            return f"requires unsupported {language} scripting"
        return None

    def _mark_virtual_plugin(self, path: Path) -> None:
        plugin_id, name, _language = self._plugin_metadata(path)
        if plugin_id:
            self._plugin_info[plugin_id] = {
                "name": name,
                "path": path,
                "enabled": True,
                "virtual": True,  # genericMud does its job; nothing of it is loaded
            }
        if path.name.casefold() in _SPEECH_PLUGINS:
            self._install_speech_commands()
        if path.name.casefold() == _UPDATER_PLUGIN:
            self._install_update_commands(plugin_id or "")

    def _install_update_commands(self, plugin_id: str) -> None:
        """Answer the pack's updater plugin's commands with genericMud's updater."""
        if self._updater_id is not None:
            return
        self._updater_id = plugin_id
        # A stand-in, like the speech commands: an alias of the pack's own takes it first.
        self._api.add_alias(
            r"^update(?: (.*))?$", self._update_command, regex=True,
            priority=_SPEECH_COMMAND_PRIORITY, fallback=True,
        )
        if plugin_id == _MIRIANI_NEXT_UPDATER:
            # Its OnPluginInstall binds this, and the pack's setup tells players about it.
            self._api.add_key(_normalize_key_spec("ctrl+u"), lambda _ctx: self._api.update_packs())
        elif plugin_id == _TOASTUSH_UPDATER:
            # Its maintainers' "index <file>" makes the index they publish; MUSHclient caught
            # it, so it never went to the MUD.
            self._api.add_alias(
                r"(?i)^index (.+?)$", lambda _ctx: self._note(_UPDATE_INDEX), regex=True,
                priority=_SPEECH_COMMAND_PRIORITY, fallback=True,
            )

    def _update_command(self, ctx: MatchContext) -> None:
        words = set((ctx.wildcards[1] if len(ctx.wildcards) > 1 else "").split())
        if words & _UPDATE_HELP_WORDS:
            self._note(_UPDATE_HELP)
        elif words & _UPDATE_CHECK_WORDS:
            self._api.check_pack_updates()
        elif words <= _UPDATE_QUIET_WORDS | _UPDATE_INSTALL_WORDS:
            self._api.update_packs(quiet=bool(words & _UPDATE_QUIET_WORDS))
        else:
            self._note(_UPDATE_ELSEWHERE)

    def _install_speech_commands(self) -> None:
        """Answer MushReader's and channel_history's commands with genericMud's own."""
        if self._speech_commands_installed:
            return
        self._speech_commands_installed = True
        # They stand in for plugins the pack didn't bring, so an alias of the pack's own that
        # takes the command, in whichever plugin, answers it instead.
        for pattern, interrupt, speaks in _SPEECH_COMMANDS:
            self._api.add_alias(
                pattern, self._speech_command(interrupt, speaks), regex=True,
                priority=_SPEECH_COMMAND_PRIORITY, fallback=True,
            )
        answers = (
            # Miriani-Next's own MushReader: its Do Not Disturb keeps incoming lines quiet.
            (r"^tts_disable_silent$", lambda _ctx: self._api.set_line_speech(False)),
            (r"^tts_enable_silent$", lambda _ctx: self._api.set_line_speech(True)),
            # channel_history.xml: file a line under a channel for the review keys. Its alias
            # is "history_add *=*", so the channel is everything before the first "=".
            (
                r"^history_add ([^=]*)=(.*)$",
                lambda ctx: self._api.file_in_history(ctx.wildcards[1], ctx.wildcards[2]),
            ),
        )
        for pattern, callback in answers:
            self._api.add_alias(
                pattern, callback, regex=True, priority=_SPEECH_COMMAND_PRIORITY, fallback=True
            )

    def _speech_command(self, interrupt: bool, speaks: bool):
        def run(ctx: MatchContext) -> None:
            if interrupt:
                self._api.stop_speech()
            text = ctx.wildcards[1] if len(ctx.wildcards) > 1 else ""
            if speaks and text.strip():
                self._api.speak(text, channel="pack")  # where nvda.say goes too

        return run

    def _skip_plugin(self, name: str, reason: str) -> None:
        item = (name, reason)
        if item not in self._skipped_plugins:
            self._skipped_plugins.append(item)

    def _record_optional_plugins(self) -> None:
        # Miriani-Next's requirements.xml plugin sets this in its own globals.
        table = None
        for env in (*self._plugin_envs.values(), self._lua.globals()):
            table = self._rawget(env, "optional_plugins")
            if table is not None:
                break
        if table is None:
            return
        try:
            names = [str(name) for _plugin_id, name in table.items()]
        except (AttributeError, TypeError):
            return
        loaded_names = {str(info["name"]).casefold() for info in self._plugin_info.values()}
        for name in sorted(names, key=str.casefold):
            if name.casefold() not in loaded_names:
                self._skip_plugin(name + ".xml", "declared optional by the pack")

    def _load_plugin(self, root: ET.Element) -> None:
        """Run one plugin/world's script + triggers; a world (<include>s) pulls in its
        plugins so they share this runtime and can ppi-message each other."""
        plugin = next(root.iter("plugin"), None)
        previous = self._current_plugin
        if plugin is not None:
            plugin_id = plugin.get("id") or "world"
        else:
            # A plain <include> adds to whatever included it, a plugin's included rules to
            # that plugin; only the world file itself is the world.
            plugin_id = self._current_plugin if self._plain_include_depth > 0 else "world"
        self._current_plugin = plugin_id
        self._loading_plugins[plugin_id] = self._loading_plugins.get(plugin_id, 0) + 1
        if plugin is not None and plugin_id != "world":
            plugin_name = plugin.get("name", "")
            if not plugin_name and self._current_document is not None:
                plugin_name = self._current_document.stem
            self._plugin_info[plugin_id] = {
                "name": plugin_name,
                "path": self._current_document,
                "enabled": True,
                # MUSHclient saves only these plugins' state when the world closes, and
                # calls their OnPluginSaveState to do it.
                "save_state": _xml_flag(plugin.attrib, "save_state"),
                **_plugin_details(plugin),
            }
        try:
            # Non-plugin includes are Lua/constants fragments and must exist before the
            # containing script executes. Plugin includes load afterward as independent
            # plugin units, matching MUSHclient's dependency order.
            for include in root.iter("include"):
                name = include.get("name")
                if name and include.get("plugin", "n").lower() != "y":
                    self._plain_include_depth += 1
                    try:
                        self._load_included(name)
                    except Exception as exc:  # noqa: BLE001 - account for one bad fragment
                        self._include_errors.append((name, f"{type(exc).__name__}: {exc}"))
                    finally:
                        self._plain_include_depth -= 1
            world = next(root.iter("world"), None)
            if world is not None:
                self._world_id = world.get("id") or self._world_id
                self._world_name = world.get("name", self._world_name)
                self._world_address = world.get("site", self._world_address)
                try:
                    self._world_port = int(world.get("port", self._world_port))
                except (TypeError, ValueError):
                    pass
                for event in _WORLD_EVENTS:
                    handler = world.get(f"on_world_{event}", "").strip()
                    if handler:
                        self._world_events[event] = handler
            script_filename = world.get("script_filename", "") if world is not None else ""
            # The file's own variables are its starting values (Erion's MSDP plugin ships
            # thirteen); ones the player's saved session already restored stay as they are.
            for container in root.iter("variables"):
                for element in container.iter("variable"):
                    name = element.get("name", "").strip()
                    if name and self._api.get_var(name, None) is None:
                        self._api.set_var(name, element.text or "")
            # The file's rules exist before its script runs, as in MUSHclient: a script that
            # switches a trigger group on as it loads finds the group, and one that imports
            # a rule of the same name replaces the file's.
            for kind, group_tag in ((TRIGGER, "triggers"), (ALIAS, "aliases")):
                for container in root.iter(group_tag):
                    for element in container.iter(kind):
                        self._load_rule(container, element, kind)
            for container in root.iter("timers"):
                for element in container.iter("timer"):
                    defaults = self._uses_defaults(container, element)
                    try:
                        self._register_timer(element, defaults=defaults)
                    except Exception as exc:  # noqa: BLE001 - one malformed timer is not the pack
                        label = element.get("name") or "(unnamed timer)"
                        self._rule_errors.append((label, f"{type(exc).__name__}: {exc}"))
            if script_filename:
                self._load_world_script(script_filename)
            script = "\n".join((el.text or "") for el in root.iter("script"))
            if plugin is not None and plugin_id in self._plugin_info:
                self._plugin_info[plugin_id]["script"] = script.strip()  # GetPluginInfo 4
            if script.strip():
                self._guard.run_strict(self._execute_lua, script)
            if self._loading_plugins[plugin_id] == 1:  # its plain includes are loaded too
                self._bind_handlers(plugin_id)
            self._capture_hooks()
        finally:
            self._loading_plugins[plugin_id] -= 1
            if not self._loading_plugins[plugin_id]:
                del self._loading_plugins[plugin_id]
            self._current_plugin = previous
        for include in root.iter("include"):
            name = include.get("name")
            if not name or include.get("plugin", "n").lower() != "y":
                continue
            try:
                self._load_included(name)
            except Exception as exc:  # noqa: BLE001 - a malformed plugin must not sink the pack
                self._include_errors.append((name, f"{type(exc).__name__}: {exc}"))

    def _load_world_script(self, script_filename: str) -> None:
        """The world's external script file, which MUSHclient runs after loading the world."""
        target = self._resolve_pack_file(script_filename)
        if target is None:
            self._skip_plugin(script_filename, "external world script is not bundled with the pack")
            return
        try:
            self._load_path(target)
        except Exception as exc:  # noqa: BLE001 - XML rules can still be useful
            self._external_script_errors.append(
                (script_filename, f"{type(exc).__name__}: {exc}")
            )

    def _bind_handlers(self, plugin_id: str) -> None:
        """Find the script functions of a plugin's rules once its script has run.

        Every plugin shares one script space here, so a function found now stays this
        plugin's even when a later plugin defines another of the same name.
        """
        for rules in self._rules.values():
            for (plugin, _label), rule in rules.items():
                if plugin == plugin_id and rule.script and rule.handler is None:
                    rule.handler = self._lua_function(rule.script)

    def _load_rule(self, container: ET.Element, element: ET.Element, kind: str) -> None:
        """One <trigger> or <alias> from a world or plugin file. A bad one is noted and
        skipped, and so is a second of the same name, as MUSHclient's loader does."""
        label = element.get("name") or element.get("match") or "(unnamed)"
        try:
            rule = self._rule_from_xml(
                element, kind, defaults=self._uses_defaults(container, element)
            )
            self._compile_fixed_script(rule)
            code = self._add_rule(rule, replace=False, skip_repeats=True)
        except Exception as exc:  # noqa: BLE001 - one malformed rule is not the pack
            self._rule_errors.append((label, f"{type(exc).__name__}: {exc}"))
            return
        if code in (E_TRIGGER_ALREADY_EXISTS, E_ALIAS_ALREADY_EXISTS):
            self._rule_errors.append((label, f"another {kind} is already named {rule.name!r}"))
        elif code in (E_TRIGGER_CANNOT_BE_EMPTY, E_ALIAS_CANNOT_BE_EMPTY):
            self._rule_errors.append((label, f"the {kind} has nothing to match"))

    def _load_included(self, filename: str) -> None:
        target = self._resolve_pack_file(filename)
        if target is None:
            self._skip_plugin(filename, "not bundled with the pack")
            return
        # Once per file, but a plain include once per plugin: each plugin that includes
        # constants.lua or a shared helper gets its definitions in its own globals.
        plain = self._plain_include_depth > 0
        key = (self._current_plugin, target) if plain else target
        if key in self._loaded_includes:
            return
        self._loaded_includes.add(key)
        if plain:  # part of the plugin including it
            self._plugin_files.setdefault(self._current_plugin, set()).add(key)
        reason = self._plugin_skip_reason(target)
        if reason:
            self._mark_virtual_plugin(target)
            self._skip_plugin(filename, reason)
            return
        self._load_path(target)

    # --- plugin lifecycle ---

    def _capture_hooks(self) -> None:
        """Claim the ``OnPlugin*`` functions the current plugin's script defined.

        Must use ``rawget``: the black-hole ``_G`` metatable reports every name as
        defined. A plugin's hooks stay in its own globals, so it can call them itself
        (packs run OnPluginInstall again from a reset alias). The world's are taken out of
        the shared globals, where every plugin would otherwise read them as its own.
        """
        env = self._env_for(self._current_plugin)
        globals_ = self._lua.globals() if env is None else env
        captured = self._hooks.setdefault(self._current_plugin, {})
        for name in _LIFECYCLE_HOOKS:
            fn = self._rawget(globals_, name)
            if fn is not None:
                captured[name] = fn
                if env is None:
                    globals_[name] = None

    def dispatch(self, name: str, *args: object, caller: object | None = None) -> None:
        """Call one lifecycle hook on every plugin that defines it, in load order.

        Each call is time-budgeted and isolated: one plugin's failing hook is
        traced to the diagnostic log and the rest still run, mirroring MUSHclient
        where one erroring plugin doesn't halt the others. ``caller`` interposes a
        Lua-side adapter (``caller(fn, *args)``) for arguments that can't cross the
        lupa boundary as-is (byte payloads).
        """
        # A list-changed hook can load more plugins, mutating _hooks during dispatch.
        for plugin_id, hooks in list(self._hooks.items()):
            fn = hooks.get(name)
            if fn is None or not self._plugin_enabled(plugin_id):
                continue
            previous = self._current_plugin
            self._current_plugin = plugin_id
            try:
                if caller is not None:
                    self._guard.run_strict(caller, fn, *args)
                else:
                    self._guard.run_strict(fn, *args)
            except Exception as exc:  # noqa: BLE001 - one plugin's hook must not stop the rest
                item = (f"{plugin_id}.{name}", f"{type(exc).__name__}: {exc}")
                if item not in self._hook_errors:
                    self._hook_errors.append(item)
                diag = self._api.diag
                if diag is not None:
                    diag.event("plugin.dispatch", hook=name, plugin=plugin_id,
                               error=f"{type(exc).__name__}: {exc}")
            finally:
                self._current_plugin = previous

    def has_hook(self, name: str) -> bool:
        """Whether any loaded plugin defined this lifecycle hook (drives tick arming)."""
        return any(hooks.get(name) is not None for hooks in self._hooks.values())

    def dispatch_install(self) -> None:
        """MUSHclient calls each plugin's ``OnPluginInstall`` at load; packs set their
        variable defaults there (Erion turns every sound toggle on), so skipping it
        leaves the pack loaded but gated silent."""
        self._lifecycle_started = True  # from now on a plugin loaded is installed at once
        # MUSHclient installs a world's plugins when it opens, before it connects; genericMud
        # installs them as the connection opens, and then calls OnPluginConnect. Plugins that
        # run their own OnPluginConnect when IsConnected() would hear it twice.
        self._opening = True
        try:
            self.dispatch("OnPluginInstall")
            # Then the world's own open handler, which MUSHclient runs once the world and its
            # plugins are loaded, before connecting (yugioh sets up its settings there).
            self._world_event("open")
        finally:
            self._opening = False

    def _world_event(self, event: str) -> None:
        """Run the world file's handler for ``event``, a function of the world's script, or
        with a leading "!" a command to execute, as MUSHclient's SeeIfHandlerCanExecute."""
        handler = self._world_events.get(event, "")
        if not handler:
            return
        if handler.startswith("!"):
            self._in_plugin("world", self._api.execute, handler[1:])
            return
        function = self._lua_function(handler, "world")
        if function is not None:
            self._in_plugin("world", self._guard.run, function)

    def dispatch_focus(self, focused: bool) -> None:
        """The world's window came to the front or went behind: the world file's handler,
        then each plugin's OnPluginGetFocus or OnPluginLoseFocus, as MUSHclient tells them
        (Miriani-Next plays its room ambience only while in front)."""
        self._world_event("get_focus" if focused else "lose_focus")
        self.dispatch("OnPluginGetFocus" if focused else "OnPluginLoseFocus")

    def dispatch_connect(self) -> None:
        self._connected_at = time.time()
        self._world_event("connect")  # before the plugins hear it, as in MUSHclient
        self.dispatch("OnPluginConnect")

    def resume_timers(self) -> None:
        """Schedule every enabled timer again. A dropped connection cancels the session's
        pending timers, and MUSHclient's carry on through one (Toastush's heartbeat)."""
        for key, state in list(self._timers.items()):
            state["generation"] += 1  # anything still pending from before no-ops
            if state["enabled"]:
                self._schedule_timer(key, state)

    def dispatch_disconnect(self) -> None:
        """The connection dropped; packs stop their music and save in OnPluginDisconnect."""
        self._connected_at = None
        self._world_event("disconnect")
        self.dispatch("OnPluginDisconnect")

    def dispatch_close(self) -> None:
        """The session is closing: plugins that save their state do it now, through their
        OnPluginSaveState, as MUSHclient does when a world closes, then every plugin hears
        OnPluginClose (Miriani-Next fixes up its settings in both)."""
        self._saving_state = True  # a SaveState inside the hook doesn't run it again
        try:
            for plugin_id, info in list(self._plugin_info.items()):
                if info.get("save_state") and not info.get("virtual"):
                    self._call_hook(plugin_id, "OnPluginSaveState")
        finally:
            self._saving_state = False
        self._world_event("close")
        self.dispatch("OnPluginClose")

    def pack_updated(self, result, *, quiet: bool = False) -> None:
        """genericMud's updater finished with this pack (``result`` is a GitUpdateResult): tell
        the pack as the updater plugin genericMud stands in for told it."""
        plugin_id = self._updater_id
        if not plugin_id:
            return

        def ours(path: str) -> bool:  # leave out plugins genericMud never loads
            return Path(path).name.casefold() not in _HOST_PLUGIN_REPLACEMENTS

        written = [path for path in result.written_files if ours(path)]
        removed = [path for path in result.removed_files if ours(path)]
        if plugin_id == _MIRIANI_NEXT_UPDATER:
            # As its update_watcher.xml reports a finished update: the pack plays its theme
            # and, if its player turned automatic_changelog on, lists the files atop the
            # changelog. 999 is its "already up to date", sent only when not asked quietly.
            if written or removed:
                report = {"result": "success", "files": written + removed, "channel": "stable"}
                self._broadcast_from(plugin_id, 777, json.dumps(report))
            elif result.up_to_date and not quiet:
                self._broadcast_from(plugin_id, 999, "")
        elif plugin_id == _TOASTUSH_UPDATER and (written or removed):
            # Its broadcast_event: each entry is {path, index entry}; Toastush reads the path.
            report = {
                "create": [],
                "delete": [[path, {}] for path in removed],
                "update": [[path, {}] for path in written],
            }
            self._broadcast_from(plugin_id, 1, json.dumps(report))

    def dispatch_line(self, line: str) -> None:
        # A Tell that nothing finished still reaches the output, ahead of the MUD's line.
        self._flush_tell()
        self.dispatch("OnPluginLineReceived", line)
        self._refresh_expanding()  # after the hook, which may have changed a variable

    def dispatch_gmcp(self, package: str, value: object) -> None:
        """Expose parsed GMCP data and mirror GMCP_handler_NJG's plugin broadcast."""
        root = package.casefold()
        self._gmcp_values[root] = value

        def flatten(prefix: str, item: object) -> None:
            if not isinstance(item, dict):
                return
            for key, child in item.items():
                child_path = f"{prefix}.{str(key).casefold()}"
                self._gmcp_values[child_path] = child
                flatten(child_path, child)

        flatten(root, value)
        self.dispatch("OnPluginBroadcast", 1, _GMCP_HANDLER_ID, "GMCP_handler_NJG", root)

    def dispatch_telnet_request(self, option: int, message: str) -> None:
        """``OnPluginTelnetRequest(option, "WILL"/"SENT_DO")`` -- the SENT_DO round is
        where MSDP packs send their REPORT list; without it the server streams nothing."""
        self.dispatch("OnPluginTelnetRequest", option, message)

    def dispatch_telnet_subnegotiation(self, option: int, payload: bytes) -> None:
        # MUSHclient hands plugins the raw payload as a Lua byte-string. It crosses
        # into Lua as a byte table and is reassembled there (_payload_caller).
        table = self._lua.table_from(list(payload))
        self.dispatch(
            "OnPluginTelnetSubnegotiation", option, table, caller=self._payload_caller
        )

    def _uses_defaults(self, container: ET.Element, element: ET.Element) -> bool:
        """MUSHclient's "defaults" switch for one rule.

        MUSHclient only writes a boolean attribute when it's true, so a missing one reads as
        false: a trigger with no ``enabled`` is a disabled one (Materia Magica keeps 211
        capture-mode rules that way and switches their groups on when needed). With the
        switch on, missing attributes take the class defaults instead, enabled included.
        It's on for a plain (non-plugin) <include> and wherever a container or the rule
        itself says defaults="y" (xml_load_world.cpp GET_VERSION_AND_DEFAULTS).
        """
        if self._plain_include_depth > 0:
            return True
        return _xml_flag(element.attrib, "defaults", _xml_flag(container.attrib, "defaults"))


_CDATA_RE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_BARE_AMP_RE = re.compile(r"&(?!(?:[A-Za-z][\w.-]*|#\d+|#x[0-9A-Fa-f]+);)")


def _sanitize_attr_markup(xml: str) -> str:
    """Escape raw ``<`` and bare ``&`` inside quoted attribute values (outside CDATA).

    MUSHclient plugins put regexes with named groups in ``match="(?P<name>...)"`` attributes;
    the raw ``<`` is illegal XML and trips ElementTree even though MUSHclient tolerates it.
    Script bodies live in CDATA and are left untouched; well-formed packs have nothing to
    escape (entities already use ``&...;``), so this is a no-op for them.
    """
    out: list[str] = []
    last = 0
    for cdata in _CDATA_RE.finditer(xml):
        out.append(_escape_attr_values(xml[last : cdata.start()]))
        out.append(cdata.group(0))
        last = cdata.end()
    out.append(_escape_attr_values(xml[last:]))
    return "".join(out)


def _escape_attr_values(segment: str) -> str:
    """Repair the attribute values in each start tag, and leave everything else alone.

    Only a value that follows ``=`` inside a tag counts. Text between tags is never touched:
    send text like Mush-Z's ``Execute("history_add all=" .. msg)`` holds an ``="`` too, and
    reading that as an attribute escaped every tag up to the next quote, which lost the 857
    triggers of its sound module. Values may be quoted with " or ', with spaces around the
    ``=`` (Mush-Z writes ``regexp = "y"``).
    """
    out: list[str] = []
    index = 0
    length = len(segment)
    while index < length:
        start = segment.find("<", index)
        if start < 0:
            out.append(segment[index:])
            break
        out.append(segment[index:start])
        if segment.startswith("<!--", start):
            end = segment.find("-->", start + 4)
            end = length if end < 0 else end + 3
            out.append(segment[start:end])  # a comment's text is nobody's markup
            index = end
            continue
        if start + 1 >= length or not _starts_name(segment[start + 1]):
            out.append("<")  # an end tag, declaration, or text
            index = start + 1
            continue
        index = _copy_start_tag(segment, start, out)
    return "".join(out)


def _starts_name(character: str) -> bool:
    return character.isalpha() or character in "_:"


def _copy_start_tag(segment: str, start: int, out: list[str]) -> int:
    """Copy the start tag at ``start`` with its quoted values repaired; where it ended."""
    out.append("<")
    position = start + 1
    length = len(segment)
    after_equals = False
    while position < length:
        character = segment[position]
        if character == ">":
            out.append(">")
            return position + 1
        if character in "\"'" and after_equals:
            end = segment.find(character, position + 1)
            if end < 0:
                break  # an unclosed value: leave the rest as it is
            value = segment[position + 1 : end]
            value = _BARE_AMP_RE.sub("&amp;", value).replace("<", "&lt;")
            out.append(f"{character}{value}{character}")
            position = end + 1
            after_equals = False
            # A few hand-edited packs leave out the space before the next attribute
            # (`send_to="12"sequence="100"`). MUSHclient accepts that; ElementTree does not.
            if position < length and _starts_name(segment[position]):
                out.append(" ")
            continue
        if character == "=":
            after_equals = True
        elif not character.isspace():
            after_equals = False
        out.append(character)
        position += 1
    out.append(segment[position:])
    return length


class _HeadlessWindow:
    """The window's calls for a session without a window (tests, the census): the clipboard
    and command box keep what packs put there, and nothing is shown or opened."""

    def __init__(self) -> None:
        self.clipboard = ""
        self.command = ""
        self.selection = (0, 0)

    def get_clipboard(self) -> str:
        return self.clipboard

    def set_clipboard(self, text: str) -> None:
        self.clipboard = text

    def get_command(self) -> str:
        return self.command

    def set_command(self, text: str) -> bool:
        if self.command:
            return False
        self.command = text
        self.selection = (len(text), len(text))
        return True

    def paste_command(self, text: str) -> str:
        start, end = (min(edge, len(self.command)) for edge in self.selection)
        replaced = self.command[start:end]
        self.command = self.command[:start] + text + self.command[end:]
        self.selection = (start + len(text), start + len(text))
        return replaced

    def select_command(self, first: int, last: int) -> None:
        length = len(self.command)
        start = min(max(first - 1, 0), length)
        end = length if last < 0 else min(max(last, start), length)
        self.selection = (start, end)

    def open_url(self, url: str) -> bool:
        return False

    def show_notepad(self, title: str, text: str) -> None:
        return None


def _split_escaped(text: str, separator: str) -> list[str]:
    """Split ArrayImport's text the way MUSHclient does: a backslash before the separator
    keeps it inside a key or value, and two backslashes are one."""
    if not text:
        return []
    escaped = "\\" + separator
    placeholder = ""
    if escaped in text:
        placeholder = next((chr(code) for code in range(1, 256) if chr(code) not in text), "")
        if placeholder:
            text = text.replace(escaped, placeholder)
    parts = []
    for part in text.split(separator):
        part = part.replace("\\\\", "\\")
        parts.append(part.replace(placeholder, separator) if placeholder else part)
    return parts


def _command_lines(text: str) -> list[str]:
    """The commands in a send body: one per line, blank lines left out."""
    return [line.rstrip("\r") for line in text.split("\n") if line.strip()]

