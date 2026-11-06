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
from datetime import datetime, time, timedelta
from pathlib import Path

from genericmud.automation.engine import MatchContext
from genericmud.config.atomic import atomic_write_text
from genericmud.model.buffer import Line
from genericmud.render.ansi import strip_ansi
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import REVIEW_CHANNEL

SOURCE = "user"  # registration source: remove_source(SOURCE) clears rules for reload
# The order a rule has when the player has not set one. Priority is worked out from it so that a
# world with no orders behaves as it did before orders existed (MUSHclient's own default).
DEFAULT_ORDER = 100
RULES_FILENAME = "rules.json"
SOUNDS_DIRNAME = "sounds"  # picked sound files are copied here (keeps the pack portable)

_CAPTURE_RE = re.compile(r"%(\d)")
_GAG_CHOICES = ("none", "speech", "line")
# Where a rule's response text goes -- MUSHclient's "send to" box (its eSendTo* enum). The
# order is the order they are offered in, and matches SEND_TO_LABELS.
SEND_TO_CHOICES = ("world", "command", "output", "log", "execute", "script", "variable")
SEND_TO_LABELS = (
    "The MUD (send these commands)",
    "My command box, unsent (finish it myself)",
    "The output, as a note (kept in the log)",
    "The session log only (never shown)",
    "This client again (run it as if I typed it)",
    "A script (a function name, or Lua code)",
    "A script variable (store the text under a name)",
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
    # The name the text is stored under when "send to" is a variable: MUSHclient's variable
    # target, which is how a capture becomes something ${script:name} can use later.
    variable: str = ""
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
    # MUSHclient's sequence: the lower the number, the sooner the rule is tried. Rules with the
    # same order are tried by their pattern text, as MUSHclient does, so the order is never a
    # matter of which file they were added from.
    order: int = DEFAULT_ORDER
    # Whether the rules after this one are still tried when it matches. True keeps today's
    # behaviour (every matching rule acts); clearing it makes this rule the last one heard about
    # the line. MUSHclient's default is the other way round, which is why this says what it does.
    keep_evaluating: bool = True
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
    variable: str = ""  # as for triggers: the name a variable target stores under
    speak: str = ""  # optional speech, same templates; can read a value with no commands
    label: str = ""  # as for triggers: the handle that turns it off and on
    group: str = ""  # as for triggers: switched together with the rest of its group
    order: int = DEFAULT_ORDER  # as for triggers: lower is tried first
    # Off for an alias, which is how this client has always behaved: your first matching alias
    # takes the command, and a later one does not send it a second time.
    keep_evaluating: bool = False
    enabled: bool = True


@dataclass
class UserKey:
    key: str = ""  # keymap combo, e.g. "ctrl+h", "alt+shift+f2"
    send: str = ""
    speak: str = ""  # e.g. "${mud:Char.Vitals.hp} health": a key that reads a value
    sound: str = ""  # pack-relative one-shot cue
    group: str = ""  # as for triggers: switched together with the rest of its group
    enabled: bool = True


@dataclass
class UserTimer:
    """Something to do on a clock rather than on a line (MUSHclient's timer).

    Two shapes, as MUSHclient has them: every so often (``every`` seconds), or once a day at a
    time (``at``, "HH:MM:SS"). ``one_shot`` does it once and stops, and ``offset`` makes an
    interval timer fire early within its interval. What it does is the same question every other
    rule answers -- a text and a send-to target -- so a timer can send to the MUD, put something
    in your command box, write a note, store a value, or run Lua.
    """

    label: str = ""  # MUSHclient's timer name: the handle a script switches it by
    enabled: bool = True
    every: float = 0.0  # seconds between fires; 0 means "not an interval timer"
    # MUSHclient's offset: fire this many seconds *before* the interval is up, so two timers on
    # the same interval take turns instead of both going off at once. It only shifts the wait
    # before the first fire; the gap between fires is ``every`` either way.
    offset: float = 0.0
    at: str = ""  # "HH:MM:SS" for a daily timer; "" means "not a daily one"
    one_shot: bool = False
    send: str = ""
    send_to: str = "world"
    variable: str = ""  # as for triggers: the name a variable target stores under
    # MUSHclient's ActiveWhenClosed: keep the clock through a disconnect. What it can then do is
    # the local half of a rule -- speak, note, log, store a value, run a script -- since there is
    # no MUD to send to. Everything else pauses, and comes back when the world does.
    run_disconnected: bool = False
    omit_log: bool = False  # leave whatever it writes out of the session log
    group: str = ""  # as for triggers: switched together with the rest of its group


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
    group: str = ""  # as for triggers: switched together with the rest of its group
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


# The name says what the copy is for: the version that wrote it understood none of these.
_BEFORE_SEND_TO = "rules.json.before-send-to.bak"


def drop_spent_one_shots(rules: UserRules, spent: set[str]) -> list[str]:
    """Take out the one-shots that have already fired; returns the names it removed.

    MUSHclient's one-shot timer deletes itself when it fires, and its world file is written from
    the live list, so the timer is gone from that file at the next save. This is that, for a
    player's own rules: what has run once is not saved to run again.
    """
    removed: list[str] = []
    kept: list[UserTimer] = []
    for place, timer in enumerate(rules.timers):
        name = timer_name(timer, place)
        if timer.one_shot and name in spent:
            removed.append(name)
        else:
            kept.append(timer)
    rules.timers = kept
    return removed


def save_rules(pack_dir: Path, rules: UserRules) -> None:
    pack_dir = Path(pack_dir)
    _keep_the_old_file_once(pack_dir)
    atomic_write_text(rules_path(pack_dir), rules.to_json())


def _keep_the_old_file_once(pack_dir: Path) -> None:
    """Copy a rules file from before the send-to fields, once, before overwriting it.

    A release without them drops the keys it does not know, so going back to it after this
    version has written labels and destinations turns every such rule into plain commands for
    the MUD. One copy taken before the first save is what makes that reversible.
    """
    path = rules_path(pack_dir)
    backup = path.with_name(_BEFORE_SEND_TO)
    if backup.exists() or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not _has_a_send_to_field(data):
        shutil.copy2(path, backup)


def _has_a_send_to_field(data: object) -> bool:
    """Whether this parsed rules file already carries the newer fields."""
    if isinstance(data, dict):
        if "send_to" in data:
            return True
        return any(_has_a_send_to_field(value) for value in data.values())
    if isinstance(data, list):
        return any(_has_a_send_to_field(value) for value in data)
    return False


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


def _response_parts(text: str, send_to: str) -> tuple[str, ...]:
    """A rule's text, cut into the pieces its destination wants.

    Every destination but a variable is a stack: one command per line, for the MUD, the command
    box, the output, the log, or the client. A variable holds one value, so its text is not cut
    at all -- a semicolon in it is part of the value, not another command.
    """
    if send_to_kind(send_to) == "variable":
        value = text.strip()
        return (value,) if value else ()
    return _commands(text, keep_spaces=send_to_kind(send_to) == "command")


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
    api: ScriptApi,
    commands: tuple[str, ...],
    ctx: MatchContext | None = None,
    *,
    as_value: bool = False,
) -> list[str]:
    """Expand a command stack completely without sending any part of it.

    ``as_value`` is text going into a variable rather than out to the MUD: a line break in it is
    then content, not a second command. The guard against one belongs where the value is used as
    a command, and stays there.
    """
    wildcards = ctx.wildcards if ctx is not None else [""]
    values = _capture_values(ctx)
    expanded = []
    for command in commands:
        # Expand ${...} before legacy %1 replacement. Captured MUD text containing
        # `${...}` must stay literal instead of becoming a second command template.
        value = _substitute(
            api.expand_command(command, values, one_line=not as_value), wildcards
        )
        forbidden = ("\x00",) if as_value else ("\r", "\n", "\x00")
        if any(char in value for char in forbidden):
            raise ValueError(
                "expanded text contains a NUL" if as_value
                else "expanded command contains a line break or NUL"
            )
        expanded.append(value)
    return expanded


