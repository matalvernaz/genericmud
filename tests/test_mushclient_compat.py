"""MUSHclient importer tests: a hermetic world + the real Erion plugin."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.sound.bus import SoundBus
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
ppi.unload()
ppi.init()
ppi.Expose("play")
SomeUnimplementedHostFunc()  -- permissive fallback must no-op, not crash the load
]]></script></muclient>"""


def test_ppi_shim_exposes_and_permissive_globals_no_op():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir="/tmp"))
    pack.load_source(PPI_PLUGIN)  # loads despite the unimplemented host call
    assert "play" in pack._exposed["audio"]  # exposed under its own plugin id


def test_ppi_loads_required_bundled_plugin_by_id(tmp_path):
    (tmp_path / "audio.xml").write_text(
        '<muclient><plugin name="audio" id="audio-id"/><script><![CDATA['
        'local ppi = require("ppi"); function unload() Send("unloaded") end; '
        'ppi.Expose("unload", unload)]]></script></muclient>',
        encoding="latin-1",
    )
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        '<muclient><plugin id="main"/><script><![CDATA['
        'local ppi = require("ppi").Load("audio-id"); ppi.unload()'
        "]]></script></muclient>"
    )
    assert sink.sent == ["unloaded"]
    assert pack._plugin_info["audio-id"]["name"] == "audio"


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
    # escape hatches: debug.getregistry and debug.sethook are gone. package.loadlib does
    # NOT load native code either -- but it's a truthy no-op loader, not nil, so a plugin's
    # `assert(package.loadlib(dll, sym))()` bootstrap runs to completion instead of throwing
    # and aborting OnPluginInstall (the failure that killed Erion's LuaAudio/mushReader).
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(
        ScriptApi(engine, source="m", base_dir=str(tmp_path)), full_stdlib=True
    ).load_source(
        "<muclient><script><![CDATA[\n"
        'if os and io and loadstring and debug and debug.traceback then Send("stdlib") end\n'
        'assert(package.loadlib("audio.dll", "luaopen_audio"))()\n'
        'Send("loadlib-noop-ran")\n'
        'if debug.getregistry == nil then Send("no-getregistry") end\n'
        'if debug.sethook == nil then Send("no-sethook") end\n'
        "]]></script></muclient>"
    )
    assert {"stdlib", "loadlib-noop-ran", "no-getregistry", "no-sethook"} <= set(sink.sent)


def test_full_stdlib_normalizes_legacy_file_read_modes(tmp_path):
    (tmp_path / "help.txt").write_text("first\nsecond\n", encoding="latin-1")
    sink = RecordingSink()
    MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path)),
        full_stdlib=True,
    ).load_source(
        "<muclient><script><![CDATA["
        "local f = io.open(GetInfo(66) .. 'help.txt', 'r'); "
        "local first, second = f:read('l*', 'l*'); f:close(); Send(first .. ':' .. second)"
        "]]></script></muclient>"
    )
    assert sink.sent == ["first:second"]


def test_full_stdlib_anchors_relative_io_to_pack(tmp_path, monkeypatch):
    pack_dir = tmp_path / "pack"
    process_dir = tmp_path / "process"
    pack_dir.mkdir()
    process_dir.mkdir()
    monkeypatch.chdir(process_dir)

    MushclientPack(
        ScriptApi(AutomationEngine(RecordingSink()), source="m", base_dir=str(pack_dir)),
        full_stdlib=True,
    ).load_source(
        "<muclient><script><![CDATA["
        "local f = assert(io.open('Config.txt', 'w')); f:write('pack-local'); f:close()"
        "]]></script></muclient>"
    )

    assert (pack_dir / "Config.txt").read_text(encoding="utf-8") == "pack-local"
    assert not (process_dir / "Config.txt").exists()


def test_full_stdlib_opens_windows_style_absolute_paths(tmp_path):
    # Krinnzenn's help reads GetInfo(66) .. "/docs/x.txt" with every / turned into \.
    pack_dir = tmp_path / "pack"
    (pack_dir / "docs").mkdir(parents=True)
    (pack_dir / "docs" / "index.txt").write_text("the index", encoding="utf-8")
    sink = RecordingSink()
    MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(pack_dir)),
        full_stdlib=True,
    ).load_source(
        "<muclient><script><![CDATA["
        "local path = (GetInfo(66) .. 'docs/index.txt'):gsub('/', '\\\\')\n"
        "local f = assert(io.open(path, 'r')); Send(f:read('*a')); f:close()"
        "]]></script></muclient>"
    )
    assert sink.sent == ["the index"]


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


def test_one_malformed_rule_is_skipped_without_losing_siblings():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><triggers>"
        '<trigger match="bad" enabled="y" send_to="12"><send>not valid lua.</send></trigger>'
        '<trigger match="good" enabled="y" send_to="12"><send>Send("ok")</send></trigger>'
        "</triggers></muclient>"
    )
    engine.process_line(Line("good"))
    assert sink.sent == ["ok"]
    assert pack._rule_errors and pack._rule_errors[0][0] == "bad"


