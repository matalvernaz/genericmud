"""Golden tests for the VIPMud .set interpreter."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.vipmud_dialect import (
    VipMudPack,
    _expand_sound_variant,
    _parse_vip_settings,
    _serialize_vip_settings,
    tokenize_statements,
)
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


def test_sphook_loop_action_loops_and_stores_handle(tmp_path):
    # A real file: a failed play now (correctly) stores handle 0, which is its own
    # test -- this one is about the handle bookkeeping for a play that worked.
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "intro.wav").write_bytes(b"RIFF")
    sink, engine = _load(SPHOOK, base_dir=str(tmp_path))
    engine.set_var("sppath", str(tmp_path))
    engine.process_line(Line("$sphook loop:music/intro:100:0:0:42"))
    cue = sink.played[-1]
    assert cue["loop"] is True
    # "#var @id %playhandle": @id == "42", so the handle is stored under var "42".
    assert engine.get_var("42") == cue["channel"].split("-")[1]


def test_sphook_unknown_action_plays_nothing():
    sink, engine = _load(SPHOOK, base_dir="/snd")
    engine.process_line(Line("$sphook bogus:x:50:0:0:1"))
    assert sink.played == []


def test_playloop_then_pc_stop_by_handle(tmp_path):
    (tmp_path / "a.wav").write_bytes(b"RIFF")
    sink, engine = _load(
        "#trigger {go} {#playloop {a.wav} 50}\n#trigger {halt} {#pc %playhandle stop}",
        base_dir=str(tmp_path),
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


def test_forall_iterates_with_loop_variable():
    # #ForAll runs its body once per |-item, substituting the %I loop token.
    sink, engine = _load("#KEY f1 {#ForAll {one|two|three} {#say {got %I}}}")
    engine.press_key("f1")
    assert [s[0] for s in sink.spoken] == ["got one", "got two", "got three"]


def test_forall_loads_each_listed_script(tmp_path):
    # The real loader idiom: #ForAll {a|b} {#load {Scripts\%I.set}} pulls in every script.
    (tmp_path / "main.set").write_text(
        "#ForAll {combat|keys} {#load {Scripts\\%I.set}}", encoding="utf-8"
    )
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "combat.set").write_text("#trigger {hit} {#say {boom}}", encoding="utf-8")
    (scripts / "keys.set").write_text("#key f1 {look}", encoding="utf-8")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(
        (tmp_path / "main.set").read_text(encoding="utf-8")
    )
    engine.process_line(Line("hit"))
    assert sink.spoken and sink.spoken[-1][0] == "boom"
    engine.press_key("f1")
    assert sink.sent == ["look"]


def test_gagline_voice_gags_speech_but_keeps_line_reviewable():
    sink, engine = _load("#TRIGGER {chatter *} {#gagline voice}")
    line = engine.process_line(Line("chatter from afar"))
    assert line.gagged is True
    assert line.display_when_gagged is True


def test_gagline_count_then_all_removes_line_entirely():
    # Prometheus writes "#gagline 1 all" (a count precedes the mode); no "voice" -> full gag.
    sink, engine = _load("#TRIGGER {spam} {#gagline 1 all}")
    line = engine.process_line(Line("spam"))
    assert line.gagged is True
    assert line.display_when_gagged is False


def test_alarm_defers_body_until_the_scheduler_fires():
    # Packs defer loading via "#alarm 0 {#load ...}" off a login trigger; nothing runs until
    # an event loop drives the scheduler (RecordingSink.run_pending stands in for it).
    sink, engine = _load("#TRIGGER {login} {#alarm 0 {#say {ready}}}")
    engine.process_line(Line("login"))
    assert sink.spoken == [] and sink.scheduled, "alarm body ran early or wasn't scheduled"
    sink.run_pending()
    assert sink.spoken == [("ready", "main", False)]


def test_abort_stops_the_rest_of_the_body():
    sink, engine = _load("#TRIGGER {go} {#say {first}; #abort; #say {second}}")
    engine.process_line(Line("go"))
    assert [s[0] for s in sink.spoken] == ["first"]


def test_sound_variant_expands_to_a_random_numbered_file():
    # VIPMud "name*N.wav" picks one of name1..nameN at random; plain names pass through.
    picks = {_expand_sound_variant("beep*3.wav") for _ in range(40)}
    assert picks <= {"beep1.wav", "beep2.wav", "beep3.wav"}
    assert len(picks) >= 2  # the choice varies (P(all 40 equal) is vanishing)
    plain = "Star Conquest\\Music\\plain.wav"  # no *N marker -> unchanged
    assert _expand_sound_variant(plain) == plain


def test_file_read_loads_settings_and_opens_the_sound_gate(tmp_path):
    # Star Conquest gates every #Play on @silent=1, a flag that lives in the pack's binary
    # Settings.set. Reading it (via #file/#Read) is what makes the pack audible.
    (tmp_path / "Settings.set").write_bytes(_serialize_vip_settings(["50", "1", "1"]))
    src = (
        "#if {NOT %Defined(silent)} {#var silent 0}\n"  # the pack's pre-read default: gated off
        "#file 6 {Star Conquest/Settings.set} 1\n"
        "#Read 6 vol 1\n#Read 6 socialson 2\n#Read 6 silent 3\n"
        "#TRIGGER {bonk} {#if {@silent = 1} {#play {CantGo.wav} @vol}}"
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(src)
    assert engine.get_var("silent") == "1"  # the read overrode the off-by-default
    assert engine.get_var("vol") == "50"
    engine.process_line(Line("bonk"))
    assert sink.played and sink.played[-1]["file"].endswith("CantGo.wav")


def test_file_write_close_persists_settings(tmp_path):
    settings = tmp_path / "Settings.set"
    settings.write_bytes(_serialize_vip_settings(["50", "0"]))
    engine = AutomationEngine(RecordingSink())
    pack = VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path)))
    pack.load_source("#file 6 {Settings.set}\n#Write 6 99 1\n#Write 6 1 2\n#Close 6")
    assert _parse_vip_settings(settings.read_bytes()) == ["99", "1"]  # flushed back in format


def test_gvar_sets_global_namespace_not_local():
    sink, engine = _load("#GVAR realm {aetherius}")
    assert engine.get_gvar("realm") == "aetherius"  # landed in the global map
    assert "realm" not in engine._vars  # not the local map (#GVAR is not #VAR)
    assert engine.get_var("realm") == "aetherius"  # @-reads still resolve it via fallback


# --- Cosmic Rage verification round (0.6.12): the parts of the real pack's $sphook
# dispatch that were broken -- stop-by-stored-handle, <>, comments, #math, #trig,
# alias expansion of bare body text, and failed-play handles.

CR_DISPATCH = """#trigger {$sphook &{action}:&{soundpath}:&{volume}:&{pitch}:&{pan}:&{id}} {
#if {@action = "loop"} {#playloop {@sppath/@soundpath.wav} @volume; #var @id %playhandle};
#if {@action = "play"} {#play {@sppath/@soundpath.wav} @volume};
#if {@pan <> "na"} {#math pan {@pan * 50}; #pc %playhandle pan @pan};
#if {@action = "stop"} {#if {%defined(@id) = 1} {#pc %var(@id) stop}}}"""


