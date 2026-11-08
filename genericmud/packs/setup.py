"""One-shot soundpack setup: install a pack, derive its world, wire it up.

Ties :class:`PackStore` (install/enable/trust) and :func:`world_from_pack` (the
connection read out of the pack's own MUSHclient world file) together, so a single
call turns a downloaded pack into a ready-to-connect world with sound. UI-agnostic,
so it is testable headless; the caller persists the returned world and connects.
"""

from __future__ import annotations

import filecmp
import os
import re
import tempfile
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from genericmud.config.atomic import atomic_write_text
from genericmud.config.worlds import World
from genericmud.packs import git_remote, git_sync, manifest_sync, vault
from genericmud.packs.git_sources import GitSource, UpdateSource
from genericmud.packs.manifest import CODE_EXEC_DIALECTS, DIALECT_BY_SUFFIX, PackManifest
from genericmud.packs.manifest_sources import ManifestSource
from genericmud.packs.store import PackError, PackStore, extract_pack
from genericmud.packs.world_import import world_from_pack
from genericmud.scripting.vipmud_dialect import linked_vipmud_scripts

_GIT_ARCHIVE_MAX_BYTES = 3 * 1000 * 1000 * 1000  # a full pack repo (Erion ~1 GB); generous cap

# Conventional load-script filenames, best-first.
_ENTRY_PREFERENCE = ("main.set", "main.lua", "main.xml", "start.set", "startup.set", "load.set")
_MIN_NAMED_STEM = 4  # only match a script "named after the pack" if the stem is this long


def _normalize_name(text: str) -> str:
    """Lowercase, strip non-alphanumerics: 'star conquest' == 'Star Conquest' == 'StarConquest'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass
class SetupResult:
    manifest: PackManifest
    world: World | None  # parsed from the pack (host/port); None -> caller prompts
    enabled_for: str | None  # the world name the pack was enabled for, if any


@dataclass(frozen=True)
class UpdateUnchanged:
    """An update whose download held nothing the installed pack didn't already have."""

    pack_id: str


def _count(path: Path, needle: str) -> int:
    try:
        return path.read_text(encoding="latin-1", errors="ignore").lower().count(needle)
    except OSError:
        return 0


def _mush_world_score(path: Path) -> int:
    """How much runnable configuration a MUSHclient world file contains."""
    try:
        text = path.read_text(encoding="latin-1", errors="ignore").lower()
    except OSError:
        return 0
    includes = text.count("<include")
    rules = len(re.findall(r"<(?:trigger|alias)(?:\s|>)", text))
    external_script = bool(re.search(r'script_filename\s*=\s*"[^"\s]+"', text))
    return includes * 1000 + rules + (100 if external_script else 0)


def _mush_plugin_score(path: Path, mud_name: str | None) -> int:
    """Rank standalone MUSHclient plugins when a bundled world is inert."""
    if path.suffix.lower() != ".xml" or "/state/" in f"/{path.as_posix().lower()}/":
        return -1
    try:
        text = path.read_text(encoding="latin-1", errors="ignore").lower()
    except OSError:
        return -1
    if "<plugin" not in text:
        return -1
    stem = _normalize_name(path.stem)
    target = _normalize_name(mud_name or "")
    score = len(re.findall(r"<trigger(?:\s|>)", text)) * 2
    score += len(re.findall(r"<alias(?:\s|>)", text))
    if "soundpack" in stem or "soundpack" in text[:4000]:
        score += 500
    if target and stem == target:
        score += 1000
    elif target and len(stem) >= 4 and stem[:4] == target[:4]:
        score += 300
    return score


