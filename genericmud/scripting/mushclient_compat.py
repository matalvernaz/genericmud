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

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.guard import ScriptGuard
from genericmud.scripting.lua_runtime import install_pack_require, make_sandboxed_runtime

_WILDCARD_RE = re.compile(r"%(\d)")
_SEND_TO_SCRIPT = "12"
_DEFAULT_TRIGGER_SEQUENCE = "100"

_SOUND_CHANNEL = "sound"  # MUSHclient Sound() is a single-voice channel
_VOLUME_MAX = 100.0  # MUSHclient volume is 0..100
# GetInfo() directory codes (app/config/world/plugin dirs). A genericMud pack bundles
# its scripts + sounds under one dir, so every dir code resolves to the pack root.
_DIR_INFO_CODES = frozenset({56, 60, 64, 66, 67})


class MushclientPack:
    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._base_dir = api.base_dir
        self._exposed: dict[str, dict] = {}  # ppi: plugin id -> {exposed name -> Lua fn}
        self._current_plugin = "world"  # whose script is loading now (for ppi.Expose)
        self._loaded_includes: set[str] = set()
        self._lua, install_hook = make_sandboxed_runtime(lua51=True)  # MUSHclient targets Lua 5.1
        self._guard = ScriptGuard(install_hook)
        self._install_api()
        # Hand each plugin our own ppi (its bundled ppi.lua needs package.seeall, which the
        # sandbox strips). Then make any still-unimplemented host name a "black hole" that is
        # callable AND indexable (returns itself) -- so Window/InfoBox/etc. we don't implement
        # no-op (even Foo.bar.baz()) and the plugin loads + its sound path runs.
        install_pack_require(self._lua, self._base_dir, builtins={"ppi": self._make_ppi()})
        self._lua.execute(
            "local bh = setmetatable({}, {__call=function() end, "
            "__index=function(t) return t end});"
            "setmetatable(_G, { __index = function() return bh end })"
        )

    def _make_ppi(self):
        """A minimal in-process ppi (plugin-to-plugin interface): Expose registers a
        function under the loading plugin; Load returns a plugin's exposed functions."""
        ppi = self._lua.table()
        ppi.Expose = self._ppi_expose
        ppi.Load = self._ppi_load
        return ppi

    def _ppi_expose(self, name: object = "", fn: object = None) -> None:
        key = str(name)
        functions = self._exposed.setdefault(self._current_plugin, {})
        functions[key] = fn if fn is not None else self._lua.globals()[key]

    def _ppi_load(self, plugin_id: object = None):
        return self._lua.table_from(self._exposed.get(str(plugin_id), {}))

    # --- MUSHclient global API ---

    def _install_api(self) -> None:
        api = self._api
        funcs = {
            "Send": api.send,
            "SendNoEcho": api.send,
            "Execute": api.send,
            "Note": api.echo,
            "ColourNote": self._colour_note,
            "GetVariable": api.get_var,
            "SetVariable": api.set_var,
            "DeleteVariable": lambda name: api.set_var(name, ""),
            "EnableTrigger": lambda *_a: None,
            "EnableAlias": lambda *_a: None,
            "EnableTimer": lambda *_a: None,
            "Hyperlink": lambda *_a: None,
            "GetSoundKeyword": lambda *_a: "",
            "PlaySound": self._play_sound,
            "Sound": self._sound,
            "GetInfo": self._get_info,
            "DoAfterSpecial": self._do_after_special,
        }
        g = self._lua.globals()
        for name, fn in funcs.items():
            g[name] = fn
        # Packs call both bare (Sound(...)) and through the world object
        # (world.Sound(...), world.getvariable(...)). Mirror funcs onto a world
        # table, with lowercase aliases for the world.lowercase() callers.
        world = self._lua.table()
        for name, fn in funcs.items():
            world[name] = fn
            world[name.lower()] = fn
        g.world = world

    def _colour_note(self, *args: object) -> None:
        # ColourNote(fg, bg, text [, fg, bg, text]...) — concatenate the text parts.
        texts = [str(args[i]) for i in range(2, len(args), 3)]
        self._api.echo("".join(texts))

    def _play_sound(
        self,
        buffer: object = 0,
        file: str = "",
        loop: object = False,
        volume: object = 100,
        pan: object = 0,
    ) -> None:
        self._api.play(str(file), loop=bool(loop))

    def _sound(self, arg: object = "", *_rest: object) -> None:
        """MUSHclient ``Sound``: a path plays it; a ``key=value`` string is a control
        directive (``volume=``/``pan=``/``freq=``) for the current cue."""
        text = str(arg)
        if "=" in text:
            self._sound_control(text)
        elif text:
            self._api.play(text, channel=_SOUND_CHANNEL)

    def _sound_control(self, directive: str) -> None:
        key, _, raw = directive.partition("=")
        if key.strip().lower() != "volume":
            return  # pan/freq: no live per-cue control in the bus yet — accept, ignore
        try:
            level = float(raw)
        except ValueError:
            return
        if level <= 0:
            self._api.stop(_SOUND_CHANNEL)  # "volume=0" is the soundpack idiom for stop
        else:
            self._api.set_volume(_SOUND_CHANNEL, level / _VOLUME_MAX)

    def _get_info(self, code: object = 0) -> str:
        """MUSHclient ``GetInfo``: the pack dir for directory codes, else ``""``.

        Packs build sound paths as ``GetInfo(67).."/sounds/x.ogg"``; returning the
        pack root makes those resolve against the bundled files.
        """
        try:
            number = int(code)
        except (TypeError, ValueError):
            return ""
        return (self._base_dir or "") if number in _DIR_INFO_CODES else ""

    def _do_after_special(self, delay: float, code: str, sendto: object = _SEND_TO_SCRIPT) -> None:
        deferred = self._compile(str(code))
        self._api.add_timer(float(delay), lambda: self._guard.run(deferred))

    def _compile(self, code: str):
        """Host-side compile of a Lua chunk into a zero-arg callable."""
        return self._lua.eval(f"function()\n{code}\nend")

    # --- loading ---

    def load_file(self, path: str) -> None:
        with open(path, encoding="utf-8") as handle:
            self.load_source(handle.read())

    def load_source(self, xml: str) -> None:
        # Strip the XML + DOCTYPE declarations: .MCL world files carry an encoding decl
        # (ElementTree rejects that on a str) and a <!DOCTYPE muclient> it can't resolve.
        xml = re.sub(r"<\?xml[^>]*\?>", "", xml)
        xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml)
        self._load_plugin(ET.fromstring(xml))

    def _load_plugin(self, root: ET.Element) -> None:
        """Run one plugin/world's script + triggers; a world (<include>s) pulls in its
        plugins so they share this runtime and can ppi-message each other."""
        plugin = next(root.iter("plugin"), None)
        previous = self._current_plugin
        self._current_plugin = (plugin.get("id") if plugin is not None else "") or "world"
        script = "\n".join((el.text or "") for el in root.iter("script"))
        if script.strip():
            self._guard.run_strict(self._lua.execute, script)
        for element in root.iter("trigger"):
            self._register(element, is_alias=False)
        for element in root.iter("alias"):
            self._register(element, is_alias=True)
        self._current_plugin = previous
        for include in root.iter("include"):
            name = include.get("name")
            if name:
                self._load_included(name)

    def _load_included(self, filename: str) -> None:
        if not self._base_dir or filename in self._loaded_includes:
            return
        self._loaded_includes.add(filename)  # each plugin loads once
        matches = sorted(Path(self._base_dir).rglob(filename))  # layouts vary -> match by name
        if matches:
            self.load_source(matches[0].read_text(encoding="latin-1", errors="ignore"))

    def _register(self, element: ET.Element, *, is_alias: bool) -> None:
        attrs = element.attrib
        if attrs.get("enabled", "y") != "y":
            return
        pattern = attrs.get("match", "")
        regex = attrs.get("regexp", "n") == "y"
        priority = -int(attrs.get("sequence", _DEFAULT_TRIGGER_SEQUENCE))
        keep_default = "n" if is_alias else "y"  # aliases consume by default
        keep = attrs.get("keep_evaluating", keep_default) == "y"
        callback = self._make_callback(element, attrs)
        if is_alias:
            self._api.add_alias(
                pattern, callback, regex=regex, priority=priority, keep_evaluating=keep
            )
        else:
            self._api.add_trigger(
                pattern, callback, regex=regex, priority=priority, keep_evaluating=keep
            )

    def _make_callback(self, element: ET.Element, attrs: dict[str, str]):
        lua = self._lua
        api = self._api
        name = attrs.get("name", "")

        script_name = attrs.get("script")
        if script_name:
            handler = lua.globals()[script_name]

            def call_named(ctx: MatchContext) -> None:
                if handler is not None:
                    wildcards = lua.table_from(ctx.wildcards[1:])
                    self._guard.run(handler, name, ctx.line.plain_text, wildcards)

            return call_named

        send_element = element.find("send")
        body = (send_element.text or "") if send_element is not None else ""
        if body.strip():
            if attrs.get("send_to", "0") == _SEND_TO_SCRIPT:
                compiled = self._compile(body)

                def call_script(_ctx: MatchContext) -> None:
                    self._guard.run(compiled)

                return call_script

            def call_send(ctx: MatchContext) -> None:
                api.send(_substitute(body, ctx.wildcards))

            return call_send

        return lambda _ctx: None


def _substitute(text: str, wildcards: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return wildcards[index] if index < len(wildcards) else ""

    return _WILDCARD_RE.sub(replace, text)
