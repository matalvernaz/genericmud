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

import ipaddress
import os
import re
import unicodedata
from pathlib import Path

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WORLD_PUNCTUATION = "._-"  # the only non-letters a world's file name keeps as they are
# Zero-width non-joiner and joiner: invisible, but they change how Persian, Sinhala and
# Indic words are written, so two names that differ only by one are different names.
_WORLD_JOINERS = "\u200c\u200d"


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


def trusted_addresses(address: str) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The non-public addresses a fetch may reach on a MUD's say-so: the MUD's own address, and
    the rest of loopback when that one is loopback ("localhost" is 127.0.0.1 and ::1).

    A host *name* trusts nothing here: the MUD decides what its own name resolves to.
    """
    try:
        peer = ipaddress.ip_address(address)
    except ValueError:
        return frozenset()
    if peer.is_loopback:
        return frozenset({peer, ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")})
    return frozenset({peer})


def is_allowed_host(host: str, trusted_address: str = "") -> bool:
    """Whether a server-supplied address may be fetched from: public, or the MUD's own machine.

    Needed for anything the renderer loads itself -- an inline image is fetched by the browser,
    so the downloader's SSRF check never runs for it. This applies the same rule by hand, to the
    host as written.

    A *host name* is taken on trust, because judging it means resolving it here and again when
    the page loads. What this stops is the cheap case: an address that is private, loopback or
    link-local in its own text, which is what a MUD would use to point a player's browser at
    their router.
    """
    name = host.strip().strip("[]")
    if not name:
        return False
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return True  # a host name: nothing to judge without resolving it
    if address in trusted_addresses(trusted_address):
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def is_absolute(name: str) -> bool:
    """True for an absolute path (POSIX ``/``, UNC ``//``, or a Windows drive).

    Unlike :func:`is_unsafe`, an absolute path is not automatically forbidden: the dialects
    pre-resolve sound paths against ``@sppath``/the pack dir and hand us an absolute path, so
    the caller confines it with :func:`within` against the allowed roots instead.
    """
    return bool(name) and (name.replace("\\", "/").startswith("/") or has_drive(name))


def within(root: str | Path, candidate: str | Path) -> bool:
    """True if ``candidate`` resolves to ``root`` or a path inside it (symlinks resolved)."""
    root_real = real_path(root)
    cand_real = real_path(candidate)
    return root_real is not None and cand_real is not None and inside(root_real, cand_real)


def real_path(path: str | Path) -> Path | None:
    """``path`` with its links followed, or None when it can't be resolved."""
    try:
        return Path(path).resolve()
    except OSError:
        return None


def inside(root_real: Path, candidate_real: Path) -> bool:
    """:func:`within` for paths already resolved: ``candidate_real`` is ``root_real`` or in it.

    For a caller that checks many paths against the same few roots. Resolving a path on
    Windows opens it, so resolving the roots again for every path costs more than the check.
    """
    return candidate_real == root_real or root_real in candidate_real.parents


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


def world_component(name: str, fallback: str = "session") -> str:
    """The file or folder name a world's own data is kept under, in any alphabet.

    Per-world rules, scripts, maps and saved pack variables are filed by world name, so
    two worlds must never land on one name. :func:`sanitize_component` keeps ASCII only,
    which filed every all-Cyrillic or all-CJK name under the same fallback: every such
    world shared one set of triggers and one room map. This keeps letters and digits in
    any script, with the combining marks many scripts write their vowels with (normalised
    to NFC, so one spelling is one folder), and still collapses separators, dots at the
    edges and everything else that could leave the directory.
    """
    out: list[str] = []
    replacing = False  # inside a run of dropped characters, which becomes one "_"
    for char in unicodedata.normalize("NFC", name):
        if _keeps_in_world_name(char):
            out.append(char)
            replacing = False
        elif not replacing:
            out.append("_")
            replacing = True
    return "".join(out).strip("._") or fallback


def _keeps_in_world_name(char: str) -> bool:
    """A letter or digit in any script, a combining mark, a joiner, or one of ``._-``.

    Marks matter: Devanagari, Bengali, Tamil and Thai vowel signs are marks, not letters,
    and NFC doesn't compose them away, so dropping them filed "का" as "क".
    """
    return (
        char.isalnum()
        or char in _WORLD_PUNCTUATION
        or char in _WORLD_JOINERS
        or unicodedata.category(char)[0] == "M"
    )


def legacy_world_component(name: str) -> str:
    """The ASCII-only name a world's data was filed under before :func:`world_component`,
    or "" when nothing ASCII was left (that shared fallback belonged to no one world)."""
    return sanitize_component(name, fallback="")