def test_repairs_missing_attribute_space_and_exposes_sendto_constants():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><script><![CDATA["
        "assert(sendto.world == 0 and sendto.execute == 10 and sendto.script == 12)"
        "]]></script><triggers>"
        '<trigger match="ping" enabled="y" send_to="12"sequence="100">'
        '<send>Send("pong")</send></trigger></triggers></muclient>'
    )
    engine.process_line(Line("ping"))
    assert sink.sent == ["pong"]


def test_disabled_xml_group_can_be_enabled_by_install_hook():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><plugin name='p' id='p'/><script><![CDATA["
        "function OnPluginInstall() EnableTriggerGroup('sounds', true) end"
        "]]></script><triggers>"
        '<trigger match="ping" group="sounds" enabled="n" send_to="12">'
        '<send>Send("pong")</send></trigger></triggers></muclient>'
    )
    engine.process_line(Line("ping"))
    assert sink.sent == []
    pack.dispatch_install()
    engine.process_line(Line("ping"))
    assert sink.sent == ["pong"]


def test_world_external_script_and_windows_case_insensitive_include(tmp_path):
    plugins = tmp_path / "Worlds" / "Plugins"
    scripts = tmp_path / "Worlds" / "Scripts"
    plugins.mkdir(parents=True)
    scripts.mkdir(parents=True)
    (scripts / "Core.LUA").write_text(
        "function external() Send('external') end", encoding="latin-1"
    )
    (plugins / "Sound.XML").write_text(
        "<muclient><triggers><trigger match='plugin' enabled='y' script='external'/>"
        "</triggers></muclient>",
        encoding="latin-1",
    )
    world = tmp_path / "Worlds" / "world.MCL"
    world.write_text(
        '<muclient><world script_filename="worlds\\scripts\\core.lua"/>'
        '<include name="worlds\\plugins\\sound.xml" plugin="y"/></muclient>',
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(world))
    engine.process_line(Line("plugin"))
    assert sink.sent == ["external"]
    assert pack._external_script_errors == []


def test_missing_world_script_is_accounted_for_without_failing_xml_rules(tmp_path):
    world = tmp_path / "world.MCL"
    world.write_text(
        '<muclient><world script_filename="worlds\\missing.lua"/><triggers>'
        '<trigger match="ping" enabled="y" send_to="12"><send>Send("pong")</send></trigger>'
        "</triggers></muclient>",
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(world))
    engine.process_line(Line("ping"))
    assert sink.sent == ["pong"]
    assert pack._external_script_errors == []
    assert pack._skipped_plugins == [
        ("worlds\\missing.lua", "external world script is not bundled with the pack")
    ]


def test_dependency_manager_loads_required_and_accounts_for_optional(tmp_path):
    (tmp_path / "sound.xml").write_text(
        "<muclient><plugin name='sound' id='sound'/><triggers>"
        '<trigger match="ding" enabled="y" send_to="12"><send>Send("loaded")</send></trigger>'
        "</triggers></muclient>",
        encoding="latin-1",
    )
    (tmp_path / "optional.xml").write_text(
        "<muclient><plugin name='optional' id='optional'/></muclient>", encoding="latin-1"
    )
    (tmp_path / "requirements.xml").write_text(
        "<muclient><plugin name='requirements' id='requirements'/><script><![CDATA["
        "optional_plugins = { optional = 'optional' }\n"
        "function OnPluginListChanged() "
        "LoadPlugin(GetPluginInfo(GetPluginID(), 20) .. 'sound.xml') end"
        "]]></script></muclient>",
        encoding="latin-1",
    )
    world = tmp_path / "world.MCL"
    world.write_text(
        '<muclient><include name="requirements.xml" plugin="y"/></muclient>', encoding="latin-1"
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(world))
    engine.process_line(Line("ding"))
    assert sink.sent == ["loaded"]
    assert ("optional.xml", "declared optional by the pack") in pack._skipped_plugins


def test_gmcp_helper_broadcast_exposes_nested_values():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><plugin name='sounds' id='sounds'/><script><![CDATA["
        "function OnPluginBroadcast(msg, id, name, text) "
        "if text == 'comm.channel' and gmcp('comm.channel.msg') == 'hello' "
        "then Send('gmcp-ok') end end"
        "]]></script></muclient>"
    )
    pack.dispatch_gmcp("Comm.Channel", {"msg": "hello"})
    assert sink.sent == ["gmcp-ok"]


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


def test_get_info_resolves_sound_path(tmp_path):
    sink, engine = _load(SOUNDS, base_dir=str(tmp_path))
    engine.process_line(Line("local"))
    assert any(Path(played["file"]) == tmp_path / "snd" / "x.ogg" for played in sink.played)


def test_resolve_keeps_forward_slashes_and_collapses_doubles():
    # GetInfo() builds ".../worlds/".."/sounds/x" with a doubled slash; _resolve must collapse
    # it to a single FORWARD slash on every OS. os.path.normpath would flip / to \ on Windows
    # -- the dev host is Linux so only the Windows CI catches that; this pins the contract.
    api = ScriptApi(AutomationEngine(RecordingSink()), base_dir="/p")
    resolved, _exists = api._resolve("/p/sounds//x.ogg")
    assert resolved == "/p/sounds/x.ogg"


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


def test_get_info_distinguishes_client_and_world_directories(tmp_path):
    client = tmp_path / "portable-client"
    worlds = client / "worlds"
    worlds.mkdir(parents=True)
    world = worlds / "w.MCL"
    world.write_text(
        '<muclient><world id="0123456789abcdef01234567" name="Test MUD" '
        'site="mud.example" port="4000"/><script><![CDATA['
        "Send(GetInfo(56)); Send(GetInfo(66)); Send(GetInfo(67)); "
        "Send(Version()); Send(GetInfo(72)); "
        "local id = GetUniqueID(); Send(type(id) .. ':' .. #id); "
        "Send(GetWorldID()); Send(WorldName() .. ':' .. WorldAddress() .. ':' .. WorldPort()); "
        "Send(GetInfo(1) .. ':' .. GetInfo(2)); Send(GetWorldList()[1]); "
        "Send('[' .. Trim(' x ') .. ']')"
        "]]></script></muclient>",
        encoding="latin-1",
    )
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )

    pack.load_file(str(world))

    assert sink.sent == [
        client.as_posix() + "/",
        client.as_posix() + "/",
        worlds.as_posix() + "/",
        "5.06",
        "5.06",
        "string:24",
        "0123456789abcdef01234567",
        "Test MUD:mud.example:4000",
        "mud.example:Test MUD",
        "Test MUD",
        "[x]",
    ]


def test_host_speech_plugin_is_virtual_and_accounted_for(tmp_path):
    plugin = tmp_path / "Sapi_speaker.xml"
    plugin.write_text(
        '<muclient><plugin name="Sapi_speaker" id="speech"/>'
        '<script><![CDATA[function OnPluginInstall() Send("wrong") end]]></script></muclient>',
        encoding="latin-1",
    )
    world = tmp_path / "world.MCL"
    world.write_text(
        '<muclient><include name="Sapi_speaker.xml" plugin="y"/></muclient>',
        encoding="latin-1",
    )
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )

    pack.load_file(str(world))
    pack.dispatch_install()

    assert sink.sent == []
    assert pack._plugin_info["speech"]["enabled"] is True
    assert pack._skipped_plugins == [
        ("Sapi_speaker.xml", "genericMud owns automatic speech output")
    ]


def test_world_and_global_options_return_scalar_host_values(tmp_path):
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "Send(tostring(GetOption('enable_triggers'))); "
        "SetOption('enable_triggers', 0); Send(tostring(GetOption('enable_triggers'))); "
        "Send(tostring(GetGlobalOption('F1macro'))); "
        "Send(tostring(#GetGlobalOptionList()))"
        "]]></script></muclient>"
    )
    assert sink.sent == ["1", "0", "1", "0"]


def test_standard_error_codes_and_variable_mutations_return_ok(tmp_path):
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "Send(tostring(error_code.eOK)); Send(tostring(SetVariable('x', '1'))); "
        "Send(tostring(DeleteVariable('x'))); "
        "AddTimer('t', 0, 0, 5, '', 0, ''); Send(tostring(EnableTimer('t', true)))"
        "]]></script></muclient>"
    )
    assert sink.sent == ["0", "0", "0", "0"]


def test_unique_numbers_and_named_timer_callbacks(tmp_path):
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "wait = {}; function wait.resume(name) Send(name) end; "
        "Send(tostring(GetUniqueNumber())); Send(tostring(GetUniqueNumber())); "
        "AddTimer('wake', 0, 0, 0.1, '', "
        "bit.bor(timer_flag.Enabled, timer_flag.OneShot), 'wait.resume')"
        "]]></script></muclient>"
    )
    sink.run_pending()
    assert sink.sent == ["1", "2", "wake"]


def test_utils_split_and_readdir_match_mushclient_shapes(tmp_path):
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    (sounds / "hit1.ogg").write_bytes(b"OggS")
    (sounds / "hit2.ogg").write_bytes(b"OggS-more")
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "local version = utils.split('3.1', '.'); Send(version[1]); "
        "local files = utils.readdir('sounds/hit*.ogg'); "
        "Send(tostring(files['hit1.ogg'].size)); "
        "Send(tostring(files['hit2.ogg'].directory)); "
        "Send(tostring(utils.readdir('sounds/missing*.ogg') == nil))"
        "]]></script></muclient>"
    )
    assert sink.sent == ["3", "4", "false", "true"]


def test_miriani_bass_module_routes_stream_to_sound_bus(tmp_path):
    sound = tmp_path / "sounds" / "hit.ogg"
    sound.parent.mkdir()
    sound.write_bytes(b"OggS")
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "local bass = require('miriani.lib.audio.bass')(); "
        "local stream = bass:StreamCreateFile(false, 'sounds/hit.ogg', 0, 0, 4); "
        "bass:SetAttribute(stream, 2, 0.4); bass:SetAttribute(stream, 3, -0.5); "
        "stream:Play()"
        "]]></script></muclient>"
    )
    assert len(sink.played) == 1
    played = sink.played[0]
    assert Path(played["file"]) == sound
    assert {**played, "file": str(sound)} == {
        "file": str(sound),
        "channel": "mush-bass-1",
        "gain": 0.4,
        "pan": -0.5,
        "loop": True,
        "times": 1,
    }


def test_json_bridge_decodes_and_encodes_without_native_lpeg(tmp_path):
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "require('json'); "
        "local value = json.decode('{\"patch\":5,\"items\":[\"a\",\"b\"]}'); "
        "Send(value.patch .. ':' .. value.items[2]); "
        "local roundtrip = json.decode(json.encode({name='Miriani', enabled=true})); "
        "Send(roundtrip.name .. ':' .. tostring(roundtrip.enabled)); "
        "Send(type(require('socket').gettime()))"
        "]]></script></muclient>"
    )
    assert sink.sent == ["5:b", "Miriani:true", "number"]