def _command_action(
    api: ScriptApi,
    text: str,
    *,
    send_to: str = "world",
    variable: str = "",
    carry_words: bool = False,
) -> tuple[tuple[str, ...], Callable[[MatchContext | None], bool]]:
    """A contained action and whether it has any text, delivering to the rule's send-to target.

    ``carry_words`` puts the match's first capture, the words typed after a plain alias, on
    the end of the last line.
    """
    if send_to_kind(send_to) == "script":
        # Code, not a template. Expanding ${1} or %1 here would paste whatever the MUD sent
        # into the Lua before it is parsed, so a line like `Mallory says x") mud.command(...
        # would run as the player. Inline code reads `line` and `wildcards` instead, which
        # arrive as values rather than as text to be parsed.
        script = text if text.strip() else ""

        def run_script(ctx: MatchContext | None = None) -> bool:
            if not script:
                return False
            deliver(api, [script], send_to, ctx)
            return True

        return ((script,) if script else ()), run_script

    commands = _response_parts(text, send_to)
    as_value = send_to_kind(send_to) == "variable"
    spoken_errors: set[str] = set()

    def run(ctx: MatchContext | None = None) -> bool:
        try:
            expanded = _expand_commands(api, commands, ctx, as_value=as_value)
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
        refusal = _command_refusal(api, commands, expanded, ctx, send_to)
        if refusal is not None:
            if not _refused_once(api, refusal, spoken_errors):
                return False
            return False
        if carry_words and expanded and ctx is not None and len(ctx.wildcards) > 1:
            # Added after expansion, so the player's own words go out as typed and are never
            # read as a template.
            expanded[-1] = f"{expanded[-1].rstrip()} {ctx.wildcards[1]}"
        deliver(api, expanded, send_to, ctx, variable=variable)
        return True

    return commands, run


