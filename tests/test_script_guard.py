"""The Lua execution guard aborts runaway scripts instead of hanging the engine."""

from __future__ import annotations

import time

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.lua_runtime import LuaPackRuntime
from tests.helpers import RecordingSink


def test_runaway_trigger_is_aborted_and_runtime_recovers():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    runtime = LuaPackRuntime(ScriptApi(engine, source="lua"))
    if not runtime._guard.enabled:
        pytest.skip("Lua debug hook unavailable; guard inactive")
    runtime._guard._max_seconds = 0.3  # short budget so the test is fast

    runtime.run_source('mud.trigger("spin", function() while true do end end)')
    runtime.run_source('mud.trigger("hi", function() mud.send("ok") end)')

    start = time.monotonic()
    engine.process_line(Line("spin"))  # runaway: must abort, not hang
    assert time.monotonic() - start < 2.0
    assert isinstance(runtime._guard.last_error, Exception)

    engine.process_line(Line("hi"))  # runtime still works afterwards
    assert "ok" in sink.sent