def test_json_takes_luajsons_module_shapes_too(tmp_path):
    # Toastush and Miriani-Next ship LuaJSON, where json.decode and json.encode are modules:
    # Toastush reads its updater's broadcast with json.decode.decode, and both read their old
    # index through require("json.decode"). Neither worked when they were plain functions.
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "require('json'); "
        "Send(json.decode.decode('{\"update\":[[\"a.ogg\",{}]]}').update[1][1]); "
        "local decoder = require('json.decode'); "
        "Send(decoder.decode('[7]')[1] .. ':' .. decoder('[8]')[1]); "
        "local encoder = require('json.encode'); "
        "Send(encoder.encode({1, 2}) .. ':' .. json.encode.encode({3})); "
        "local ok, value = pcall(json.decode.decode, '[9]'); "
        "Send(tostring(ok) .. ':' .. value[1])"
        "]]></script></muclient>"
    )
    assert sink.sent == ["a.ogg", "7:8", "[1,2]:[3]", "true:9"]


def test_socket_http_downloads_binary_cue_and_confined_mkdir(tmp_path, monkeypatch):
    class Response:
        status = 200
        reason = "OK"
        headers = {"Content-Length": "3", "Content-Type": "audio/ogg"}

        def __init__(self):
            self._body = bytearray((0, 255, 65))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

        def read(self, size):
            chunk = bytes(self._body[:size])
            del self._body[:size]
            return chunk

    monkeypatch.setattr(
        "genericmud.scripting.mushclient_compat._open_http", lambda _url: Response()
    )
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path)),
        full_stdlib=True,
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "local dir = GetInfo(66) .. 'sounds/ogg/ships/combat/'; "
        "os.execute('mkdir \"' .. dir .. '\" 2>nul'); "
        "require('socket.http'); require('ltn12'); local body = {}; "
        "local ok, status, headers, reason = "
        "socket.http.request{url='https://example.test/bomb.ogg', "
        "sink=ltn12.sink.table(body)}; "
        "local file = io.open(dir .. 'bomb.ogg', 'wb'); "
        "file:write(table.concat(body)); file:close(); "
        "Send(tostring(ok) .. ':' .. status .. ':' .. tostring(reason))"
        "]]></script></muclient>"
    )
    assert sink.sent == ["1:200:OK"]
    assert (tmp_path / "sounds" / "ogg" / "ships" / "combat" / "bomb.ogg").read_bytes() == bytes(
        (0, 255, 65)
    )


def test_bundled_penlight_sees_pack_files_through_confined_lfs(tmp_path):
    (tmp_path / "pl").mkdir()
    # Minimal path module exercises the same `lfs.attributes(path, 'mode')` contract.
    (tmp_path / "pl" / "path.lua").write_text(
        "local lfs = require('lfs'); return {"
        "isfile=function(p) return lfs.attributes(p, 'mode') == 'file' end}",
        encoding="latin-1",
    )
    sound = tmp_path / "sounds" / "hit.ogg"
    sound.parent.mkdir()
    sound.write_bytes(b"OggS")
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "local path = require('pl.path'); Send(tostring(path.isfile('sounds/hit.ogg')))"
        "]]></script></muclient>"
    )
    assert sink.sent == ["true"]


def test_sqlite_bridge_reads_named_rows_and_rejects_escape(tmp_path):
    database = tmp_path / "locations.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE locations (uid INTEGER, name TEXT)")
        connection.execute("INSERT INTO locations VALUES (7, 'Market Square')")
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "local db = assert(sqlite3.open(GetInfo(66) .. 'locations.db')); "
        "for row in db:nrows('SELECT uid, name FROM locations') do "
        "Send(tostring(row.uid) .. ':' .. row.name) end; "
        "Send(tostring(sqlite3.open('../outside.db') == nil)); db:close()"
        "]]></script></muclient>"
    )
    assert sink.sent == ["7:Market Square", "true"]


def test_sqlite_bridge_prepared_statement_round_trip(tmp_path):
    database = tmp_path / "history.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE messages (category TEXT, body TEXT)")
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path))
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "local db = sqlite3.open(GetInfo(66) .. 'history.db'); "
        "local put = db:prepare('INSERT INTO messages VALUES (?, ?)'); "
        "put:bind_values('tell', 'hello'); Send(tostring(put:step())); put:finalize(); "
        "local get = db:prepare('SELECT category, body FROM messages'); "
        "Send(tostring(get:step())); local row = get:get_named_values(); "
        "Send(row.category .. ':' .. row.body); get:finalize(); db:close()"
        "]]></script></muclient>"
    )
    assert sink.sent == ["101", "100", "tell:hello"]


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
    # A separate tree, not beside the world file.
    real_sound = tmp_path / "MUSHclient" / "sounds" / "boom.wav"
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
    engine.process_line(Line("You are hit"))  # a plain trigger is the whole line
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
    engine.process_line(Line("You are hit"))  # a plain trigger is the whole line
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


# --- plugin lifecycle dispatch (the v0.6.5 Erion silence: loaded, fired, gated off) ---


def _make_pack(tmp_path, world_xml: str) -> tuple[RecordingSink, AutomationEngine, MushclientPack]:
    world = tmp_path / "World.mcl"
    world.write_text(world_xml, encoding="latin-1")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(
        ScriptApi(engine, source="erion", base_dir=str(tmp_path)), full_stdlib=True
    )
    pack.load_file(str(world))
    return sink, engine, pack


def test_install_dispatch_opens_erion_style_toggle_gates(tmp_path):
    """Erion's MSDP_handler defaults every sound toggle to 1 inside OnPluginInstall
    (nil-checked). Without install dispatch AND nil-for-unset GetVariable, the 'Alas'
    trigger fires but its gated body exits silently -- the exact v0.6.5 log shape."""
    (tmp_path / "alas.ogg").write_bytes(b"OggS")
    sink, engine, pack = _make_pack(
        tmp_path,
        '<muclient><plugin id="handler"/>\n'
        '<triggers><trigger match="^Alas, you cannot go.*\\.$" enabled="y" regexp="y"'
        ' send_to="12" sequence="100"><send>\n'
        'if tonumber(GetVariable("toggleAlas")) == 1 then\n'
        '  Sound(GetInfo(67) .. "alas.ogg")\n'
        "end\n"
        "</send></trigger></triggers>\n"
        "<script><![CDATA[\n"
        "function OnPluginInstall()\n"
        '  local var = GetVariable("worldtoggleAlas")\n'
        '  if var ~= nil then SetVariable("toggleAlas", var)\n'
        '  else SetVariable("toggleAlas", 1) end\n'
        "end\n"
        "]]></script></muclient>",
    )
    engine.process_line(Line("Alas, you cannot go east."))
    assert sink.played == []  # gate closed pre-install: trigger fires, body skips
    pack.dispatch_install()
    engine.process_line(Line("Alas, you cannot go east."))
    assert len(sink.played) == 1  # toggle defaulted on; the cue reaches the sink


def test_getvariable_unset_is_nil_but_empty_is_set():
    """MUSHclient GetVariable semantics: unset -> nil (Erion's install loop nil-checks
    saved settings), but an explicitly-empty variable is still set."""
    sink, engine = _load(
        "<muclient><script><![CDATA[\n"
        'assert(GetVariable("never_set") == nil, "unset must be nil")\n'
        'SetVariable("empty", "")\n'
        'assert(GetVariable("empty") ~= nil, "empty-but-set must not be nil")\n'
        'Send("ok")\n'
        "]]></script></muclient>"
    )
    assert sink.sent == ["ok"]  # both asserts held


def test_hooks_are_captured_per_plugin_and_do_not_leak(tmp_path):
    """Plugins share one _G, so each plugin's OnPlugin* must be claimed after its
    script runs: the next plugin must neither inherit nor overwrite them."""
    (tmp_path / "alpha.xml").write_text(
        '<muclient><plugin id="alpha"/><script><![CDATA[\n'
        'function OnPluginInstall() Send("alpha") end\n'
        "]]></script></muclient>",
        encoding="latin-1",
    )
    (tmp_path / "beta.xml").write_text(
        '<muclient><plugin id="beta"/><script><![CDATA[\n'
        'assert(rawget(_G, "OnPluginInstall") == nil, "inherited alpha hook")\n'
        'function OnPluginInstall() Send("beta") end\n'
        "]]></script></muclient>",
        encoding="latin-1",
    )
    sink, engine, pack = _make_pack(
        tmp_path,
        '<muclient><include name="alpha.xml" plugin="y"/>'
        '<include name="beta.xml" plugin="y"/></muclient>',
    )
    assert pack._include_errors == []  # beta's rawget assert held: no hook leaked
    pack.dispatch_install()
    assert sink.sent == ["alpha", "beta"]  # both ran, in load order