def _command_refusal(
    api: ScriptApi,
    commands: tuple[str, ...],
    expanded: list[str],
    ctx: MatchContext | None,
    send_to: str,
) -> str | None:
    """Why this rule's text may not go into the command box, or None if it may.

    Only when the capture is what made it wrong: the player's own text may start a command or
    hold the separator, because they wrote it and can see what it says.
    """
    if send_to_kind(send_to) != "command":
        return None
    check = getattr(api, "command_text_refusal", None)
    if check is None:
        return None
    try:
        authored = _expand_commands(api, commands, _without_captures(ctx))
    except ValueError:  # a template that cannot be filled without the captures
        authored = list(commands)
    return check("\n".join(expanded), "\n".join(authored))


def _refused_once(api: ScriptApi, refusal: str, spoken: set[str]) -> bool:
    """Say why once, and trace it every time."""
    if api.diag is not None:
        api.diag.event(
            "user_rule.command_refused", source=api.source or SOURCE, reason=refusal
        )
    if refusal in spoken:
        return False
    spoken.add(refusal)
    api.speak(f"Automation text not put in the command box: {refusal}.", channel="system")
    return True


def _without_captures(ctx: MatchContext | None) -> MatchContext | None:
    """The same match with every capture empty, so what the player wrote can be read alone."""
    if ctx is None:
        return None
    return MatchContext(
        line=ctx.line,
        wildcards=[""] * len(ctx.wildcards),
        named={name: "" for name in ctx.named},
        engine=ctx.engine,
    )


def deliver(
    api: ScriptApi,
    lines: list[str],
    send_to: str,
    ctx: MatchContext | None = None,
    *,
    variable: str = "",
) -> None:
    """Put the expanded text where the rule says (MUSHclient's "send to" targets).

    The text is one field and the destination another, for triggers, aliases and timers alike:
    never a separate field per destination.
    """
    kind = send_to_kind(send_to)
    if kind == "command":
        # The box holds several lines now, so a stack arrives as a block to finish and send,
        # which is the point of putting it there rather than sending it.
        api.type_text("\n".join(lines), only_if_empty=True)
    elif kind == "output":
        for line in lines:
            api.note(line)
    elif kind == "log":
        for line in lines:
            api.log_line(line)
    elif kind == "variable":
        name = variable.strip()
        if not name:
            return  # register_rules refuses a variable target with no name, so this is unreached
        api.set_var(name, "\n".join(lines))
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


def _seconds(value: object) -> float:
    """A saved number of seconds, whatever JSON handed back; 0 when it is not one."""
    try:
        return float(value)  # type: ignore[arg-type]  # a field read back from a file
    except (TypeError, ValueError):
        return 0.0


