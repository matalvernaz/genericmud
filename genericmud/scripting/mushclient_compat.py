"""MUSHclient compatibility: load `<muclient>` XML worlds/plugins + run their Lua.

Parses MUSHclient triggers/aliases and executes their `<script>` CDATA against a
sandboxed Lua runtime whose globals are MUSHclient's API (``Send``,
``DoAfterSpecial``, ``ColourNote``, ``GetVariable``...), backed by the shared
:class:`ScriptApi`. This lets Matt's existing plugins (e.g.
``/home/matt/erion/erion_gathering.xml``) and MUSHclient soundpacks run on the
genericMud engine unchanged.

Scope: covers the API surface real soundpacks/plugins use. A few MUSHclient
semantics are simplified — notably ``DoAfterSpecial`` always runs its deferred
text as Lua (the soundpack-standard "send to script" case) rather than honouring
every sendto code.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.lua_runtime import make_sandboxed_runtime

_WILDCARD_RE = re.compile(r"%(\d)")
_SEND_TO_SCRIPT = "12"
_DEFAULT_TRIGGER_SEQUENCE = "100"


class MushclientPack:
    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._lua = make_sandboxed_runtime()
        self._install_api()

    # --- MUSHclient global API ---

    def _install_api(self) -> None:
        api = self._api
        g = self._lua.globals()
        g.Send = api.send
        g.SendNoEcho = api.send
        g.Execute = api.send
        g.Note = api.echo
        g.ColourNote = self._colour_note
        g.GetVariable = api.get_var
        g.SetVariable = api.set_var
        g.DeleteVariable = lambda name: api.set_var(name, "")
        g.EnableTrigger = lambda *args: None
        g.EnableAlias = lambda *args: None
        g.EnableTimer = lambda *args: None
        g.Hyperlink = lambda *args: None
        g.GetSoundKeyword = lambda *args: ""
        g.PlaySound = self._play_sound
        g.DoAfterSpecial = self._do_after_special

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

    def _do_after_special(self, delay: float, code: str, sendto: object = _SEND_TO_SCRIPT) -> None:
        deferred = self._compile(str(code))
        self._api.add_timer(float(delay), deferred)

    def _compile(self, code: str):
        """Host-side compile of a Lua chunk into a zero-arg callable."""
        return self._lua.eval(f"function()\n{code}\nend")

    # --- loading ---

    def load_file(self, path: str) -> None:
        with open(path, encoding="utf-8") as handle:
            self.load_source(handle.read())

    def load_source(self, xml: str) -> None:
        root = ET.fromstring(xml)
        script = "\n".join((el.text or "") for el in root.iter("script"))
        if script.strip():
            self._lua.execute(script)
        for element in root.iter("trigger"):
            self._register(element, is_alias=False)
        for element in root.iter("alias"):
            self._register(element, is_alias=True)

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
                    handler(name, ctx.line.plain_text, lua.table_from(ctx.wildcards[1:]))

            return call_named

        send_element = element.find("send")
        body = (send_element.text or "") if send_element is not None else ""
        if body.strip():
            if attrs.get("send_to", "0") == _SEND_TO_SCRIPT:
                compiled = self._compile(body)

                def call_script(_ctx: MatchContext) -> None:
                    compiled()

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
