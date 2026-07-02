"""MUSHclient importer tests: a hermetic world + the real Erion plugin."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from tests.helpers import RecordingSink


def test_loads_despite_regex_attr_and_unresolvable_require(tmp_path):
    # Two things that used to abort a real MUSHclient pack at load: a regex named group in an
    # attribute (raw '<' -> ElementTree ParseError) and a require of a stdlib/native module with
    # no pack file ("string" / "socket.core"). Both must now degrade, not kill the plugin.
    world = (
        "<muclient><script><![CDATA[\n"
        'local _ = require "string"\n'  # stdlib: resolves to the real library
        'require "socket.core"\n'  # native module, no pack file: black-holed, must not error
        "function bonk() Send('ouch') end\n"
        "]]></script>\n"
        '<triggers><trigger match="(?P<who>\\w+) bonks you" enabled="y" regexp="y"'
        ' script="bonk" sequence="50"/></triggers></muclient>'
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="p", base_dir=str(tmp_path)), full_stdlib=True)
    pack.load_source(world)  # neither the raw '<' nor the unresolvable require aborts the load
    engine.process_line(Line("Goblin bonks you"))
    assert sink.sent == ["ouch"]  # plugin loaded and the named-group regex trigger fired

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


def test_resolve_keeps_forward_slashes_and_collapses_doubles():
    # GetInfo() builds ".../worlds/".."/sounds/x" with a doubled slash; _resolve must collapse
    # it to a single FORWARD slash on every OS. os.path.normpath would flip / to \ on Windows
    # -- the dev host is Linux so only the Windows CI catches that; this pins the contract.
    api = ScriptApi(AutomationEngine(RecordingSink()), base_dir="/p")
    assert api._resolve("/p/sounds//x.ogg") == "/p/sounds/x.ogg"


def test_get_info_anchors_on_the_world_dir_not_pack_root(tmp_path):
    # Erion's layout: the world + sounds are nested under the pack (base_dir), not at its
    # root. GetInfo(67) must return the WORLD file's dir (with a trailing slash, so a plugin
    # that appends "sounds/.." with no leading slash still resolves beside the world).
    worlds = tmp_path / "MUSHclient" / "worlds"
    worlds.mkdir(parents=True)
    (worlds / "w.MCL").write_text(
        "<muclient><triggers>"
        '<trigger match="boom" enabled="y" send_to="12">'
        '<send>Sound(GetInfo(67).."sounds/boom.wav")</send></trigger>'
        "</triggers></muclient>",
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    # base_dir is the PACK ROOT (where require resolves libs); the world is nested below it.
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(worlds / "w.MCL"))
    engine.process_line(Line("boom"))
    assert sink.played, "no sound played"
    played = sink.played[0]["file"].replace("\\", "/")  # normalize separators for a portable check
    assert played.endswith("MUSHclient/worlds/sounds/boom.wav")  # beside the world, not the root
    assert "//" not in played  # the doubled-slash join was collapsed


def test_sppath_defaults_to_pack_dir_for_the_sounds_fallback(tmp_path):
    # The fix for the Erion installer case (loaded with @sppath=''): default @sppath to the pack
    # dir so _find_in_sounds_dir has somewhere to walk, mirroring the VIPMud default.
    engine = AutomationEngine(RecordingSink())
    MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    assert engine.get_var("sppath") == str(tmp_path)


def test_world_sounds_dir_is_not_clobbered_by_the_sppath_default(tmp_path):
    # The session sets @sppath from world.sounds before packs load; the pack must preserve it.
    engine = AutomationEngine(RecordingSink())
    engine.set_var("sppath", "/my/sounds")
    MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    assert engine.get_var("sppath") == "/my/sounds"


def test_sppath_fallback_finds_a_sound_the_world_anchored_path_misses(tmp_path):
    # Erion's real failure: cues build GetInfo(67).."sounds/.." (beside the world), but the file
    # lives in a SEPARATE sounds tree under the pack. With @sppath defaulted to the pack dir, the
    # basename fallback (_find_in_sounds_dir) locates it where the world-anchored path missed.
    worlds = tmp_path / "MUSHclient" / "worlds"
    worlds.mkdir(parents=True)
    (worlds / "w.MCL").write_text(
        "<muclient><triggers>"
        '<trigger match="boom" enabled="y" send_to="12">'
        '<send>Sound(GetInfo(67).."sounds/boom.wav")</send></trigger>'
        "</triggers></muclient>",
        encoding="latin-1",
    )
    real_sound = tmp_path / "MUSHclient" / "sounds" / "boom.wav"  # a separate tree, not beside the world
    real_sound.parent.mkdir(parents=True)
    real_sound.write_bytes(b"RIFF")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(worlds / "w.MCL"))
    engine.process_line(Line("boom"))
    assert sink.played, "no sound played"
    assert sink.played[0]["file"] == str(real_sound)


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


def test_regex_attr_and_native_require_do_not_kill_the_plugin(tmp_path):
    # Two things that used to abort a real MUSHclient pack at load: a regex named group in an
    # attribute (the raw "<" is illegal XML -> ParseError) and a require of a stdlib/native
    # module with no pack file ("string"/"socket.core" -> module-not-found). Both must now
    # degrade (sanitise the attr; resolve stdlib; black-hole the native module), not kill it.
    world = (
        "<muclient><script><![CDATA[\n"
        'local s = require "string"\n'  # stdlib -> the real library
        'require "socket.core"\n'  # native, no pack file -> black-holed, must not raise
        'function bonk() Send("ouch") end\n'
        "]]></script>\n"
        "<triggers>\n"
        ' <trigger match="(?P<who>\\w+) bonks you" enabled="y" regexp="y"\n'
        '  script="bonk" sequence="50"/>\n'
        "</triggers></muclient>"
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="p", base_dir=str(tmp_path)), full_stdlib=True)
    pack.load_source(world)  # must not raise (ParseError) or abort on the requires
    engine.process_line(Line("Goblin bonks you"))
    assert sink.sent == ["ouch"]  # the named-group trigger registered and fired


def test_trusted_pack_resolves_and_plays_a_getinfo_anchored_sound(tmp_path):
    """The Erion 'no sound' case: once trusted and loaded, a Sound(GetInfo(67).."sounds/x") cue
    must resolve to the bundled file and play. @sppath defaults to the pack dir, so resolution
    works even though the pack hardcodes a world-relative path. (In 0.6.1 the pack never got this
    far -- it was skipped as untrusted; the fix is to let the user trust it at setup.)"""
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "hit.wav").write_bytes(b"RIFFfake")
    world_file = tmp_path / "erion.mcl"
    world_file.write_text(
        '<?xml version="1.0"?>\n'
        '<muclient><world site="erionmud.com" port="1234" name="Erion"/>\n'
        '<triggers><trigger enabled="y" match="You are hit" send_to="12" sequence="100">\n'
        '<send>Sound(GetInfo(67) .. "sounds/hit.wav")</send>\n'
        "</trigger></triggers></muclient>\n",
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(
        ScriptApi(engine, source="erion", base_dir=str(tmp_path)), full_stdlib=True
    ).load_file(str(world_file))
    assert engine.get_var("sppath") == str(tmp_path)  # sppath defaulted to the pack dir
    engine.process_line(Line("You are hit hard!"))
    assert len(sink.played) == 1
    # Compare as Path, not string: the engine builds sound paths with forward slashes on every OS
    # (it deliberately avoids os.path.normpath), so an os.path.join here would mismatch on Windows.
    assert Path(sink.played[0]["file"]) == tmp_path / "sounds" / "hit.wav"
    assert os.path.exists(sink.played[0]["file"])


def _erion_like_pack(root):
    """A minimal Erion-shaped pack: an audio-engine plugin exposing play() -> audio.play(),
    and a dispatcher that reaches it via ppi (MSDP -> ppi.Load -> LuaAudio -> audio.play)."""
    (root / "sounds").mkdir()
    (root / "sounds" / "hit.ogg").write_bytes(b"OggS-fake")
    (root / "engine.xml").write_text(
        '<muclient><plugin id="aud123"/><script><![CDATA[\n'
        'local ppi = require "ppi"\n'
        'ppi.Expose("play", function(f, loop, pan, vol) return audio.play(f, loop, pan, vol) end)\n'
        "]]></script></muclient>",
        encoding="latin-1",
    )
    (root / "dispatch.xml").write_text(
        '<muclient><plugin id="disp"/><script><![CDATA[\n'
        'local PPI = require "ppi"\n'
        'local snd = PPI.Load("aud123")\n'
        'function boom() snd.play(GetInfo(67) .. "sounds/hit.ogg", 0, 0, 80) end\n'
        "]]></script>"
        '<triggers><trigger enabled="y" match="You are hit" send_to="12" script="boom"'
        ' sequence="50"/></triggers></muclient>',
        encoding="latin-1",
    )
    world = root / "w.mcl"
    world.write_text(
        '<?xml version="1.0"?><muclient>'
        '<world site="erionmud.com" port="1234" name="Erion"/>'
        '<include name="engine.xml"/><include name="dispatch.xml"/></muclient>',
        encoding="latin-1",
    )
    return world


def test_audio_play_via_ppi_chain_reaches_the_sink(tmp_path):
    """The Erion 'triggers fire but nothing plays' bug: game cues route through audio.play()
    (bass), not Sound(), and reach it via ppi. gm must shim audio.play onto the ScriptApi or the
    cue is swallowed by the black-hole even though the pack loads (MSDP -> ppi -> LuaAudio)."""
    world = _erion_like_pack(tmp_path)
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(
        ScriptApi(engine, source="erion", base_dir=str(tmp_path)), full_stdlib=True
    ).load_file(str(world))
    engine.process_line(Line("You are hit for 10 damage"))
    assert len(sink.played) == 1
    assert Path(sink.played[0]["file"]) == tmp_path / "sounds" / "hit.ogg"
    assert sink.played[0]["gain"] == 0.8  # vol 80 -> gain 0.8
    assert sink.played[0]["loop"] is False


def test_audio_shim_loop_and_stop(tmp_path):
    """audio.play(file, 1) loops (music); audio.stop(id) stops that cue's channel."""
    (tmp_path / "m.ogg").write_bytes(b"OggS")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(
        ScriptApi(engine, source="erion", base_dir=str(tmp_path)), full_stdlib=True
    )
    pack.load_source(
        '<muclient><plugin id="p"/><script><![CDATA[\n'
        'audio.play(GetInfo(67) .. "m.ogg", 1)\n'  # explicit 1 -> looped
        'local id = audio.play(GetInfo(67) .. "m.ogg", 0)\n'  # one-shot, capture its id
        "audio.stop(id)\n"  # stop that specific cue -> api.stop(channel)
        "]]></script></muclient>"
    )
    assert sink.played[0]["loop"] is True
    assert sink.played[1]["loop"] is False
    assert sink.stopped == ["erion-audio-2"]  # the second cue's channel, stopped by id