def _rule_name(rule: object) -> str:
    """What to call a rule in a spoken complaint: its label, else what it matches or when."""
    label = str(getattr(rule, "label", "") or "").strip()
    if label:
        return label
    pattern = str(getattr(rule, "pattern", "") or "").strip()
    if pattern:
        return pattern
    at = str(getattr(rule, "at", "") or "").strip()
    if at:
        return f"at {at}"
    every = getattr(rule, "every", 0) or 0
    if isinstance(every, (int, float)) and every > 0:
        return f"every {every:g} seconds"
    return "with no label"


def _check_variable_targets(rules: UserRules) -> None:
    """Refuse a rule that stores to a variable without saying which one.

    Before registering anything: a rule that cannot do what it says would otherwise be a rule
    that quietly does nothing, and the reload speaks this instead. Only rules that will actually
    register are checked, so a draft left half-finished cannot stop the working set loading.
    """
    live = [
        *(t for t in rules.triggers if t.enabled and t.pattern),
        *(a for a in rules.aliases if a.enabled and a.pattern),
        *(t for t in rules.timers if t.enabled),
    ]
    for rule in live:
        if not str(rule.send).strip():
            continue  # nothing to store
        if send_to_kind(rule.send_to) == "variable" and not str(rule.variable).strip():
            raise ValueError(
                f"the rule {_rule_name(rule)} stores to a variable but has no variable name"
            )


def _check_timer_offsets(rules: UserRules) -> None:
    """Refuse an offset that is not a head start inside its own interval.

    The offset is subtracted from the wait before the first fire, so an offset at or past the
    interval asks for a fire before the timer starts. Said rather than clamped: the number is
    the player's, and a quiet clamp would hide the typo that made it wrong. A time of day has no
    interval to be inside -- its wait comes from the clock -- and keeps its offset unused.
    """
    for timer in rules.timers:
        if not timer.enabled or not str(timer.send).strip():
            continue  # only what will actually be scheduled
        offset = _seconds(timer.offset)
        interval = _seconds(timer.every)
        if offset < 0:
            raise ValueError(f"the timer {_rule_name(timer)} has a negative offset")
        if offset and interval > 0 and offset >= interval:
            raise ValueError(
                f"the timer {_rule_name(timer)}: the offset ({offset:g} seconds) has to be "
                f"less than the interval ({interval:g} seconds)"
            )


def register_rules(api: ScriptApi, rules: UserRules) -> None:
    """Register every rule on the engine under the ``user`` source.

    ``api.base_dir`` must be the user pack dir so sound paths resolve inside it
    (the builder copies picked files there). Call ``engine.remove_source(SOURCE)``
    first when reloading.
    """
    _check_variable_targets(rules)
    _check_timer_offsets(rules)
    for channel in rules.channels:
        if channel.enabled and channel.name:
            api.set_channel(
                channel.name, speak=channel.speak, display=channel.display,
                interrupt=channel.interrupt,
            )
    # A rule the file says is off is registered anyway, in the off state: a group or a script
    # has to be able to switch it back on, and a rule the engine never heard of cannot be.
    for place, trigger in enumerate(rules.triggers):
        if trigger.pattern:
            _register_trigger(api, trigger, place=place)
    for place, alias in enumerate(rules.aliases):
        if alias.pattern:
            _register_alias(api, alias, place=place)
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


def _register_trigger(api: ScriptApi, t: UserTrigger, *, place: int = 0) -> None:
    gag = t.gag if t.gag in _GAG_CHOICES else "none"
    commands, send_commands = _command_action(
        api, t.send, send_to=t.send_to, variable=t.variable
    )
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
    rule = api.add_trigger(
        pattern,
        fire if has_actions else None,
        regex=regex,
        # A lower order number is tried first, so the engine's higher-priority-first becomes a
        # sign change; equal orders fall back to the pattern, which is MUSHclient's order too.
        # MUSHclient's sequence is 1-10000, lower first, and packs register as -sequence;
        # DEFAULT_ORDER - order puts an unordered player rule at 0, above every pack rule, which
        # is where player rules sat before orders existed.
        priority=DEFAULT_ORDER - int(t.order),
        # Equal orders fall back to the pattern text, then to the rule's place in the file, so two
        # rules matching the same text keep their order across a save and reload.
        tiebreak=f"{t.pattern}\x00{place:04d}",
        # A rule that stops the search stops it for the player's own rules, never for a pack's:
        # stop_scope keeps one player rule from silencing a soundpack. That is what MUSHclient
        # means by stopping evaluation inside one plugin.
        stop_scope=SOURCE,
        keep_evaluating=t.keep_evaluating,
        gag=(gag == "line"),
        gag_but_display=(gag == "speech"),
        omit_from_log=t.omit_log,
        channel=t.channel or None,
        source=SOURCE,
        name=t.label.strip(),
        group=t.group.strip(),
    )
    if rule is not None:
        rule.enabled = t.enabled


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


