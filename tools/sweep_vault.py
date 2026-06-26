"""Headless sweep of the mudsoundpack.com vault: install + activate every supported pack.

The dev host can't run the Windows UI, so this exercises the real install/activation path
end to end without wx: for each catalogue pack genericMud can install (Mush/VIPMud, with a
downloadable archive) it downloads the pack (cached), runs the real ``detect_entry`` +
``setup_pack`` install into a throwaway :class:`PackStore`, then activates it against a
headless :class:`AutomationEngine` and reports the registered trigger/alias/key counts plus
how many of the pack's own ``#play`` references resolve to real files.

A pack that loads with triggers > 0 and resolving sounds is live; triggers == 0, a load
error, or no detectable entry is the signature of a broken pack -- the bug classes this
sweep hunts. Run it after a dialect/loader change to confirm nothing regressed.

    python -m tools.sweep_vault [--client {mush,vipmud,all}] [--limit N] [--max-mb 600]

Run from the repo root (it imports ``genericmud`` off the cwd). Downloads are cached under
``--cache`` (default ``/tmp/gm-vault-cache``) so re-runs are fast.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import Path

from genericmud.automation.engine import AutomationEngine
from genericmud.packs import vault
from genericmud.packs.loader import activate_world
from genericmud.packs.setup import detect_entry, entry_problem, setup_pack
from genericmud.packs.store import PackStore, extract_pack
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.vipmud_dialect import VipMudPack, _expand_sound_variant

_SWEEP_WORLD = "sweep"  # dummy world to enable each pack for, so activate_world will run it
_DEFAULT_MAX_MB = 600  # skip a download past this; the installer-follow source repos are huge
_PLAY_RE = re.compile(r"#play(?:loop)?\s*\{([^}]*?\.wav)\}", re.IGNORECASE)
_SOUND_SAMPLE = 20  # how many distinct #play references to spot-check for resolution
_DEFERRED_FLOOR = 50  # below this, look for a SoundpackLoader the pack loads on connect


@dataclass
class PackReport:
    name: str
    mud: str
    client: str
    status: str = "?"  # ok | inert | no-entry | load-error | skipped-large | download-error
    entry: str | None = None
    world: str | None = None
    triggers: int = 0
    aliases: int = 0
    keys: int = 0
    sounds_ok: int = 0
    sounds_total: int = 0
    detail: str = ""


def _cache_path(cache: Path, pack: vault.VaultPack, url: str) -> Path:
    suffix = ".zip" if url.lower().split("?")[0].endswith(".zip") else ".bin"
    return cache / f"{pack.id}-{pack.client.lower()}{suffix}"


def _download(pack: vault.VaultPack, url: str, cache: Path, max_bytes: int) -> Path:
    """Download ``url`` to the cache (skipping if already present). Raises on cap/IO error."""
    dest = _cache_path(cache, pack, url)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    cache.mkdir(parents=True, exist_ok=True)
    try:
        vault.download(url, dest, max_bytes=max_bytes)
    except BaseException:
        dest.unlink(missing_ok=True)  # never leave a truncated archive to poison re-runs
        raise
    return dest


def _sample_sounds(pack_dir: Path, api: ScriptApi) -> tuple[int, int]:
    """Spot-check that the pack's own ``#play`` references resolve to files that exist."""
    base = str(pack_dir)
    refs: list[str] = []
    seen: set[str] = set()
    for script in sorted(pack_dir.rglob("*")):
        if script.suffix.lower() != ".set" or not script.is_file():
            continue
        for raw in _PLAY_RE.findall(script.read_text(encoding="latin-1", errors="ignore")):
            # @sppath/@scpath default to the pack dir; substitute so the check matches runtime.
            ref = _expand_sound_variant(raw).replace("@sppath", base).replace("@scpath", base)
            if "@" in ref or "%" in ref:
                continue  # path built from a runtime/server variable -- not statically checkable
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
        if len(refs) >= _SOUND_SAMPLE:
            break
    refs = refs[:_SOUND_SAMPLE]
    hits = sum(1 for ref in refs if os.path.exists(api._resolve(ref)))
    return hits, len(refs)


def _activate(store: PackStore, pack_id: str) -> tuple[AutomationEngine, dict]:
    store.enable(pack_id, _SWEEP_WORLD)
    store.trust(pack_id)
    engine = AutomationEngine()
    activate_world(store, _SWEEP_WORLD, engine)
    counts = engine.registrations_by_source().get(pack_id, {"trigger": [], "alias": [], "key": []})
    return engine, counts


def _deferred_loader_counts(pack_dir: Path) -> dict | None:
    """Measure the deferred-load potential of a pack headlessly.

    Miriani/Prometheus register most of their scripts only on connect, via a
    ``SoundpackLoader.set`` fired from a login trigger + ``#alarm``. The sweep can't see the
    login line, so it loads that loader directly to report what the pack would register live.
    """
    loader = next(
        (p for p in sorted(pack_dir.rglob("*")) if p.name.lower() == "soundpackloader.set"), None
    )
    if loader is None:
        return None
    engine = AutomationEngine()
    api = ScriptApi(engine, source="loader", base_dir=str(pack_dir))
    try:
        VipMudPack(api).load_source(loader.read_text(encoding="latin-1", errors="ignore"))
    except Exception:  # noqa: BLE001 - best-effort measurement, never fail the sweep over it
        return None
    return engine.registrations_by_source().get("loader", {"trigger": [], "alias": [], "key": []})


