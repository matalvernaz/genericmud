"""Reading MUD data and script values (issue #4): the listing, and speech that uses them."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.automation.engine import AutomationEngine
from genericmud.automation.variables import (
    GMCP,
    MSDP,
    MSSP,
    ROW_VALUE_CHARS,
    SCRIPT,
    VariableEntry,
    filter_variables,
    format_value,
    list_variables,
)
from genericmud.config.keymap import load_keymap
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.packs.user_rules import UserAlias, UserKey, UserRules, UserTrigger, register_rules
from genericmud.protocol import msdp
from genericmud.protocol.telnet import OPT_GMCP, OPT_MSDP, OPT_MSSP, Subnegotiation
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import REVIEW_CHANNEL, VoiceRouter
from tests.helpers import RecordingBackend, RecordingSink


def _register(rules: UserRules, tmp_path) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    register_rules(ScriptApi(engine, source=user_rules.SOURCE, base_dir=str(tmp_path)), rules)
    return sink, engine


def _app() -> EngineApp:
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    return EngineApp(voice, keymap=load_keymap("vipmud"))


def _names(entries: list[VariableEntry]) -> list[str]:
    return [entry.name for entry in entries]


def _stored(*values: tuple[str, str, object]) -> dict[str, object]:
    """MUD data the way EngineApp._handle_subnegotiation stores it: bare name plus a
    source-prefixed copy of the same value."""
    out: dict[str, object] = {}
    for source, name, value in values:
        out[name] = value
        out[f"{source}.{name}"] = value
    return out


# --- the listing ---


def test_gmcp_tables_flatten_to_the_paths_references_use():
    entries, truncated = list_variables(
        _stored((GMCP, "Char.Vitals", {"hp": 90, "maxhp": 100})), {}
    )
    assert not truncated
    assert _names(entries) == ["Char.Vitals.hp", "Char.Vitals.maxhp"]  # not listed twice
    hp = entries[0]
    assert (hp.value, hp.source, hp.reference) == ("90", GMCP, "${mud:Char.Vitals.hp}")


def test_every_reference_the_list_offers_resolves_to_the_value_shown():
    app = _app()
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'Char.Vitals {"hp":42,"mp":7}'))
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'Room.Info {"name":"Temple","exits":{"n":5}}'))
    app.on_telnet_event(
        Subnegotiation(OPT_MSDP, msdp.encode_msdp("HEALTH", "500"))
    )
    app.on_telnet_event(Subnegotiation(OPT_MSSP, b"\x01NAME\x02Test MUD"))
    app.engine.set_var("target", "goblin")
    entries, _truncated = app.variable_listing()
    by_name = {entry.name: entry for entry in entries}
    for name, source, value in (
        ("Char.Vitals.hp", GMCP, "42"),
        ("Room.Info.exits.n", GMCP, "5"),
        ("HEALTH", MSDP, "500"),
        ("NAME", MSSP, "Test MUD"),
        ("target", SCRIPT, "goblin"),
    ):
        entry = by_name[name]
        assert (entry.source, entry.value) == (source, value)
        api = ScriptApi(app.engine)
        assert api.expand_speech(entry.reference) == value


def test_mud_data_comes_first_grouped_by_protocol_then_script_values():
    entries, _ = list_variables(
        _stored((MSSP, "PLAYERS", "3"), (MSDP, "HEALTH", "5"), (GMCP, "b", 1), (GMCP, "A", 2)),
        {"zeta": "1", "alpha": "2"},
    )
    assert [(entry.source, entry.name) for entry in entries] == [
        (GMCP, "A"), (GMCP, "b"), (MSDP, "HEALTH"), (MSSP, "PLAYERS"),
        (SCRIPT, "alpha"), (SCRIPT, "zeta"),
    ]


def test_lists_stay_one_row_and_values_format_like_expansion():
    entries, _ = list_variables(
        _stored((GMCP, "Char.Items", {"list": [{"id": 1}, {"id": 2}], "flag": True,
                                      "none": None, "empty": {}})),
        {},
    )
    by_name = {entry.name: entry.value for entry in entries}
    assert by_name == {
        "Char.Items.list": '[{"id":1},{"id":2}]',
        "Char.Items.flag": "true",
        "Char.Items.none": "",
        "Char.Items.empty": "{}",
    }
    assert format_value(True) == "true" and format_value(None) == ""


def test_the_server_cannot_grow_the_list_without_bound():
    flood = _stored(*((GMCP, f"Spam{index}", index) for index in range(50)))
    entries, truncated = list_variables(flood, {}, limit=10)
    assert len(entries) == 10 and truncated
    deep: dict = {"leaf": 1}
    for _ in range(40):
        deep = {"d": deep}
    entries, _ = list_variables(_stored((GMCP, "Deep", deep)), {})
    assert len(entries) == 1  # past the depth cap the rest is one JSON row


def test_rows_read_as_speech_and_details_carry_the_reference():
    entry = VariableEntry("Char.Vitals.hp", "90", GMCP)
    assert entry.row == "Char.Vitals.hp, 90, GMCP"
    assert "Use it as: ${mud:Char.Vitals.hp}" in entry.details
    long_entry = VariableEntry("Blob", "x" * 500, SCRIPT)
    assert len(long_entry.row) < ROW_VALUE_CHARS + 30
    assert VariableEntry("Empty", "", SCRIPT).row == "Empty, empty, script"
    assert VariableEntry("target", "orc", SCRIPT).reference == "${script:target}"


def test_filter_matches_any_part_of_the_name_in_any_case():
    entries = [VariableEntry("Char.Vitals.hp", "1", GMCP), VariableEntry("HEALTH", "2", MSDP)]
    assert _names(filter_variables(entries, "VITALS")) == ["Char.Vitals.hp"]
    assert _names(filter_variables(entries, "  ")) == ["Char.Vitals.hp", "HEALTH"]


# --- speech that reads variables ---


def test_hotkey_reads_a_mud_value_on_the_reply_channel(tmp_path):
    rules = UserRules(keys=[UserKey(key="f2", speak="${mud:Char.Vitals.hp} health")])
    sink, engine = _register(rules, tmp_path)
    engine.set_mud_var("Char.Vitals", {"hp": 90})
    assert engine.press_key("f2")
    # Answers the keypress straight away: not queued behind MUD output, not throttled by
    # the flood governor, and still heard with self-voice off.
    assert sink.spoken == [("90 health", REVIEW_CHANNEL, True)]


def test_a_missing_value_is_said_every_time_not_skipped(tmp_path):
    rules = UserRules(keys=[UserKey(key="f2", speak="HP ${mud:Char.Vitals.hp}")])
    sink, engine = _register(rules, tmp_path)
    engine.press_key("f2")
    engine.press_key("f2")
    assert [text for text, _channel, _interrupt in sink.spoken] == [
        "HP no value for Char.Vitals.hp",
        "HP no value for Char.Vitals.hp",
    ]


def test_an_alias_can_read_a_value_without_sending_anything(tmp_path):
    rules = UserRules(aliases=[UserAlias(pattern="hp", speak="${mud:HEALTH} of ${mud:MAX}")])
    sink, engine = _register(rules, tmp_path)
    engine.set_mud_var("HEALTH", 50)
    engine.set_mud_var("MAX", 80)
    assert engine.process_input("hp") == []
    assert sink.sent == []
    # On the main channel, like before: an alias can fire in a background tab (/to,
    # mud.send_to, a pack's Execute), and the review channel speaks through the mute
    # that keeps background tabs quiet.
    assert sink.spoken == [("50 of 80", "main", False)]


def test_trigger_speech_fills_captures_and_script_values(tmp_path):
    rules = UserRules(triggers=[UserTrigger(
        pattern="* attacks you", speak="${1} again, target ${script:target}, %1",
    )])
    sink, engine = _register(rules, tmp_path)
    engine.set_var("target", "orc")
    engine.process_line(Line("goblin attacks you"))
    assert sink.spoken[-1] == ("goblin again, target orc, goblin", "main", False)


def test_mud_text_containing_a_template_is_read_literally(tmp_path):
    rules = UserRules(triggers=[UserTrigger(pattern="says *", speak="%1")])
    sink, engine = _register(rules, tmp_path)
    engine.set_var("secret", "should-not-expand")
    engine.process_line(Line("Bob says ${script:secret}"))
    assert sink.spoken[-1][0] == "${script:secret}"


def test_command_expansion_still_refuses_a_missing_value():
    api = ScriptApi(AutomationEngine(RecordingSink()))
    try:
        api.expand_command("kill ${script:nobody}")
    except ValueError as error:
        assert "unknown command variable: script:nobody" in str(error)
    else:
        raise AssertionError("a command with a missing value must not be sent")
    assert api.expand_speech("hi ${nobody}") == "hi no value for nobody"


def test_values_read_the_way_commands_would_put_them():
    assert format_value({"hp": 1}) == '{"hp":1}'
    assert format_value(True) == "true" and format_value(None) == ""


def test_an_alias_fired_in_a_background_tab_stays_quiet(tmp_path):
    # /to from another tab reaches this session through _dispatch_remote. The tab is in
    # the background, so its voice is muted; the alias's reply must not speak through
    # that mute, or cut off the tab the player is actually in.
    from genericmud.app import EngineApp
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    backend = RecordingBackend()
    app = EngineApp(VoiceRouter(backend, clock=lambda: 0.0))
    rules = UserRules(aliases=[UserAlias(pattern="hp", speak="${mud:HEALTH} health")])
    register_rules(ScriptApi(app.engine, source=user_rules.SOURCE, base_dir=str(tmp_path)), rules)
    app.engine.set_mud_var("HEALTH", 50)
    app.voice.set_muted(True)  # what SessionPanel._apply_active does for a background tab
    app._dispatch_remote("hp")
    assert backend.spoken == [] and backend.stops == 0


def test_awkward_shapes_never_offer_a_reference_that_reads_something_else():
    # Found in review: dotted concatenation offered rows whose reference resolved to a
    # different value, or to nothing. Every row must read back exactly what it shows,
    # and no two rows may share a reference.
    from genericmud.app import EngineApp
    from genericmud.config.keymap import load_keymap
    from genericmud.protocol import msdp
    from genericmud.protocol.telnet import OPT_GMCP, OPT_MSDP, Subnegotiation
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    app = EngineApp(VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
                    keymap=load_keymap("vipmud"))
    for payload in (
        b'Char {"Vitals":{"hp":1},"level":5}',  # "Char.Vitals.hp" would read package Char.Vitals
        b'Char.Vitals {"hp":2}',
        b'Char.Status {"current.hp":7,"name":"Bob"}',  # a dot inside a key
        b'Room.Info {"exits":["n","s"]}',
    ):
        app.on_telnet_event(Subnegotiation(OPT_GMCP, payload))
    msdp_payload = bytes([msdp.MSDP_VAR]) + b"gmcp.hack" + bytes([msdp.MSDP_VAL]) + b"9"
    app.on_telnet_event(Subnegotiation(OPT_MSDP, msdp_payload))  # a name shaped like a prefix

    entries, _ = app.variable_listing()
    api = ScriptApi(app.engine)
    references = [entry.reference for entry in entries]
    assert len(references) == len(set(references))
    for entry in entries:
        assert api.expand_speech(entry.reference) == entry.value, entry
    by_name = {entry.name: entry for entry in entries}
    assert by_name["Char.Vitals.hp"].value == "2"
    assert by_name["Char.level"].value == "5"
    assert by_name["Char.Status"].value == '{"current.hp":7,"name":"Bob"}'
    assert by_name["gmcp.hack"].source == "msdp"


def test_a_wide_table_is_not_walked_past_the_row_limit():
    # The limit has to bound the work, not only the result: a server decides how wide a
    # table is, and the listing runs on the loop thread every session shares.
    from genericmud.automation.variables import list_variables

    visited = 0

    class Watched(dict):
        def items(self):
            nonlocal visited
            for pair in super().items():
                visited += 1
                yield pair

    wide = Watched({f"k{index}": index for index in range(100_000)})
    entries, truncated = list_variables({"Wide": wide, "gmcp.Wide": wide}, {}, limit=10)
    assert truncated and len(entries) == 10
    assert visited < 50
