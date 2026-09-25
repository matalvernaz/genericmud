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
from pathlib import Path

from genericmud.automation.engine import MatchContext
from genericmud.config.atomic import atomic_write_text
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import REVIEW_CHANNEL

SOURCE = "user"  # registration source: remove_source(SOURCE) clears rules for reload
RULES_FILENAME = "rules.json"
SOUNDS_DIRNAME = "sounds"  # picked sound files are copied here (keeps the pack portable)

_CAPTURE_RE = re.compile(r"%(\d)")
_GAG_CHOICES = ("none", "speech", "line")
# How a trigger pattern matches a line. "contains" and "exact" are newbie-facing
# sugar over regex (escaped literal, searched / anchored); "wildcard" is * and ?.
MATCH_CHOICES = ("contains", "wildcard", "exact", "regex")
_MAX_COMMANDS_PER_RULE = 100


@dataclass
class UserTrigger:
    pattern: str = ""
    regex: bool = False  # kept in sync with match for files older builds read
    sound: str = ""  # pack-relative path ("sounds/x.ogg"); "" = no cue
    volume: int = 100  # 0..100
    pan: int = 0  # -100 (left) .. 100 (right)
    loop: bool = False
    speak: str = ""  # spoken text; %1 or ${1}, ${script:x}, ${mud:x}
    send: str = ""  # commands, one per line; %1 or ${1}, ${script:x}, ${mud:x}
    gag: str = "none"  # "none" | "speech" (silent but shown) | "line" (removed)
    channel: str = ""  # route the line to this channel ("" = leave on main)
    stop_channel: str = ""  # stop this user cue channel when fired ("" = none)
    match: str = ""  # one of MATCH_CHOICES; "" = legacy file (the regex flag decides)
    interrupt: bool = False  # cut current speech the moment this fires
    enabled: bool = True

    def match_kind(self) -> str:
        if self.match in MATCH_CHOICES:
            return self.match
        return "regex" if self.regex else "wildcard"


@dataclass
class UserAlias:
    pattern: str = ""  # what the user types; * ? wildcards unless regex
    regex: bool = False
    send: str = ""  # replacement commands, one per line; capture/variable templates allowed
    speak: str = ""  # optional speech, same templates; can read a value with no commands
    enabled: bool = True


@dataclass
class UserKey:
    key: str = ""  # keymap combo, e.g. "ctrl+h", "alt+shift+f2"
    send: str = ""
    speak: str = ""  # e.g. "${mud:Char.Vitals.hp} health": a key that reads a value
    sound: str = ""  # pack-relative one-shot cue
    enabled: bool = True


@dataclass
class UserChannel:
    name: str = ""
    speak: bool = True
    display: bool = True
    interrupt: bool = False
    enabled: bool = True


@dataclass
class UserRules:
    triggers: list[UserTrigger] = field(default_factory=list)
    aliases: list[UserAlias] = field(default_factory=list)
    keys: list[UserKey] = field(default_factory=list)
    channels: list[UserChannel] = field(default_factory=list)

    def to_json(self) -> str:
        payload = {"version": 1, **asdict(self)}
        return json.dumps(payload, indent=2, sort_keys=True)

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
            channels=build(UserChannel, data.get("channels")),
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


def _commands(text: str) -> tuple[str, ...]:
    """Non-empty commands from a one-command-per-line field."""
    commands = tuple(line.strip() for line in text.splitlines() if line.strip())
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
    api: ScriptApi, text: str
) -> tuple[tuple[str, ...], Callable[[MatchContext | None], bool]]:
    """A contained command-stack action plus whether the stack has any commands."""
    commands = _commands(text)
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
                api.speak(f"Automation command not sent: {detail}", channel="system")
            return False
        for command in expanded:
            api.send(command)
        return True

    return commands, run


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
    commands, send_commands = _command_action(api, t.send)
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
        channel=t.channel or None,
        source=SOURCE,
    )


# A hotkey's speech answers a key the player just pressed, like a review key does, so it
# goes on the review channel: straight away, not queued behind a screenful of MUD output,
# not coalesced by the main channel's flood governor, and still spoken with self-voice off
# (Ctrl+M). Only hotkeys: keys only ever reach the tab in front. An alias can also fire in a
# background tab -- /to from another tab, mud.send_to, a pack's Execute -- and the review
# channel speaks through the mute that keeps background tabs quiet, so an alias there would
# talk over, and with interrupt cut off, the session the player is actually in.
_KEY_REPLY_CHANNEL = REVIEW_CHANNEL


def _register_alias(api: ScriptApi, a: UserAlias) -> None:
    _, send_commands = _command_action(api, a.send)

    def fire(ctx: MatchContext) -> None:
        sent = send_commands(ctx)
        if a.speak and sent:
            api.speak(_speech(api, a.speak, ctx))

    api.add_alias(a.pattern, fire, regex=a.regex, source=SOURCE)


def _register_key(api: ScriptApi, k: UserKey) -> None:
    _, send_commands = _command_action(api, k.send)

    def fire(_ctx: MatchContext) -> None:
        if k.sound:
            api.play(k.sound, channel="user-key")
        if k.speak:
            api.speak(_speech(api, k.speak), channel=_KEY_REPLY_CHANNEL, interrupt=True)
        send_commands()

    api.add_key(k.key, fire)
