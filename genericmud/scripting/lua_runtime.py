"""Native Lua scripting runtime (the first-class authoring path).

Embeds Lua via ``lupa`` and exposes genericMud's primitives as a global ``mud``
table backed by :class:`ScriptApi`. The runtime is sandboxed: the dangerous
standard libraries (``os``/``io``/``package``/``debug``) and lupa's Python
bridge are removed, so a soundpack cannot touch the filesystem or host process.
One runtime (one Lua state) per loaded pack.

Native callback signature mirrors MUSHclient minus the name:
``mud.trigger(pattern, function(line, wildcards) ... end, {regex=true, priority=50})``
where ``wildcards[1]`` is the first capture.
"""

from __future__ import annotations

from collections.abc import Callable

from lupa import LuaRuntime

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi

# Removed from the sandbox: filesystem, process, dynamic code loading, lupa bridge.
_SANDBOX_REMOVE = (
    "os",
    "io",
    "package",
    "require",
    "dofile",
    "loadfile",
    "load",
    "loadstring",
    "debug",
    "python",
)


def make_sandboxed_runtime() -> LuaRuntime:
    """A LuaRuntime with the dangerous globals and lupa's Python bridge removed."""
    lua = LuaRuntime(unpack_returned_tuples=True, register_eval=False, register_builtins=False)
    globals_ = lua.globals()
    for name in _SANDBOX_REMOVE:
        globals_[name] = None
    return lua


class LuaPackRuntime:
    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._lua = make_sandboxed_runtime()
        self._install_mud()

    def _install_mud(self) -> None:
        api = self._api
        mud = self._lua.table()
        # ScriptApi methods already carry the right defaults, so bind directly.
        mud.send = api.send
        mud.echo = api.echo
        mud.speak = api.speak
        mud.play = api.play
        mud.stop = api.stop
        mud.music = api.music
        mud.get_var = api.get_var
        mud.set_var = api.set_var
        mud.trigger = self._lua_register(api.add_trigger)
        mud.alias = self._lua_register(api.add_alias)
        mud.key = self._lua_register_key()
        self._lua.globals().mud = mud

    def _lua_register(self, register: Callable[..., None]):
        """Build a mud.trigger/mud.alias function that bridges a Lua callback."""
        lua = self._lua

        def factory(pattern, callback, opts=None):
            has_opts = opts is not None
            regex = bool(opts["regex"]) if has_opts and opts["regex"] is not None else False
            priority = int(opts["priority"]) if has_opts and opts["priority"] is not None else 0

            def py_callback(ctx: MatchContext) -> None:
                callback(ctx.line.plain_text, lua.table_from(ctx.wildcards[1:]))

            register(pattern, py_callback, regex=regex, priority=priority)

        return factory

    def _lua_register_key(self):
        api = self._api

        def factory(key, callback):
            def py_callback(_ctx: MatchContext) -> None:
                callback()

            api.add_key(key, py_callback)

        return factory

    def run_source(self, code: str):
        return self._lua.execute(code)

    def run_file(self, path: str):
        with open(path, encoding="utf-8") as handle:
            return self.run_source(handle.read())