# Seconds of head start before a daily target counts as "today". asyncio may run a handle a
# few milliseconds early, and without this the timer would decide it is still before today's
# target and fire a second time.
_DAILY_GUARD = 0.5


def at_time(value: str) -> time | None:
    """The wall-clock time an "HH:MM:SS" names, or None if it does not name one."""
    seconds = parse_at(value)
    if seconds is None:
        return None
    hours, rest = divmod(seconds, 3600)
    minutes, leftover = divmod(rest, 60)
    return time(int(hours), int(minutes), int(leftover))


def next_daily_wait(target: time, *, now: datetime | None = None) -> tuple[datetime, float]:
    """The next wall-clock ``target`` and the real seconds until it, clocks changing or not.

    Counted with datetimes rather than seconds since midnight: a timer set for 03:00 has to be
    03:00 on the day the clocks go forward, which is 23 real hours after the last one, and 25
    when they go back. The guard is the other half -- a timer that fires a few milliseconds
    early must not work out that today's target is still ahead and fire again.
    """
    moment = now or datetime.now()
    upcoming = _next_occurrence(target, moment)
    return upcoming, max(_DAILY_GUARD, upcoming.timestamp() - moment.timestamp())


def _next_occurrence(target: time, moment: datetime) -> datetime:
    today = datetime.combine(moment.date(), target)
    if today.timestamp() - moment.timestamp() >= _DAILY_GUARD:
        return today
    return datetime.combine(moment.date() + timedelta(days=1), target)


def seconds_until(target: time, *, now: datetime | None = None) -> float:
    """Seconds until the next ``target``, from :func:`next_daily_wait`."""
    return next_daily_wait(target, now=now)[1]


def timer_name(t: UserTimer, place: int = 0) -> str:
    """The name a timer is registered under: its label, or one made from its schedule.

    The place in the file is part of a made-up name because two timers may keep the same clock --
    two sixty-second timers taking turns is the example -- and a name two timers share leaves one
    of them unreachable and lets the other overwrite it on every reload.
    """
    label = t.label.strip()
    if label:
        return label
    return f"timer {t.at or f'{float(t.every):g}s'} #{place + 1}"


def first_wait(interval: float, offset: object) -> float:
    """How long an interval timer waits before its first fire.

    MUSHclient's offset is a head start *inside* the interval, so the first fire comes that much
    before the interval would be up: "every 120 seconds, offset 60" fires after 60, then 120, 120.
    That is what lets two timers on one interval take turns. The interval itself is unchanged,
    which is why a time of day has no use for it: its wait comes from the clock, not from a start.
    """
    return max(interval - _seconds(offset), 0.0)