def test_loadlib_bootstrap_runs_for_audio_while_host_speech_is_virtual(tmp_path):
    """LuaAudio remains productive, while redundant MushReader is represented virtually."""
    (tmp_path / "luaaudio.xml").write_text(
        '<muclient><plugin id="luaaudio"/><script><![CDATA[\n'
        "function OnPluginInstall()\n"
        '  assert(package.loadlib("audio.dll", "luaopen_audio"))()\n'
        '  SetVariable("vol", "100")\n'  # the line that used to be skipped
        '  Send("luaaudio-installed")\n'
        "end\n"
        "]]></script></muclient>",
        encoding="latin-1",
    )
    (tmp_path / "mushreader.xml").write_text(
        '<muclient><plugin id="mushreader"/><script><![CDATA[\n'
        "function OnPluginInstall()\n"
        '  assert(package.loadlib("MushReader.dll", "luaopen_audio"))()\n'
        "  nvda.stop()\n"  # black-holed: no-ops instead of indexing a nil `nvda`
        '  nvda.say("mush reader initialized")\n'
        '  Send("mushreader-installed")\n'
        "end\n"
        "]]></script></muclient>",
        encoding="latin-1",
    )
    sink, engine, pack = _make_pack(
        tmp_path,
        '<muclient><include name="luaaudio.xml" plugin="y"/>'
        '<include name="mushreader.xml" plugin="y"/></muclient>',
    )
    pack.dispatch_install()
    assert sink.sent == ["luaaudio-installed"]
    assert pack._api.get_var("vol") == "100"
    assert ("mushreader.xml", "genericMud owns automatic speech output") in (
        pack._skipped_plugins
    )


def test_execute_runs_aliases_before_the_wire(tmp_path):
    """MUSHclient Execute = "as if typed": aliases match first. Erion's historyadd()
    Executes "history_add all=..." at MSDP-dispatch time expecting its channel_history
    alias to consume it; the old Execute->send binding shipped it to the MUD, which
    rejected it -- and the rejection was spoken -- on every captured line. Unmatched
    text still goes out."""
    sink, engine, pack = _make_pack(
        tmp_path,
        "<muclient>"
        '<aliases><alias match="^history_add (\\w[\\w ]*)=(.*)$" enabled="y" regexp="y"'
        ' script="history_add" sequence="100"/></aliases>'
        "<script><![CDATA[\n"
        'function history_add(name, line, wc) SetVariable("hist", wc[2]) end\n'
        "]]></script></muclient>",
    )
    # Runtime, like Erion's MSDP handler (load is long done; the alias is registered):
    pack._lua.execute('Execute("history_add all=goblin says hi")')
    pack._lua.execute('Execute("look")')
    assert pack._api.get_var("hist") == "goblin says hi"  # the alias consumed it
    assert sink.sent == ["look"]  # only the unmatched command reached the wire


def test_audio_isplaying_is_truthful_and_fadeout_lets_the_cue_play_out(tmp_path):
    """Erion's ambience/music switching is gated on ppi.isPlaying(old): the old
    hardcoded 0 told it the outgoing cue was already done, so it started the new one
    without stopping the old -- ambiences stacking on every room change. isPlaying
    must reflect the backend. fadeout fades the cue to silence and stops its loop, and
    the cue plays out silently to its end, as audio.dll's does."""
    from genericmud.sound.bus import SoundBackend, SoundBus

    class _Backend(SoundBackend):
        def __init__(self):
            self.busy: set[str] = set()
            self.slides: list[tuple] = []
            self.looping: dict[str, bool] = {}

        def is_playing(self, channel):
            return channel in self.busy

        def slide(self, channel, gain=None, pan=None, frequency=None, seconds=0.0):
            self.slides.append((channel, gain, seconds))

        def set_looping(self, channel, loop):
            self.looping[channel] = loop

    backend = _Backend()
    sink = RecordingSink()
    engine = AutomationEngine(sink, sound=SoundBus(backend))
    (tmp_path / "amb.ogg").write_bytes(b"OggS")
    pack = MushclientPack(
        ScriptApi(engine, source="erion", base_dir=str(tmp_path)), full_stdlib=True
    )
    pack.load_source(
        '<muclient><script><![CDATA[ id1 = audio.play("amb.ogg", 1) ]]></script></muclient>'
    )
    assert pack._lua.globals().id1 == 1  # cue 1 -> bus channel "erion-audio-1"
    # In production AppSink forwards plays into the bus; tests record them on the sink
    # instead, so drive the backend's "still audible" truth directly.
    backend.busy.add("erion-audio-1")
    assert pack._lua.eval("audio.isPlaying(1)") == 1
    backend.busy.discard("erion-audio-1")
    assert pack._lua.eval("audio.isPlaying(1)") == 0  # finished cue reads as done
    assert pack._lua.eval("audio.isPlaying(999)") == 0  # unknown id: never an error
    # fadeout: down to silence over its 5 ms, no more going round, and audible till its end.
    backend.busy.add("erion-audio-1")
    pack._lua.execute("audio.fadeout(1, 5)")
    assert backend.slides == [("erion-audio-1", 0.0, 0.005)]
    assert backend.looping == {"erion-audio-1": False}
    assert sink.stopped == []
    assert pack._lua.eval("audio.isPlaying(1)") == 1


def test_accelerator_binds_pack_hotkeys(tmp_path):
    """Accelerator(key, send) is a pack's keyboard UI (Erion ships 54 of them); the key
    spec is normalized (spacing, mod order) to the combo form the wx layer emits, and
    the bound command runs "as if typed" -- aliases first, wire for the rest."""
    sink, engine, pack = _make_pack(
        tmp_path,
        "<muclient>"
        '<aliases><alias match="^hppercent$" enabled="y" regexp="y" sequence="100">'
        "<send>score hp</send></alias></aliases>"
        "<script><![CDATA[\n"
        'Accelerator("shift+alt + f5", "hppercent")\n'  # messy spacing + mod order, like real packs
        'Accelerator("f9", "look")\n'
        "]]></script></muclient>",
    )
    assert engine.press_key("alt+shift+f5")  # canonical order: ctrl, alt, shift
    assert sink.sent == ["score hp"]  # the alias consumed the command
    assert engine.press_key("f9")
    assert sink.sent == ["score hp", "look"]  # no alias: straight to the wire


def test_accelerator_to_binds_script_hotkey(tmp_path):
    sink, engine, _pack = _make_pack(
        tmp_path,
        "<muclient><script><![CDATA["
        "function report() Send('status') end; "
        "world.AcceleratorTo('Ctrl + Shift + V', 'report()', sendto.script)"
        "]]></script></muclient>",
    )
    assert engine.press_key("ctrl+shift+v")
    assert sink.sent == ["status"]


def test_accelerator_answers_like_mushclient_and_unbinds(tmp_path):
    # Materia Magica's accelerators plugin checks for 0 after each Accelerator call and
    # announced every key it bound as an error when it got nothing back.
    sink, engine, _pack = _make_pack(
        tmp_path,
        "<muclient><script><![CDATA["
        "Send(Accelerator('ctrl + f12', 'look'))\n"
        "Send(Accelerator('ctrl+alt', 'look'))\n"
        "Send(AcceleratorTo('f8', 'look', 99))\n"
        "Send(Accelerator('f7', 'score'))\n"
        "Send(Accelerator('f7', ''))\n"
        "]]></script></muclient>",
    )
    assert sink.sent == ["0", "30046", "30026", "0", "0"]
    assert engine.press_key("ctrl+f12")
    assert not engine.press_key("f7")  # an empty command takes the key away


