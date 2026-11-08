"""Pack activation: dialect dispatch, sound-path rooting, failure isolation, conflicts."""

from __future__ import annotations

import os

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs import PackStore, activate_world
from tests.helpers import RecordingDiag, RecordingSink


def _install(store, tmp_path, name, body, *, world="mud", trust=True):
    src = tmp_path / name
    src.write_text(body, encoding="utf-8")
    return store.install(src, world=world, trust=trust)


def _engine_and_store(tmp_path):
    sink = RecordingSink()
    return sink, AutomationEngine(sink), PackStore(tmp_path / "store")


def test_activate_lua_pack_registers_a_working_trigger(tmp_path):
    sink, engine, store = _engine_and_store(tmp_path)
    _install(
        store, tmp_path, "hunt.lua",
        'mud.trigger("You see *", function(line, wc) mud.send("get " .. wc[1]) end)',
    )
    result = activate_world(store, "mud", engine)
    assert result.loaded == ["hunt"]
    engine.process_line(Line("You see a sword"))
    assert sink.sent == ["get a sword"]


def test_activate_vipmud_pack_dispatches_by_dialect(tmp_path):
    _sink, engine, store = _engine_and_store(tmp_path)
    _install(store, tmp_path, "cosmic.set", "#alias hi {#send hello}")
    result = activate_world(store, "mud", engine)
    assert result.loaded == ["cosmic"]
    assert engine.process_input("hi") == []  # the alias consumed the input


def test_pack_sound_paths_resolve_against_the_pack_dir(tmp_path):
    sink, engine, store = _engine_and_store(tmp_path)
    _install(store, tmp_path, "sfx.lua", 'mud.play("hit.wav")')  # fires on load
    activate_world(store, "mud", engine)
    assert sink.played[0]["file"] == os.path.join(str(store.pack_dir("sfx")), "hit.wav")


def test_a_failing_pack_is_isolated_not_fatal(tmp_path):
    sink, engine, store = _engine_and_store(tmp_path)
    _install(store, tmp_path, "good.lua", 'mud.trigger("ping", function() mud.send("pong") end)')
    _install(store, tmp_path, "bad.lua", "this is not valid lua (((")
    result = activate_world(store, "mud", engine)
    assert "good" in result.loaded
    assert "bad" in result.failed and result.failed["bad"]
    engine.process_line(Line("ping"))
    assert sink.sent == ["pong"]  # the good pack still works


def test_activate_mushclient_world_includes_its_plugins(tmp_path):
    # The real multi-file MUSHclient path: a .MCL world (the entry) <include>s a plugin,
    # which must load on the shared runtime and arm its trigger after activation.
    sink, engine, store = _engine_and_store(tmp_path)
    pack = tmp_path / "erionish"
    pack.mkdir()
    (pack / "boom.xml").write_text(
        '<?xml version="1.0" encoding="iso-8859-1"?>\n'
        '<muclient><plugin name="boom" id="boompack"/>\n'
        '<triggers><trigger match="boom" enabled="y" regexp="n" send_to="12">'
        '<send>Sound("boom.wav")</send></trigger></triggers></muclient>',
        encoding="latin-1",
    )
    (pack / "world.MCL").write_text(
        '<?xml version="1.0" encoding="iso-8859-1"?>\n<!DOCTYPE muclient>\n'
        '<muclient><include name="boom.xml"/></muclient>',
        encoding="latin-1",
    )
    store.install(pack, world="mud", entry="world.MCL", trust=True)
    result = activate_world(store, "mud", engine)
    assert result.loaded == ["erionish"]  # the .MCL world loaded as a mushclient pack
    engine.process_line(Line("boom"))
    assert any(p["file"].endswith("boom.wav") for p in sink.played)


def test_activation_reports_optional_plugins_and_bad_rules(tmp_path):
    _sink, engine, store = _engine_and_store(tmp_path)
    pack = tmp_path / "compat"
    pack.mkdir()
    (pack / "bad.lua").write_text("error('broken module')", encoding="latin-1")
    (pack / "world.MCL").write_text(
        "<muclient><script><![CDATA[optional_plugins={x='extra'}; require('bad')]]></script>"
        "<triggers>"
        '<trigger match="bad" enabled="y" send_to="12"><send>invalid lua.</send></trigger>'
        '<trigger match="good" enabled="y" send_to="0"><send>look</send></trigger>'
        "</triggers></muclient>",
        encoding="latin-1",
    )
    store.install(pack, world="mud", entry="world.MCL", trust=True)
    result = activate_world(store, "mud", engine)
    assert result.loaded == ["compat"]
    assert result.skipped_plugins["compat"] == [
        ("extra.xml", "declared optional by the pack")
    ]
    assert result.skipped_rules["compat"][0][0] == "bad"
    assert result.module_errors["compat"][0][0].startswith("require(bad)")


