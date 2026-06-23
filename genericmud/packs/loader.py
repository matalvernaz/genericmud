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


def _load_lua(api: ScriptApi, entry: str, trusted: bool) -> None:
    LuaPackRuntime(api).run_file(entry)


def _load_vipmud(api: ScriptApi, entry: str, trusted: bool) -> None:
    with open(entry, encoding="utf-8") as handle:
        VipMudPack(api).load_source(handle.read())


def _load_mushclient(api: ScriptApi, entry: str, trusted: bool) -> None:
    # Trusted packs run with the full Lua stdlib their libraries assume; untrusted
    # ones (a dry-run with require_trust=False) stay sandboxed.
    MushclientPack(api, full_stdlib=trusted).load_file(entry)


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


def activate_pack(
    manifest: PackManifest, api: ScriptApi, entry: str, *, trusted: bool = False
) -> None:
    """Run one pack's entry through its dialect front-end (raises on a bad pack)."""
    DIALECT_LOADERS[manifest.dialect](api, entry, trusted)


def activate_world(
    store: PackStore, world: str, engine: AutomationEngine, *, require_trust: bool = True
) -> ActivationResult:
    """Load every pack enabled for ``world`` (in order) and report conflicts.

    With ``require_trust`` (the default, matching connect), an enabled-but-untrusted
    pack is held back and listed in ``skipped_untrusted`` instead of running.
    """
    result = ActivationResult()
    for manifest in store.enabled(world):
        trusted = store.is_trusted(manifest.id)
        if require_trust and not trusted:
            result.skipped_untrusted.append(manifest.id)
            continue
        api = ScriptApi(engine, source=manifest.id, base_dir=str(store.pack_dir(manifest.id)))
        try:
            activate_pack(manifest, api, str(store.entry_path(manifest.id)), trusted=trusted)
            result.loaded.append(manifest.id)
        except Exception as exc:  # noqa: BLE001 - one bad pack must not sink the others
            result.failed[manifest.id] = f"{type(exc).__name__}: {exc}"
    result.conflicts = detect_conflicts(engine)
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