def test_addtriggerex_oneshot_and_replace(tmp_path):
    """Erion's F-key reports: the hotkey alias AddTriggerEx's a OneShot+Replace trigger
    (flags built with bit.bor over trigger_flag constants -- all previously black-holed)
    to speak the MUD's reply. One fire per registration, and re-registering must not
    stack a second live rule."""
    register = (
        'AddTriggerEx("AnnounceHPFull", "^(.*?) of (.*?) hp$", "",'
        " bit.bor(trigger_flag.Enabled, trigger_flag.RegularExpression,"
        " trigger_flag.Temporary, trigger_flag.Replace, trigger_flag.OneShot),"
        ' custom_colour.NoChange, 0, "", "SpeakFullHp", 12, 100)'
    )
    sink, engine, pack = _make_pack(
        tmp_path,
        "<muclient><script><![CDATA[\n"
        'function SpeakFullHp(name, line, wc) Send("hp " .. wc[1] .. "/" .. wc[2]) end\n'
        "]]></script></muclient>",
    )
    pack._lua.execute(register)
    engine.process_line(Line("324 of 400 hp"))
    assert sink.sent == ["hp 324/400"]
    engine.process_line(Line("324 of 400 hp"))
    assert sink.sent == ["hp 324/400"]  # OneShot: spent after the first fire
    pack._lua.execute(register)  # F-key pressed again: Replace re-arms, ONE rule fires
    engine.process_line(Line("350 of 400 hp"))
    assert sink.sent == ["hp 324/400", "hp 350/400"]
    pack._lua.execute('DeleteTrigger("AnnounceHPFull")')
    pack._lua.execute(register.replace("AnnounceHPFull", "Other"))
    pack._lua.execute('EnableTrigger("Other", 0)')  # registered then disabled
    engine.process_line(Line("1 of 2 hp"))
    assert sink.sent == ["hp 324/400", "hp 350/400"]  # deleted + disabled: neither fired


def test_callplugin_reaches_exposed_functions(tmp_path):
    """CallPlugin routes through the ppi Expose registry; an unknown target no-ops."""
    (tmp_path / "msgs.xml").write_text(
        '<muclient><plugin id="msgsid"/><script><![CDATA[\n'
        'local ppi = require "ppi"\n'
        'function MsgNote(text) Send("note:" .. tostring(text)) end\n'
        'ppi.Expose("MsgNote")\n'
        "]]></script></muclient>",
        encoding="latin-1",
    )
    sink, engine, pack = _make_pack(
        tmp_path, '<muclient><include name="msgs.xml" plugin="y"/></muclient>'
    )
    pack._lua.execute('CallPlugin("msgsid", "MsgNote", "hi")')
    pack._lua.execute('CallPlugin("nosuch", "MsgNote", "bye")')  # absent plugin: silent no-op
    assert sink.sent == ["note:hi"]


def test_failing_hook_is_isolated(tmp_path):
    """One plugin's erroring hook must not stop the others (MUSHclient isolation)."""
    (tmp_path / "bad.xml").write_text(
        '<muclient><plugin id="bad"/><script><![CDATA[\n'
        'function OnPluginInstall() error("boom") end\n'
        "]]></script></muclient>",
        encoding="latin-1",
    )
    (tmp_path / "good.xml").write_text(
        '<muclient><plugin id="good"/><script><![CDATA[\n'
        'function OnPluginInstall() Send("good") end\n'
        "]]></script></muclient>",
        encoding="latin-1",
    )
    sink, engine, pack = _make_pack(
        tmp_path,
        '<muclient><include name="bad.xml" plugin="y"/>'
        '<include name="good.xml" plugin="y"/></muclient>',
    )
    pack.dispatch_install()
    assert sink.sent == ["good"]
    assert pack._hook_errors[0][0] == "bad.OnPluginInstall"


def test_sent_do_round_sends_report_packet_verbatim(tmp_path):
    """MSDP packs send their REPORT list on the SENT_DO round via SendPkt. The packet
    carries IAC (255) framing -- invalid UTF-8 -- and must reach the wire byte-exact."""
    sink, engine, pack = _make_pack(
        tmp_path,
        '<muclient><plugin id="msdp"/><script><![CDATA[\n'
        "function OnPluginTelnetRequest(t, data)\n"
        '  if t == 69 and data == "WILL" then return true end\n'
        '  if t == 69 and data == "SENT_DO" then\n'
        "    SendPkt(string.char(255, 250, 69)"
        ' .. string.char(1) .. "REPORT" .. string.char(2) .. "ROOM_NAME"'
        " .. string.char(255, 240))\n"
        "  end\n"
        "  return false\n"
        "end\n"
        "]]></script></muclient>",
    )
    pack.dispatch_telnet_request(69, "WILL")
    assert sink.packets == []  # the WILL round only answers; SENT_DO carries the REPORTs
    pack.dispatch_telnet_request(69, "SENT_DO")
    expected = bytes([255, 250, 69, 1]) + b"REPORT" + bytes([2]) + b"ROOM_NAME" + bytes([255, 240])
    assert sink.packets == [expected]


def test_subnegotiation_payload_reaches_plugin_byte_exact(tmp_path):
    """An MSDP payload (VAR/VAL control bytes + possible high bytes) must arrive in the
    plugin's OnPluginTelnetSubnegotiation as the same byte string MUSHclient would pass."""
    (tmp_path / "hit.ogg").write_bytes(b"OggS")
    sink, engine, pack = _make_pack(
        tmp_path,
        '<muclient><plugin id="msdp"/><script><![CDATA[\n'
        "function OnPluginTelnetSubnegotiation(t, data)\n"
        "  if t ~= 69 then return end\n"
        "  local expected = string.char(1) .. \"SOUND\" .. string.char(2) .. \"hit\""
        " .. string.char(233)\n"
        "  if data == expected then\n"
        '    Sound(GetInfo(67) .. "hit.ogg")\n'
        "  end\n"
        "end\n"
        "]]></script></muclient>",
    )
    payload = bytes([1]) + b"SOUND" + bytes([2]) + b"hit" + bytes([233])
    pack.dispatch_telnet_subnegotiation(69, payload)
    assert len(sink.played) == 1  # byte-exact round trip; the gated cue fired


def test_nvda_speaks_and_probes_read_falsy(tmp_path):
    """The pack's speech object: tts_interrupt does nvda.stop(); say(text) -- as a
    black hole both were silent no-ops, so the F-key hp reports ran and said nothing.
    Probes like nvda.jaws_running() must read FALSY (the truthy black hole sent
    speech to a JAWS object that doesn't exist)."""
    sink, engine, pack = _make_pack(
        tmp_path,
        "<muclient><script><![CDATA[\n"
        "if not nvda.jaws_running() then\n"
        "  nvda.stop()\n"
        '  nvda.say("42 % hp")\n'
        "end\n"
        "]]></script></muclient>",
    )
    assert sink.speech_stops == 1
    assert [s[0] for s in sink.spoken] == ["42 % hp"]


def test_onplugintick_reaches_plugin_and_isconnected_is_real(tmp_path):
    """Erion's OnPluginTick is its music/ambience engine (restarts finished ambience);
    it branches on IsConnected(), which must be the real transport state, not a
    truthy black hole."""
    sink, engine, pack = _make_pack(
        tmp_path,
        "<muclient><script><![CDATA[\n"
        "ticks = 0\n"
        'function OnPluginTick() if IsConnected() then ticks = ticks + 1 Send("tick") end end\n'
        "]]></script></muclient>",
    )
    assert pack.has_hook("OnPluginTick")
    pack.dispatch("OnPluginTick")
    assert sink.sent == ["tick"]
    engine.connected = False
    pack.dispatch("OnPluginTick")
    assert sink.sent == ["tick"]  # disconnected: the guard held


