"""MUSHclient compatibility: load `<muclient>` XML worlds/plugins + run their Lua.

Parses MUSHclient triggers/aliases and executes their `<script>` CDATA against a
sandboxed Lua runtime whose globals are MUSHclient's API (``Send``, ``Sound``,
``GetInfo``, ``DoAfterSpecial``, ``ColourNote``, ``GetVariable``...), backed by the
shared :class:`ScriptApi`. The same functions are also mirrored onto a ``world``
table, since real packs call both bare (``Sound(...)``) and through the world
object (``world.Sound(...)``). This lets Matt's existing plugins (e.g.
``/home/matt/erion/erion_gathering.xml``) and MUSHclient soundpacks run on the
genericMud engine unchanged.

Scope: covers the API surface real soundpacks use, audio included — ``Sound`` (the
BASS-backed call mudsoundpack.com packs use, with its ``volume=``/``pan=`` control
strings) and ``GetInfo`` directory codes (so ``GetInfo(67).."/sounds/x.ogg"``
resolves against the pack dir). Out of scope: the full plugin-suite surface
(LuaSocket, GUI windows, VBScript) and a few simplified semantics — notably
``DoAfterSpecial`` always runs its deferred text as Lua (the soundpack-standard
"send to script" case) rather than honouring every sendto code.
"""

from __future__ import annotations

import glob
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.guard import ScriptGuard
from genericmud.scripting.lua_runtime import install_pack_require, make_sandboxed_runtime

_WILDCARD_RE = re.compile(r"%(\d)")
_SEND_TO_SCRIPT = "12"
_DEFAULT_TRIGGER_SEQUENCE = "100"

_SOUND_CHANNEL = "sound"  # MUSHclient Sound() is a single-voice channel
_VOLUME_MAX = 100.0  # MUSHclient volume is 0..100
_PAN_MAX = 100.0  # bass/MUSHclient pan is -100..100; the SoundBus wants -1..1
_AUDIO_CHANNEL_PREFIX = "erion-audio-"  # one bus channel per cue, so stop(id) can target it

# Lifecycle entry points MUSHclient calls on each plugin. All plugins share one _G here,
# so each plugin's hooks are captured (and the globals cleared) right after its script
# runs -- otherwise a later plugin would inherit or silently overwrite an earlier one's.
# Every known name is captured for isolation; dispatch() is only wired up for the
# sound-critical ones today (install/connect + the telnet pair that carries MSDP).
_LIFECYCLE_HOOKS = (
    "OnPluginInstall",
    "OnPluginConnect",
    "OnPluginDisconnect",
    "OnPluginClose",
    "OnPluginEnable",
    "OnPluginDisable",
    "OnPluginSaveState",
    "OnPluginTick",
    "OnPluginLineReceived",
    "OnPluginBroadcast",
    "OnPluginTelnetRequest",
    "OnPluginTelnetSubnegotiation",
)


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # lupa hands Lua numbers over as int/float; nil arrives as None
    except (TypeError, ValueError):
        return None
# GetInfo() directory codes (app/config/world/plugin dirs). Real packs build sound paths
# relative to the WORLD file's directory, so every dir code resolves to it (see _get_info).
_DIR_INFO_CODES = frozenset({56, 60, 64, 66, 67})