def detect_entry(pack_dir: str | Path, *, mud_name: str | None = None) -> str | None:
    """Best-guess load script for a multi-file pack, relative to ``pack_dir``.

    Real packs rarely match a single naming rule, so try, in order: the MUSHclient ``.MCL``
    world that ``<include>``s the most plugins, when the pack has no VIPMud ``.set`` (a
    MUSHclient pack — the world wins over a stray ``main.*`` plugin, and over a bundle's
    extra captures/sandbox worlds); a conventional ``main.*``/``start.*`` name; a script
    named after the MUD (``mud_name``: VIPMud loaders are named for the MUD, e.g.
    ``star conquest.set`` for Star Conquest); a VIPMud ``.set`` that ``#load``s the others,
    ranked shallowest-first (the loader sits above the ``Scripts/`` dir it pulls in — a
    ``#ForAll {list} {#load {Scripts\\%I.set}}`` reads as one literal ``#load`` but drives
    many, so a deeper script that ``#load``s from inside a reload alias must not outrank it);
    a script named after the pack dir; finally a lone script. None means ambiguous — the
    caller explains why.
    Entry paths are POSIX (forward slashes) so they're portable; pathlib accepts
    them on every OS.
    """
    pack_dir = Path(pack_dir)
    scripts = sorted(p for p in pack_dir.rglob("*") if p.suffix.lower() in DIALECT_BY_SUFFIX)
    if not scripts:
        return None

    def rel(script: Path) -> str:
        return script.relative_to(pack_dir).as_posix()

    # A MUSHclient pack (no VIPMud .set entry) normally loads from a productive .MCL world.
    # Do not select an empty saved world merely because it exists: several distributions
    # carry an inert .MCL beside the actual MUD-named sound plugin.
    worlds = [s for s in scripts if s.suffix.lower() == ".mcl"]
    if worlds and not any(s.suffix.lower() == ".set" for s in scripts):
        productive = [(world, _mush_world_score(world)) for world in worlds]
        productive = [(world, score) for world, score in productive if score]
        if productive:
            return rel(max(productive, key=lambda pair: (pair[1], rel(pair[0])))[0])
        label = mud_name or pack_dir.name
        plugins = sorted(
            ((_mush_plugin_score(script, label), rel(script), script) for script in scripts),
            reverse=True,
        )
        if plugins and plugins[0][0] >= 300:
            return rel(plugins[0][2])
        if len(worlds) == 1:
            return rel(worlds[0])
    inert_worlds = {world for world in worlds if not _mush_world_score(world)}
    candidates = [script for script in scripts if script not in inert_worlds]
    linked = linked_vipmud_scripts(pack_dir)
    if linked is not None:
        return rel(linked)
    for preferred in _ENTRY_PREFERENCE:
        for script in candidates:
            if script.name.lower() == preferred:
                return rel(script)
    if mud_name:  # a VIPMud loader is named after the MUD ("star conquest.set" -> Star Conquest)
        target = _normalize_name(mud_name)
        for script in candidates:
            if target and _normalize_name(script.stem) == target:
                return rel(script)
    loaders = [(s, _count(s, "#load")) for s in scripts if s.suffix.lower() == ".set"]
    loaders = [(s, n) for s, n in loaders if n]  # .set files that #load others
    if loaders:  # shallowest first: the entry loader sits above the Scripts/ dir it pulls in
        loaders.sort(key=lambda pair: (rel(pair[0]).count("/"), -pair[1], rel(pair[0])))
        return rel(loaders[0][0])
    root = pack_dir.name.lower()  # a plugin named after the pack (toastush.xml in toastush/)
    for script in candidates:
        stem = script.stem.lower()
        if len(stem) >= _MIN_NAMED_STEM and stem in root:
            return rel(script)
    if len(candidates) == 1:
        return rel(candidates[0])
    # Plugin-manager repositories can contain many unrelated plugins and no world file.
    # Pick only a strong MUD/soundpack match; a weak highest score remains ambiguous.
    ranked = sorted(
        ((_mush_plugin_score(script, mud_name), rel(script), script) for script in candidates),
        reverse=True,
    )
    if ranked and ranked[0][0] >= 300:
        return rel(ranked[0][2])
    return None


def entry_problem(pack_dir: str | Path) -> str:
    """A human-readable reason why no load script was found, for the UI to show.

    Distinguishes the common dead ends: a Windows installer bundle, a multi-plugin
    MUSHclient pack we can't auto-pick a load file for, or no script at all.
    """
    pack_dir = Path(pack_dir)
    suffixes = {f.suffix.lower() for f in pack_dir.rglob("*")}
    # Check for MUSHclient content BEFORE .exe: these packs bundle git/perl tooling
    # (.exe/.dll) alongside the real .mcl world + plugins, so .exe alone is misleading.
    if {".mcl", ".xml"} & suffixes:
        return "couldn't identify a single MUSHclient world file to load from this pack"
    if {".exe", ".dll"} & suffixes:
        return "this download is a Windows installer, not an importable soundpack"
    return "no soundpack script (.set/.lua/.xml) was found in this download"