def _sweep_one(pack: vault.VaultPack, cache: Path, max_bytes: int) -> PackReport:
    report = PackReport(name=pack.name, mud=pack.mud, client=pack.client)
    best = vault.best_download(vault.pack_downloads(pack.id))
    if best is None:
        report.status = "no-entry"
        report.detail = "no installable archive (exe/source only)"
        return report
    try:
        archive = _download(pack, best.url, cache, max_bytes)
    except vault.DownloadTooLarge as exc:
        report.status = "skipped-large"
        report.detail = str(exc)
        return report
    except Exception as exc:  # noqa: BLE001 - one pack's download must not sink the sweep
        report.status = "download-error"
        report.detail = f"{type(exc).__name__}: {exc}"
        return report

    with tempfile.TemporaryDirectory(prefix="gm-sweep-") as tmp:
        extracted = Path(tmp) / "pack"
        try:
            extract_pack(archive, extracted)  # descends nested zips (Miriani: sounds + scripts)
        except zipfile.BadZipFile:
            report.status = "download-error"
            report.detail = "not a zip (site may have served HTML)"
            return report

        entry = detect_entry(extracted, mud_name=pack.mud)
        report.entry = entry
        if entry is None:
            report.status = "no-entry"
            report.detail = entry_problem(extracted)
            return report
        try:
            store = PackStore(Path(tmp) / "store")
            result = setup_pack(store, extracted, entry=entry, origin=best.url)
            report.world = (
                f"{result.world.host}:{result.world.port}" if result.world else None
            )
            pack_dir = store.pack_dir(result.manifest.id)
            engine, counts = _activate(store, result.manifest.id)
            report.triggers = len(counts["trigger"])
            report.aliases = len(counts["alias"])
            report.keys = len(counts["key"])
            if report.triggers < _DEFERRED_FLOOR:  # deferred load-on-connect? measure its loader
                deferred = _deferred_loader_counts(pack_dir)
                if deferred and len(deferred["trigger"]) > report.triggers:
                    report.triggers = len(deferred["trigger"])
                    report.aliases = len(deferred["alias"])
                    report.keys = len(deferred["key"])
                    report.detail = "loads on connect via SoundpackLoader.set"
            api = ScriptApi(engine, source="sample", base_dir=str(pack_dir))
            api.set_var("sppath", str(pack_dir))
            report.sounds_ok, report.sounds_total = _sample_sounds(pack_dir, api)
            report.status = "ok" if report.triggers > 0 else "inert"
        except Exception as exc:  # noqa: BLE001 - record the failure, keep sweeping
            report.status = "load-error"
            report.detail = f"{type(exc).__name__}: {exc}"
            report.detail += "\n" + "".join(traceback.format_exception(exc))[-800:]
    return report


def _installable(pack: vault.VaultPack, want_client: str) -> bool:
    if not pack.supported:
        return False
    if want_client != "all" and pack.client.strip().lower() != want_client:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep the soundpack vault headlessly.")
    parser.add_argument("--client", choices=("mush", "vipmud", "all"), default="all")
    parser.add_argument("--limit", type=int, default=0, help="stop after N packs (0 = all)")
    parser.add_argument("--max-mb", type=int, default=_DEFAULT_MAX_MB)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/gm-vault-cache"))
    args = parser.parse_args()

    packs = [p for p in vault.list_packs() if _installable(p, args.client)]
    if args.limit:
        packs = packs[: args.limit]
    print(f"sweeping {len(packs)} packs (client={args.client}, max={args.max_mb} MB)\n")

    reports: list[PackReport] = []
    for pack in packs:
        print(f"  ... {pack.mud} ({pack.client})", flush=True)
        reports.append(_sweep_one(pack, args.cache, args.max_mb * 1_000_000))

    print(f"\n{'STATUS':13} {'CLIENT':8} {'TRG':>4} {'ALI':>4} {'KEY':>4} {'SND':>6}  MUD / detail")
    print("-" * 92)
    for r in reports:
        snd = f"{r.sounds_ok}/{r.sounds_total}" if r.sounds_total else "-"
        line = (
            f"{r.status:13} {r.client:8} {r.triggers:>4} {r.aliases:>4} {r.keys:>4} "
            f"{snd:>6}  {r.mud}"
        )
        if r.detail:
            line += f"  | {r.detail.splitlines()[0]}"
        print(line)

    ok = sum(1 for r in reports if r.status == "ok")
    print(f"\n{ok}/{len(reports)} live (triggers > 0). "
          f"non-ok: {sorted({r.status for r in reports if r.status != 'ok'})}")


if __name__ == "__main__":
    main()