def _cr(tmp_path):
    (tmp_path / "amb.wav").write_bytes(b"RIFF")
    sink, engine = _load(CR_DISPATCH, base_dir=str(tmp_path))
    engine.set_var("sppath", str(tmp_path))
    return sink, engine


def test_cr_stop_by_stored_handle(tmp_path):
    # The server stores the loop's handle under a name it chose (%var/%defined were
    # unimplemented, so a server-issued stop was a no-op -> ambience stacking).
    sink, engine = _cr(tmp_path)
    engine.process_line(Line("$sphook loop:amb:50:na:na:h1"))
    assert sink.played[-1]["loop"] is True
    assert engine.get_var("h1") == "1"
    engine.process_line(Line("$sphook stop:na:na:na:na:h1"))
    assert sink.stopped == ["vip-1"]


def test_cr_not_equal_gates_and_math_pan(tmp_path):
    # <> (not-equal) wasn't parsed and #math wasn't implemented: every directional
    # cue collapsed to centre. pan "2" -> #math 2*50=100 -> #pc pan (live adjust).
    sink, engine = _cr(tmp_path)
    engine.process_line(Line("$sphook play:amb:80:na:2:na"))
    assert sink.played[-1]["gain"] == 0.8
    assert engine.get_var("pan") == "100"  # the #math result landed


def test_comment_lines_are_not_sent():
    # ";core variables"-style comment lines were tokenized as bare text and SENT to
    # the MUD at load. Mid-line ';' stays a statement separator.
    sink, _engine = _load(";comment at line start\n#var a 1;#var b 2\n  ;indented comment\n")
    assert sink.sent == []


def test_trig_abbreviation_registers_a_trigger():
    # Cosmic Rage's voice-only line arrives via `#trig {$buffer *}` -- TRIG wasn't a
    # recognized definition command, so the pack's speech line never registered.
    sink, engine = _load("#trig {$buffer *} {#say %1}")
    engine.process_line(Line("$buffer docking clamps released"))
    assert [s[0] for s in sink.spoken] == ["docking clamps released"]


def test_bare_body_text_expands_aliases():
    # VIPMud script bodies run text "as if typed": a body may call the pack's own
    # alias by name (CR's `makebetter`). Unmatched text still reaches the wire.
    sink, engine = _load(
        "#alias {makebetter} {#var better 1}\n"
        "#trigger {improve} {makebetter; look}\n"
    )
    engine.process_line(Line("improve"))
    assert engine.get_var("better") == "1"  # alias consumed
    assert sink.sent == ["look"]  # non-alias text sent


def test_failed_play_reports_handle_zero(tmp_path):
    # VIPMud reports a failed play as %playhandle 0; CR branches on it to speak the
    # failure and auto-fetch the file.
    sink, engine = _load(
        "#trigger {chirp} {#play {missing.wav}; #if {%playhandle=0} {#say failed}}",
        base_dir=str(tmp_path),
    )
    engine.process_line(Line("chirp"))
    assert [s[0] for s in sink.spoken] == ["failed"]


def test_unvar_deletes_for_defined(tmp_path):
    sink, engine = _load(
        "#trigger {setup} {#var flag 1; #unvar flag;"
        " #if {%defined(flag) = 0} {#say gone}}"
    )
    engine.process_line(Line("setup"))
    assert [s[0] for s in sink.spoken] == ["gone"]
