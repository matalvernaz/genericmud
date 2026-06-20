"""All three script dialects loaded into one engine, firing on one line stream.

This is the proof of the core requirement: Lua soundpacks and VIPMud .set
soundpacks (and MUSHclient packs) active simultaneously, each firing only on its
own patterns.
"""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.lua_runtime import LuaPackRuntime
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.vipmud_dialect import VipMudPack
from tests.helpers import RecordingSink

_MUSHCLIENT_PACK = (
    '<muclient><triggers>'
    '<trigger match="ping" enabled="y" regexp="n" send_to="12"><send>Send("pong")</send></trigger>'
    "</triggers></muclient>"
)


def test_three_dialects_coexist_on_one_engine():
    sink = RecordingSink()
    engine = AutomationEngine(sink)

    VipMudPack(ScriptApi(engine, source="vipmud")).load_source(
        "#TRIGGER {* hits you} {#play {hit.wav} 80}"
    )
    lua = LuaPackRuntime(ScriptApi(engine, source="lua"))
    lua.run_source('mud.trigger("You see *", function(line, wc) mud.speak("found " .. wc[1]) end)')
    MushclientPack(ScriptApi(engine, source="mushclient")).load_source(_MUSHCLIENT_PACK)

    engine.process_line(Line("A goblin hits you"))  # VIPMud .set
    engine.process_line(Line("You see a dragon"))  # Lua
    engine.process_line(Line("ping"))  # MUSHclient

    assert sink.played and sink.played[0]["file"] == "hit.wav"
    assert ("found a dragon", "main", False) in sink.spoken
    assert "pong" in sink.sent


def test_packs_do_not_cross_fire():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vipmud")).load_source("#TR {only vip} {#say {vip}}")
    LuaPackRuntime(ScriptApi(engine, source="lua")).run_source(
        'mud.trigger("only lua", function() mud.speak("lua") end)'
    )

    engine.process_line(Line("only vip here"))
    assert sink.spoken == [("vip", "main", False)]
