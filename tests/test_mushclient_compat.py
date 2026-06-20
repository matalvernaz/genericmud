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


def _load(xml: str) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    MushclientPack(ScriptApi(engine, source="mushclient")).load_source(xml)
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