def setup_pack(
    store: PackStore,
    source: str | Path,
    *,
    entry: str | None = None,
    sounds: str | None = None,
    trust: bool = True,
    origin: str | None = None,
) -> SetupResult:
    """Install ``source``, derive its world, and enable+trust it for that world.

    Installs the pack (``entry`` picks the load script of a multi-file pack), reads
    the connection from the pack's MUSHclient world file, and — if one is found —
    points it at ``sounds`` and enables the pack for that world. Trusts by default,
    since setting a pack up is an explicit vouch. ``origin`` records where the content
    came from (a URL) so the pack can be updated later. A pack with no world file (a
    bare VIPMud ``.set``) returns ``world=None`` so the caller can prompt for host/port.
    The returned ``world`` is not yet saved; the caller persists it and connects.
    """
    manifest = store.install(source, replace=True, entry=entry, origin=origin)
    world = world_from_pack(store.pack_dir(manifest.id))
    enabled_for = None
    if world is not None:
        if sounds:
            world.sounds = sounds
        store.enable(manifest.id, world.name)
        enabled_for = world.name
    # Auto-trust the vouch -- but never a code-executing dialect (MUSHclient runs the full Lua
    # stdlib when trusted). A one-click vault download is too weak a vouch to grant os/io on
    # connect, so such packs install enabled-but-untrusted; the user trusts them deliberately.
    if trust and manifest.dialect not in CODE_EXEC_DIALECTS:
        store.trust(manifest.id)
    return SetupResult(manifest=manifest, world=world, enabled_for=enabled_for)


def update_pack(
    store: PackStore, pack_id: str, *, fetch: Callable[[str, Path], object]
) -> SetupResult | UpdateUnchanged:
    """Re-fetch a pack from its recorded ``origin`` URL and reinstall it in place.

    ``fetch(url, dest_zip)`` downloads the archive (injected, so this stays testable
    and network-free). Per-world enablement and trust are preserved — install
    ``replace=True`` rewrites only the pack content, not ``worlds.json``/``trust.json``.
    Raises if the pack has no origin (e.g. it was set up from a local folder). A download
    holding nothing new leaves the installed pack untouched and returns
    :class:`UpdateUnchanged`.
    """
    manifest = store.manifest(pack_id)
    if not manifest.origin:
        raise PackError(f"{pack_id} has no recorded source to update from")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "update.zip"
        fetch(manifest.origin, archive)
        extracted = Path(tmp) / pack_id  # same id -> install replaces in place
        # The guarded extractor, like the first install: it refuses a zip bomb and opens the
        # nested zips some packs ship (Miriani: Scripts.zip and Sounds.zip in a wrapper).
        try:
            extract_pack(archive, extracted)
        except zipfile.BadZipFile as exc:
            raise PackError(
                f"the update for {pack_id} wasn't a ZIP (the site may have served a web page)"
            ) from exc
        # A pack set up from the Vault's own copy updates from that copy, which rarely changes,
        # so most of these downloads are what is installed already. Replacing the folder anyway
        # throws away whatever the pack saved in it, and "Updated" would not be true.
        if _already_installed(extracted, store.pack_dir(pack_id)):
            return UpdateUnchanged(pack_id)
        entry = detect_entry(extracted)
        if entry is None:
            raise PackError(f"the updated download for {pack_id} has no load script")
        return setup_pack(store, extracted, entry=entry, origin=manifest.origin)


def _already_installed(download: Path, installed: Path) -> bool:
    """Whether every file in ``download`` is already in ``installed``, byte for byte.

    A file only the installed copy has doesn't count against it: that is the pack's own, like
    settings it saved in its folder. A download with no files at all is never already
    installed, so it still fails the way a broken download always has.
    """
    found_a_file = False
    for path in download.rglob("*"):
        if not path.is_file():
            continue
        found_a_file = True
        installed_copy = installed / path.relative_to(download)
        if not installed_copy.is_file():
            return False
        if not filecmp.cmp(path, installed_copy, shallow=False):
            return False
    return found_a_file


