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
from pathlib import Path

from lupa import LuaRuntime

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.guard import ScriptGuard, ScriptTimeout

# Removed from the sandbox: filesystem, process, dynamic code loading, lupa bridge, and
# coroutines. The loop guard's debug count-hook is per-Lua-thread and is NOT inherited by a
# coroutine, so an untrusted pack could run `coroutine.resume(coroutine.create(loop-forever))`
# and hang the engine past the 1s deadline. Trusted (full_stdlib) packs keep coroutine.
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
    "coroutine",
)


def _deny_dunder_attrs(obj: object, attr_name: object, is_setting: bool) -> object:
    """lupa attribute filter: block Lua access to ``_``-prefixed Python attributes.

    Exposed objects (the ``mud`` API methods) are meant to be CALLED, never
    introspected. Without this, a pack escapes the sandbox through any bound
    method: ``mud.send.__globals__`` hands back the api module's namespace --
    ``os``, ``__builtins__``, ``__import__``, ``eval`` -- which is arbitrary code
    execution even with the ``os``/``io``/``python`` globals removed. Blocking
    every dunder severs the ``__self__``/``__class__``/``__globals__`` gadget chain.
    """
    if isinstance(attr_name, str) and attr_name.startswith("_"):
        raise AttributeError("sandbox: access to private attributes is denied")
    return attr_name


def make_sandboxed_runtime(
    *, lua51: bool = False, full_stdlib: bool = False
) -> tuple[LuaRuntime, object | None]:
    """A sandboxed LuaRuntime plus an install_hook callable for the script guard.

    install_hook(check, n) registers a Lua debug count-hook (a Lua closure, since
    lupa rejects a Python hook) that calls ``check`` every n instructions. It's
    captured before ``debug`` is removed from the sandbox; None if unavailable.

    ``lua51=True`` uses lupa's Lua 5.1 backend, which is the dialect MUSHclient
    embeds — its plugin scripts assume 5.1 semantics (e.g. writable for-loop
    variables) that 5.4 rejects.

    ``full_stdlib=True`` keeps the rest of the Lua standard library (``os``/``io``/
    ``loadstring``/``package``) instead of stripping it — real MUSHclient soundpacks
    assume the full stdlib (MUSHclient runs them unsandboxed), and the ``module(...,
    package.seeall)`` idiom their libraries use needs ``package``. It still removes the
    escape hatches that no soundpack uses and that exceed even that grant: the lupa
    Python bridge (``python``), native-code loading (``package.loadlib``), the lupa
    object registry (``debug.getregistry``), and the hook primitive a pack could use to
    disable our loop guard (``debug.sethook`` — captured above first). Reserved for
    *trusted* packs; untrusted packs get the fully locked-down set.
    """
    runtime_cls = LuaRuntime
    if lua51:
        from lupa.lua51 import LuaRuntime as Lua51Runtime

        runtime_cls = Lua51Runtime
    lua = runtime_cls(
        unpack_returned_tuples=True,
        register_eval=False,
        register_builtins=False,
        attribute_filter=_deny_dunder_attrs,
    )
    globals_ = lua.globals()
    install_hook = None
    if globals_.debug is not None:
        # Capture debug.sethook as an upvalue NOW, before `debug` is (possibly) removed;
        # the returned installer sets a Lua count-hook that calls `check`.
        installer_src = (
            "(function()"
            " local sethook = debug.sethook;"
            " return function(check, n) sethook(function() check() end, '', n) end"
            " end)()"
        )
        install_hook = lua.eval(installer_src)
    if full_stdlib:
        globals_["python"] = None  # the lupa<->host bridge; not part of any Lua stdlib
        # Close the in-process escape hatches a soundpack never uses but that exceed the
        # FS/process surface trusted packs are granted: the lupa object registry and our
        # loop guard's own hook (already captured above).
        #
        # Native-code loading stays disabled too -- but as a truthy NO-OP, not a missing
        # function. Real MUSHclient plugins bootstrap their native module with
        # `assert(package.loadlib(dll, sym))()`; a nil loadlib makes that assert THROW and
        # abort the entire OnPluginInstall (Erion's LuaAudio and mushReader both died here).
        # genericMud already provides those modules' surface itself (the `audio` shim; `nvda`
        # et al. via the compat black hole), so the DLL load is redundant -- returning a
        # harmless no-op loader lets the assert pass and install continue, while still never
        # loading a DLL.
        lua.execute(
            "if package then package.loadlib = function() return function() end end end\n"
            "if debug then debug.getregistry = nil; debug.sethook = nil end"
        )
    else:
        for name in _SANDBOX_REMOVE:
            globals_[name] = None
    return lua, install_hook