def _register_timer(
    api: ScriptApi, t: UserTimer, *, now=datetime.now, place: int = 0
) -> None:


    """Schedule one timer under a name, so a label or a group can switch it later.

    A draft with no label still gets a name of its own (its place in the file), so every timer
    is findable: the manager and the script calls work the same whether the player named it or
    not.
    """
    if not t.enabled or not t.send.strip():
        return
    daily = bool(t.at.strip())
    at = at_time(t.at) if daily else None
    if daily and at is None:
        _timer_refused(api, t, f"the time {t.at!r} is not a time; write 03:00 or 21:15:30")
        return
    if not daily and t.every <= 0:
        _timer_refused(api, t, "an interval timer needs a number of seconds, such as 30")
        return

    def fire() -> None:
        parts = _response_parts(t.send, t.send_to)
        if not parts:
            return
        if send_to_kind(t.send_to) == "world" and not api.is_connected():
            # Nothing to send to. A timer that runs while the world is disconnected can still
            # speak, note, log, store a value or run a script; commands have nowhere to go, and
            # saying so every few seconds would be worse than the silence.
            return
        deliver(api, list(parts), t.send_to, variable=t.variable)

    # A timer that names a time of day has no fixed period -- its wait is "until the next
    # 03:00" -- so it asks the clock each time; an interval timer just repeats.
    repeating_daily = at is not None and not t.one_shot
    repeat = None if t.one_shot or at is not None else float(t.every)
    delay = (
        next_daily_wait(at, now=now())[1] if at is not None else first_wait(t.every, t.offset)
    )
    name = timer_name(t, place)
    api.add_named_timer(
        name,
        delay,
        fire,
        every=repeat,
        next_delay=(lambda: next_daily_wait(at, now=now())[1]) if repeating_daily else None,
        group=t.group.strip(),
        enabled=t.enabled,
        while_disconnected=bool(t.run_disconnected),
    )


def _timer_refused(api: ScriptApi, t: UserTimer, reason: str) -> None:
    """Say why a timer will not run, once per reason, and put it in the diagnostics.

    A timer that saves and never fires is the one failure nobody can hear: there is nothing to
    notice. A hand-edited rules file reaches this, and so does an older file.
    """
    name = t.label.strip() or "a timer"
    detail = f"{name}: {reason}"
    said = getattr(api, "_timer_refusals", None)
    if said is None:
        said = set()
        api._timer_refusals = said  # noqa: SLF001 - one set per session's api
    if api.diag is not None:
        api.diag.event("user_rule.timer_refused", source=api.source or SOURCE, reason=detail)
    if detail in said:
        return
    said.add(detail)
    api.speak(f"Timer not started: {detail}.", channel="system")


def _register_alias(api: ScriptApi, a: UserAlias, *, place: int = 0) -> None:
    commands, send_commands = _command_action(
        api, a.send, send_to=a.send_to, variable=a.variable
    )

    def fire(ctx: MatchContext) -> None:
        sent = send_commands(ctx)
        if a.speak and sent:
            api.speak(_speech(api, a.speak, ctx))

    rule = api.add_alias(
        a.pattern,
        fire,
        regex=a.regex,
        source=SOURCE,
        name=a.label.strip(),
        group=a.group.strip(),
        priority=DEFAULT_ORDER - int(a.order),
        tiebreak=f"{a.pattern}\x00{place:04d}",
        # An alias that stops stops the whole search, pack aliases included, which is what this
        # client did before orders existed: your first matching alias takes the command.
        stop_scope=None,
        keep_evaluating=a.keep_evaluating,
        # An alias that matches takes the command -- what was typed does not also go to the
        # MUD -- whether or not later aliases are allowed to see it.
        consumes=True,
    )
    if rule is not None:
        rule.enabled = a.enabled
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
        api, a.send, send_to=a.send_to, variable=a.variable, carry_words=not places_words
    )

    def fire_with_words(ctx: MatchContext) -> None:
        sent = send_with_words(ctx)
        if a.speak and sent:
            api.speak(_speech(api, a.speak, ctx))

    extra = api.add_alias(
        extra_words_pattern(a.pattern), fire_with_words, regex=True, source=SOURCE,
        priority=EXTRA_WORDS_PRIORITY, fallback=True,
        consumes=True,  # it takes the words it was given, as the alias itself does
    )
    if extra is not None:
        extra.enabled = a.enabled


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

    api.add_data_watch(
        w.name.strip(), fire, only_changes=w.only_changes, group=w.group.strip()
    )


def _register_key(api: ScriptApi, k: UserKey) -> None:
    _, send_commands = _command_action(api, k.send)

    def fire(_ctx: MatchContext) -> None:
        if k.sound:
            api.play(k.sound, channel="user-key")
        if k.speak:
            api.speak(_speech(api, k.speak), channel=_KEY_REPLY_CHANNEL, interrupt=True)
        send_commands()

    api.add_key(k.key, fire, group=k.group.strip())
