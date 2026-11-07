"""What a rule's "send to script" runs.

MUSHclient's "send to script" calls a named routine, handing it the matching line and the
wildcards -- and that is the whole reason a hand-made trigger can do more than its five built-in
actions: start a timer, enable a group of triggers, ask the player something, call anything the
client can do.

This client's scripts are Lua files loaded per world, each with its own runtime, so a rule's text
is read in two ways:

* a name one of the world's scripts defines is **called**, with the line and the captures, which
  is MUSHclient's form and needs no pack to be written;
* anything else is **run as Lua code** against the same ``mud`` table those scripts use, so a
  rule can say its piece without a script file at all: ``mud.set_enabled("combat", false)``.

Errors are spoken and logged rather than swallowed: a rule whose script is broken would otherwise
be a rule that silently stopped working.
"""

from __future__ import annotations

from collections.abc import Sequence

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.lua_runtime import LuaPackRuntime

# One runtime for the code rules write, shared by every rule of this world, so a rule can leave a
# value behind for the next one (and for the player's own sense of it being one scripting space).
RULE_SOURCE = "user-rule-script"


class RuleScripts:
    """Runs rule-authored Lua: a function from a loaded script, or the text itself."""

    def __init__(
        self,
        engine: AutomationEngine,
        base_dir: str,
        scripts: Sequence[object] = (),
    ) -> None:
        self.base_dir = str(base_dir)  # one runner per world, keyed by where its rules live
        self._scripts = list(scripts)
        self._runtime = LuaPackRuntime(
            ScriptApi(engine, source=RULE_SOURCE, base_dir=base_dir)
        )

    def set_scripts(self, scripts: Sequence[object]) -> None:
        """Follow a reload of the world's scripts, so a rule calls what is loaded now."""
        self._scripts = list(scripts)

    def run(self, text: str, *, line: str = "", wildcards: Sequence[str] = ()) -> None:
        """Run one rule's script text: by name if a script defines it, else as code."""
        source = text.strip()
        if not source:
            return
        found = self._lookup(source)
        if found is not None:
            runtime, function = found
            # The captures are built by the runtime the function came from: a table from one Lua
            # state cannot be handed to a function in another ("cannot mix objects from
            # different Lua runtimes"), and every script file has its own state.
            function(line, runtime.table_of(list(wildcards)))
            return
        # Wrapped in a function, so `line` and `wildcards` are its parameters: as globals
        # they overwrote whatever a script had called by either name, every time a rule fired.
        self._runtime.run_with_match(source, line=line, wildcards=wildcards)

    def _lookup(self, name: str):
        """The runtime and a Lua function of that name from the loaded scripts, or None."""
        if not name.isidentifier():
            return None
        found = None
        for script in self._scripts:
            runtime = getattr(script, "runtime", None)
            function = getattr(runtime, "function", None)
            if function is None:
                continue
            candidate = function(name)
            if candidate is not None:
                found = (runtime, candidate)  # last one loaded wins, as a later file would
        return found


def rule_scripts_for(
    engine: AutomationEngine, base_dir: str, scripts: Sequence[object] = ()
) -> RuleScripts:
    """The runner a world's rules use; built once and kept on the ScriptApi they call through."""
    return RuleScripts(engine, base_dir, scripts)
