"""Native Lua scripting runtime (the first-class authoring path).

Embeds Lua via ``lupa`` and exposes genericMud's primitives as a global ``mud``
table backed by :class:`ScriptApi`. The runtime is sandboxed: the dangerous
standard libraries (``os``/``io``/``package``/``debug``) and lupa's Python
bridge are removed, so a soundpack cannot touch the filesystem or host process.
One runtime (one Lua state) per loaded pack.

:class:`MudTable` is that table on its own, so it can also be laid beside MUSHclient's own
vocabulary in the world's one scripting space (``genericmud.scripting.world_runtime``), which is
where a player's own scripts run.

Native callback signature mirrors MUSHclient minus the name:
``mud.trigger(pattern, function(line, wildcards) ... end, {regex=true, priority=50})``
where ``wildcards[1]`` is the first capture.

``mud.command`` accepts one command or a Lua array of commands. ``${1}``/named captures,
``${script:name}``, and ``${mud:Char.Vitals.hp}`` are expanded before each command is sent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from lupa import LuaRuntime

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.guard import ScriptGuard, ScriptTimeout

_MAX_COMMANDS_PER_CALL = 100

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
    lua: LuaRuntime,
    base_dir: str | None,
    builtins: dict | None = None,
    fallback: object = None,
    on_error: Callable[[str, Path, Exception], None] | None = None,
    scope: Callable[[], tuple[object, object, object]] | None = None,
) -> Callable[[object], None]:
    """Install a ``require()`` scoped to the pack directory (+ optional builtins).

    A trusted pack may load its OWN bundled Lua libraries (``json``, ``ppi``, ...)
    via ``require``; resolution is confined to the pack dir (looked up by filename
    anywhere under it, MUSHclient-style), so it can't reach the host filesystem.
    ``builtins`` maps a module name to a value returned verbatim — used to hand a
    plugin our own ``ppi`` shim instead of its bundled ``ppi.lua`` (which needs
    ``package.seeall``, stripped by the sandbox). Note: require only makes a file
    *load* — a lib that then calls unimplemented MUSHclient APIs still fails in use.

    ``scope`` answers who is requiring, as (key, globals table, package.loaded table), the
    tables None for the shared ones. A module then runs once for each key, in that key's
    globals, the way each MUSHclient plugin loads its own copy into its own Lua state; a
    module built with ``module()`` registers in that key's loaded table. Returns a function
    that forgets one key's modules, so they run again for it.
    """
    builtins = dict(builtins or {})
    if not base_dir and not builtins and fallback is None:
        return lambda _key: None
    module_paths: list[tuple[str, Path]] = []
    if base_dir:
        root = Path(base_dir)
        for path in sorted(root.rglob("*.lua"), key=lambda item: item.as_posix().casefold()):
            relative = path.relative_to(root).with_suffix("")
            module_paths.append((relative.as_posix().replace("/", ".").casefold(), path))
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
    _legacy_module = lua.eval(
        "function(key, root)"
        " local current = root or _G;"
        " for segment in string.gmatch(key, '[^.]+') do"
        "  local child = rawget(current, segment);"
        "  if type(child) ~= 'table' then child = {}; rawset(current, segment, child) end;"
        "  current = child;"
        " end;"
        " setmetatable(current, {__index = root or _G});"
        " if root == nil and package and package.loaded then package.loaded[key] = current end;"
        " return current"
        " end"
    )
    _setfenv = lua.eval("setfenv")
    scoped: dict[object, dict[str, object]] = {}  # each scope's own copies of modules

    def _require(name: object = "", *_args: object) -> object:
        key = str(name)
        if key in builtins:
            return builtins[key]
        scope_key, env, loaded = scope() if scope is not None else (None, None, None)
        shared = env is None
        # A plugin never gets the world's copy of a module, which would be bound to the
        # world's globals: it has its own, cached separately.
        store = cache if shared else scoped.setdefault(scope_key, {})
        if key in store:
            return store[key]
        requested = key.replace("\\", ".").replace("/", ".").removesuffix(".lua").casefold()
        candidates: list[tuple[int, str, Path]] = []
        for module_name, candidate in module_paths:
            import_names = [module_name]
            if module_name.endswith(".init"):
                import_names.append(module_name.removesuffix(".init"))
            for import_name in import_names:
                if import_name == requested:
                    extra_segments = 0
                elif import_name.endswith("." + requested):
                    extra_segments = import_name.count(".") - requested.count(".")
                else:
                    continue
                candidates.append((extra_segments, module_name, candidate))
        path = min(candidates, default=None, key=lambda item: (item[0], item[1]))
        path = path[2] if path is not None else None
        if path is None:
            stdlib = _loaded_module(key) or _global_module(key)  # require "string"/"table"/...
            if stdlib is not None:
                store[key] = stdlib
                return stdlib
            if fallback is not None:
                # A native/external module (socket.core, luacom, ...) we can't provide: hand back
                # a black-hole so the plugin loads and its sound path runs, that feature no-op'd.
                store[key] = fallback
                return fallback
            raise FileNotFoundError(f"pack module {key!r} not found")
        try:
            code = path.read_text(encoding="latin-1", errors="ignore")
        except OSError as exc:
            # Unreadable, as for a module that errors on load below: the plugin goes on.
            if on_error is not None:
                on_error(key, path, exc)
            if fallback is not None:
                store[key] = fallback
                return fallback
            raise
        if code.startswith("#!"):
            code = code.partition("\n")[2]
        store[key] = None  # sentinel: break a require cycle before executing
        try:
            chunk = lua.eval("function(...)\n" + code + "\nend", name="@" + path.as_posix())
            if not shared:
                _setfenv(chunk, env)
            result = chunk(key)
        except ScriptTimeout:
            # A runaway loop in a required module is a real failure of a hostile/broken pack, not
            # a missing optional module -- surface it (fail the pack load), don't black-hole it.
            store.pop(key, None)
            raise
        except Exception as exc:
            # A bundled module that errors on load (e.g. luasocket's pure-Lua layer without its
            # native core) must not kill the plugin that required it: black-hole it if we can.
            # (Catch Exception, not a specific lupa.LuaError -- the class differs per Lua backend;
            # the loop guard raises BaseException, which still propagates below.)
            store.pop(key, None)
            if on_error is not None:
                on_error(key, path, exc)
            if fallback is not None:
                store[key] = fallback
                return fallback
            raise
        except BaseException:
            store.pop(key, None)  # a failed load must not stay cached as if it returned nil
            raise
        if result is None:
            # A module(..., package.seeall)-style lib (Lua 5.1) registers itself in
            # package.loaded under its name and returns nothing; hand back that table.
            if shared:
                result = _loaded_module(key)
            elif loaded is not None:
                result = loaded[key]
        if result is None and "." in key:
            # Some portable-MUSHclient packs split dotted modules across files that write
            # globals and return nothing, then read them through the dotted namespace. Keep
            # bare no-return modules nil, but provide that legacy namespace for qualified ones.
            result = _legacy_module(key, None if shared else env)
        store[key] = result
        return result

    lua.globals().require = _require
    return lambda scope_key: scoped.pop(scope_key, None)


class MudTable:
    """The ``mud`` table: genericMud's own scripting vocabulary, built for one Lua state.

    Two kinds of runtime install it. A native Lua pack gets it *instead* of MUSHclient's
    vocabulary (see :class:`LuaPackRuntime`), because that is the dialect its author wrote for;
    the player's own scripting space gets it *beside* MUSHclient's, so a script there can call
    either (see ``genericmud.scripting.world_runtime``).

    It is bound to the state and the loop guard of whichever runtime hosts it, so a callback is
    timed and reported by that runtime, however the table got there.
    """

    def __init__(self, lua: LuaRuntime, api: ScriptApi, guard: ScriptGuard) -> None:
        self._lua = lua
        self._api = api
        self._guard = guard
        self._match_context: MatchContext | None = None  # the match the running callback is for

    def install(self) -> object:
        """Build the table, put it in the state's globals as ``mud``, and return it."""
        api = self._api
        mud = self._lua.table()
        # ScriptApi methods already carry the right defaults, so bind directly.
        mud.send = api.send
        mud.execute = api.execute
        mud.command = self._lua_command()
        mud.echo = api.echo
        mud.speak = api.speak
        mud.play = api.play
        mud.stop = api.stop
        mud.music = api.music
        mud.get_var = api.get_var
        mud.set_var = api.set_var
        mud.delete_var = api.delete_var
        mud.get_mud_var = self._lua_get_mud_var()
        mud.mud_vars = self._lua_mud_vars()
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
        mud.timer = self._lua_timer()
        mud.on_data = self._lua_on_data()
        mud.set_channel = self._lua_set_channel()
        mud.set_enabled = api.set_enabled  # MUSHclient's EnableTrigger, by label
        mud.set_group_enabled = api.set_group_enabled  # its EnableGroup, by group name
        mud.add_timer = self._lua_add_timer()  # AddTimer: a timer with a name
        mud.delete_timer = api.remove_named_timer  # DeleteTimer
        mud.enable_timer = api.set_timer_enabled  # EnableTimer
        self._lua.globals().mud = mud
        return mud

    def _run_callback(self, callback, ctx: MatchContext | None, *args: object):
        previous = self._match_context
        self._match_context = ctx
        try:
            return self._guard.run(callback, *args)
        finally:
            self._match_context = previous

    def _callback_values(self, overrides=None) -> dict[str, object]:
        values: dict[str, object] = {}
        if self._match_context is not None:
            for index, value in enumerate(self._match_context.wildcards[1:], start=1):
                values[str(index)] = value
            values.update(self._match_context.named)
        if overrides is not None:
            try:
                values.update({str(key): value for key, value in overrides.items()})
            except AttributeError as error:
                raise TypeError("command variables must be a Lua table") from error
        return values

    def _lua_command(self):
        """mud.command(string|{strings}, vars?) -> variable expansion + direct MUD sends."""
        api = self._api

        def factory(commands, variables=None):
            if isinstance(commands, str):
                # Match command-box stacking while also making a multiline Lua string useful.
                pending = [
                    part.strip() for line in commands.splitlines() or [commands]
                    for part in line.split(";") if part.strip()
                ]
            else:
                pending = []
                for index in range(1, _MAX_COMMANDS_PER_CALL + 2):
                    command = commands[index]
                    if command is None:
                        break
                    pending.append(str(command).strip())
                else:
                    raise ValueError("too many commands")
            if len(pending) > _MAX_COMMANDS_PER_CALL:
                raise ValueError(f"too many commands (maximum {_MAX_COMMANDS_PER_CALL})")
            values = self._callback_values(variables)
            # Expand the whole stack first. One missing value must not send the first half of a
            # combat sequence and then abort in the middle.
            expanded = [api.expand_command(command, values) for command in pending]
            for command in expanded:
                if command.strip():
                    api.send(command)

        return factory

    def _to_lua(self, value: object):
        if isinstance(value, dict):
            table = self._lua.table()
            for key, item in value.items():
                table[key] = self._to_lua(item)
            return table
        if isinstance(value, (list, tuple)):
            return self._lua.table_from([self._to_lua(item) for item in value])
        return value

    def _lua_get_mud_var(self):
        api = self._api

        def factory(name, default=None):
            return self._to_lua(api.get_mud_var(str(name), default))

        return factory

    def _lua_mud_vars(self):
        return lambda: self._to_lua(self._api.mud_vars())

    def _lua_add_timer(self):
        """MUSHclient's AddTimer: a named timer, with an options table for the flags.

        Lua has no keyword arguments and ``repeat`` is a reserved word there, so the options
        table is read here: ``mud.add_timer("tick", 5, fn, {every=5, group="combat"})``.
        """

        def add(name, delay, callback, options=None):
            every = None
            group = ""
            enabled = True
            if options is not None:
                value = options["every"] if options["every"] is not None else options["repeat"]
                if value is not None:
                    every = float(value)
                if options["group"] is not None:
                    group = str(options["group"])
                if options["enabled"] is not None:
                    enabled = bool(options["enabled"])
            def fire() -> None:
                # Under the guard, like mud.timer's callback: a Lua error is reported to the
                # player and the timer survives it, instead of dying into the crash log.
                self._run_callback(callback, None)

            return self._api.add_named_timer(
                str(name), float(delay), fire, every=every, group=group, enabled=enabled
            )

        return add

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
                captures = lua.table_from(ctx.wildcards[1:])
                for name, value in ctx.named.items():
                    captures[name] = value
                self._run_callback(
                    callback, ctx, ctx.line.plain_text, captures
                )

            register(pattern, py_callback, **kwargs)

        return factory

    def _lua_register_key(self):
        api = self._api

        def factory(key, callback):
            def py_callback(ctx: MatchContext) -> None:
                self._run_callback(callback, ctx)

            api.add_key(key, py_callback)

        return factory

    def _lua_timer(self):
        api = self._api

        def factory(delay, callback):
            api.add_timer(float(delay), lambda: self._run_callback(callback, None))

        return factory

    def _lua_on_data(self):
        """mud.on_data(name, function(name, value) ... end, {changes=true}): MUD data events.

        Mudlet's GMCP events, for genericMud: the function runs each time the MUD sends that
        package or variable (or one below it, or a value inside it), after the value is
        stored. With {changes=true} it runs only when the value differs from last time.
        """
        api = self._api

        def factory(path, callback, opts=None):
            changes = opts is not None and bool(opts["changes"])

            def heard(name: str, value: object) -> None:
                self._run_callback(callback, None, name, self._to_lua(value))

            api.add_data_watch(str(path), heard, only_changes=changes)

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

    def set_match(self, line: str, wildcards) -> None:
        """Offer the match to code that runs next: globals ``line`` and ``wildcards``.

        A called function gets them as arguments; code a rule runs inline has nowhere else to
        read them from, and "the line that matched" is the first thing such code wants.
        """
        values = self._lua.globals()
        values.line = str(line)
        values.wildcards = self.table_of(wildcards)

    def table_of(self, values):
        """Values as a 1-based Lua table, the shape a ``mud`` callback's captures take.

        Built by hand rather than with ``table_from``: its arguments differ across lupa
        versions, and a captured line is not worth a version fight.
        """
        table = self._lua.table()
        for position, value in enumerate(values, start=1):
            table[position] = value
        return table