def setup_pack_from_manifest(
    store: PackStore, source: ManifestSource, *, progress=None, download=None, diag=None
) -> SetupResult:
    """Install or update a manifest-style pack (e.g. Mush-Z) by syncing its files in place.

    Syncs the pack's file tree from its remote manifest into ``packs_dir/<id>`` — a fresh
    install downloads everything, a re-run fetches only what changed — then registers and
    enables it for its curated world. Deliberately NOT auto-trusted: like any MUSHclient pack
    it runs its own Lua, so the caller must ``store.trust(id)`` before it auto-loads on connect.
    ``progress(done, total, relpath)`` reports per file; ``download`` is injectable for tests.
    ``diag`` (a DiagnosticLog or None) records the sync outcome so a partial install — files the
    manifest listed that were rejected or failed to fetch — leaves a durable trace instead of a
    silently sound-short pack. Re-running is the update path (same call, only changed files move).
    """
    kwargs = {"progress": progress}
    if download is not None:
        kwargs["download"] = download
    result = manifest_sync.sync(source, store.pack_dir(source.id), **kwargs)
    if diag is not None:
        diag.event(
            "pack.sync", id=source.id, downloaded=result.downloaded,
            skipped=result.skipped_unchanged, deleted=result.deleted,
            rejected=len(result.rejected), failed=len(result.failed),
            ok=result.ok, first_failed=(result.failed[0] if result.failed else ""),
        )
    if not result.ok:
        problems = [*result.failed, *(f"rejected unsafe path: {path}" for path in result.rejected)]
        raise PackError(
            f"{source.name} sync was incomplete ({len(problems)} file errors): {problems[0]}"
        )
    _write_pack_toml(store.pack_dir(source.id), source)
    manifest = store.register(source.id, origin=source.manifest_url)
    world = replace(source.world)  # a copy; the caller persists/edits it, don't mutate the registry
    store.enable(manifest.id, world.name)
    return SetupResult(manifest=manifest, world=world, enabled_for=world.name)


def setup_pack_from_git(
    store: PackStore, source: GitSource, *, download=None, diag=None
) -> SetupResult:
    """Install/update a pack straight from its own git repo, skipping the installer.

    Downloads the repo archive (trying ``master`` then ``main``) and hands the ``.zip`` to
    :meth:`PackStore.install`, which extracts it through the guarded extractor, strips the
    ``<repo>-<branch>`` wrapper dir, and installs it under the source's curated ``id`` -- so no
    installer ``.exe`` is fetched and the pack gets a real id/world/origin instead of the temp-dir
    ``source``. Like any MUSHclient pack it is NOT auto-trusted; the caller trusts it (the Connect
    dialog checkbox) before it auto-loads. ``download`` is injectable for tests; it must match
    :func:`vault.download`'s ``(url, dest, *, max_bytes=...)`` shape. Re-running is the update path.
    """
    fetch = download or vault.download
    urls = list(source.archive_urls) or vault.git_archive_urls(source.repo_url)
    if not urls:
        raise PackError(f"{source.name}: can't derive an archive URL from {source.repo_url!r}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "repo.zip"
        errors: list[str] = []
        for url in urls:  # master, then main
            try:
                fetch(url, archive, max_bytes=_GIT_ARCHIVE_MAX_BYTES)
                if zipfile.is_zipfile(archive):
                    break
                errors.append(f"{url}: response was not a ZIP archive")
            except Exception as exc:  # noqa: BLE001 - wrong branch / not found -> try the next url
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
        else:
            raise PackError(
                f"couldn't fetch {source.name} from {source.repo_url} ({'; '.join(errors)})"
            )
        manifest = store.install(
            archive, replace=True, entry=source.entry, origin=source.repo_url, pack_id=source.id
        )
    world = replace(source.world)  # a copy; the caller persists/edits it, don't mutate the registry
    store.enable(manifest.id, world.name)
    if diag is not None:
        diag.event("pack.git", id=source.id, repo=source.repo_url, entry=source.entry)
    return SetupResult(manifest=manifest, world=world, enabled_for=world.name)


@dataclass
class GitUpdateResult:
    """What updating a pack from its repository did, for telling the player."""

    name: str
    up_to_date: bool
    written: int = 0
    removed: int = 0
    new_versions: list[str] = field(default_factory=list)  # player's files, .new beside
    in_use: list[str] = field(default_factory=list)  # open elsewhere, updated next time
    failed: list[str] = field(default_factory=list)  # couldn't be written, tried next time
    blocked: list[str] = field(default_factory=list)  # something of the same name in the way
    # The files themselves, as the repository names them: a pack's own updater told the
    # pack which ones changed, and Miriani-Next lists them atop its changelog.
    written_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)


