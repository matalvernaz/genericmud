"""User-authored rules: the Automation Manager's storage and engine layer.

The wx automation dialogs edit a plain JSON file (one per world, under
``genericmud-data/userpacks/<world>/rules.json``); this module owns the schema,
load/save, and registration onto the shared :class:`AutomationEngine` via
:class:`ScriptApi` -- the same surface the scripting dialects use. Field-made
rules support wildcard or regex patterns, sound cues, speech, command stacks,
capture/script/MUD variables, line hiding, and channel routing.

Everything here is headless (no wx), so the whole rules core is testable on
the build-blind dev host; the dialogs are a thin shell over ``save()`` +
``EngineApp.reload_user_rules()``.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from genericmud.automation.engine import MatchContext
from genericmud.config.atomic import atomic_write_text
from genericmud.model.buffer import Line
from genericmud.render.ansi import strip_ansi
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import REVIEW_CHANNEL

SOURCE = "user"  # registration source: remove_source(SOURCE) clears rules for reload
RULES_FILENAME = "rules.json"
SOUNDS_DIRNAME = "sounds"  # picked sound files are copied here (keeps the pack portable)

_CAPTURE_RE = re.compile(r"%(\d)")
_GAG_CHOICES = ("none", "speech", "line")
# Where a rule's response text goes -- MUSHclient's "send to" box (its eSendTo* enum). The
# order is the order they are offered in, and matches SEND_TO_LABELS.
SEND_TO_CHOICES = ("world", "command", "output", "log", "execute", "script")
SEND_TO_LABELS = (
    "The MUD (send these commands)",
    "My command box, unsent (finish it myself)",
    "The output, as a note (kept in the log)",
    "The session log only (never shown)",
    "This client again (run it as if I typed it)",
    "A script (a function name, or Lua code)",
)


def send_to_kind(value: object) -> str:
    """The send-to target a saved value names; anything unknown goes to the MUD, as before."""
    return value if isinstance(value, str) and value in SEND_TO_CHOICES else "world"
# How a trigger pattern matches a line. "contains" and "exact" are newbie-facing
# sugar over regex (escaped literal, searched / anchored); "wildcard" is * and ?.
MATCH_CHOICES = ("contains", "wildcard", "exact", "regex")
_MAX_COMMANDS_PER_RULE = 100
# A plain alias (no wildcards, not a regular expression) also takes a command with more words
# after it and carries them along: "sh" sending "shoot" turns "sh goblin" into "shoot goblin".
# That reading runs below every other alias, so it only takes a command nothing else would: a
# "sh *" alias, or a soundpack's own, still wins. MUSHclient's speech commands sit at
# -1,000,000, the lowest any pack uses.
EXTRA_WORDS_PRIORITY = -2_000_000
# A command that places the first capture itself, as %1 or ${1}, decides where the extra words
# go; otherwise they go on the end of the last command.
_FIRST_CAPTURE_RE = re.compile(r"%1|\$\{1\}")


@dataclass
class UserTrigger:
    pattern: str = ""
    regex: bool = False  # kept in sync with match for files older builds read
    sound: str = ""  # pack-relative path ("sounds/x.ogg"); "" = no cue
    volume: int = 100  # 0..100
    pan: int = 0  # -100 (left) .. 100 (right)
    loop: bool = False
    speak: str = ""  # spoken text; %1 or ${1}, ${script:x}, ${mud:x}
    send: str = ""  # what to do with the matched line; %1 or ${1}, ${script:x}, ${mud:x}
    # Where "send" goes: SEND_TO_CHOICES. MUSHclient calls this "send to", and it is the same
    # question for triggers, aliases and timers -- the text is one field, the destination
    # another. Never a separate field per destination.
    send_to: str = "world"
    gag: str = "none"  # "none" | "speech" (silent but shown) | "line" (removed)
    omit_log: bool = False  # leave the line out of the session log, whatever gag does
    channel: str = ""  # route the line to this channel ("" = leave on main)
    stop_channel: str = ""  # stop this user cue channel when fired ("" = none)
    match: str = ""  # one of MATCH_CHOICES; "" = legacy file (the regex flag decides)
    interrupt: bool = False  # cut current speech the moment this fires
    # MUSHclient's trigger name: the handle a script turns it off and on with, and what the
    # manager reads out first. Empty means no handle; the manager can still disable it.
    label: str = ""
    # MUSHclient's group: rules sharing a name are switched together by one call, which is what
    # "turn all the combat rules off" needs. Empty means the rule stands alone.
    group: str = ""
    enabled: bool = True

    def match_kind(self) -> str:
        if self.match in MATCH_CHOICES:
            return self.match
        return "regex" if self.regex else "wildcard"


@dataclass
class UserAlias:
    pattern: str = ""  # what the user types; * ? wildcards unless regex
    regex: bool = False
    send: str = ""  # what to do with the typed command; capture/variable templates allowed
    send_to: str = "world"  # where that text goes, as for triggers (SEND_TO_CHOICES)
    speak: str = ""  # optional speech, same templates; can read a value with no commands
    label: str = ""  # as for triggers: the handle that turns it off and on
    group: str = ""  # as for triggers: switched together with the rest of its group
    enabled: bool = True


@dataclass
class UserKey:
    key: str = ""  # keymap combo, e.g. "ctrl+h", "alt+shift+f2"
    send: str = ""
    speak: str = ""  # e.g. "${mud:Char.Vitals.hp} health": a key that reads a value
    sound: str = ""  # pack-relative one-shot cue
    enabled: bool = True


@dataclass
class UserTimer:
    """Something to do on a clock rather than on a line (MUSHclient's timer).

    Two shapes, as MUSHclient has them: every so often (``every`` seconds), or once a day at a
    time (``at``, "HH:MM:SS"). ``one_shot`` does it once and stops. What it does is the same
    question every other rule answers -- a text and a send-to target -- so a timer can send to
    the MUD, put something in your command box, write a note, or run Lua.
    """

    label: str = ""  # MUSHclient's timer name: the handle a script switches it by
    enabled: bool = True
    every: float = 0.0  # seconds between fires; 0 means "not an interval timer"
    at: str = ""  # "HH:MM:SS" for a daily timer; "" means "not a daily one"
    one_shot: bool = False
    send: str = ""
    send_to: str = "world"
    omit_log: bool = False  # leave whatever it writes out of the session log


@dataclass
class UserChannel:
    name: str = ""
    speak: bool = True
    display: bool = True
    interrupt: bool = False
    enabled: bool = True


@dataclass
class UserWatch:
    """A MUD data rule: do something when the MUD sends a value (GMCP, MSDP or MSSP)."""

    name: str = ""  # a GMCP package, an MSDP/MSSP variable, or a path inside one: Char.Vitals.hp
    # On by default: Char.Vitals arrives with every prompt, and nobody wants their health
    # read out two hundred times when it hasn't moved.
    only_changes: bool = True
    sound: str = ""  # pack-relative one-shot cue
    volume: int = 100  # 0..100
    pan: int = 0  # -100 (left) .. 100 (right)
    speak: str = ""  # ${value} and ${name}, plus ${mud:x} and ${script:x}
    send: str = ""  # commands, one per line, same templates
    interrupt: bool = False  # cut current speech when it fires
    enabled: bool = True


@dataclass
class UserRules:
    triggers: list[UserTrigger] = field(default_factory=list)
    timers: list[UserTimer] = field(default_factory=list)
    aliases: list[UserAlias] = field(default_factory=list)
    keys: list[UserKey] = field(default_factory=list)
    channels: list[UserChannel] = field(default_factory=list)
    watches: list[UserWatch] = field(default_factory=list)

    def to_json(self) -> str:
        payload = {"version": 1, **asdict(self)}
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> UserRules:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("rules file is not a JSON object")

        def build(kind, items):
            fields = {f for f in kind.__dataclass_fields__}
            out = []
            for item in items or []:
                if isinstance(item, dict):
                    out.append(kind(**{k: v for k, v in item.items() if k in fields}))
            return out

        return cls(
            triggers=build(UserTrigger, data.get("triggers")),
            aliases=build(UserAlias, data.get("aliases")),
            keys=build(UserKey, data.get("keys")),
            timers=build(UserTimer, data.get("timers")),
            channels=build(UserChannel, data.get("channels")),
            watches=build(UserWatch, data.get("watches")),
        )


def rules_path(pack_dir: Path) -> Path:
    return Path(pack_dir) / RULES_FILENAME


def load_rules(pack_dir: Path) -> UserRules:
    """The world's saved rules; empty (not an error) when none exist yet."""
    try:
        return UserRules.from_json(rules_path(pack_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return UserRules()


def save_rules(pack_dir: Path, rules: UserRules) -> None:
    pack_dir = Path(pack_dir)
    atomic_write_text(rules_path(pack_dir), rules.to_json())


def copy_sound_into_pack(pack_dir: Path, source_path: str) -> str:
    """Copy a picked sound file into the pack's sounds dir; return its pack-relative path.

    Sound paths must live under the pack dir (ScriptApi confines media there), and
    copying keeps the user pack self-contained/portable. A file already inside the
    pack is referenced in place, not duplicated.
    """
    pack_dir = Path(pack_dir).resolve()
    src = Path(source_path)
    try:
        resolved = src.resolve()
        if resolved.is_relative_to(pack_dir):
            return resolved.relative_to(pack_dir).as_posix()
    except OSError:
        pass
    dest_dir = pack_dir / SOUNDS_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / src.name)
    return f"{SOUNDS_DIRNAME}/{src.name}"


def _substitute(text: str, wildcards: list[str]) -> str:
    def one(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return wildcards[index] if index < len(wildcards) else ""

    return _CAPTURE_RE.sub(one, text)


def _commands(text: str, *, keep_spaces: bool = False) -> tuple[str, ...]:
    """Non-empty commands from a one-command-per-line field.

    ``keep_spaces`` leaves the ends alone, for text going into the command box rather than out
    to the MUD: the trailing space of "tell Ariadne " is there to be typed into.
    """
    cleaner = (lambda line: line) if keep_spaces else (lambda line: line.strip())
    commands = tuple(cleaner(line) for line in text.splitlines() if line.strip())
    if len(commands) > _MAX_COMMANDS_PER_RULE:
        raise ValueError(f"too many commands (maximum {_MAX_COMMANDS_PER_RULE})")
    return commands


def _capture_values(ctx: MatchContext | None) -> dict[str, object]:
    """``${1}``.. and named groups from a match, for template expansion."""
    wildcards = ctx.wildcards if ctx is not None else [""]
    values: dict[str, object] = {
        str(index): value
        for index, value in enumerate(wildcards[1:], start=1)
    }
    if ctx is not None:
        values.update(ctx.named)
    return values


def _speech(api: ScriptApi, text: str, ctx: MatchContext | None = None) -> str:
    """A rule's speech with its variables filled in.

    Same order as commands, and for the same reason: ``${...}`` first, then the legacy
    ``%1`` captures, so MUD text that happens to contain ``${...}`` is read out literally
    instead of becoming a template.
    """
    wildcards = ctx.wildcards if ctx is not None else [""]
    return _substitute(api.expand_speech(text, _capture_values(ctx)), wildcards)


def _expand_commands(
    api: ScriptApi, commands: tuple[str, ...], ctx: MatchContext | None = None
) -> list[str]:
    """Expand a command stack completely without sending any part of it."""
    wildcards = ctx.wildcards if ctx is not None else [""]
    values = _capture_values(ctx)
    expanded = []
    for command in commands:
        # Expand ${...} before legacy %1 replacement. Captured MUD text containing
        # `${...}` must stay literal instead of becoming a second command template.
        value = _substitute(api.expand_command(command, values), wildcards)
        if any(char in value for char in ("\r", "\n", "\x00")):
            raise ValueError("expanded command contains a line break or NUL")
        expanded.append(value)
    return expanded


def _command_action(
    api: ScriptApi, text: str, *, send_to: str = "world", carry_words: bool = False
) -> tuple[tuple[str, ...], Callable[[MatchContext | None], bool]]:
    """A contained action and whether it has any text, delivering to the rule's send-to target.

    ``carry_words`` puts the match's first capture, the words typed after a plain alias, on
    the end of the last line.
    """
    commands = _commands(text, keep_spaces=send_to_kind(send_to) == "command")
    spoken_errors: set[str] = set()

    def run(ctx: MatchContext | None = None) -> bool:
        try:
            expanded = _expand_commands(api, commands, ctx)
        except ValueError as error:
            detail = str(error)
            if api.diag is not None:
                api.diag.event(
                    "user_rule.command_error", source=api.source or SOURCE, error=detail
                )
            if detail not in spoken_errors:
                spoken_errors.add(detail)
                api.speak(f"Automation text not used: {detail}", channel="system")
            return False
        if carry_words and expanded and ctx is not None and len(ctx.wildcards) > 1:
            # Added after expansion, so the player's own words go out as typed and are never
            # read as a template.
            expanded[-1] = f"{expanded[-1].rstrip()} {ctx.wildcards[1]}"
        deliver(api, expanded, send_to, ctx)
        return True

    return commands, run


def deliver(
    api: ScriptApi, lines: list[str], send_to: str, ctx: MatchContext | None = None
) -> None:
    """Put the expanded text where the rule says (MUSHclient's "send to" targets).

    The text is one field and the destination another, for triggers, aliases and -- later --
    timers alike: never a separate field per destination.
    """
    kind = send_to_kind(send_to)
    if kind == "command":
        # The box holds several lines now, so a stack arrives as a block to finish and send,
        # which is the point of putting it there rather than sending it.
        api.type_text("\n".join(lines))
    elif kind == "output":
        for line in lines:
            api.note(line)
    elif kind == "log":
        for line in lines:
            api.log_line(line)
    elif kind == "script":
        line = ctx.line.plain_text if ctx is not None else ""
        # ctx.wildcards[0] is the whole matched line; a Lua function's wildcards[1] is the
        # first capture, as mud.trigger's callback has it.
        wildcards = list(ctx.wildcards[1:]) if ctx is not None else []
        for text in lines:  # each line is its own call: a function name, or Lua code
            try:
                api.run_script(text, line=line, wildcards=wildcards)
            except Exception as error:  # noqa: BLE001 - a broken rule must say so, once
                _script_failed(api, error)
    elif kind == "execute":
        # MUSHclient's "execute": hand it back to the client as if it had been typed, so an
        # alias takes it first and only what falls through reaches the MUD. Its "send to
        # script" is a different thing -- calling a named routine in a script -- and this
        # client's scripts are hook files with no named entry points yet, so that one waits.
        api.execute("\n".join(lines))
    else:
        for line in lines:
            api.send(line)


def _script_failed(api: ScriptApi, error: Exception) -> None:
    """A rule's script went wrong: log it, and say it once per distinct problem."""
    detail = f"{type(error).__name__}: {error}"
    said = getattr(api, "_rule_script_failures", None)
    if said is None:
        said = set()
        api._rule_script_failures = said  # noqa: SLF001 - one set per session's api
    if detail in said:
        return
    said.add(detail)
    if api.diag is not None:
        api.diag.event("user_rule.script_error", source=api.source or SOURCE, error=detail)
    api.speak(f"Automation script failed: {detail}", channel="system")


def extra_words_pattern(shortcut: str) -> str:
    """The regex for a plain alias with more words typed after it; those words are capture 1.

    Matched from the start of the command and to its end, with at least one space between, so
    "sh" takes "sh goblin" but never "shout".
    """
    return re.escape(shortcut) + r"[ \t]+(.*\S)[ \t]*\Z"


def register_rules(api: ScriptApi, rules: UserRules) -> None:
    """Register every rule on the engine under the ``user`` source.

    ``api.base_dir`` must be the user pack dir so sound paths resolve inside it
    (the builder copies picked files there). Call ``engine.remove_source(SOURCE)``
    first when reloading.
    """
    for channel in rules.channels:
        if channel.enabled and channel.name:
            api.set_channel(
                channel.name, speak=channel.speak, display=channel.display,
                interrupt=channel.interrupt,
            )
    for trigger in rules.triggers:
        if trigger.enabled and trigger.pattern:
            _register_trigger(api, trigger)
    for alias in rules.aliases:
        if alias.enabled and alias.pattern:
            _register_alias(api, alias)
    for key in rules.keys:
        if key.enabled and key.key:
            _register_key(api, key)
    for timer in rules.timers:
        _register_timer(api, timer)
    for watch in rules.watches:
        if watch.enabled and watch.name.strip():
            _register_watch(api, watch)


def _trigger_pattern(t: UserTrigger) -> tuple[str, bool]:
    """The (pattern, regex) pair to register for the trigger's match kind.

    Triggers match with ``search``, so an escaped literal IS "contains" and an
    anchored escaped literal IS "exact"; "wildcard" keeps the engine's * and ?
    translation with its capture groups.
    """
    kind = t.match_kind()
    if kind == "contains":
        return re.escape(t.pattern), True
    if kind == "exact":
        return f"^{re.escape(t.pattern)}$", True
    return t.pattern, kind == "regex"


def _register_trigger(api: ScriptApi, t: UserTrigger) -> None:
    gag = t.gag if t.gag in _GAG_CHOICES else "none"
    commands, send_commands = _command_action(api, t.send, send_to=t.send_to)
    has_actions = bool(t.sound or t.speak or commands or t.stop_channel or t.interrupt)

    def fire(ctx: MatchContext) -> None:
        if t.interrupt:
            api.stop_speech()
        if t.stop_channel:
            api.stop(f"user-{t.stop_channel}")
        if t.sound:
            api.play(
                t.sound,
                channel=f"user-{t.channel or 'sound'}",
                gain=max(0, min(100, t.volume)) / 100,
                pan=max(-100, min(100, t.pan)) / 100,
                loop=t.loop,
            )
        if t.speak:
            api.speak(
                _speech(api, t.speak, ctx),
                channel=t.channel or "main",
                interrupt=t.interrupt,
            )
        send_commands(ctx)

    pattern, regex = _trigger_pattern(t)
    api.add_trigger(
        pattern,
        fire if has_actions else None,
        regex=regex,
        gag=(gag == "line"),
        gag_but_display=(gag == "speech"),
        omit_from_log=t.omit_log,
        channel=t.channel or None,
        source=SOURCE,
        name=t.label.strip(),
        group=t.group.strip(),
    )


# A hotkey's speech answers a key the player just pressed, like a review key does, so it
# goes on the review channel: straight away, not queued behind a screenful of MUD output,
# not coalesced by the main channel's flood governor, and still spoken with self-voice off
# (Ctrl+M). Only hotkeys: keys only ever reach the tab in front. An alias can also fire in a
# background tab -- /to from another tab, mud.send_to, a pack's Execute -- and the review
# channel speaks through the mute that keeps background tabs quiet, so an alias there would
# talk over, and with interrupt cut off, the session the player is actually in.
_KEY_REPLY_CHANNEL = REVIEW_CHANNEL


def parse_at(value: str) -> float | None:
    """The seconds after midnight an "HH:MM:SS" names, or None if it does not name one."""
    parts = str(value).strip().split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    hours, minutes = numbers[0], numbers[1]
    seconds = numbers[2] if len(numbers) == 3 else 0.0
    if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds < 60):
        return None
    return hours * 3600 + minutes * 60 + seconds


def seconds_until(target: float, *, now: float) -> float:
    """How long from ``now`` (seconds since midnight) until ``target`` comes round again.

    A time that has already passed today is tomorrow's, so a daily timer set for 03:00 fires at
    the next 03:00 rather than in the past. Exactly now counts as a day away, not as zero: a
    timer that fires the instant it is created has nothing to wait for.
    """
    difference = (target - now) % 86400
    return difference if difference > 0 else 86400.0


def clock_seconds(now: datetime | None = None) -> float:
    """Seconds since midnight, for the daily timers; a seam so tests need no waiting."""
    moment = now or datetime.now()
    return moment.hour * 3600 + moment.minute * 60 + moment.second + moment.microsecond / 1e6


def _register_timer(api: ScriptApi, t: UserTimer, *, clock=clock_seconds) -> None:
    """Schedule one timer, and reschedule it after each fire unless it is a one-shot."""
    if not t.enabled or not t.send.strip():
        return
    at = parse_at(t.at) if t.at.strip() else None
    if at is None and t.every <= 0:
        return  # neither shape: nothing to schedule

    def delay() -> float:
        return seconds_until(at, now=clock()) if at is not None else float(t.every)

    def fire() -> None:
        commands = _commands(t.send, keep_spaces=send_to_kind(t.send_to) == "command")
        if commands:
            deliver(api, list(commands), t.send_to)
        if not t.one_shot:
            schedule()  # the next one, from this moment

    def schedule() -> None:
        # The api's timer call is the one that counts against the session's limit and is
        # cancelled with this source when the rules reload.
        api.add_timer(delay(), fire)

    schedule()


def _register_alias(api: ScriptApi, a: UserAlias) -> None:
    commands, send_commands = _command_action(api, a.send, send_to=a.send_to)

    def fire(ctx: MatchContext) -> None:
        sent = send_commands(ctx)
        if a.speak and sent:
            api.speak(_speech(api, a.speak, ctx))

    api.add_alias(
        a.pattern,
        fire,
        regex=a.regex,
        source=SOURCE,
        name=a.label.strip(),
        group=a.group.strip(),
    )
    # A plain alias also takes more words typed after it. Not one that only speaks: it has no
    # command for the words to join, and taking the line would stop it reaching the MUD.
    plain = (
        not a.regex
        and not any(wildcard in a.pattern for wildcard in "*?")
        and send_to_kind(a.send_to) == "world"  # nothing to carry words onto elsewhere
    )
    if not plain or not commands:
        return
    places_words = any(_FIRST_CAPTURE_RE.search(command) for command in commands)
    _, send_with_words = _command_action(
        api, a.send, send_to=a.send_to, carry_words=not places_words
    )

    def fire_with_words(ctx: MatchContext) -> None:
        sent = send_with_words(ctx)
        if a.speak and sent:
            api.speak(_speech(api, a.speak, ctx))

    api.add_alias(
        extra_words_pattern(a.pattern), fire_with_words, regex=True, source=SOURCE,
        priority=EXTRA_WORDS_PRIORITY, fallback=True,
    )


def data_text(value: object) -> str:
    """A MUD value as words, for speech and commands.

    Colour codes come out (IRE games colour the text inside Comm.Channel.Text), line breaks
    become spaces since a command is one line, and a table reads as compact JSON.
    """
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        text = ""
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return " ".join(strip_ansi(text).split())


def _register_watch(api: ScriptApi, w: UserWatch) -> None:
    _, send_commands = _command_action(api, w.send)

    def fire(name: str, value: object) -> None:
        text = data_text(value)
        # What the rule's templates can read: ${value} and ${1} are the value, ${name} is
        # what arrived. Nothing here reads the context's engine, so it has none.
        ctx = MatchContext(Line(text), [text, text], {"value": text, "name": name}, None)
        if w.interrupt:
            api.stop_speech()
        if w.sound:
            api.play(
                w.sound,
                channel="user-data",
                gain=max(0, min(100, w.volume)) / 100,
                pan=max(-100, min(100, w.pan)) / 100,
            )
        if w.speak:
            api.speak(_speech(api, w.speak, ctx), interrupt=w.interrupt)
        send_commands(ctx)

    api.add_data_watch(w.name.strip(), fire, only_changes=w.only_changes)


def _register_key(api: ScriptApi, k: UserKey) -> None:
    _, send_commands = _command_action(api, k.send)

    def fire(_ctx: MatchContext) -> None:
        if k.sound:
            api.play(k.sound, channel="user-key")
        if k.speak:
            api.speak(_speech(api, k.speak), channel=_KEY_REPLY_CHANNEL, interrupt=True)
        send_commands()

    api.add_key(k.key, fire)
