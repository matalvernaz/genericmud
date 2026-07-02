"""GitHub-release self-update for the portable Windows build.

Ported from ffn-dl's ``self_update.py``, which in turn uses the ``ZipExtractor.exe``
pattern popularised by Libation / ravibpatel's AutoUpdater.NET (MIT): a tiny helper .exe
ships next to ``genericMud.exe``; to update we download the release zip, copy the helper
to ``%TEMP%`` so it isn't locked in the install dir, spawn it (elevating via UAC only when
the install dir isn't writable), and exit. The helper waits on our process handle,
overlays the update zip onto the install using Windows Restart Manager to diagnose locked
files, and relaunches us.

Before spawning the helper we hand the flat zip to
:mod:`genericmud.update.upgrade_manager`, which snapshots the critical files and records
their expected hashes; if the overlay only partly lands, the next startup rolls it back.

In-place update is Windows-frozen only. Everywhere else (source runs, macOS, Linux) the UI
offers the release page instead -- see :func:`can_self_replace`. HTTP is stdlib ``urllib``
(genericMud ships no third-party HTTP client); ``api.github.com`` needs no browser
impersonation. This module keeps its own hardened downloader rather than reusing
``packs.vault.download`` because a self-replace must reject a truncated or oversized body
outright -- extracting a short zip over the install is exactly the half-install failure the
whole flow guards against.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from urllib.request import Request, urlopen

from genericmud.update import upgrade_manager

logger = logging.getLogger(__name__)

REPO = "matalvernaz/genericmud"
# genericMud publishes every release flagged prerelease, so GitHub's
# ``/releases/latest`` (which skips prereleases) 404s. Query the list and pick the newest
# ``vX.Y.Z`` ourselves. A modest page size covers far more than the handful of releases
# that ever sit above the installed version.
_RELEASES_PER_PAGE = 30
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page={_RELEASES_PER_PAGE}"

APP_EXE = "genericMud.exe"
# Bundled beside genericMud.exe by CI (built from ravibpatel/AutoUpdater.NET). Without it we
# refuse to self-replace and send the user to the release page.
ZIP_EXTRACTOR_EXE = "ZipExtractor.exe"

_USER_AGENT = f"genericMud-updater (+https://github.com/{REPO})"
_API_TIMEOUT_S = 15
_DOWNLOAD_TIMEOUT_S = 30  # per socket read; a healthy slow link still completes
_DOWNLOAD_CHUNK = 1 << 20  # 1 MiB
# The portable build is ~50 MB; cap the download and the uncompressed extraction well above that
# so a bad/hostile asset can't fill the disk (the digest check already covers our own releases).
_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MiB
_MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024  # 1 GiB uncompressed


def _parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse 'v1.2.3' -> (1, 2, 3). ``None`` for anything else.

    Anchored so a prerelease-shaped tag like ``v1.2.3-beta`` does not parse as stable
    ``(1, 2, 3)`` -- those are a channel we don't offer through the in-app updater.
    """
    if not tag:
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def current_version() -> str | None:
    """Installed version, preferring the source ``__version__`` over dist metadata.

    ``genericmud.__version__`` is baked into the frozen build unconditionally, so reading it
    can't silently fail. ``importlib.metadata`` is only the fallback: it needs the dist-info
    bundled (``--copy-metadata genericmud``) and returns nothing otherwise, which used to make
    :func:`check_for_update` skip every check and report no update -- a silent dead updater.
    (ficary's updater reads ``__version__`` for exactly this reason.) ``None`` only if both fail.
    """
    try:
        from genericmud import __version__

        if __version__:
            return __version__
    except Exception:  # noqa: BLE001 - a broken import must fall through, never crash the check
        pass
    try:
        return _pkg_version("genericmud")
    except PackageNotFoundError:
        return None


def _get_json(url: str):
    request = Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    with urlopen(request, timeout=_API_TIMEOUT_S) as response:  # noqa: S310 - https api.github.com
        return json.loads(response.read().decode("utf-8"))


