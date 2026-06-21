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
from genericmud.scripting.guard import ScriptGuard

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


def make_sandboxed_runtime() -> tuple[LuaRuntime, object | None]:
    """A sandboxed LuaRuntime plus an install_hook callable for the script guard.

    install_hook(check, n) registers a Lua debug count-hook (a Lua closure, since
    lupa rejects a Python hook) that calls ``check`` every n instructions. It's
    captured before ``debug`` is removed from the sandbox; None if unavailable.
    """
    lua = LuaRuntime(unpack_returned_tuples=True, register_eval=False, register_builtins=False)
    globals_ = lua.globals()
    install_hook = None
    if globals_.debug is not None:
        # Capture debug.sethook as an upvalue NOW, before `debug` is removed below;
        # the returned installer sets a Lua count-hook that calls `check`.
        installer_src = (
            "(function()"
            " local sethook = debug.sethook;"
            " return function(check, n) sethook(function() check() end, '', n) end"
            " end)()"
        )
        install_hook = lua.eval(installer_src)
    for name in _SANDBOX_REMOVE:
        globals_[name] = None
    return lua, install_hook


class LuaPackRuntime:
    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._lua, install_hook = make_sandboxed_runtime()
        self._guard = ScriptGuard(install_hook)
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
        mud.trigger = self._lua_register(api.add_trigger, routable=True)
        mud.alias = self._lua_register(api.add_alias)
        mud.key = self._lua_register_key()
        mud.set_channel = self._lua_set_channel()
        self._lua.globals().mud = mud

    def _lua_register(self, register: Callable[..., None], *, routable: bool = False):
        """Build a mud.trigger/mud.alias function that bridges a Lua callback.

        With opts {regex=, priority=, channel=}; channel only applies to triggers.
        A nil callback registers a pure routing/policy rule (no script action).
        """
        lua = self._lua

        def factory(pattern, callback, opts=None):
            has_opts = opts is not None
            regex = bool(opts["regex"]) if has_opts and opts["regex"] is not None else False
            priority = int(opts["priority"]) if has_opts and opts["priority"] is not None else 0
            kwargs: dict = {"regex": regex, "priority": priority}
            if routable and has_opts and opts["channel"] is not None:
                kwargs["channel"] = opts["channel"]

            if callback is None:
                register(pattern, None, **kwargs)
                return

            def py_callback(ctx: MatchContext) -> None:
                self._guard.run(callback, ctx.line.plain_text, lua.table_from(ctx.wildcards[1:]))

            register(pattern, py_callback, **kwargs)

        return factory

    def _lua_register_key(self):
        api = self._api

        def factory(key, callback):
            def py_callback(_ctx: MatchContext) -> None:
                self._guard.run(callback)

            api.add_key(key, py_callback)

        return factory

    def _lua_set_channel(self):
        """mud.set_channel(name, {speak=, display=, interrupt=, voice=}) -> policy."""
        api = self._api

        def factory(name, opts=None):
            def opt(key, default):
                return opts[key] if opts is not None and opts[key] is not None else default

            api.set_channel(
                name,
                speak=bool(opt("speak", True)),
                display=bool(opt("display", True)),
                interrupt=bool(opt("interrupt", False)),
                voice=opt("voice", None),
            )

        return factory

    def run_source(self, code: str):
        return self._guard.run_strict(self._lua.execute, code)

    def run_file(self, path: str):
        with open(path, encoding="utf-8") as handle:
            return self.run_source(handle.read())
