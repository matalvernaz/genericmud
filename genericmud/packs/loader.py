"""Pack loader: activate a world's enabled packs against an engine, report conflicts.

Each enabled pack runs through its dialect front-end with a :class:`ScriptApi`
tagged by the pack id and rooted at the pack directory (so relative sound paths
resolve and every registration is attributed to the pack). A failing pack is
recorded and skipped, not allowed to abort the rest. After activation,
:func:`detect_conflicts` reports key collisions (a hard, silent overwrite) and
identical trigger/alias patterns claimed by more than one pack.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from genericmud.automation.engine import AutomationEngine
from genericmud.packs.manifest import PackManifest
from genericmud.packs.store import PackStore
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.lua_runtime import LuaPackRuntime
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.vipmud_dialect import VipMudPack


def _load_lua(api: ScriptApi, entry: str, trusted: bool) -> LuaPackRuntime:
    runtime = LuaPackRuntime(api)
    runtime.run_file(entry)
    return runtime


def _load_vipmud(
    api: ScriptApi, entry: str, trusted: bool, log_notes: bool = False
) -> VipMudPack:
    pack = VipMudPack(api, log_notes=log_notes)
    pack.load_entry(entry)  # read as latin-1, like every .set the pack loads
    return pack


def _load_mushclient(
    api: ScriptApi, entry: str, trusted: bool, log_notes: bool = False
) -> MushclientPack:
    # Trusted packs run with the full Lua stdlib their libraries assume; untrusted
    # ones (a dry-run with require_trust=False) stay sandboxed.
    pack = MushclientPack(api, full_stdlib=trusted, log_notes=log_notes)
    pack.load_file(entry)
    return pack


DIALECT_LOADERS = {"lua": _load_lua, "vipmud": _load_vipmud, "mushclient": _load_mushclient}


@dataclass(frozen=True)
class Conflict:
    kind: str  # "key" | "trigger" | "alias"
    token: str  # the key combo or pattern claimed by more than one pack
    sources: tuple[str, ...]


@dataclass
class ActivationResult:
    loaded: list[str] = field(default_factory=list)  # pack ids that loaded cleanly
    failed: dict[str, str] = field(default_factory=dict)  # pack id -> error message
    conflicts: list[Conflict] = field(default_factory=list)
    skipped_untrusted: list[str] = field(default_factory=list)  # enabled but not trusted
    # Compatibility decisions stay inspectable without turning optional/client-only plugins
    # or one malformed upstream rule into a failed pack activation.
    skipped_plugins: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    skipped_rules: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    plugin_errors: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    external_script_errors: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    module_errors: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # Live dialect front-ends for the loaded packs, in load order. The app needs the
    # MUSHclient ones after activation to dispatch plugin lifecycle hooks
    # (OnPluginInstall/Connect and the telnet pair that carries MSDP).
    packs: dict[str, object] = field(default_factory=dict)


def activate_pack(
    manifest: PackManifest,
    api: ScriptApi,
    entry: str,
    *,
    trusted: bool = False,
    log_notes: bool = False,
) -> object:
    """Run one pack's entry through its dialect front-end (raises on a bad pack).

    Returns the live front-end instance so callers can keep driving it (lifecycle
    hooks) after the initial load. ``log_notes`` is the world's answer to whether the
    lines a pack shows belong in the session log: a MUSHclient pack's Note and a
    VIPMud pack's #Say both take it. A Lua pack's own ``mud.echo`` does not yet.
    """
    if manifest.dialect == "mushclient":
        return _load_mushclient(api, entry, trusted, log_notes)
    if manifest.dialect == "vipmud":
        return _load_vipmud(api, entry, trusted, log_notes)
    return DIALECT_LOADERS[manifest.dialect](api, entry, trusted)


def activate_world(
    store: PackStore,
    world: str,
    engine: AutomationEngine,
    *,
    require_trust: bool = True,
    log_notes: bool = False,
) -> ActivationResult:
    """Load every pack enabled for ``world`` (in order) and report conflicts.

    With ``require_trust`` (the default, matching connect), an enabled-but-untrusted
    pack is held back and listed in ``skipped_untrusted`` instead of running.
    """
    diag = engine.diag
    result = ActivationResult()
    for manifest in store.enabled(world):
        trusted = store.is_trusted(manifest.id)
        if require_trust and not trusted:
            result.skipped_untrusted.append(manifest.id)
            if diag is not None:
                diag.event("pack.load", id=manifest.id, dialect=manifest.dialect,
                           status="skipped_untrusted")
            continue
        api = ScriptApi(engine, source=manifest.id, base_dir=str(store.pack_dir(manifest.id)))
        try:
            pack = activate_pack(
                manifest,
                api,
                str(store.entry_path(manifest.id)),
                trusted=trusted,
                log_notes=log_notes,
            )
            result.packs[manifest.id] = pack
            result.loaded.append(manifest.id)
            if diag is not None:
                diag.event("pack.load", id=manifest.id, dialect=manifest.dialect, status="loaded")
            if isinstance(pack, MushclientPack):
                if pack._skipped_plugins:
                    result.skipped_plugins[manifest.id] = list(pack._skipped_plugins)
                if pack._rule_errors:
                    result.skipped_rules[manifest.id] = list(pack._rule_errors)
                if pack._include_errors:
                    result.plugin_errors[manifest.id] = list(pack._include_errors)
                if pack._external_script_errors:
                    result.external_script_errors[manifest.id] = list(
                        pack._external_script_errors
                    )
                if pack._module_errors:
                    result.module_errors[manifest.id] = list(pack._module_errors)
                if diag is not None:
                    for name, reason in pack._skipped_plugins:
                        diag.event(
                            "pack.plugin", id=manifest.id, name=name,
                            status="skipped", reason=reason,
                        )
                    for name, reason in pack._include_errors:
                        diag.event(
                            "pack.plugin", id=manifest.id, name=name,
                            status="failed", reason=reason,
                        )
                    for name, reason in pack._rule_errors:
                        diag.event(
                            "pack.rule", id=manifest.id, name=name,
                            status="skipped", reason=reason,
                        )
                    for name, reason in pack._external_script_errors:
                        diag.event(
                            "pack.script", id=manifest.id, name=name,
                            status="failed", reason=reason,
                        )
                    for name, reason in pack._module_errors:
                        diag.event(
                            "pack.module", id=manifest.id, name=name,
                            status="failed", reason=reason,
                        )
        except Exception as exc:  # noqa: BLE001 - one bad pack must not sink the others
            # Roll back anything it registered before raising: a half-loaded pack's triggers
            # would otherwise stay live and could gag/reroute/send with incomplete state.
            engine.remove_source(manifest.id)
            result.failed[manifest.id] = f"{type(exc).__name__}: {exc}"
            if diag is not None:
                diag.event("pack.load", id=manifest.id, dialect=manifest.dialect,
                           status="failed", error=f"{type(exc).__name__}: {exc}")
    result.conflicts = detect_conflicts(engine)
    if diag is not None:
        reg = engine.registrations_by_source()
        for pack_id in result.loaded:
            # A pack that loaded but registered zero triggers is inert -- its sounds can never
            # fire. This line is the signature of that case (candidate D for silent soundpacks).
            counts = reg.get(pack_id, {"trigger": [], "alias": [], "key": []})
            diag.event("pack.counts", id=pack_id, triggers=len(counts["trigger"]),
                       aliases=len(counts["alias"]), keys=len(counts["key"]))
    return result


def detect_conflicts(engine: AutomationEngine) -> list[Conflict]:
    """Tokens registered by more than one source. Keys first (hardest collision)."""
    reg = engine.registrations_by_source()
    conflicts: list[Conflict] = []
    for kind in ("key", "trigger", "alias"):
        by_token: dict[str, list[str]] = {}
        for source, tokens in reg.items():
            if not source:
                continue  # unattributed (built-in / app-level) bindings don't conflict-report
            for token in tokens[kind]:
                by_token.setdefault(token, []).append(source)
        for token, sources in sorted(by_token.items()):
            distinct = sorted(set(sources))
            if len(distinct) > 1:
                conflicts.append(Conflict(kind, token, tuple(distinct)))
    return conflicts