def _select_asset(assets: list[dict]) -> dict | None:
    """The Windows portable zip asset (``genericMud-windows.zip``), or ``None``."""
    for asset in assets:
        name = asset.get("name", "").lower()
        if name.endswith(".zip") and "windows" in name:
            return asset
    return None


def check_for_update() -> dict | None:
    """Return update info for the newest release above the installed version, else ``None``.

    Network/JSON errors propagate; callers run this off the UI thread and treat any
    exception as "couldn't check". The returned dict carries ``tag``, ``download_url``,
    ``size``, ``digest`` (``"sha256:<hex>"`` when GitHub populates it), ``release_url``, and
    ``notes``.
    """
    current = _parse_version(current_version() or "")
    if current is None:
        logger.warning("Could not determine the installed version; skipping update check.")
        return None

    releases = _get_json(RELEASES_URL)
    best: tuple[tuple[int, int, int], dict] | None = None
    for release in releases:
        if release.get("draft"):
            continue
        parsed = _parse_version(release.get("tag_name", ""))
        if parsed is None:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, release)

    if best is None or best[0] <= current:
        return None

    _, release = best
    asset = _select_asset(release.get("assets") or [])
    if asset is None:
        logger.warning(
            "Release %s has no Windows zip asset; not offering it.", release.get("tag_name")
        )
        return None

    return {
        "tag": release["tag_name"],
        "download_url": asset["browser_download_url"],
        "size": asset.get("size", 0),
        "digest": asset.get("digest"),
        "release_url": release.get("html_url"),
        "notes": release.get("body") or "",
    }


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def can_self_replace() -> bool:
    """True only for a frozen Windows build with the helper bundled.

    ``.is_file()`` (not ``.exists()``) so a directory accidentally named ``ZipExtractor.exe``
    can't fool the UI into offering an in-place update that would fail at the copy step.
    """
    if not (is_frozen() and sys.platform.startswith("win")):
        return False
    return (_install_dir() / ZIP_EXTRACTOR_EXE).is_file()


def _install_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _verify_digest(path: Path, digest: str | None) -> None:
    """Check the download against the release asset's SHA-256 when GitHub supplied one.

    A missing digest is logged but not fatal: GitHub doesn't always populate it, and the
    bytes still arrived over HTTPS from ``api.github.com``, so the channel is authenticated.
    """
    if not digest or ":" not in digest:
        logger.warning("Update asset has no SHA-256 digest; skipping content verification.")
        return
    algorithm, expected = digest.split(":", 1)
    if algorithm.lower() != "sha256":
        logger.warning("Update asset uses unsupported digest %r; skipping verification.", algorithm)
        return
    if _sha256_file(path).lower() != expected.lower():
        raise RuntimeError(
            "Downloaded update failed SHA-256 verification. It was not installed; the "
            "running version is unchanged."
        )


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_DOWNLOAD_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _download(url: str, dest: Path, progress_cb=None, expected_size: int = 0) -> None:
    """Stream ``url`` to ``dest``; raise on HTTP errors, truncation, or overshoot.

    Both the ``Content-Length`` header and the release API's declared size are checked
    independently: a buggy or hostile server can serve a short body that matches its own
    header but disagrees with the asset size. Catching that here keeps a partial zip from
    ever reaching the extractor -- the only other guard is the SHA-256 digest, which GitHub
    doesn't always provide.
    """
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310 - https github
        header_size = int(response.headers.get("Content-Length") or 0)
        api_size = int(expected_size or 0)
        if header_size > 0 and api_size > 0 and header_size != api_size:
            raise RuntimeError(
                f"Update size mismatch: server reports {header_size} bytes, release API "
                f"reports {api_size}. Refusing to install."
            )
        max_expected = max((size for size in (header_size, api_size) if size > 0), default=0)
        # Cap the stream even when neither size is known (a malformed API response), so a bad
        # redirect / hostile asset can't fill the disk before the hash check.
        cap = max_expected or _MAX_DOWNLOAD_BYTES
        done = 0
        with open(dest, "wb") as handle:
            while chunk := response.read(_DOWNLOAD_CHUNK):
                handle.write(chunk)
                done += len(chunk)
                if done > cap:
                    raise RuntimeError(
                        f"Update download exceeded {cap} bytes; refusing (possible bad asset)."
                    )
                if progress_cb is not None:
                    progress_cb(done, max_expected)

    if header_size > 0 and done != header_size:
        raise RuntimeError(
            f"Update download truncated: got {done} bytes, expected {header_size}. The "
            "current version is unchanged; please retry."
        )
    if api_size > 0 and done != api_size:
        raise RuntimeError(
            f"Update download size mismatch: got {done} bytes, release API declared "
            f"{api_size}. The current version is unchanged."
        )


