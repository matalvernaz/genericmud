"""Path-safety helpers for untrusted file references (soundpack + MUD-server input).

A soundpack script and a MUD server both hand us filenames — sound cues, settings
files, world/log names — that must never let them read or write outside the directory
we intend. A relative ``combat/hit.wav`` is fine; ``C:\\Windows\\...``,
``\\\\attacker\\share\\x`` (a Windows UNC that leaks NTLM on open), and ``../../secret``
are not. These helpers reject the hostile shapes and confine a name under an allowed
root. Kept tiny and dependency-free so every surface (MSP, the ScriptApi sound path,
VIPMud #file, log names) can share one check.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def is_unsafe(name: str) -> bool:
    """True if ``name`` is absolute, drive-qualified, UNC, parent-traversing, or NUL-bearing.

    These are the shapes an untrusted path uses to escape its directory. A well-formed
    pack-relative name (no leading slash, no drive, no ``..`` segment) is safe.
    """
    if not name or "\x00" in name:
        return True
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):  # POSIX-absolute or UNC ("//host/share")
        return True
    if _DRIVE_RE.match(name):  # Windows drive ("C:...", even "C:rel")
        return True
    return ".." in normalized.split("/")


def is_unc(name: str) -> bool:
    """True for a UNC path (``\\\\host\\share`` / ``//host/share``) — always refused."""
    return bool(name) and name.replace("\\", "/").startswith("//")


def is_traversal(name: str) -> bool:
    """True if any path segment is ``..`` (parent traversal) — always refused."""
    return ".." in name.replace("\\", "/").split("/")


def has_drive(name: str) -> bool:
    """True for a Windows drive-qualified path (``C:...``)."""
    return bool(name) and _DRIVE_RE.match(name) is not None


def is_absolute(name: str) -> bool:
    """True for an absolute path (POSIX ``/``, UNC ``//``, or a Windows drive).

    Unlike :func:`is_unsafe`, an absolute path is not automatically forbidden: the dialects
    pre-resolve sound paths against ``@sppath``/the pack dir and hand us an absolute path, so
    the caller confines it with :func:`within` against the allowed roots instead.
    """
    return bool(name) and (name.replace("\\", "/").startswith("/") or has_drive(name))


def within(root: str | Path, candidate: str | Path) -> bool:
    """True if ``candidate`` resolves to ``root`` or a path inside it (symlinks resolved)."""
    try:
        root_real = Path(root).resolve()
        cand_real = Path(candidate).resolve()
    except OSError:
        return False
    return cand_real == root_real or root_real in cand_real.parents


def confine(root: str | Path, name: str) -> Path | None:
    """Resolve pack-relative ``name`` under ``root``; return it only if it stays inside.

    Returns ``None`` for an unsafe shape or a path that escapes ``root`` (e.g. through a
    symlink). The returned path is absolute and normalised.
    """
    if is_unsafe(name):
        return None
    candidate = Path(root) / name.replace("\\", "/")
    if not within(root, candidate):
        return None
    try:
        return candidate.resolve()
    except OSError:
        return None


def resolve_media(name: str, *roots: str | None) -> str | None:
    """First existing file for ``name`` confined under one of ``roots`` (None roots skipped).

    Tries the name as a path under each root, then its bare basename under each root — so a
    server/pack cue that hard-codes a subpath still resolves against the user's sounds folder.
    Returns an absolute path string, or ``None`` when the name is unsafe or nothing matches.
    """
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1] if name else ""
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for rel in (name, leaf):
            path = confine(root, rel)
            if path is not None and path.is_file():
                return str(path)
    return None


def sanitize_component(name: str, fallback: str = "session") -> str:
    """One safe filename component: keep ``[A-Za-z0-9_.-]``, collapse the rest, no dot/_ edges.

    For building a filename out of an untrusted label (a world/session name) so it can't
    become ``../`` or an absolute path when joined onto a directory.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return safe or fallback