@dataclass
class PendingUpdate:
    """An update a pack's repository has waiting: how many files it would fetch."""

    pack_id: str
    name: str
    files: int


def check_pack_update(
    store: PackStore, pack_id: str, source: UpdateSource, *, list_tree=git_remote.list_tree
) -> PendingUpdate | None:
    """Whether the pack's repository has anything new, without downloading it: None when the
    pack is up to date. Raises :class:`PackError` when the repository can't be read."""
    manifest = store.manifest(pack_id)
    pack_dir = store.pack_dir(pack_id)
    prefix = _repository_prefix(manifest.entry, source)
    try:
        tree = list_tree(source.repo_url)
    except git_remote.RemoteError as error:
        raise PackError(f"couldn't check {source.name} for updates: {error}") from error
    baseline = _read_baseline(pack_dir, source)
    plan = git_sync.make_plan(tree.files, pack_dir, prefix, baseline)
    if plan.up_to_date:
        return None
    return PendingUpdate(pack_id, source.name, len(plan.download) + len(plan.modified))


def _read_baseline(pack_dir: Path, source: UpdateSource) -> git_sync.Baseline | None:
    try:
        return git_sync.read_baseline(pack_dir)
    except git_sync.BaselineDamaged as error:
        raise PackError(
            f"genericMud's record of which {source.name} files it installed is damaged "
            f"({error}), so it can't tell your changes from the pack's, and nothing was "
            f"changed. To update anyway, delete {git_sync.BASELINE_NAME} in the pack's "
            "folder: your settings files will be kept, but other files you changed will be "
            "replaced."
        ) from error


# One update of a pack folder at a time: two worlds that share a pack can both ask at once.
_UPDATE_LOCKS: dict[str, threading.Lock] = {}
_UPDATE_LOCKS_GUARD = threading.Lock()


def _update_lock(pack_dir: Path) -> threading.Lock:
    key = os.path.normcase(str(Path(pack_dir).resolve()))
    with _UPDATE_LOCKS_GUARD:
        return _UPDATE_LOCKS.setdefault(key, threading.Lock())


def describe_update(result: GitUpdateResult) -> str:
    """What updating a pack from its repository did, in a sentence or three."""
    if result.up_to_date and not result.in_use and not result.failed and not result.blocked:
        return f"{result.name} is up to date."
    changes = [f"{result.written} {'file' if result.written == 1 else 'files'} updated"]
    if result.removed:
        changes.append(f"{result.removed} removed")
    lines = [f"Updated {result.name}: {', '.join(changes)}."]
    count = len(result.new_versions)
    if count == 1:
        changed = result.new_versions[0]
        lines.append(
            f"You'd changed {changed}, so your copy stayed, and the pack's new version is "
            f"next to it as {changed}.new."
        )
    elif count > 1:
        named = ", ".join(result.new_versions[:5])
        more = f" and {count - 5} more" if count > 5 else ""
        lines.append(
            f"You'd changed {count} of its files, so your copies stayed, and the pack's new "
            f"version of each is next to it with .new on the end: {named}{more}."
        )
    busy = len(result.in_use)
    if busy:
        lines.append(
            f"{busy} {'file was' if busy == 1 else 'files were'} in use; update again after "
            "you reconnect to finish."
        )
    failed = len(result.failed)
    if failed:
        lines.append(
            f"{failed} {'file' if failed == 1 else 'files'} couldn't be written; check there's "
            "space on the disk, then update again to finish."
        )
    blocked = len(result.blocked)
    if blocked:
        named = ", ".join(result.blocked[:5])
        more = f" and {blocked - 5} more" if blocked > 5 else ""
        lines.append(
            f"{blocked} {'file' if blocked == 1 else 'files'} couldn't go in place, because a "
            f"file or folder of the same name is in the way: {named}{more}."
        )
    lines.append("Reconnect to apply.")
    return " ".join(lines)