_SOUND_WORLD = (
    "<muclient><script><![CDATA[\n"
    'function amb() PlaySound(1, "amb.wav", true, -6, 0) end\n'
    'function hit() PlaySound(2, "hit.wav", false, 0, 6) end\n'
    'function auto() PlaySound(0, "auto.wav", false, 0, 0) end\n'
    'function tweak() PlaySound(1, "", false, -12, -100) end\n'
    'function halt1() StopSound(1) end\n'
    'function haltall() StopSound() end\n'
    'function panit() Sound("pan=-100") end\n'
    "]]></script>\n"
    "<triggers>\n"
    '<trigger match="amb" enabled="y" script="amb" sequence="50"/>\n'
    '<trigger match="hit" enabled="y" script="hit" sequence="50"/>\n'
    '<trigger match="auto" enabled="y" script="auto" sequence="50"/>\n'
    '<trigger match="tweak" enabled="y" script="tweak" sequence="50"/>\n'
    '<trigger match="halt1" enabled="y" script="halt1" sequence="50"/>\n'
    '<trigger match="haltall" enabled="y" script="haltall" sequence="50"/>\n'
    '<trigger match="panit" enabled="y" script="panit" sequence="50"/>\n'
    "</triggers></muclient>"
)


class _SpyBus(SoundBus):
    """Records adjust() calls and answers is_playing from a settable set."""

    def __init__(self) -> None:
        super().__init__()
        self.adjusted: list[tuple[str, float | None, float | None]] = []
        self.busy: set[str] = set()

    def adjust(self, channel, gain=None, pan=None) -> None:
        self.adjusted.append((channel, gain, pan))

    def is_playing(self, channel) -> bool:
        return channel in self.busy


def _sound_pack(tmp_path) -> tuple[RecordingSink, AutomationEngine, _SpyBus]:
    (tmp_path / "sounds").mkdir()
    for name in ("amb.wav", "hit.wav", "auto.wav"):
        (tmp_path / "sounds" / name).write_bytes(b"RIFF")
    world = tmp_path / "World.mcl"
    world.write_text(_SOUND_WORLD, encoding="latin-1")
    sink = RecordingSink()
    bus = _SpyBus()
    engine = AutomationEngine(sink, sound=bus)
    pack = MushclientPack(
        ScriptApi(engine, source="p", base_dir=str(tmp_path)), full_stdlib=True
    )
    pack.load_file(str(world))
    return sink, engine, bus


def test_playsound_buffers_map_to_distinct_channels(tmp_path):
    # MUSHclient's ten sound buffers play simultaneously; collapsing them onto one
    # channel made a one-shot on buffer 2 cut off a loop started on buffer 1.
    sink, engine, _bus = _sound_pack(tmp_path)
    engine.process_line(Line("amb"))
    engine.process_line(Line("hit"))
    assert [p["channel"] for p in sink.played] == ["mush-1", "mush-2"]
    assert sink.played[0]["loop"] is True
    assert sink.played[0]["gain"] == pytest.approx(10 ** (-6 / 20))  # 6 dB down
    assert sink.played[1]["pan"] == pytest.approx(1 - 10 ** (-6 / 20))  # left 6 dB down


def test_playsound_buffer_zero_picks_the_first_free_buffer(tmp_path):
    sink, engine, bus = _sound_pack(tmp_path)
    engine.process_line(Line("amb"))
    bus.busy = {"mush-1"}  # buffer 1 still playing -> 0 must select buffer 2
    engine.process_line(Line("auto"))
    assert [p["channel"] for p in sink.played] == ["mush-1", "mush-2"]


def test_playsound_empty_filename_adjusts_the_playing_buffer(tmp_path):
    # Per the PlaySound docs an empty FileName modifies the sound already playing in
    # that buffer; it used to be forwarded as a (failing) play of "".
    sink, engine, bus = _sound_pack(tmp_path)
    engine.process_line(Line("amb"))
    bus.busy = {"mush-1"}  # the loop is still playing
    engine.process_line(Line("tweak"))
    assert len(sink.played) == 1  # no phantom play of ""
    [(channel, gain, pan)] = bus.adjusted
    assert channel == "mush-1"
    assert gain == pytest.approx(10 ** (-12 / 20))  # 12 dB down
    assert pan == pytest.approx(-1.0, abs=1e-4)  # the right side 100 dB down: hard left


def test_stopsound_stops_one_buffer_or_all(tmp_path):
    # StopSound was previously unbound entirely (swallowed by the CapWords black
    # hole), so packs had no way to stop a looping buffer.
    sink, engine, _bus = _sound_pack(tmp_path)
    engine.process_line(Line("halt1"))
    assert sink.stopped == ["mush-1"]
    engine.process_line(Line("haltall"))
    assert sink.stopped[1:] == [f"mush-{n}" for n in range(1, 11)]


def test_sound_pan_directive_adjusts_the_live_cue(tmp_path):
    sink, engine, bus = _sound_pack(tmp_path)
    engine.process_line(Line("panit"))
    assert bus.adjusted == [("sound", None, -1.0)]


def test_dynamic_triggers_do_not_leak_dead_engine_rules(tmp_path):
    # AddTriggerEx used to register a nameless engine rule that no delete/one-shot/replace
    # could remove; on a churny pack (Erion's per-line "capture the next line" temporaries)
    # dead rules accumulated and every one was regex-scanned against every line forever.
    sink, engine, pack = _make_pack(
        tmp_path,
        "<muclient><script><![CDATA[\nfunction noop() end\n]]></script></muclient>",
    )
    oneshot = (
        "bit.bor(trigger_flag.Enabled, trigger_flag.RegularExpression, "
        "trigger_flag.OneShot)"
    )
    for i in range(50):
        pack._lua.execute(
            f'AddTriggerEx("t{i}", "^capture {i}$", "", {oneshot}, '
            'custom_colour.NoChange, 0, "", "noop", 12, 100)'
        )
    assert len(engine._triggers) == 50
    for i in range(50):
        engine.process_line(Line(f"capture {i}"))  # each one-shot fires once, then retires
    assert len(engine._triggers) == 0  # every spent one-shot removed itself from the engine

    # An explicit delete also removes the engine rule, not just deactivates it.
    enabled = "bit.bor(trigger_flag.Enabled, trigger_flag.RegularExpression)"
    pack._lua.execute(
        f'AddTriggerEx("keep", "^x$", "", {enabled}, custom_colour.NoChange, 0, "", '
        '"noop", 12, 100)'
    )
    assert len(engine._triggers) == 1
    pack._lua.execute('DeleteTrigger("keep")')
    assert len(engine._triggers) == 0


WHOLE_LINE = """<?xml version="1.0"?>
<muclient>
<aliases>
 <alias match="sc" enabled="y" send_to="12"><send>Send("score")</send></alias>
</aliases>
<triggers>
 <trigger match="You are hungry." enabled="y" regexp="n" send_to="12">
  <send>Send("eat bread")</send></trigger>
 <trigger match="Ready? *" enabled="y" regexp="n" send_to="12">
  <send>Send("go %1")</send></trigger>
 <trigger match="you feel thirsty" enabled="y" ignore_case="y" send_to="12">
  <send>Send("drink water")</send></trigger>
</triggers>
</muclient>"""


def test_plain_patterns_follow_mushclients_whole_line_rule():
    # MUSHclient wraps a plain pattern in ^...$ and keeps "?" literal (its own
    # ConvertToRegularExpression). Erion's alias "vol" used to catch voldown and voltoggle.
    sink, engine = _load(WHOLE_LINE)
    assert engine.process_input("scan") == ["scan"]
    engine.process_line(Line('Bob says, "You are hungry."'))
    assert sink.sent == []
    engine.process_line(Line("You are hungry."))
    engine.process_line(Line("Ready? north"))
    engine.process_line(Line("YOU FEEL THIRSTY"))
    assert sink.sent == ["eat bread", "go north", "drink water"]


def test_add_trigger_ex_plain_pattern_is_the_whole_line():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(ScriptApi(engine, source="m")).load_source(
        '<muclient><script><![CDATA[\n'
        'AddTriggerEx("hp", "* hit points", "hp %1", trigger_flag.Enabled, -1, 0, "", "", 0, 100)\n'
        "]]></script></muclient>"
    )
    engine.process_line(Line("Bob shouts: 12 hit points left!"))
    engine.process_line(Line("40 hit points"))
    assert sink.sent == ["hp 40"]


