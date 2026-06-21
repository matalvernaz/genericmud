"""Pack activation: dialect dispatch, sound-path rooting, failure isolation, conflicts."""

from __future__ import annotations

import os

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs import PackStore, activate_world
from tests.helpers import RecordingSink


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
