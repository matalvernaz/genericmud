"""MUSHclient importer tests: a hermetic world + the real Erion plugin."""

from __future__ import annotations

import os

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from tests.helpers import RecordingSink

INLINE = """<?xml version="1.0"?>
<muclient>
<triggers>
 <trigger match="You are hit by * for * damage" enabled="y" regexp="n"
  script="on_hit" sequence="50"></trigger>
 <trigger match="ping" enabled="y" regexp="n" send_to="12"><send>Send("pong")</send></trigger>
 <trigger match="autolook" enabled="y" regexp="n" send_to="0"><send>look</send></trigger>
</triggers>
<aliases>
 <alias match="^kk$" enabled="y" regexp="y" script="do_kk"></alias>
</aliases>
<script><![CDATA[
function on_hit(name, line, wildcards) Send("ouch " .. wildcards[2]) end
function do_kk(name, line, wildcards) Send("kill kobold") end
]]></script>
</muclient>"""

ERION = "/home/matt/erion/erion_gathering.xml"

# Real packs (mudsoundpack.com) use Sound() not PlaySound(), build paths with
# GetInfo(67), and call through the world object — exercise all three.
SOUNDS = """<?xml version="1.0"?>
<muclient><triggers>
 <trigger match="boom" enabled="y" regexp="n" send_to="12">
  <send>Sound("boom.wav")</send></trigger>
 <trigger match="hush" enabled="y" regexp="n" send_to="12">
  <send>Sound("volume=0")</send></trigger>
 <trigger match="ding" enabled="y" regexp="n" send_to="12">
  <send>world.Sound("ding.wav")</send></trigger>
 <trigger match="local" enabled="y" regexp="n" send_to="12">
  <send>Sound(GetInfo(67) .. "/snd/x.ogg")</send></trigger>
</triggers></muclient>"""


def _load(xml: str, base_dir: str | None = None) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(ScriptApi(engine, source="mushclient", base_dir=base_dir)).load_source(xml)
    return sink, engine


def test_named_script_trigger_with_wildcards():
    sink, engine = _load(INLINE)
    engine.process_line(Line("You are hit by a goblin for 7 damage"))
    assert "ouch 7" in sink.sent


def test_inline_script_send():
    sink, engine = _load(INLINE)
    engine.process_line(Line("ping"))
    assert "pong" in sink.sent


def test_inline_world_send():
    sink, engine = _load(INLINE)
    engine.process_line(Line("autolook"))
    assert "look" in sink.sent


def test_alias_named_script_consumes_input():
    sink, engine = _load(INLINE)
    assert engine.process_input("kk") == []
    assert "kill kobold" in sink.sent


PPI_PLUGIN = """<?xml version="1.0"?>
<muclient><plugin name="audio" id="audio"/>
<script><![CDATA[
local ppi = require "ppi"
function play(file) Sound(file) end
ppi.Expose("play")
SomeUnimplementedHostFunc()  -- permissive fallback must no-op, not crash the load
]]></script></muclient>"""


def test_ppi_shim_exposes_and_permissive_globals_no_op():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir="/tmp"))
    pack.load_source(PPI_PLUGIN)  # loads despite the unimplemented host call
    assert "play" in pack._exposed["audio"]  # exposed under its own plugin id


def test_include_pulls_in_plugin(tmp_path):
    # A world that <include>s a separate plugin file: the plugin must load on the shared
    # runtime and its trigger must fire. Guards _load_included + the Path import.
    (tmp_path / "audio.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<muclient><plugin name="audio" id="audiopack"/>\n'
        '<triggers><trigger match="boom" enabled="y" regexp="n" send_to="12">'
        '<send>Sound("boom.wav")</send></trigger></triggers>\n'
        '<script><![CDATA[ local ppi = require "ppi"'
        ' function play(f) Sound(f) end ppi.Expose("play") ]]></script>'
        "</muclient>",
        encoding="utf-8",
    )
    (tmp_path / "world.xml").write_text(
        '<?xml version="1.0"?>\n<muclient><include name="audio.xml"/></muclient>',
        encoding="utf-8",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="mushclient", base_dir=str(tmp_path)))
    pack.load_file(str(tmp_path / "world.xml"))
    engine.process_line(Line("boom"))
    # Sound() resolves a relative path against the pack dir (base_dir).
    assert any(played["file"].endswith("boom.wav") for played in sink.played)
    assert "play" in pack._exposed["audiopack"]  # included plugin exposed under its id


def test_require_of_a_nil_module_is_nil_not_the_black_hole(tmp_path):
    # A required lib that returns nothing must yield nil, not the permissive black-hole
    # table _G's metatable would hand back (the rawget guard in _require).
    (tmp_path / "empty.lua").write_text("-- returns nothing\n", encoding="latin-1")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path))).load_source(
        "<muclient><script><![CDATA[\n"
        'local m = require("empty")\n'
        'if m == nil then Send("got-nil") else Send("got-blackhole") end\n'
        "]]></script></muclient>"
    )
    assert "got-nil" in sink.sent