def _rules(rules: str) -> tuple[RecordingSink, AutomationEngine, MushclientPack]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(f'<?xml version="1.0"?><muclient>{rules}</muclient>')
    return sink, engine, pack


def test_send_to_codes_do_what_mushclient_does_not_send_everything_to_the_mud():
    # Empire MUD's area-ambience triggers are send_to 14 (script after omit); Cosmic Rage's
    # $buffer trigger is send_to 10 (execute); SquareSoft's greeting credits are send_to 2.
    # All of it used to go to the MUD as typed commands, Lua source included.
    sink, engine, _pack = _rules(
        '<aliases><alias match="tts_interrupt *" enabled="y" send_to="12">'
        '<send>SetVariable("spoken", "%1")</send></alias></aliases>'
        "<triggers>"
        '<trigger match="^You died\\.$" regexp="y" enabled="y" send_to="14">'
        '<send>SetVariable("deaths", "1")</send></trigger>'
        '<trigger match="^\\$buffer (.*?)$" regexp="y" enabled="y" send_to="10">'
        "<send>tts_interrupt %1</send></trigger>"
        '<trigger match="Welcome to the MUD" enabled="y" send_to="2">'
        "<send>Soundpack by Someone</send></trigger>"
        '<trigger match="Status *" enabled="y" send_to="3"><send>status text</send></trigger>'
        "</triggers>"
    )
    for text in ("You died.", "$buffer hello there", "Welcome to the MUD", "Status 5"):
        engine.process_line(Line(text))
    assert sink.sent == []
    assert engine.get_var("deaths") == "1"
    assert engine.get_var("spoken") == "hello there"
    assert ("Soundpack by Someone", "main") in sink.echoed


def test_send_to_variable_and_world_still_work():
    sink, engine, _pack = _rules(
        "<triggers>"
        '<trigger match="You see *" enabled="y" send_to="9" variable="seen">'
        "<send>%1</send></trigger>"
        '<trigger match="You are hungry." enabled="y"><send>eat bread</send></trigger>'
        "</triggers>"
    )
    engine.process_line(Line("You see a rat"))
    engine.process_line(Line("You are hungry."))
    assert engine.get_var("seen") == "a rat"
    assert sink.sent == ["eat bread"]


def test_omit_from_output_hides_the_line_and_print_shows_the_reworded_copy():
    # Empire's "omit and reprint" pattern: hide the MUD's line, print a speech-friendly one.
    sink, engine, pack = _rules(
        '<triggers><trigger match="Lie still; you are DEAD!!!" enabled="y" name="dead"'
        ' omit_from_output="y" send_to="14"><send>print("Lie still: you are DEAD!!!")</send>'
        "</trigger></triggers>"
    )
    line = engine.process_line(Line("Lie still; you are DEAD!!!"))
    assert line.gagged and not line.display_when_gagged
    assert sink.echoed == [("Lie still: you are DEAD!!!", "main")]
    # Spoken too, as MushReader (the pack's screen-reader plugin) speaks every note it draws.
    assert sink.spoken == [("Lie still: you are DEAD!!!", "main", False)]
    pack._enable_trigger("dead", 0)  # a disabled trigger stops hiding the line too
    assert not engine.process_line(Line("Lie still; you are DEAD!!!")).gagged


def test_note_send_and_tell_join_their_arguments_like_mushclient():
    sink, engine, _pack = _rules(
        '<aliases><alias match="hp" enabled="y" send_to="12"><send>'
        'Note("HP: ", 42, "/", 50) Send("say ", "hi") '
        'ColourTell("white", "black", "MP: ") Tell(7) ColourNote("white", "black", "!")'
        "</send></alias></aliases>"
    )
    engine.process_input("hp")
    assert sink.echoed == [("HP: 42/50", "main"), ("MP: 7!", "main")]
    assert sink.sent == ["say hi"]


def test_do_after_family_sends_and_notes_later():
    sink, engine, _pack = _rules(
        '<aliases><alias match="later" enabled="y" send_to="12"><send>'
        'DoAfter(2, "look") DoAfterNote(1, "noted") DoAfterSpecial(1, "Send(\'scripted\')", 1)'
        "</send></alias></aliases>"
    )
    engine.process_input("later")
    assert [delay for delay, _callback in sink.scheduled] == [2, 1, 1]
    sink.run_pending()
    assert sink.sent == ["look", "scripted"]  # DoAfterSpecial text always runs as Lua
    assert sink.echoed == [("noted", "main")]


def test_add_timer_repeats_unless_one_shot_and_obeys_enable_delete_and_reset():
    sink, engine, pack = _rules(
        '<aliases><alias match="go" enabled="y" send_to="12"><send>'
        'AddTimer("beat", 0, 0, 1, "ping", timer_flag.Enabled, "") '
        'AddTimer("once", 0, 0, 1, "pong", timer_flag.Enabled + timer_flag.OneShot, "")'
        "</send></alias></aliases>"
    )
    engine.process_input("go")
    sink.run_pending()
    assert sorted(sink.sent) == ["ping", "pong"]
    sink.run_pending()  # only the repeating timer re-armed
    assert sorted(sink.sent) == ["ping", "ping", "pong"]
    pack._enable_timer("beat", False)
    sink.run_pending()  # the fire scheduled before the disable does nothing
    assert sink.scheduled == []
    pack._enable_timer("beat", True)
    pack._reset_timer("beat")
    assert len(sink.scheduled) == 2  # enable and reset each scheduled; only the newest counts
    sink.run_pending()
    assert sink.sent.count("ping") == 3
    pack._delete_timer("beat")
    sink.run_pending()
    assert sink.sent.count("ping") == 3


def test_xml_timer_repeats_and_waits_while_disconnected():
    # Toastush's one-second heartbeat plays the roundtime sound; XML timers used to be ignored.
    sink, engine, _pack = _rules(
        '<timers><timer name="heartbeat" enabled="y" second="1" send_to="12">'
        '<send>Send("tick")</send></timer></timers>'
    )
    assert [delay for delay, _callback in sink.scheduled] == [1.0]
    sink.run_pending()
    engine.connected = False  # not active_closed: skip the action, keep the schedule
    sink.run_pending()
    engine.connected = True
    sink.run_pending()
    assert sink.sent == ["tick", "tick"]


def test_a_skipped_speech_plugins_commands_are_spoken_not_sent(tmp_path):
    # genericMud stands in for MushReader.xml, so its "tts_interrupt <text>" command has to
    # be answered here; Erion's channel history and Cosmic Rage's $buffer lines say things
    # that way. Skipping the plugin used to send those commands to the MUD.
    worlds = tmp_path / "worlds"
    (worlds / "plugins").mkdir(parents=True)
    (worlds / "plugins" / "MushReader.xml").write_text(
        '<muclient><plugin id="reader" name="MushReader" language="Lua"/></muclient>',
        encoding="latin-1",
    )
    (worlds / "w.MCL").write_text(
        '<muclient><world site="x" port="1" name="W"/>'
        '<include name="MushReader.xml" plugin="y"/>'
        '<triggers><trigger match="^\\$buffer (.*?)$" regexp="y" enabled="y" send_to="10">'
        "<send>tts_interrupt %1</send></trigger></triggers></muclient>",
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path))).load_file(
        str(worlds / "w.MCL")
    )
    engine.process_line(Line("$buffer incoming transmission"))
    assert sink.sent == []
    assert sink.speech_stops == 1
    assert ("incoming transmission", "pack", False) in sink.spoken


def test_a_rule_with_no_enabled_attribute_starts_disabled():
    # MUSHclient writes a boolean only when it's true, so no "enabled" means disabled; Materia
    # Magica keeps 211 capture-mode rules that way. defaults="y" restores the class defaults.
    sink, engine, _pack = _rules(
        "<triggers>"
        '<trigger match="spam" keep_evaluating="y" send_to="0"><send>off</send></trigger>'
        '<trigger match="spam" enabled="1" keep_evaluating="y" send_to="0"><send>on</send>'
        "</trigger></triggers>"
        '<triggers defaults="y"><trigger match="spam" keep_evaluating="y" send_to="0">'
        "<send>default</send></trigger></triggers>"
    )
    engine.process_line(Line("spam"))
    assert sorted(sink.sent) == ["default", "on"]