def test_trusted_mushclient_pack_gets_full_stdlib(tmp_path):
    # A trusted pack's script uses stdlib the sandbox normally strips: os.time, loadstring,
    # and a module(..., package.seeall) library (whose own ipairs needs seeall). activate_world
    # threads trusted -> full_stdlib, so all of it works and the guarded Send fires.
    sink, engine, store = _engine_and_store(tmp_path)
    pack = tmp_path / "stdlibpack"
    (pack / "lib").mkdir(parents=True)
    (pack / "lib" / "mylib.lua").write_text(
        "module(..., package.seeall)\n"
        "function count() local n=0 for _ in ipairs({1,2,3}) do n=n+1 end return n end\n",
        encoding="latin-1",
    )
    (pack / "world.MCL").write_text(
        '<?xml version="1.0" encoding="iso-8859-1"?>\n<!DOCTYPE muclient>\n'
        "<muclient><script><![CDATA[\n"
        'local m = require("mylib")\n'
        "local now = os.time()\n"
        'local f = loadstring("return 42")\n'
        'if m and m.count() == 3 and now and f and f() == 42 then Send("stdlib-ok") end\n'
        "]]></script></muclient>",
        encoding="latin-1",
    )
    store.install(pack, world="mud", entry="world.MCL", trust=True)
    result = activate_world(store, "mud", engine)
    assert result.loaded == ["stdlibpack"]
    assert result.failed == {}
    assert "stdlib-ok" in sink.sent


def test_untrusted_mushclient_pack_stays_sandboxed(tmp_path):
    # The same stdlib reliance, untrusted: loaded with require_trust=False it stays
    # sandboxed (os/loadstring black-holed to no-ops), so the guarded Send never fires.
    sink, engine, store = _engine_and_store(tmp_path)
    pack = tmp_path / "stdlibpack"
    pack.mkdir()
    (pack / "world.MCL").write_text(
        '<?xml version="1.0" encoding="iso-8859-1"?>\n<!DOCTYPE muclient>\n'
        "<muclient><script><![CDATA[\n"
        'local f = loadstring("return 42")\n'
        'if os.time() and f and f() == 42 then Send("stdlib-ok") end\n'
        "]]></script></muclient>",
        encoding="latin-1",
    )
    store.install(pack, world="mud", entry="world.MCL", trust=False)
    activate_world(store, "mud", engine, require_trust=False)
    assert "stdlib-ok" not in sink.sent


def test_detect_key_conflict_between_two_packs(tmp_path):
    _sink, engine, store = _engine_and_store(tmp_path)
    _install(store, tmp_path, "packa.lua", 'mud.key("f1", function() mud.send("a") end)')
    _install(store, tmp_path, "packb.lua", 'mud.key("f1", function() mud.send("b") end)')
    result = activate_world(store, "mud", engine)
    keys = [c for c in result.conflicts if c.kind == "key"]
    assert len(keys) == 1
    assert keys[0].token == "f1"
    assert keys[0].sources == ("packa", "packb")


def test_no_conflict_when_packs_bind_distinct_keys(tmp_path):
    _sink, engine, store = _engine_and_store(tmp_path)
    _install(store, tmp_path, "packa.lua", 'mud.key("f1", function() end)')
    _install(store, tmp_path, "packb.lua", 'mud.key("f2", function() end)')
    result = activate_world(store, "mud", engine)
    assert result.conflicts == []


def test_untrusted_enabled_pack_is_skipped(tmp_path):
    sink, engine, store = _engine_and_store(tmp_path)
    _install(
        store, tmp_path, "hunt.lua",
        'mud.trigger("ping", function() mud.send("pong") end)', trust=False,
    )
    result = activate_world(store, "mud", engine)
    assert result.loaded == []
    assert result.skipped_untrusted == ["hunt"]
    engine.process_line(Line("ping"))
    assert sink.sent == []  # untrusted pack never armed its trigger


