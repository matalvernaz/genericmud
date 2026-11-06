"""A world's script files are one program, in one Lua state.

Splitting automation across numbered files is the normal way to keep it readable -- settings,
combat, healing -- and it only works if the files can see each other: a function defined in
10-settings.lua has to be callable from 20-combat.lua. This is also what makes a rule's "send to
script" find a function wherever in the world's scripts it lives.
"""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting import user_scripts
from tests.helpers import RecordingSink

lupa = pytest.importorskip("lupa")
lua51 = pytest.importorskip("lupa.lua51")  # a world's own scripts run in Lua 5.1


def _load(tmp_path, files: dict[str, str]) -> tuple[RecordingSink, list]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    for name, source in files.items():
        (tmp_path / "scripts").mkdir(exist_ok=True)
        user_scripts.save_script(tmp_path, name, source)
    loaded = user_scripts.load_scripts(engine, tmp_path, source_prefix="user-script:1")
    return sink, loaded, engine


SETTINGS = """
function warn_low_hp()
    mud.command("say health low")
end
"""

COMBAT = """
mud.trigger("You are bleeding", function()
    warn_low_hp()
end)
"""


def test_a_function_from_an_earlier_file_is_callable_in_a_later_one(tmp_path):
    sink, _loaded, engine = _load(tmp_path, {"10-settings.lua": SETTINGS, "20-combat.lua": COMBAT})
    engine.process_line(Line("You are bleeding"))
    assert sink.sent == ["say health low"]


def test_the_files_share_one_lua_state(tmp_path):
    _sink, loaded, _engine = _load(tmp_path, {"10-a.lua": "x = 1", "20-b.lua": "y = 2"})
    assert len({id(item.runtime) for item in loaded}) == 1  # one state for the whole world
    state = loaded[0].runtime
    assert state.function("warn_low_hp") is None  # nothing defined here, and no error


def test_a_later_file_wins_when_two_define_the_same_name(tmp_path):
    # Reading the files in order is the mental model, so the last definition is the one in force.
    first = 'function who() mud.command("say first") end'
    second = 'function who() mud.command("say second") end\nmud.trigger("test", who)'
    sink, _loaded, engine = _load(tmp_path, {"10-a.lua": first, "20-b.lua": second})
    engine.process_line(Line("test"))
    assert sink.sent == ["say second"]


def test_a_broken_file_discards_the_whole_set_and_leaves_the_old_one(tmp_path):
    # Half a new state is worse than the old one: a rule registered by the first file must not
    # survive a second file that fails.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    (tmp_path / "scripts").mkdir()
    user_scripts.save_script(
        tmp_path, "10-good.lua", 'mud.trigger("test", function() mud.command("say yes") end)'
    )
    first = user_scripts.load_scripts(engine, tmp_path, source_prefix="user-script:1")
    assert first
    engine.process_line(Line("test"))
    assert sink.sent == ["say yes"]

    user_scripts.save_script(tmp_path, "20-broken.lua", "this is not lua ((")
    with pytest.raises(lua51.LuaSyntaxError):  # the file is not Lua at all
        user_scripts.load_scripts(engine, tmp_path, source_prefix="user-script:2")
    # Only the old set is registered: nothing from the half-loaded one survived.
    assert [rule.source for rule in engine._triggers] == ["user-script:1"]
    engine.process_line(Line("test"))
    assert sink.sent == ["say yes", "say yes"]  # the old trigger fired once more, not twice


def test_a_local_stays_local_to_its_file(tmp_path):
    files = {
        "10-a.lua": "local secret = 1",
        "20-b.lua": (
            'mud.trigger("check", function() '
            'if secret == nil then mud.command("say none") end end)'
        ),
    }
    sink, _loaded, engine = _load(tmp_path, files)
    engine.process_line(Line("check"))
    assert sink.sent == ["say none"]