class MushclientPack:
    def __init__(self, api: ScriptApi, *, full_stdlib: bool = False) -> None:
        self._api = api
        self._base_dir = api.base_dir
        # Default @sppath to the pack dir when the session didn't pre-set it from world.sounds, so
        # _find_in_sounds_dir can locate bundled audio by basename when the pack's own
        # GetInfo()-anchored paths miss (e.g. Erion's split sounds/ + worlds/sounds/ layout). Mirrors
        # the VIPMud default; the guard keeps a session-set world.sounds path from being clobbered.
        if self._base_dir and not api.get_var("sppath"):
            api.set_var("sppath", self._base_dir)
        self._world_dir: str | None = None  # dir of the loaded world file; anchors GetInfo() paths
        self._exposed: dict[str, dict] = {}  # ppi: plugin id -> {exposed name -> Lua fn}
        self._current_plugin = "world"  # whose script is loading now (for ppi.Expose)
        self._loaded_includes: set[Path] = set()  # resolved paths, so each file loads once
        self._include_errors: list[tuple[str, str]] = []  # plugins that failed to load (name, why)
        self._hooks: dict[str, dict[str, object]] = {}  # plugin id -> {hook name -> Lua fn}
        self._arrays: dict[str, dict[str, str]] = {}  # MUSHclient Array* API backing store
        # MUSHclient targets Lua 5.1; trusted packs keep the full stdlib their
        # libraries assume (os/io/loadstring + the module(..., package.seeall) idiom).
        self._lua, install_hook = make_sandboxed_runtime(lua51=True, full_stdlib=full_stdlib)
        # Untrusted packs fail closed if the runaway-loop guard can't be installed; a trusted
        # pack is user-vouched arbitrary code, so a missing hook is acceptable there.
        self._guard = ScriptGuard(install_hook, require_hook=not full_stdlib)
        self._install_api()
        self._install_sendpkt()
        self._install_audio()
        # Calls fn(option, <lua byte-string>) entirely on the Lua side: an MSDP payload
        # is rarely valid UTF-8, so it crosses the lupa boundary as a table of byte
        # values, never as a string (lupa's string conversion would raise or mangle it).
        self._payload_caller = self._lua.eval(
            "function(fn, option, t)\n"
            "  local parts = {}\n"
            "  for i = 1, #t do parts[i] = string.char(t[i]) end\n"
            "  return fn(option, table.concat(parts))\n"
            "end"
        )
        # Hand each plugin our own ppi (its bundled ppi.lua needs package.seeall, which the
        # sandbox strips). Then make any still-unimplemented host name a "black hole" that is
        # callable AND indexable (returns itself) -- so Window/InfoBox/etc. we don't implement
        # no-op (even Foo.bar.baz()) and the plugin loads + its sound path runs.
        # A black hole: callable AND self-indexing (returns itself), so Window/InfoBox/socket
        # and any other host API we don't implement no-op, even Foo.bar.baz(). It backs both an
        # unresolved require (native/external modules) and any unknown global, so a plugin loads
        # + its sound path runs regardless of the peripheral features it reaches for.
        black_hole = self._lua.eval(
            "setmetatable({}, {__call=function(t) return t end, __index=function(t) return t end})"
        )
        install_pack_require(
            self._lua, self._base_dir, builtins={"ppi": self._make_ppi()}, fallback=black_hole
        )
        # Only API-shaped names fall into the black hole: MUSHclient functions are CapWords
        # (Sound, WindowCreate, BroadcastPlugin...) plus a few lowercase host libraries
        # (utils/bit/rex/serialize) and native modules a plugin loads via loadlib (nvda --
        # mushReader's speech object; genericMud self-voices every line already, so its
        # say()/stop() no-op rather than double-speaking). A plain script variable (var,
        # dir, roomName) must read
        # back as nil -- assigning nil to a global DELETES it, so an unconditional fallback
        # made `if var ~= nil` true right after `var = nil` and Erion's OnPluginInstall
        # stored the black hole into every sound toggle instead of defaulting them to 1.
        self._lua.eval(
            "function(bh)\n"
            "  local hosted = {utils=true, bit=true, rex=true, serialize=true, nvda=true}\n"
            "  setmetatable(_G, {__index = function(_, key)\n"
            "    if type(key) == 'string' and (string.match(key, '^%u') or hosted[key]) then\n"
            "      return bh\n"
            "    end\n"
            "    return nil\n"
            "  end})\n"
            "end"
        )(black_hole)

    def _make_ppi(self):
        """A minimal in-process ppi (plugin-to-plugin interface): Expose registers a
        function under the loading plugin; Load returns a plugin's exposed functions."""
        ppi = self._lua.table()
        ppi.Expose = self._ppi_expose
        ppi.Load = self._ppi_load
        return ppi

    def _ppi_expose(self, name: object = "", fn: object = None) -> None:
        key = str(name)
        functions = self._exposed.setdefault(self._current_plugin, {})
        functions[key] = fn if fn is not None else self._lua.globals()[key]

    def _ppi_load(self, plugin_id: object = None):
        return self._lua.table_from(self._exposed.get(str(plugin_id), {}))

    # --- MUSHclient global API ---

    def _install_api(self) -> None:
        api = self._api
        funcs = {
            "Send": api.send,
            "SendNoEcho": api.send,
            "Execute": api.send,
            "Note": api.echo,
            "ColourNote": self._colour_note,
            # nil (not "") for an unset variable -- MUSHclient semantics. Erion's
            # OnPluginInstall does `if GetVariable(...) ~= nil` to keep saved toggle
            # settings; an ""-for-unset answer makes it adopt "" and every toggle-gated
            # sound stays off.
            "GetVariable": lambda name="": api.get_var(str(name), None),
            "SetVariable": api.set_var,
            # The Array* trio MSDP packs use for state (room name etc.). Real bindings,
            # not black-holed: a black-holed ArrayGet returns a table, and concatenating
            # that raises inside the plugin's subnegotiation handler.
            "ArrayCreate": self._array_create,
            "ArraySet": self._array_set,
            "ArrayGet": self._array_get,
            "DeleteVariable": lambda name: api.set_var(name, ""),
            "EnableTrigger": lambda *_a: None,
            "EnableAlias": lambda *_a: None,
            "EnableTimer": lambda *_a: None,
            "Hyperlink": lambda *_a: None,
            "GetSoundKeyword": lambda *_a: "",
            "PlaySound": self._play_sound,
            "Sound": self._sound,
            "GetInfo": self._get_info,
            "DoAfterSpecial": self._do_after_special,
        }
        g = self._lua.globals()
        for name, fn in funcs.items():
            g[name] = fn
        # Packs call both bare (Sound(...)) and through the world object
        # (world.Sound(...), world.getvariable(...)). Mirror funcs onto a world
        # table, with lowercase aliases for the world.lowercase() callers.
        world = self._lua.table()
        for name, fn in funcs.items():
            world[name] = fn
            world[name.lower()] = fn
        g.world = world

    def _install_audio(self) -> None:
        """Provide the ``audio`` global that bass.dll-backed packs (Erion) play every cue through.

        Erion's sound engine (LuaAudio.xml) routes all game audio through ``audio.play`` /
        ``audio.playDelay`` -- and MSDP dispatch reaches it via ppi -- NOT through ``Sound()``.
        Without ``audio`` those calls hit the compat black-hole and no cue is ever heard, even
        though the pack loads its triggers. Map the sound-producing methods onto the ScriptApi
        (one bus channel per cue id, so ``stop(id)`` works); the DSP-only rest (pan/pitch/fades)
        no-op via the table's ``__index``. ``play``'s loop flag is honoured only for an explicit
        ``1`` (LuaAudio's music case) -- a stuck looping combat cue is worse than one that doesn't.
        """
        api = self._api
        channels: dict[int, str] = {}
        next_id = [1]

        def _alloc() -> tuple[int, str]:
            cue_id = next_id[0]
            next_id[0] += 1
            channel = f"{_AUDIO_CHANNEL_PREFIX}{cue_id}"
            channels[cue_id] = channel
            return cue_id, channel

        def _gain(vol: object) -> float:
            value = _to_float(vol)
            return value / _VOLUME_MAX if value is not None else 1.0

        def _pan(pan: object) -> float:
            value = _to_float(pan)
            return max(-1.0, min(1.0, value / _PAN_MAX)) if value is not None else 0.0

        def _start(file: object, loop: object, pan: object, vol: object, delay: float) -> int:
            if not str(file or ""):
                return 0
            cue_id, channel = _alloc()
            gain, pan_value, looped = _gain(vol), _pan(pan), _to_float(loop) == 1

            def fire() -> None:
                api.play(str(file), channel=channel, gain=gain, pan=pan_value, loop=looped)

            if delay > 0:
                api.add_timer(delay, fire)
            else:
                fire()
            return cue_id

        def play(file: object = "", loop: object = 0, pan: object = None, vol: object = None,
                 *_rest: object) -> int:
            return _start(file, loop, pan, vol, 0.0)

        def play_delay(file: object = "", delay: object = 0, pan: object = None, vol: object = None,
                       *_rest: object) -> int:
            return _start(file, 0, pan, vol, max(_to_float(delay) or 0.0, 0.0))

        def play_delay_looped(file: object = "", delay: object = 0, pan: object = None,
                              vol: object = None, *_rest: object) -> int:
            return _start(file, 1, pan, vol, max(_to_float(delay) or 0.0, 0.0))

        def stop(cue_id: object = 0, *_rest: object) -> None:
            if _to_float(cue_id) == 0:  # bass convention: id 0 stops every cue
                api.flush()
                return
            channel = channels.get(int(_to_float(cue_id) or 0))
            if channel is not None:
                api.stop(channel)

        # A table backed by a no-op __index, so any bass method we don't implement
        # (pan/freq/pitch/fadeout/slide*/dll) is safely callable and just does nothing.
        audio = self._lua.eval("setmetatable({}, {__index = function() return function() end end})")
        audio.play = play
        audio.playLooped = lambda file="", *_a: _start(file, 1, None, None, 0.0)
        audio.playDelay = play_delay
        audio.playDelayLooped = play_delay_looped
        audio.stop = stop
        audio.free = lambda *_a: api.flush()
        audio.getVolume = lambda *_a: _VOLUME_MAX
        audio.isPlaying = lambda *_a: 0
        self._lua.globals().audio = audio

    def _colour_note(self, *args: object) -> None:
        # ColourNote(fg, bg, text [, fg, bg, text]...) — concatenate the text parts.
        texts = [str(args[i]) for i in range(2, len(args), 3)]
        self._api.echo("".join(texts))

    def _array_create(self, name: object = "") -> None:
        self._arrays.setdefault(str(name), {})

    def _array_set(self, name: object = "", key: object = "", value: object = "") -> None:
        self._arrays.setdefault(str(name), {})[str(key)] = str(value)

    def _array_get(self, name: object = "", key: object = "") -> str | None:
        return self._arrays.get(str(name), {}).get(str(key))  # nil when absent (MUSHclient)

    def _install_sendpkt(self) -> None:
        """Bind ``SendPkt`` via a Lua-side byte-table trampoline.

        The packet is a pre-framed telnet sequence (IAC SB ... IAC SE) full of bytes
        that are invalid UTF-8, so the Lua string must never cross the lupa boundary
        directly -- the runtime's string conversion would raise. The trampoline
        explodes it into a table of byte values; Python reassembles and sends verbatim
        (no re-framing: SendPkt's contract is that the caller built the framing).
        """
        make_sendpkt = self._lua.eval(
            "function(deliver)\n"
            "  return function(data)\n"
            "    data = tostring(data or '')\n"
            "    local bytes = {}\n"
            "    for i = 1, #data do bytes[i] = string.byte(data, i) end\n"
            "    deliver(bytes)\n"
            "  end\n"
            "end"
        )
        sendpkt = make_sendpkt(self._deliver_packet)
        globals_ = self._lua.globals()
        globals_["SendPkt"] = sendpkt
        globals_.world["SendPkt"] = sendpkt  # packs also call through the world object

    def _deliver_packet(self, table: object) -> None:
        data = bytes(bytearray(table[i] for i in range(1, len(table) + 1)))
        self._api.send_packet(data)

    def _play_sound(
        self,
        buffer: object = 0,
        file: str = "",
        loop: object = False,
        volume: object = 100,
        pan: object = 0,
    ) -> None:
        self._api.play(str(file), loop=bool(loop))

    def _sound(self, arg: object = "", *_rest: object) -> None:
        """MUSHclient ``Sound``: a path plays it; a ``key=value`` string is a control
        directive (``volume=``/``pan=``/``freq=``) for the current cue."""
        text = str(arg)
        if "=" in text:
            self._sound_control(text)
        elif text:
            self._api.play(text, channel=_SOUND_CHANNEL)

    def _sound_control(self, directive: str) -> None:
        key, _, raw = directive.partition("=")
        if key.strip().lower() != "volume":
            return  # pan/freq: no live per-cue control in the bus yet — accept, ignore
        try:
            level = float(raw)
        except ValueError:
            return
        if level <= 0:
            self._api.stop(_SOUND_CHANNEL)  # "volume=0" is the soundpack idiom for stop
        else:
            self._api.set_volume(_SOUND_CHANNEL, level / _VOLUME_MAX)

    def _get_info(self, code: object = 0) -> str:
        """MUSHclient ``GetInfo``: the world file's directory for dir codes, else ``""``.

        Packs build sound paths as ``GetInfo(67).."sounds/x.ogg"`` (with or without a
        leading slash), so return the loaded world's directory WITH a trailing slash --
        MUSHclient dir codes end in a separator, and some plugins (Erion's MSDP_handler)
        append ``"sounds/.."`` with none. Sounds sit beside the world, which may be nested
        under the pack root, so anchor on the world dir, not ``base_dir``. ``api.play``
        normpath's the result, so the doubled slash a leading-slash plugin produces is fine.
        """
        try:
            number = int(code)
        except (TypeError, ValueError):
            return ""
        if number not in _DIR_INFO_CODES:
            return ""
        root = self._world_dir or self._base_dir or ""
        return f"{root.rstrip('/')}/" if root else ""

    def _do_after_special(self, delay: float, code: str, sendto: object = _SEND_TO_SCRIPT) -> None:
        deferred = self._compile(str(code))
        self._api.add_timer(float(delay), lambda: self._guard.run(deferred))

    def _compile(self, code: str):
        """Host-side compile of a Lua chunk into a zero-arg callable."""
        return self._lua.eval(f"function()\n{code}\nend")

    # --- loading ---

    def load_file(self, path: str) -> None:
        # The world file's directory anchors GetInfo() sound paths: sounds sit beside the
        # world (often nested below the pack root that require/ resolves against).
        self._world_dir = Path(path).resolve().parent.as_posix()
        # MUSHclient world/plugin files are iso-8859-1 (a .MCL declares it); latin-1
        # decodes any byte without error, and load_source strips the encoding decl.
        with open(path, encoding="latin-1") as handle:
            self.load_source(handle.read())

    def load_source(self, xml: str) -> None:
        # Strip only the XML declaration -- ElementTree rejects an encoding decl on a str.
        # Keep the DOCTYPE: MUSHclient plugins declare config entities in its internal
        # subset (<!ENTITY foo "...">) and reference them as &foo;, which ET expands. (An
        # earlier strip of the whole DOCTYPE corrupted that subset -> ParseError.)
        xml = re.sub(r"<\?xml[^>]*\?>", "", xml)
        xml = _sanitize_attr_markup(xml)  # MUSHclient regex attrs carry raw < (named groups)
        self._load_plugin(ET.fromstring(xml))

    def _load_plugin(self, root: ET.Element) -> None:
        """Run one plugin/world's script + triggers; a world (<include>s) pulls in its
        plugins so they share this runtime and can ppi-message each other."""
        plugin = next(root.iter("plugin"), None)
        previous = self._current_plugin
        self._current_plugin = (plugin.get("id") if plugin is not None else "") or "world"
        script = "\n".join((el.text or "") for el in root.iter("script"))
        if script.strip():
            self._guard.run_strict(self._lua.execute, script)
        for element in root.iter("trigger"):
            self._register(element, is_alias=False)
        for element in root.iter("alias"):
            self._register(element, is_alias=True)
        self._capture_hooks()
        self._current_plugin = previous
        for include in root.iter("include"):
            name = include.get("name")
            if not name:
                continue
            try:
                self._load_included(name)
            except Exception as exc:  # noqa: BLE001 - a malformed plugin must not sink the pack
                self._include_errors.append((name, f"{type(exc).__name__}: {exc}"))

    def _load_included(self, filename: str) -> None:
        if not self._base_dir or not filename:
            return
        base = Path(self._base_dir).resolve()
        # Match by filename (layouts vary). Escape glob metachars so a literal name like
        # "a[1].xml" isn't read as a pattern, and confirm each hit is a real file under the
        # pack dir (rglob can surface a symlink or directory outside it).
        target = None
        for match in sorted(base.rglob(glob.escape(Path(filename).name))):
            try:
                resolved = match.resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved.is_relative_to(base):
                target = resolved
                break
        if target is None or target in self._loaded_includes:  # dedup by file (dirs share names)
            return
        self._loaded_includes.add(target)
        self.load_source(target.read_text(encoding="latin-1", errors="ignore"))

    # --- plugin lifecycle ---

    def _capture_hooks(self) -> None:
        """Claim the ``OnPlugin*`` functions the current plugin's script defined.

        Must use ``rawget``: the black-hole ``_G`` metatable reports every name as
        defined. Captured globals are cleared so the next plugin in the shared
        runtime neither inherits nor overwrites them (MUSHclient gives each plugin
        its own script space; this is the shared-``_G`` equivalent).
        """
        rawget = self._lua.eval("rawget")
        globals_ = self._lua.globals()
        captured = self._hooks.setdefault(self._current_plugin, {})
        for name in _LIFECYCLE_HOOKS:
            fn = rawget(globals_, name)
            if fn is not None:
                captured[name] = fn
                globals_[name] = None

    def dispatch(self, name: str, *args: object, caller: object | None = None) -> None:
        """Call one lifecycle hook on every plugin that defines it, in load order.

        Each call is time-budgeted and isolated: one plugin's failing hook is
        traced to the diagnostic log and the rest still run, mirroring MUSHclient
        where one erroring plugin doesn't halt the others. ``caller`` interposes a
        Lua-side adapter (``caller(fn, *args)``) for arguments that can't cross the
        lupa boundary as-is (byte payloads).
        """
        for plugin_id, hooks in self._hooks.items():
            fn = hooks.get(name)
            if fn is None:
                continue
            previous = self._current_plugin
            self._current_plugin = plugin_id
            try:
                if caller is not None:
                    self._guard.run_strict(caller, fn, *args)
                else:
                    self._guard.run_strict(fn, *args)
            except Exception as exc:  # noqa: BLE001 - one plugin's hook must not stop the rest
                diag = self._api.diag
                if diag is not None:
                    diag.event("plugin.dispatch", hook=name, plugin=plugin_id,
                               error=f"{type(exc).__name__}: {exc}")
            finally:
                self._current_plugin = previous

    def dispatch_install(self) -> None:
        """MUSHclient calls each plugin's ``OnPluginInstall`` at load; packs set their
        variable defaults there (Erion turns every sound toggle on), so skipping it
        leaves the pack loaded but gated silent."""
        self.dispatch("OnPluginInstall")

    def dispatch_connect(self) -> None:
        self.dispatch("OnPluginConnect")

    def dispatch_telnet_request(self, option: int, message: str) -> None:
        """``OnPluginTelnetRequest(option, "WILL"/"SENT_DO")`` -- the SENT_DO round is
        where MSDP packs send their REPORT list; without it the server streams nothing."""
        self.dispatch("OnPluginTelnetRequest", option, message)

    def dispatch_telnet_subnegotiation(self, option: int, payload: bytes) -> None:
        # MUSHclient hands plugins the raw payload as a Lua byte-string. It crosses
        # into Lua as a byte table and is reassembled there (_payload_caller).
        table = self._lua.table_from(list(payload))
        self.dispatch(
            "OnPluginTelnetSubnegotiation", option, table, caller=self._payload_caller
        )

    def _register(self, element: ET.Element, *, is_alias: bool) -> None:
        attrs = element.attrib
        if attrs.get("enabled", "y") != "y":
            return
        pattern = attrs.get("match", "")
        regex = attrs.get("regexp", "n") == "y"
        try:  # a malformed sequence attribute must not abort the whole world load
            priority = -int(attrs.get("sequence", _DEFAULT_TRIGGER_SEQUENCE))
        except ValueError:
            priority = -int(_DEFAULT_TRIGGER_SEQUENCE)
        keep_default = "n" if is_alias else "y"  # aliases consume by default
        keep = attrs.get("keep_evaluating", keep_default) == "y"
        callback = self._make_callback(element, attrs)
        if is_alias:
            self._api.add_alias(
                pattern, callback, regex=regex, priority=priority, keep_evaluating=keep
            )
        else:
            self._api.add_trigger(
                pattern, callback, regex=regex, priority=priority, keep_evaluating=keep
            )

    def _make_callback(self, element: ET.Element, attrs: dict[str, str]):
        lua = self._lua
        api = self._api
        name = attrs.get("name", "")

        script_name = attrs.get("script")
        if script_name:
            handler = lua.globals()[script_name]

            def call_named(ctx: MatchContext) -> None:
                if handler is not None:
                    wildcards = lua.table_from(ctx.wildcards[1:])
                    self._guard.run(handler, name, ctx.line.plain_text, wildcards)

            return call_named

        send_element = element.find("send")
        body = (send_element.text or "") if send_element is not None else ""
        if body.strip():
            if attrs.get("send_to", "0") == _SEND_TO_SCRIPT:
                if _WILDCARD_RE.search(body):
                    # MUSHclient substitutes %1.. into send-to-script text per match, then
                    # runs it. Can't precompile: a bare %1 (e.g. `for i=1,%1`) isn't valid Lua.
                    def call_script(ctx: MatchContext) -> None:
                        # Compile inside the guard: a syntax error from substituted MUD text
                        # must be contained, not raised into line processing.
                        self._guard.run(lambda: self._compile(_substitute(body, ctx.wildcards))())
                else:
                    compiled = self._compile(body)  # no wildcards: compile once at registration

                    def call_script(_ctx: MatchContext) -> None:
                        self._guard.run(compiled)

                return call_script

            def call_send(ctx: MatchContext) -> None:
                api.send(_substitute(body, ctx.wildcards))

            return call_send

        return lambda _ctx: None


