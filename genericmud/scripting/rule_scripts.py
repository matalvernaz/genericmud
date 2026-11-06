"""What a rule's "send to script" runs.

MUSHclient's "send to script" calls a named routine, handing it the matching line and the
wildcards -- and that is the whole reason a hand-made trigger can do more than its five built-in
actions: start a timer, enable a group of triggers, ask the player something, call anything the
client can do.

A world has one scripting space (:func:`genericmud.scripting.world_runtime.world_runtime`), and
this runs a rule's text in it, which is why the text is read in two ways:

* a name one of the world's scripts defines is **called**, with the line and the captures, which
  is MUSHclient's form and needs no pack to be written;
* anything else is **run as Lua code** against the same ``mud`` table and MUSHclient names those
  scripts use, so a rule can say its piece without a script file at all:
  ``mud.set_enabled("combat", false)``.

Being one state is the point of it: a rule calls a function wherever in the world's scripts it
lives, and leaves a value behind for the next rule to read.

Errors are spoken and logged rather than swallowed: a rule whose script is broken would otherwise
be a rule that silently stopped working.
"""

from __future__ import annotations

from collections.abc import Sequence

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.world_runtime import world_runtime

# The engine source for code rules write, so what a rule's script registers is filed under the
# world's own scripting (the scripts' rules carry their own ``user-script:<n>`` source).
RULE_SOURCE = "user-rule-script"


class RuleScripts:
    """Runs rule-authored Lua in the world's one scripting space."""

    def __init__(
        self,
        engine: AutomationEngine,
        base_dir: str,
        scripts: Sequence[object] = (),
    ) -> None:
        self.base_dir = str(base_dir)  # one runner per world, keyed by where its rules live
        self._engine = engine
        # None until something needs it. A world whose scripts loaded already has its state (that
        # is the one adopted below), and a world that sends nothing to script never pays for one.
        self._runtime = None
        self.set_scripts(scripts)

    def set_scripts(self, scripts: Sequence[object]) -> None:
        """Follow a reload of the world's scripts: the state they ran in is this one now."""
        for script in scripts:
            runtime = getattr(script, "runtime", None)
            if runtime is not None:
                # One state per world, so the first file's runtime is the whole world's.
                self._runtime = runtime
                return

    def _state(self):
        """The world's scripting space, built on first use if no script has made one."""
        if self._runtime is None:
            self._runtime = world_runtime(self._engine, self.base_dir, source_prefix=RULE_SOURCE)
        return self._runtime

    def run(self, text: str, *, line: str = "", wildcards: Sequence[str] = ()) -> None:
        """Run one rule's script text: by name if a script defines it, else as code."""
        source = text.strip()
        if not source:
            return
        state = self._state()
        function = self._lookup(state, source)
        if function is not None:
            function(line, state.table_of(list(wildcards)))
            return
        state.set_match(line, wildcards)  # `line` and `wildcards` for inline code
        state.run_source(source)

    def _lookup(self, state, name: str):
        """A Lua function of that name in the world's scripting space, or None."""
        if not name.isidentifier():
            return None  # not a bare name: it is code, however short
        return state.function(name)


def rule_scripts_for(
    engine: AutomationEngine, base_dir: str, scripts: Sequence[object] = ()
) -> RuleScripts:
    """The runner a world's rules use; built once and kept on the ScriptApi they call through."""
    return RuleScripts(engine, base_dir, scripts)
