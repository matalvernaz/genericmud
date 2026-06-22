"""Golden tests for the VIPMud .set interpreter."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.vipmud_dialect import VipMudPack, tokenize_statements
from tests.helpers import RecordingSink


def _load(source: str, base_dir: str | None = None) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vipmud", base_dir=base_dir)).load_source(source)
    return sink, engine


# The server-controlled sound core of real packs (Cosmic Rage VIPMud Immersion).
SPHOOK = """#trigger {$sphook &{action}:&{soundpath}:&{volume}:&{pitch}:&{pan}:&{id}} {
#if {@action = "loop"} {#playloop {@sppath/@soundpath.wav} @volume; #var @id %playhandle};
#if {@action = "play"} {#play {@sppath/@soundpath.wav} @volume};
}"""


def test_key_say_with_var_substitution():
    sink, engine = _load("#VAR hp {42}\n#KEY f2 {#say {@hp hp}}")
    assert engine.get_var("hp") == "42"
    engine.press_key("f2")
    assert sink.spoken == [("42 hp", "main", False)]


def test_trigger_wildcard_and_say():
    sink, engine = _load("#TR {You see *} {#say {found %1}}")
    engine.process_line(Line("You see a dragon"))
    assert sink.spoken == [("found a dragon", "main", False)]


def test_trigger_play_sound_with_volume():
    sink, engine = _load("#TRIGGER {* hits you} {#play {hit.wav} 80}")
    engine.process_line(Line("A goblin hits you"))
    assert sink.played and sink.played[0]["file"] == "hit.wav"
    assert abs(sink.played[0]["gain"] - 0.8) < 1e-9


def test_alias_with_wildcard_sends():
    sink, engine = _load("#ALIAS {gt *} {tell group %1}")
    assert engine.process_input("gt hello team") == []
    assert sink.sent == ["tell group hello team"]


def test_bare_line_in_body_sends():
    sink, engine = _load("#KEY f3 {kill orc}")
    engine.press_key("f3")
    assert sink.sent == ["kill orc"]


def test_tokenizer_preserves_nested_braces():
    statements = tokenize_statements("#KEY f2 {#say {@hp hp, @mp mp}}")
    command, args = statements[0]
    assert command == "KEY"
    assert args[0].text == "f2"
    assert args[1].text == "#say {@hp hp, @mp mp}"


def test_sphook_play_action_named_wildcards_and_sppath_default():
    # @sppath defaults to the pack dir; named wildcards resolve as @action/@soundpath/@volume.
    sink, engine = _load(SPHOOK, base_dir="/snd")
    engine.process_line(Line("$sphook play:general/misc/on:80:0:0:1"))
    assert sink.played, "no sound played"
    cue = sink.played[-1]
    assert cue["file"] == "/snd/general/misc/on.wav"
    assert abs(cue["gain"] - 0.8) < 1e-9
    assert cue["loop"] is False


def test_sphook_loop_action_loops_and_stores_handle():
    sink, engine = _load(SPHOOK, base_dir="/snd")
    engine.process_line(Line("$sphook loop:music/intro:100:0:0:42"))
    cue = sink.played[-1]
    assert cue["loop"] is True
    # "#var @id %playhandle": @id == "42", so the handle is stored under var "42".
    assert engine.get_var("42") == cue["channel"].split("-")[1]


def test_sphook_unknown_action_plays_nothing():
    sink, engine = _load(SPHOOK, base_dir="/snd")
    engine.process_line(Line("$sphook bogus:x:50:0:0:1"))
    assert sink.played == []


def test_playloop_then_pc_stop_by_handle():
    sink, engine = _load(
        "#trigger {go} {#playloop {a.wav} 50}\n#trigger {halt} {#pc %playhandle stop}"
    )
    engine.process_line(Line("go"))
    channel = sink.played[-1]["channel"]
    engine.process_line(Line("halt"))
    assert channel in sink.stopped


def test_if_numeric_branch_and_assignment_not_sent():
    sink, engine = _load("#alias {clamp} {#var v {150}; #if {@v > 100} {@v = 100}}")
    assert engine.process_input("clamp") == []
    assert engine.get_var("v") == "100"  # "@v = 100" assigns, not sent
    assert sink.sent == []


def test_load_chains_in_another_set_file(tmp_path):
    # #load pulls in the pack's other scripts, so triggers spread across files register.
    (tmp_path / "main.set").write_text("#load {sub.set}", encoding="utf-8")
    (tmp_path / "sub.set").write_text("#trigger {boom} {#play {bang.wav}}", encoding="utf-8")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(
        (tmp_path / "main.set").read_text(encoding="utf-8")
    )
    engine.process_line(Line("boom"))
    # base_dir is set, so the relative sound resolves under the pack dir.
    assert any(played["file"].endswith("bang.wav") for played in sink.played)


def test_load_resolves_by_filename_across_layouts(tmp_path):
    # A loader referencing @scpath/x.set still finds x.set when the layout differs.
    (tmp_path / "boot.set").write_text("#load {@scpath/deep.set}", encoding="utf-8")
    nested = tmp_path / "Scripts"
    nested.mkdir()
    (nested / "deep.set").write_text("#trigger {hi} {#say {found}}", encoding="utf-8")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    # @scpath defaults to the pack dir; deep.set lives in a subdir -> matched by name.
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(
        (tmp_path / "boot.set").read_text(encoding="utf-8")
    )
    engine.process_line(Line("hi"))
    assert sink.spoken and sink.spoken[-1][0] == "found"


def test_world_sounds_dir_overrides_pack_default_sppath():
    # The session sets @sppath from world.sounds before packs load; the pack must not
    # clobber it, so sounds resolve against the world's folder, not the pack dir.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.set_var("sppath", "/my/sounds")
    VipMudPack(ScriptApi(engine, source="vip", base_dir="/pack")).load_source(SPHOOK)
    engine.process_line(Line("$sphook play:combat/hit:100:0:0:1"))
    assert sink.played[-1]["file"] == "/my/sounds/combat/hit.wav"
