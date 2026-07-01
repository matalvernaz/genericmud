"""Install/update a soundpack by syncing its files from a remote manifest.

The pack's whole content is served as individual files under one base URL, described by
a manifest of ``<hash> <size> <relative/path>`` lines (optionally gzip'd). One routine
reconciles the local pack dir against that manifest: a fresh install is a sync from
nothing (every file downloads); an update fetches only files whose hash or size changed
upstream, and drops files removed upstream.

Two safety rails, both borrowed from the v0.6.0 hardening:
- every server-named path is confined under the pack dir with :func:`safepath.confine`
  before it is written, so a hostile manifest can't traverse out of the pack directory;
- fetches go through :func:`genericmud.packs.vault.download`, which is SSRF-checked and
  size-capped.

Downloads are written atomically (temp + ``os.replace``) and the local baseline manifest
is committed only after a clean pass, so an interrupted sync just re-runs and resumes.
Integrity is verified by size against the manifest — the manifest's own hash is an opaque
non-SHA digest we can't recompute, so it's used only to detect upstream changes, not to
verify a download.
"""

from __future__ import annotations

import gzip
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from genericmud import safepath
from genericmud.packs import vault
from genericmud.packs.manifest_sources import ManifestSource

_BASELINE_NAME = ".gm_manifest.lst"  # our copy of the last-synced manifest, kept for diffing
_MANIFEST_MAX_BYTES = 32 * 1024 * 1024  # a manifest is text; 32 MiB is vast headroom
_PER_FILE_SLACK = 1 << 20  # allow a file up to its manifest size + 1 MiB before aborting

# A manifest entry: the upstream (opaque) hash and the byte size.
ManifestEntry = tuple[str, int]


@dataclass
class SyncResult:
    downloaded: int = 0
    skipped_unchanged: int = 0
    deleted: int = 0
    rejected: list[str] = field(default_factory=list)  # paths refused by safepath (never written)
    failed: list[str] = field(default_factory=list)  # paths that errored or failed size-verify

    @property
    def ok(self) -> bool:
        return not self.failed


def parse_manifest(raw: bytes) -> dict[str, ManifestEntry]:
    """Parse a manifest (gzip or plain) of ``<hash> <size> <relative/path>`` lines.

    The path is everything after the size, so filenames may contain spaces. Blank lines and
    ``#`` comments are skipped. Backslashes in paths are normalised to ``/``. Returns
    ``{relpath: (hash, size)}``.
    """
    if raw[:2] == b"\x1f\x8b":  # gzip magic
        raw = gzip.decompress(raw)
    entries: dict[str, ManifestEntry] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(None, 2)  # hash, size, path (path keeps any spaces)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        digest, size_text, rel = parts
        entries[rel.replace("\\", "/")] = (digest, int(size_text))
    return entries


def sync(
    source: ManifestSource,
    pack_dir: str | Path,
    *,
    progress=None,
    download=vault.download,
) -> SyncResult:
    """Reconcile ``pack_dir`` with ``source``'s remote manifest; return a :class:`SyncResult`.

    ``progress(done, total, relpath)`` is called once per considered file. ``download`` is
    injected so tests run offline; it must match :func:`vault.download`'s ``(url, dest, *,
    max_bytes=...)`` shape. A fresh install (no baseline) downloads everything; a re-run pulls
    only changed files and deletes ones removed upstream.
    """
    pack_dir = Path(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)

    manifest_raw = _fetch_manifest(source, pack_dir, download)
    remote = parse_manifest(manifest_raw)
    baseline = _load_baseline(pack_dir)
    result = SyncResult()

    def included(rel: str) -> bool:
        return not source.include or any(rel.startswith(prefix) for prefix in source.include)

    wanted = {rel: entry for rel, entry in remote.items() if included(rel)}
    total = len(wanted)
    for done, (rel, entry) in enumerate(wanted.items(), start=1):
        if progress is not None:
            progress(done, total, rel)
        dest = safepath.confine(pack_dir, rel)
        if dest is None:  # traversal / absolute / UNC — refuse to write it anywhere
            result.rejected.append(rel)
            continue
        if not _needs_download(dest, entry, baseline.get(rel)):
            result.skipped_unchanged += 1
            continue
        try:
            _fetch_file(source, rel, entry, dest, download)
            result.downloaded += 1
        except Exception as exc:  # noqa: BLE001 - one bad file must not sink the whole sync
            result.failed.append(f"{rel}: {type(exc).__name__}: {exc}")

    _delete_removed(pack_dir, remote, baseline, included, result)

    # Commit the baseline only after a clean pass, so an interrupted/partial sync re-runs.
    if result.ok:
        (pack_dir / _BASELINE_NAME).write_bytes(manifest_raw)
    return result


def _fetch_manifest(source: ManifestSource, pack_dir: Path, download) -> bytes:
    tmp = pack_dir / (_BASELINE_NAME + ".new")
    try:
        download(source.manifest_url, tmp, max_bytes=_MANIFEST_MAX_BYTES)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _load_baseline(pack_dir: Path) -> dict[str, ManifestEntry]:
    path = pack_dir / _BASELINE_NAME
    if not path.is_file():
        return {}
    try:
        return parse_manifest(path.read_bytes())
    except OSError:
        return {}


def _needs_download(dest: Path, entry: ManifestEntry, baseline_entry: ManifestEntry | None) -> bool:
    if baseline_entry != entry:  # new file, or upstream hash/size changed
        return True
    try:
        return not dest.is_file() or dest.stat().st_size != entry[1]  # baseline stale vs. disk
    except OSError:
        return True


def _fetch_file(
    source: ManifestSource, rel: str, entry: ManifestEntry, dest: Path, download
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = source.base_url + quote(rel, safe="/")  # encode spaces/specials, keep path separators
    tmp = dest.with_name(dest.name + ".part")
    try:
        download(url, tmp, max_bytes=entry[1] + _PER_FILE_SLACK)
        size = tmp.stat().st_size
        if size != entry[1]:
            raise ValueError(f"size mismatch: got {size} bytes, manifest declares {entry[1]}")
        os.replace(tmp, dest)  # atomic: a partial download never becomes the live file
    finally:
        tmp.unlink(missing_ok=True)


def _delete_removed(pack_dir: Path, remote, baseline, included, result: SyncResult) -> None:
    """Remove files we synced before that dropped out of the manifest upstream."""
    for rel in baseline:
        if rel in remote or not included(rel):
            continue
        gone = safepath.confine(pack_dir, rel)
        if gone is not None and gone.is_file():
            gone.unlink(missing_ok=True)
            result.deleted += 1