def test_full_stdlib_keeps_stdlib_but_closes_escape_hatches(tmp_path):
    # Trusted packs get the Lua stdlib (os/io/loadstring/debug.traceback) but not the
    # escape hatches: package.loadlib, debug.getregistry, debug.sethook are gone.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(
        ScriptApi(engine, source="m", base_dir=str(tmp_path)), full_stdlib=True
    ).load_source(
        "<muclient><script><![CDATA[\n"
        'if os and io and loadstring and debug and debug.traceback then Send("stdlib") end\n'
        'if package.loadlib == nil then Send("no-loadlib") end\n'
        'if debug.getregistry == nil then Send("no-getregistry") end\n'
        'if debug.sethook == nil then Send("no-sethook") end\n'
        "]]></script></muclient>"
    )
    assert {"stdlib", "no-loadlib", "no-getregistry", "no-sethook"} <= set(sink.sent)


def test_send_to_script_substitutes_wildcards():
    # MUSHclient send-to-script (send_to=12) substitutes %1.. into the script text before
    # running it; a bare %1 (Repeat_Command's "for i=1,%1") must not break compilation.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(ScriptApi(engine, source="rc")).load_source(
        "<muclient><aliases>"
        '<alias match="^rep (\\d+) (.*)$" enabled="y" regexp="y" send_to="12">'
        '<send>for i = 1, %1 do Send("%2") end</send></alias>'
        "</aliases></muclient>"
    )
    engine.process_input("rep 3 jump")
    assert sink.sent == ["jump", "jump", "jump"]


def test_doctype_entities_are_expanded():
    # MUSHclient plugins declare config in a DOCTYPE internal subset and reference it as
    # &name;. The DOCTYPE must survive load_source so ElementTree expands the entities.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(ScriptApi(engine, source="m")).load_source(
        '<?xml version="1.0" encoding="iso-8859-1"?>\n'
        "<!DOCTYPE muclient [\n"
        '  <!ENTITY cue "boom.wav">\n'
        "]>\n"
        "<muclient><triggers>"
        '<trigger match="boom" enabled="y" send_to="12"><send>Sound("&cue;")</send></trigger>'
        "</triggers></muclient>"
    )
    engine.process_line(Line("boom"))
    assert any(p["file"] == "boom.wav" for p in sink.played)


def test_malformed_included_plugin_does_not_sink_the_pack(tmp_path):
    # One unparseable <include>d plugin is skipped (recorded), not allowed to abort the world.
    (tmp_path / "good.xml").write_text(
        "<muclient><triggers>"
        '<trigger match="ping" enabled="y" send_to="12"><send>Send("pong")</send></trigger>'
        "</triggers></muclient>",
        encoding="latin-1",
    )
    (tmp_path / "bad.xml").write_text("<muclient>& not well formed</muclient>", encoding="latin-1")
    (tmp_path / "world.MCL").write_text(
        '<muclient><include name="good.xml"/><include name="bad.xml"/></muclient>',
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(tmp_path / "world.MCL"))  # must not raise
    engine.process_line(Line("ping"))
    assert "pong" in sink.sent  # the good plugin loaded despite the bad sibling
    assert any(name == "bad.xml" for name, _ in pack._include_errors)


def test_sound_plays_file():
    sink, engine = _load(SOUNDS)
    engine.process_line(Line("boom"))
    assert any(played["file"] == "boom.wav" for played in sink.played)


def test_sound_volume_zero_stops():
    sink, engine = _load(SOUNDS)
    engine.process_line(Line("hush"))
    assert "sound" in sink.stopped


def test_world_sound_plays():
    sink, engine = _load(SOUNDS)
    engine.process_line(Line("ding"))
    assert any(played["file"] == "ding.wav" for played in sink.played)


def test_get_info_resolves_sound_path():
    sink, engine = _load(SOUNDS, base_dir="/packs/demo")
    engine.process_line(Line("local"))
    assert any(played["file"] == "/packs/demo/snd/x.ogg" for played in sink.played)


@pytest.mark.skipif(not os.path.exists(ERION), reason="erion plugin not present")
def test_real_erion_plugin_end_to_end():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(ScriptApi(engine, source="mushclient")).load_file(ERION)

    # /gather mining: consumes input, sets mode, sends "mine cluster", colour-notes.
    assert engine.process_input("/gather mining") == []
    assert "mine cluster" in sink.sent
    assert any("Mining started" in text for text, _channel in sink.echoed)

    # Debris trigger clears it.
    engine.process_line(Line("Dirt and rock tumble over the cluster"))
    assert "clear debris" in sink.sent

    # Cluster complete schedules the next mine via DoAfterSpecial.
    sink.sent.clear()
    engine.process_line(Line("The cluster breaks apart."))
    sink.run_pending()
    assert "mine cluster" in sink.sent