def test_send_text_takes_percent_escapes_and_named_captures():
    sink, engine, _pack = _rules(
        "<triggers>"
        '<trigger match="^(?P&lt;pct&gt;\\d+)% (.+) HP\\.$" regexp="y" enabled="y">'
        "<send>say %1%% of %2, %&lt;pct&gt;</send></trigger></triggers>"
    )
    engine.process_line(Line("85% 140 HP."))
    assert sink.sent == ["say 85% of 140, 85"]


@pytest.mark.parametrize(
    "escape",
    [
        "ATTACH DATABASE '{path}' AS o; CREATE TABLE o.t(x TEXT);"
        " INSERT INTO o.t VALUES ('\r\ncalc.exe\r\n')",
        "CREATE TABLE t(x TEXT); INSERT INTO t VALUES ('\r\ncalc.exe\r\n'); VACUUM INTO '{path}'",
    ],
)
def test_a_sandboxed_pack_cannot_write_outside_its_folder_through_sqlite(tmp_path, escape):
    # sqlite3.open() confines the database to the pack, but ATTACH and VACUUM INTO take any
    # path. This is the untrusted configuration the "Check compatibility" dry-run uses.
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    outside = tmp_path / "outside" / "startup.bat"
    outside.parent.mkdir()
    sql = escape.format(path=outside.as_posix())
    pack = MushclientPack(
        ScriptApi(AutomationEngine(RecordingSink()), source="p", base_dir=str(pack_dir)),
        full_stdlib=False,
    )
    pack.load_source(
        "<muclient><script><![CDATA[\n"
        'local db = sqlite3.open("state.db")\n'
        f"db:exec([[{sql}]])\n"
        "]]></script></muclient>"
    )
    assert not outside.exists()
    assert (pack_dir / "state.db").exists()  # the pack's own database still works


def test_history_add_without_a_channel_history_plugin_files_the_line_instead_of_sending_it():
    # Empire and Mystavaria call history_add expecting MUSHclient's channel_history plugin,
    # which players install globally. Nothing answered it here, so every chat line sent
    # "history_add say=Bob says, 'hi'" to the MUD.
    sink, engine, pack = _rules(
        '<triggers><trigger match="* says, *" enabled="y" send_to="12" sequence="100">'
        "<send>Execute(\"history_add say=%0\") Execute(\"history_add all=%0\")</send>"
        "</trigger></triggers>"
    )
    line = engine.process_line(Line("Bob says, hi"))
    assert sink.sent == []
    assert line.channel == "say"  # the channel review keys find it under "say"
    pack._lua.execute('Execute("history_add tell=a line the pack wrote itself")')
    assert sink.filed == [("a line the pack wrote itself", "tell")]
    assert sink.sent == []


def test_history_add_takes_any_channel_name_up_to_the_equals_sign():
    # channel_history's alias is "history_add *=*", so a channel is everything before the
    # first "=". Empire files "[/newbie-chat]: ..." under %1 and Chatpack under whatever
    # ChatMUD calls the channel; a name with a hyphen or a dot went to the MUD instead.
    sink, engine, pack = _rules("")
    pack._lua.execute('Execute("history_add newbie-chat=Bob: hi")')
    pack._lua.execute('Execute("history_add Private Communication=[You to Ann] hey")')
    pack._lua.execute('Execute("history_add dev.chat=a=b")')
    assert sink.sent == []
    assert sink.filed == [
        ("Bob: hi", "newbie-chat"),
        ("[You to Ann] hey", "private communication"),
        ("a=b", "dev.chat"),
    ]


def test_mushreader_commands_are_answered_even_when_the_pack_never_loads_mushreader():
    # Stellarmush and Mystavaria expect MushReader installed globally in MUSHclient, so they
    # never include it. genericMud only answered its commands when a pack included it, so
    # "tts_interrupt <text>" went to the MUD and the message was never spoken.
    sink, engine, pack = _rules(
        '<triggers><trigger match="^\\$alert (.*?)$" regexp="y" enabled="y" send_to="12">'
        '<send>Execute("tts_interrupt " .. "%1")</send></trigger></triggers>'
    )
    engine.process_line(Line("$alert red alert"))
    pack._lua.execute('Execute("tts_note quietly")')
    pack._lua.execute('Execute("tts_interrupt")')  # bare: just stop speaking
    pack._lua.execute('Execute("tts_stop")')
    assert sink.sent == []
    assert ("red alert", "pack", False) in sink.spoken
    assert ("quietly", "pack", False) in sink.spoken
    assert sink.speech_stops == 3


def test_a_packs_own_tts_alias_still_wins_over_genericmuds_answer():
    sink, engine, pack = _rules(
        '<aliases><alias match="^tts_interrupt (.*)$" regexp="y" enabled="y" send_to="12">'
        '<send>SetVariable("mine", "%1")</send></alias></aliases>'
    )
    pack._lua.execute('Execute("tts_interrupt from the pack")')
    assert pack._api.get_var("mine") == "from the pack"
    assert sink.spoken == []


def test_miriani_next_do_not_disturb_quiets_incoming_lines_and_brings_them_back():
    # Miriani-Next's own MushReader adds tts_disable_silent / tts_enable_silent for its Do Not
    # Disturb mode; genericMud skips that plugin, so both commands went to the MUD.
    sink, engine, pack = _rules("")
    pack._lua.execute('Execute("tts_disable_silent")')
    assert engine.line_speech is False
    pack._lua.execute('Execute("tts_enable_silent")')
    assert engine.line_speech is True
    assert sink.sent == []


# --- Log Notes: whose notes reach the session log -------------------------------------------


def _note_flags(sink) -> list[bool]:
    """What the sink was told about each echoed line: True where it belongs in the log."""
    return list(sink.echo_flags)


def test_a_packs_note_stays_out_of_the_session_log_by_default():
    # MUSHclient's Log Notes is off until a world turns it on, and packs note a lot: Empire notes
    # every death, and a world's whole log would otherwise fill with a pack's chatter.
    sink, engine, _pack = _rules(
        '<aliases><alias match="hp" enabled="y" send_to="12"><send>Note("HP: ", 42)</send>'
        "</alias></aliases>"
    )
    engine.process_input("hp")
    assert sink.echoed == [("HP: 42", "main")]  # shown, and spoken as MushReader would
    assert _note_flags(sink) == [False]


def test_a_world_can_keep_a_packs_notes_in_the_session_log():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"), log_notes=True)
    pack.load_source(
        '<?xml version="1.0"?><muclient><aliases><alias match="hp" enabled="y" send_to="12">'
        '<send>Note("HP: ", 42)</send></alias></aliases></muclient>'
    )
    engine.process_input("hp")
    assert sink.echoed == [("HP: 42", "main")]
    assert _note_flags(sink) == [True]


def test_the_world_setting_is_felt_by_a_pack_that_is_already_loaded():
    # A world edited while a tab is open applies at once, as its logging settings already do.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        '<?xml version="1.0"?><muclient><aliases><alias match="hp" enabled="y" send_to="12">'
        '<send>Note("HP")</send></alias></aliases></muclient>'
    )
    engine.process_input("hp")
    assert _note_flags(sink) == [False]
    pack.set_log_notes(True)
    engine.process_input("hp")
    assert _note_flags(sink) == [False, True]
    pack.set_log_notes(False)
    engine.process_input("hp")
    assert _note_flags(sink) == [False, True, False]


def test_a_worlds_own_scripts_still_keep_their_notes_whatever_the_setting():
    # The switch is named for soundpacks so nobody expects it to cover their own scripts: a note
    # a player writes into their own script is meant to be kept, and always has been.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    runtime = MushclientPack(ScriptApi(engine, source="user", base_dir=""), player_scripts=True)
    runtime._lua.execute('Note("level up")')
    assert sink.echoed == [("level up", "main")]
    assert _note_flags(sink) == [True]