class LuaPackRuntime:
    """A native pack's own Lua state, with the ``mud`` table in it.

    The player's own scripts do not use this: they run in the world's one scripting space, where
    MUSHclient's names sit beside ``mud`` (``genericmud.scripting.world_runtime``).
    """

    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._script_error_spoken = False  # speak the first fire-time script fault, trace the rest
        self._lua, install_hook = make_sandboxed_runtime()
        # native packs are sandboxed; report contained faults so a broken trigger isn't silent
        self._guard = ScriptGuard(
            install_hook, require_hook=True, report=self._report_error,
            clock=lambda: time.monotonic() - api.player_time,  # a dialog's wait isn't the script's
        )
        self._mud = MudTable(self._lua, api, self._guard)
        self._mud.install()
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
                f"A Lua script error occurred: {type(error).__name__}", channel="system"
            )

    def function(self, name: str):
        """A Lua function this script's source defined, or None (MUSHclient's named routine)."""
        try:
            value = self._lua.globals()[name]
        except Exception:  # noqa: BLE001 - a name that is not there is simply not there
            return None
        return value if value is not None and callable(value) else None

    def set_match(self, line: str, wildcards) -> None:
        """Offer the match to code that runs next: globals ``line`` and ``wildcards``."""
        self._mud.set_match(line, wildcards)

    def table_of(self, values):
        """Values as a 1-based Lua table, the shape a ``mud`` callback's captures take."""
        return self._mud.table_of(values)

    def run_source(self, code: str):
        return self._guard.run_strict(self._lua.execute, code)

    def run_with_match(self, code: str, *, line: str, wildcards) -> None:
        """Run a rule's inline code, with the match as its parameters rather than its globals.

        As globals they overwrote whatever a script had called ``line`` or ``wildcards`` every
        time a rule fired, and a world's scripts share one state.
        """
        function = self.run_source(
            "return function(line, wildcards)\n" + str(code) + "\nend"
        )
        self._guard.run(function, str(line), self.table_of(list(wildcards)))

    def run_file(self, path: str):
        with open(path, encoding="utf-8") as handle:
            return self.run_source(handle.read())