def update_pack_from_git(
    store: PackStore,
    pack_id: str,
    source: UpdateSource,
    *,
    progress: Callable[[int, int], None] | None = None,
    diag=None,
    list_tree=git_remote.list_tree,
    fetch_blobs=git_remote.fetch_blobs,
) -> GitUpdateResult:
    """Bring an installed pack up to its repository's latest commit, fetching only the files
    that changed (see :mod:`genericmud.packs.git_sync`). Files the player changed stay; the
    pack's new version of each goes beside it as ``<name>.new``. ``list_tree`` and
    ``fetch_blobs`` are injectable for tests."""
    manifest = store.manifest(pack_id)
    pack_dir = store.pack_dir(pack_id)
    prefix = _repository_prefix(manifest.entry, source)
    with _update_lock(pack_dir):
        return _update_from(
            pack_id, pack_dir, prefix, source,
            progress=progress, diag=diag, list_tree=list_tree, fetch_blobs=fetch_blobs,
        )


def _update_from(
    pack_id: str, pack_dir: Path, prefix: str, source: UpdateSource, *,
    progress, diag, list_tree, fetch_blobs,
) -> GitUpdateResult:
    try:
        tree = list_tree(source.repo_url)
    except git_remote.RemoteError as error:
        raise PackError(f"couldn't check {source.name} for updates: {error}") from error
    if source.entry not in tree.files:
        raise PackError(
            f"{source.name}'s repository no longer has {source.entry}, so genericMud won't "
            "update it from there. Please report this."
        )
    baseline = _read_baseline(pack_dir, source)
    plan = git_sync.make_plan(tree.files, pack_dir, prefix, baseline)
    if plan.up_to_date and baseline is not None and baseline.commit == tree.commit:
        return GitUpdateResult(name=source.name, up_to_date=True)
    # Fetched into the pack's own folder, so files move on the same disk.
    with tempfile.TemporaryDirectory(prefix=".gm-update-", dir=pack_dir) as scratch:
        try:
            blobs = fetch_blobs(source.repo_url, plan.blobs, Path(scratch), progress=progress)
        except git_remote.RemoteError as error:
            raise PackError(f"couldn't download {source.name}'s update: {error}") from error
        applied = git_sync.apply_plan(
            pack_dir, prefix, plan, blobs,
            repo=source.repo_url, commit=tree.commit, remote=tree.files,
        )
    if diag is not None:
        diag.event(
            "pack.git_update", id=pack_id, commit=tree.commit, written=len(applied.written),
            new_versions=len(applied.new_versions), removed=len(applied.removed),
            missing=len(applied.missing), failed=len(applied.failed),
            blocked=len(applied.blocked), kept=len(applied.kept),
            local_only=plan.local_only, skipped=list(tree.skipped)[:20],
        )
    if applied.missing:
        raise PackError(
            f"{len(applied.missing)} of {source.name}'s updated files didn't arrive. Run the "
            "update again; it carries on from where it stopped."
        )
    return GitUpdateResult(
        name=source.name,
        up_to_date=not applied.written and not applied.removed and not applied.new_versions,
        written=len(applied.written),
        removed=len(applied.removed),
        new_versions=applied.new_versions,
        in_use=applied.in_use,
        failed=applied.failed,
        blocked=applied.blocked,
        written_files=applied.written,
        removed_files=applied.removed,
    )


def _repository_prefix(installed_entry: str, source: UpdateSource) -> str:
    """Where the repository's root sits inside the installed pack, found from where the
    pack's load script is in each."""
    installed = installed_entry.replace("\\", "/")
    if not installed.lower().endswith(source.entry.lower()):
        raise PackError(
            f"genericMud can't line {source.name}'s install up with its repository (its load "
            f"script is {installed_entry}, the repository's is {source.entry}). Reinstalling "
            "the pack fixes this."
        )
    return installed[: len(installed) - len(source.entry)]


def _write_pack_toml(pack_dir: Path, source: ManifestSource) -> None:
    """Write the pack.toml that lets the store register a synced tree (id/dialect/entry/origin)."""
    lines = [
        f"id = {_toml_str(source.id)}",
        f"name = {_toml_str(source.name)}",
        f"dialect = {_toml_str(source.dialect)}",
        f"entry = {_toml_str(source.entry)}",
        f"origin = {_toml_str(source.manifest_url)}",
    ]
    atomic_write_text(Path(pack_dir) / "pack.toml", "\n".join(lines) + "\n")


def _toml_str(value: str) -> str:
    """A double-quoted TOML string literal (escape backslash then quote)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