def install_pack_require(
    lua: LuaRuntime, base_dir: str | None, builtins: dict | None = None, fallback: object = None
) -> None:
    """Install a ``require()`` scoped to the pack directory (+ optional builtins).

    A trusted pack may load its OWN bundled Lua libraries (``json``, ``ppi``, ...)
    via ``require``; resolution is confined to the pack dir (looked up by filename
    anywhere under it, MUSHclient-style), so it can't reach the host filesystem.
    ``builtins`` maps a module name to a value returned verbatim — used to hand a
    plugin our own ``ppi`` shim instead of its bundled ``ppi.lua`` (which needs
    ``package.seeall``, stripped by the sandbox). Note: require only makes a file
    *load* — a lib that then calls unimplemented MUSHclient APIs still fails in use.
    """
    builtins = dict(builtins or {})
    if not base_dir and not builtins and fallback is None:
        return
    index = {path.name.lower(): path for path in Path(base_dir).rglob("*.lua")} if base_dir else {}
    cache: dict[str, object] = {}
    # Resolve package.loaded[key] via rawget so a black-hole _G.__index metatable (the
    # MUSHclient compat layer installs one) can't fool the lookup into a no-op table.
    _loaded_module = lua.eval(
        "function(key)"
        " local p = rawget(_G, 'package'); if type(p) ~= 'table' then return nil end;"
        " local l = rawget(p, 'loaded'); if type(l) ~= 'table' then return nil end;"
        " return rawget(l, key) end"
    )
    # Stdlib (string/table/math/os/io/...) is a raw global, not a pack file; rawget bypasses the
    # compat layer's black-hole _G metatable so we hand back the real library, not a no-op.
    _global_module = lua.eval(
        "function(key) local v = rawget(_G, key); if type(v) == 'table' then return v end end"
    )

    def _require(name: object = "", *_args: object) -> object:
        key = str(name)
        if key in builtins:
            return builtins[key]
        if key in cache:
            return cache[key]
        target = key.replace(".", "/").rsplit("/", 1)[-1].lower() + ".lua"
        path = index.get(target)
        if path is None:
            stdlib = _loaded_module(key) or _global_module(key)  # require "string"/"table"/...
            if stdlib is not None:
                cache[key] = stdlib
                return stdlib
            if fallback is not None:
                # A native/external module (socket.core, luacom, ...) we can't provide: hand back
                # a black-hole so the plugin loads and its sound path runs, that feature no-op'd.
                cache[key] = fallback
                return fallback
            raise FileNotFoundError(f"pack module {key!r} not found")
        cache[key] = None  # sentinel: break a require cycle before executing
        try:
            code = path.read_text(encoding="latin-1", errors="ignore")
            result = lua.eval("function(...)\n" + code + "\nend")(key)
        except ScriptTimeout:
            # A runaway loop in a required module is a real failure of a hostile/broken pack, not
            # a missing optional module -- surface it (fail the pack load), don't black-hole it.
            cache.pop(key, None)
            raise
        except Exception:
            # A bundled module that errors on load (e.g. luasocket's pure-Lua layer without its
            # native core) must not kill the plugin that required it: black-hole it if we can.
            # (Catch Exception, not a specific lupa.LuaError -- the class differs per Lua backend;
            # the loop guard raises BaseException, which still propagates below.)
            cache.pop(key, None)
            if fallback is not None:
                cache[key] = fallback
                return fallback
            raise
        except BaseException:
            cache.pop(key, None)  # a failed load must not stay cached as if it returned nil
            raise
        if result is None:
            # A module(..., package.seeall)-style lib (Lua 5.1) registers itself in
            # package.loaded under its name and returns nothing; hand back that table.
            result = _loaded_module(key)
        cache[key] = result
        return result

    lua.globals().require = _require


class LuaPackRuntime:
    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._script_error_spoken = False  # speak the first fire-time script fault, trace the rest
        self._lua, install_hook = make_sandboxed_runtime()
        # native packs are sandboxed; report contained faults so a broken trigger isn't silent
        self._guard = ScriptGuard(install_hook, require_hook=True, report=self._report_error)
        self._install_mud()
        install_pack_require(self._lua, api.base_dir)

    def _report_error(self, error: Exception) -> None:
        """A fire-time trigger/timer fault is contained by the guard; trace every one and speak
        the first, so a silently-dropped callback (a missing accessibility cue) isn't invisible."""
        if self._api.diag is not None:
            self._api.diag.event(
                "script.error", source=self._api.source or "?",
                error=f"{type(error).__name__}: {error}",
            )
        if not self._script_error_spoken:
            self._script_error_spoken = True
            self._api.speak(
                f"A soundpack script error occurred: {type(error).__name__}", channel="system"
            )

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
        mud.set_volume = api.set_volume
        mud.mute = api.mute
        mud.send_to = api.send_to
        mud.broadcast = api.broadcast
        mud.sessions = self._lua_sessions()
        mud.shared_get = api.shared_get
        mud.shared_set = api.shared_set
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

    def _lua_sessions(self):
        """mud.sessions() -> a 1-indexed Lua table of concurrent session names."""
        api = self._api
        lua = self._lua

        def factory():
            return lua.table_from(api.sessions())

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