_CDATA_RE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_ATTR_VALUE_RE = re.compile(r'="([^"]*)"')
_BARE_AMP_RE = re.compile(r"&(?!(?:[A-Za-z][\w.-]*|#\d+|#x[0-9A-Fa-f]+);)")


def _sanitize_attr_markup(xml: str) -> str:
    """Escape raw ``<`` and bare ``&`` inside double-quoted attribute values (outside CDATA).

    MUSHclient plugins put regexes with named groups in ``match="(?P<name>...)"`` attributes;
    the raw ``<`` is illegal XML and trips ElementTree even though MUSHclient tolerates it.
    Script bodies live in CDATA and are left untouched; well-formed packs have nothing to
    escape (entities already use ``&...;``), so this is a no-op for them.
    """
    out: list[str] = []
    last = 0
    for cdata in _CDATA_RE.finditer(xml):
        out.append(_escape_attr_values(xml[last : cdata.start()]))
        out.append(cdata.group(0))
        last = cdata.end()
    out.append(_escape_attr_values(xml[last:]))
    return "".join(out)


def _escape_attr_values(segment: str) -> str:
    def fix(match: re.Match[str]) -> str:
        value = _BARE_AMP_RE.sub("&amp;", match.group(1)).replace("<", "&lt;")
        return f'="{value}"'

    return _ATTR_VALUE_RE.sub(fix, segment)


def _substitute(text: str, wildcards: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return wildcards[index] if index < len(wildcards) else ""

    return _WILDCARD_RE.sub(replace, text)