def _is_writable(path: Path) -> bool:
    """Whether this process can create/remove a file in ``path`` (else we must elevate)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".genericmud-update-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _repack_flat(src_dir: Path, dest_zip: Path) -> None:
    """Re-zip ``src_dir``'s *contents* at the archive root.

    The release zip nests everything under ``genericMud/`` so a human who double-clicks it
    gets a tidy folder. ZipExtractor unpacks as-is into the install dir, though, so a wrapped
    zip would give ``install/genericMud/genericMud.exe``. Re-packing flat keeps one release
    working for both paths, and the flat zip's entries line up with the install layout the
    rollback manager verifies against.
    """
    with zipfile.ZipFile(
        dest_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3
    ) as archive:
        for path in src_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(src_dir))


def _shell_execute(verb: str, file: Path, params: str, cwd: Path) -> None:
    """``ShellExecuteW`` wrapper that raises on failure.

    ``subprocess`` can't request the ``runas`` verb; ``ShellExecuteW`` is the only
    stdlib-reachable way to trigger a UAC prompt from Python. Argtypes/restype are declared
    so the 64-bit ``HINSTANCE`` return isn't truncated by ctypes' default ``c_int``.
    """
    if not sys.platform.startswith("win"):
        raise RuntimeError("ShellExecuteW is only available on Windows")
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    if not getattr(shell32.ShellExecuteW, "_genericmud_signature_set", False):
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = wintypes.HINSTANCE
        shell32.ShellExecuteW._genericmud_signature_set = True
    sw_shownormal = 1
    handle = shell32.ShellExecuteW(None, verb, str(file), params, str(cwd), sw_shownormal)
    rc = int(ctypes.cast(handle, ctypes.c_void_p).value or 0)
    # ShellExecuteW returns > 32 on success; <= 32 are Win32 error codes.
    if rc <= 32:
        raise RuntimeError(f"ShellExecuteW failed (code {rc}) launching {file}")


def _spawn_extractor(extractor: Path, zip_path: Path, install_dir: Path, exe: Path) -> None:
    """Launch the helper to overlay ``zip_path`` and relaunch ``exe``.

    ``runas`` only when the install dir isn't writable -- the common case (unzipped to
    Downloads/Desktop/home) needs no prompt, making the update one click. Quoting goes
    through ``subprocess.list2cmdline`` because a hand-rolled ``f'"{path}"'`` breaks at a
    drive root (``"D:\\"`` parses as an escaped quote under ``CommandLineToArgvW``).
    """
    params = subprocess.list2cmdline([
        "--input", str(zip_path),
        "--output", str(install_dir),
        "--current-exe", str(exe),
    ])
    verb = "open" if _is_writable(install_dir) else "runas"
    _shell_execute(verb, extractor, params, extractor.parent)


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract ``zip_path`` into ``dest``, refusing any member that escapes ``dest``.

    Stdlib ``extractall`` does not block ``../`` or absolute members. The digest already
    authenticates the bytes, but if a compromised release ever delivered a traversal payload
    we refuse it rather than write outside the extract dir (defense in depth). A total
    uncompressed-size cap likewise refuses a decompression bomb before writing anything.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > _MAX_EXTRACTED_BYTES:
            raise RuntimeError(
                f"Refusing to extract update — uncompressed size {total} exceeds the cap."
            )
        for info in archive.infolist():
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                raise RuntimeError(f"Refusing to extract update — suspicious zip path: {name!r}")
            target = (dest / name).resolve()
            try:
                target.relative_to(dest_root)
            except ValueError as exc:
                raise RuntimeError(
                    f"Refusing to extract update — zip member escapes the extract dir: {name!r}"
                ) from exc
            archive.extract(info, dest)


def download_and_replace(update_info: dict, progress_cb=None) -> Path:
    """Download the update, arm rollback, and spawn the helper. Returns the install dir.

    The caller MUST exit promptly after this returns: the helper blocks on our PID before
    it touches any install file, then relaunches us.
    """
    if not can_self_replace():
        raise RuntimeError(
            "In-place update needs the Windows portable build with ZipExtractor.exe bundled. "
            "Please download the new version from the release page."
        )

    current_exe = Path(sys.executable).resolve()
    install_dir = current_exe.parent
    extractor_src = install_dir / ZIP_EXTRACTOR_EXE

    workdir = Path(tempfile.mkdtemp(prefix="genericmud-update-"))
    zip_path = workdir / "genericMud-windows.zip"
    extracted = workdir / "extracted"
    flat_zip = workdir / "genericMud-flat.zip"

    try:
        _download(
            update_info["download_url"], zip_path,
            progress_cb=progress_cb, expected_size=update_info.get("size", 0),
        )
        _verify_digest(zip_path, update_info.get("digest"))

        _safe_extract(zip_path, extracted)
        zip_path.unlink(missing_ok=True)

        app_root = _find_app_root(extracted, current_exe.name)
        _repack_flat(app_root, flat_zip)
        shutil.rmtree(extracted, ignore_errors=True)

        # Arm transactional rollback: record what the install should look like after the
        # overlay, so the next startup can verify and undo a partial swap.
        upgrade_manager.prepare_for_upgrade(
            install_dir, flat_zip, update_info.get("tag", "unknown"), exe_name=current_exe.name
        )

        # Copy the helper out of the install dir so it isn't locked when it overwrites its
        # own binary there, and re-hash source vs copy immediately before an elevated spawn:
        # low-priv malware could otherwise swap the temp copy between copy and ``runas`` and
        # ride the user's UAC "Yes". A mismatch aborts instead of elevating untrusted code.
        extractor_tmp = workdir / ZIP_EXTRACTOR_EXE
        shutil.copy2(extractor_src, extractor_tmp)
        if _sha256_file(extractor_src) != _sha256_file(extractor_tmp):
            raise RuntimeError(
                "Update aborted — the ZipExtractor.exe staging copy did not match the source "
                "binary. Refusing to launch a possibly tampered helper; the install is unchanged."
            )

        _spawn_extractor(extractor_tmp, flat_zip, install_dir, current_exe)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    return install_dir


def _find_app_root(extracted: Path, exe_name: str) -> Path:
    """The directory whose contents should overlay the install: the one holding the exe.

    The release zip wraps everything under ``genericMud/``, so the usual case is a single
    top-level dir. A malformed release (stray README, ``__MACOSX``) can add siblings, so we
    look for the child dir that actually contains the exe, fall back to the bare root if the
    exe sits there, and refuse rather than half-install if neither matches.
    """
    for child in extracted.iterdir():
        if child.is_dir() and (child / exe_name).is_file():
            return child
    if (extracted / exe_name).is_file():
        return extracted
    raise RuntimeError(
        f"Downloaded zip does not contain {exe_name} at the expected location. Update aborted; "
        "install unchanged."
    )


def cleanup_stale_workdirs() -> None:
    """Sweep ``%TEMP%/genericmud-update-*`` workdirs older than a day (best effort)."""
    cutoff = time.time() - 24 * 3600
    try:
        for path in Path(tempfile.gettempdir()).glob("genericmud-update-*"):
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue
    except OSError as exc:
        logger.debug("Could not sweep stale update workdirs: %s", exc)