def test_diag_pack_counts_distinguishes_armed_from_inert(tmp_path):
    # The diag trace must make an inert pack (loaded, zero triggers) visible -- that's the
    # signature of a silent soundpack that loaded but can never fire (candidate D).
    sink, engine, store = _engine_and_store(tmp_path)
    engine.diag = RecordingDiag()
    _install(store, tmp_path, "armed.lua",
             'mud.trigger("ping", function() mud.play("p.wav") end)')
    _install(store, tmp_path, "inert.lua", 'mud.send("loaded")')  # no trigger registered
    activate_world(store, "mud", engine)
    counts = {f["id"]: f["triggers"] for stage, f in engine.diag.events if stage == "pack.counts"}
    assert counts == {"armed": 1, "inert": 0}


def test_require_trust_false_loads_untrusted(tmp_path):
    sink, engine, store = _engine_and_store(tmp_path)
    _install(
        store, tmp_path, "hunt.lua",
        'mud.trigger("ping", function() mud.send("pong") end)', trust=False,
    )
    result = activate_world(store, "mud", engine, require_trust=False)
    assert result.loaded == ["hunt"]
    engine.process_line(Line("ping"))
    assert sink.sent == ["pong"]


def _note_pack(store, tmp_path):
    """A MUSHclient pack whose alias notes a line, so its Note can be watched."""
    _install(
        store, tmp_path, "notes.xml",
        '<?xml version="1.0"?><muclient><aliases><alias match="hp" enabled="y" send_to="12">'
        '<send>Note("HP: 42")</send></alias></aliases></muclient>',
    )


def test_a_worlds_log_notes_setting_reaches_the_pack_it_loads(tmp_path):
    # The world's answer travels with the activation, so a pack loaded by the app behaves as the
    # world asked without the pack needing to know where the setting lives.
    sink, engine, store = _engine_and_store(tmp_path)
    _note_pack(store, tmp_path)
    activate_world(store, "mud", engine, log_notes=True)
    engine.process_input("hp")
    assert sink.echoed == [("HP: 42", "main")]
    assert sink.echo_flags == [True]


def test_a_pack_loaded_without_the_setting_keeps_its_notes_out_of_the_log(tmp_path):
    sink, engine, store = _engine_and_store(tmp_path)
    _note_pack(store, tmp_path)
    activate_world(store, "mud", engine)
    engine.process_input("hp")
    assert sink.echoed == [("HP: 42", "main")]
    assert sink.echo_flags == [False]


def _vipmud_note_pack(store, tmp_path):
    """A VIPMud pack whose alias shows a line with #Say, the way packs reword a message."""
    _install(store, tmp_path, "cosmic.set", "#alias hp {#say {You have 42 health.}}")


def test_a_worlds_log_notes_setting_reaches_a_vipmud_pack_too(tmp_path):
    # A VIPMud pack shows its reworded lines with #Say, which never reached the log either. The
    # same world switch covers it, or a player on Star Conquest or Cosmic Rage ticks the box and
    # nothing happens for them.
    sink, engine, store = _engine_and_store(tmp_path)
    _vipmud_note_pack(store, tmp_path)
    activate_world(store, "mud", engine, log_notes=True)
    engine.process_input("hp")
    assert sink.echoed == [("You have 42 health.", "main")]
    assert sink.echo_flags == [True]


def test_a_vipmud_pack_keeps_its_say_lines_out_of_the_log_without_the_setting(tmp_path):
    sink, engine, store = _engine_and_store(tmp_path)
    _vipmud_note_pack(store, tmp_path)
    activate_world(store, "mud", engine)
    engine.process_input("hp")
    assert sink.echoed == [("You have 42 health.", "main")]
    assert sink.echo_flags == [False]


def test_a_vipmud_say_that_shows_nothing_leaves_nothing_for_the_log(tmp_path):
    # NoDisplay shows nothing at all, so the switch has nothing to take: still shown, still
    # logged, nothing gets through.
    sink, engine, store = _engine_and_store(tmp_path)
    _install(store, tmp_path, "quiet.set", "#alias hp {#say {Not shown.} NoDisplay}")
    activate_world(store, "mud", engine, log_notes=True)
    engine.process_input("hp")
    assert sink.echoed == []


def test_the_setting_is_felt_by_a_vipmud_pack_that_is_already_loaded(tmp_path):
    # A world edited while its tab is open applies at once, for VIPMud packs as for MUSHclient.
    sink, engine, store = _engine_and_store(tmp_path)
    _vipmud_note_pack(store, tmp_path)
    result = activate_world(store, "mud", engine)
    (pack,) = result.packs.values()
    engine.process_input("hp")
    assert sink.echo_flags == [False]
    pack.set_log_notes(True)
    engine.process_input("hp")
    assert sink.echo_flags == [False, True]
    pack.set_log_notes(False)
    engine.process_input("hp")
    assert sink.echo_flags == [False, True, False]
