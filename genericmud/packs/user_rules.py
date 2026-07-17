"""User-authored rules: the no-code soundpack builder's storage and engine layer.

The wx builder dialogs edit a plain JSON file (one per world, under
``genericmud-data/userpacks/<world>/rules.json``); this module owns the schema,
load/save, and registration onto the shared :class:`AutomationEngine` via
:class:`ScriptApi` -- the same surface the scripting dialects use, so a
dialog-made trigger has the full power of a scripted one: wildcard or regex
patterns, a sound cue (with volume/pan/loop), spoken text and sent commands with
``%1``-style captures, speech-gagging or removing the matched line, and routing
to a channel whose policy (speak/display/interrupt) is itself user-defined.

Everything here is headless (no wx), so the whole builder core is testable on
the build-blind dev host; the dialogs are a thin shell over ``save()`` +
``EngineApp.reload_user_rules()``.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi

SOURCE = "user"  # registration source: remove_source(SOURCE) clears rules for reload
RULES_FILENAME = "rules.json"
SOUNDS_DIRNAME = "sounds"  # picked sound files are copied here (keeps the pack portable)

_CAPTURE_RE = re.compile(r"%(\d)")
_GAG_CHOICES = ("none", "speech", "line")
# How a trigger pattern matches a line. "contains" and "exact" are newbie-facing
# sugar over regex (escaped literal, searched / anchored); "wildcard" is * and ?.
MATCH_CHOICES = ("contains", "wildcard", "exact", "regex")


@dataclass
class UserTrigger:
    pattern: str = ""
    regex: bool = False  # kept in sync with match for files older builds read
    sound: str = ""  # pack-relative path ("sounds/x.ogg"); "" = no cue
    volume: int = 100  # 0..100
    pan: int = 0  # -100 (left) .. 100 (right)
    loop: bool = False
    speak: str = ""  # spoken text; %1..%9 substitute captures
    send: str = ""  # command sent to the MUD; %1..%9 substitute captures
    gag: str = "none"  # "none" | "speech" (silent but shown) | "line" (removed)
    channel: str = ""  # route the line to this channel ("" = leave on main)
    stop_channel: str = ""  # stop this user cue channel when fired ("" = none)
    match: str = ""  # one of MATCH_CHOICES; "" = legacy file (the regex flag decides)
    interrupt: bool = False  # cut current speech the moment this fires

    def match_kind(self) -> str:
        if self.match in MATCH_CHOICES:
            return self.match
        return "regex" if self.regex else "wildcard"


@dataclass
class UserAlias:
    pattern: str = ""  # what the user types; * ? wildcards unless regex
    regex: bool = False
    send: str = ""  # what goes to the MUD; %1..%9 substitute captures
    speak: str = ""  # optional confirmation speech


@dataclass
class UserKey:
    key: str = ""  # keymap combo, e.g. "ctrl+h", "alt+shift+f2"
    send: str = ""
    speak: str = ""
    sound: str = ""  # pack-relative one-shot cue


@dataclass
class UserChannel:
    name: str = ""
    speak: bool = True
    display: bool = True
    interrupt: bool = False


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
    pack_dir.mkdir(parents=True, exist_ok=True)
    rules_path(pack_dir).write_text(rules.to_json(), encoding="utf-8")


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


def register_rules(api: ScriptApi, rules: UserRules) -> None:
    """Register every rule on the engine under the ``user`` source.

    ``api.base_dir`` must be the user pack dir so sound paths resolve inside it
    (the builder copies picked files there). Call ``engine.remove_source(SOURCE)``
    first when reloading.
    """
    for channel in rules.channels:
        if channel.name:
            api.set_channel(
                channel.name, speak=channel.speak, display=channel.display,
                interrupt=channel.interrupt,
            )
    for trigger in rules.triggers:
        if trigger.pattern:
            _register_trigger(api, trigger)
    for alias in rules.aliases:
        if alias.pattern:
            _register_alias(api, alias)
    for key in rules.keys:
        if key.key:
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
    has_actions = bool(t.sound or t.speak or t.send or t.stop_channel or t.interrupt)

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
                _substitute(t.speak, ctx.wildcards),
                channel=t.channel or "main",
                interrupt=t.interrupt,
            )
        if t.send:
            api.send(_substitute(t.send, ctx.wildcards))

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


def _register_alias(api: ScriptApi, a: UserAlias) -> None:
    def fire(ctx: MatchContext) -> None:
        if a.send:
            api.send(_substitute(a.send, ctx.wildcards))
        if a.speak:
            api.speak(_substitute(a.speak, ctx.wildcards))

    api.add_alias(a.pattern, fire, regex=a.regex, source=SOURCE)


def _register_key(api: ScriptApi, k: UserKey) -> None:
    def fire(_ctx: MatchContext) -> None:
        if k.sound:
            api.play(k.sound, channel="user-key")
        if k.speak:
            api.speak(k.speak)
        if k.send:
            api.send(k.send)

    api.add_key(k.key, fire)
