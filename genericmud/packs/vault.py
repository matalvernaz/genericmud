"""mudsoundpack.com ("The Soundpack Vault") catalog client.

The vault has no API: `/packs.php` is a table of packs (Pack / MUD / Client /
Status), and `/pack.php?id=N` carries labelled download links — "Vault mirror" is
the hosted archive (usually a `.zip`), "Official URL"/"Source page" are external.
This scrapes those pages and downloads a pack archive so the setup flow can install
it. HTML scraping is inherently fragile, so it is isolated here: a site change
breaks only this module, not the rest of the app.
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BASE_URL = "https://mudsoundpack.com"
_USER_AGENT = "genericMud-soundpack-browser"
_DOWNLOAD_CHUNK = 65536
_ALLOWED_SCHEMES = frozenset({"http", "https"})  # urllib also honours file://, gopher:// etc.
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB default cap on any pack/source download


class DownloadTooLarge(Exception):
    """A download passed its size cap (e.g. an installer's source repo is huge)."""


class BlockedUrl(Exception):
    """A download URL was refused (non-web scheme, or a private/loopback host)."""


def _validate_url(url: str) -> None:
    """Refuse SSRF-shaped download URLs before urlopen sees them.

    urllib will happily open ``file://`` (local file read) and reach loopback/RFC1918 hosts, so a
    compromised vault page or a malicious pack installer could point us at ``file:///etc/...`` or an
    intranet service. Allow only http(s), and reject a host that resolves to a non-public address.
    An unresolvable host isn't our SSRF concern -- the real fetch fails on its own -- so we skip it.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise BlockedUrl(f"refusing non-web URL scheme: {parsed.scheme or '(none)'!r}")
    host = parsed.hostname
    if not host:
        raise BlockedUrl("download URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
    except OSError:
        return  # unresolvable: let the actual fetch fail normally, not an SSRF risk
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise BlockedUrl(f"refusing to fetch from a non-public address ({ip})")

# Clients genericMud can actually load (one of the three script dialects); others
# (Mudlet, TinTin++, MonkeyTerm) are listed but flagged as unsupported.
SUPPORTED_CLIENTS = frozenset({"mush", "vipmud"})

_PACK_LINK_RE = re.compile(r'<a\s+href="/pack\.php\?id=(\d+)">([^<]*)</a>', re.I)
_META_RE = re.compile(r'<span\s+class="meta">(.*?)</span>', re.I | re.S)
_TD_RE = re.compile(r"<td>(.*?)</td>", re.I | re.S)
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# Label on a pack page -> the role of the link right after it (best-first order).
_DOWNLOAD_LABELS = (
    ("Vault mirror", "mirror"),
    ("Official URL", "official"),
    ("Source page", "source"),
)


@dataclass(frozen=True)
class VaultPack:
    id: int
    name: str
    mud: str
    client: str  # Mush / VIPMud / Mudlet / TinTin++ / MonkeyTerm
    version: str
    status: str  # active / archived

    @property
    def supported(self) -> bool:
        """True if genericMud can load this client's packs (Mush/.set/Lua)."""
        return self.client.strip().lower() in SUPPORTED_CLIENTS


@dataclass(frozen=True)
class VaultDownload:
    url: str
    role: str  # mirror / official / source
    kind: str  # zip / exe / other

    @property
    def installable(self) -> bool:
        return self.kind == "zip"


def _text(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _fetch(url: str, opener) -> str:
    with opener(Request(url, headers={"User-Agent": _USER_AGENT})) as response:
        return response.read().decode("utf-8", "replace")


def _kind(url: str) -> str:
    low = url.lower()
    if low.endswith(".zip") or "/archive/" in low or "dl=1" in low:
        return "zip"  # a direct/archive download we can extract
    if low.endswith(".exe"):
        return "exe"  # a Windows installer — can't install as a pack
    return "other"


def list_packs(opener=urlopen) -> list[VaultPack]:
    """Every pack in the vault catalogue (a `/packs.php` table row each)."""
    page = _fetch(f"{BASE_URL}/packs.php", opener)
    packs: list[VaultPack] = []
    for row in _ROW_RE.findall(page):
        link = _PACK_LINK_RE.search(row)
        if link is None:
            continue  # header row / non-pack row
        cells = _TD_RE.findall(row)
        meta = _META_RE.search(cells[0]) if cells else None
        packs.append(
            VaultPack(
                id=int(link.group(1)),
                name=html.unescape(link.group(2)).strip(),
                mud=_text(cells[1]) if len(cells) > 1 else "",
                client=_text(cells[2]) if len(cells) > 2 else "",
                version=_text(meta.group(1)) if meta else "",
                status=_text(cells[3]) if len(cells) > 3 else "",
            )
        )
    return packs


def pack_downloads(pack_id: int, opener=urlopen) -> list[VaultDownload]:
    """Download links for a pack, best-first (vault mirror, then official, source)."""
    page = _fetch(f"{BASE_URL}/pack.php?id={pack_id}", opener)
    downloads: list[VaultDownload] = []
    for label, role in _DOWNLOAD_LABELS:
        match = re.search(re.escape(label) + r':\s*<a[^>]+href="([^"]+)"', page, re.I)
        if match:
            url = html.unescape(match.group(1))
            downloads.append(VaultDownload(url=url, role=role, kind=_kind(url)))
    return downloads


def best_download(downloads: list[VaultDownload]) -> VaultDownload | None:
    """The first installable (.zip/archive) link, preferring the vault mirror order."""
    return next((d for d in downloads if d.installable), None)


_GIT_CLONE_RE = re.compile(r'git(?:\.exe)?\s+clone\s+(?:--\S+\s+)*["\']?(\S+)', re.I)
_REPO_URL_RE = re.compile(r'[A-Z][A-Z0-9_]*REPO_URL\s*=\s*["\']?([^\s"\']+)', re.I)
_INSTALLER_SCRIPTS = (".bat", ".cmd", ".ps1", ".sh")


def installer_source(pack_dir: str | Path) -> str | None:
    """A source-repo URL an installer script clones, or None.

    Many "soundpack" downloads are just a Windows installer that ``git clone``s the
    real pack. Scan its scripts (skipping bundled git tooling) for a ``git clone
    <url>`` or a ``*_REPO_URL=`` and return the repo URL, so the meat can be fetched
    directly instead of running the .exe.
    """
    base = Path(pack_dir)
    for path in sorted(base.rglob("*")):
        if path.suffix.lower() not in _INSTALLER_SCRIPTS or "portablegit" in str(path).lower():
            continue
        try:
            text = path.read_text(encoding="latin-1", errors="ignore")
        except OSError:
            continue
        for pattern in (_GIT_CLONE_RE, _REPO_URL_RE):
            match = pattern.search(text)
            if match and (".git" in match.group(1) or _is_git_host(match.group(1))):
                return match.group(1)
    return None


def _is_git_host(url: str) -> bool:
    return any(host in url.lower() for host in ("github", "gitlab", "gitea"))


def git_archive_urls(clone_url: str) -> list[str]:
    """Candidate archive-zip URLs for a github/gitlab/gitea clone URL (master, then main)."""
    url = clone_url.rstrip("/").removesuffix(".git")
    match = re.match(r"https?://([^/]+)/(.+)", url)
    if not match:
        return []
    host, path = match.group(1), match.group(2).strip("/")
    urls = []
    for branch in ("master", "main"):
        if "gitlab" in host:
            name = path.rsplit("/", 1)[-1]
            urls.append(f"https://{host}/{path}/-/archive/{branch}/{name}-{branch}.zip")
        elif "github.com" in host:
            urls.append(f"https://{host}/{path}/archive/refs/heads/{branch}.zip")
        else:  # gitea and similar self-hosted forges
            urls.append(f"https://{host}/{path}/archive/{branch}.zip")
    return urls


def download(
    url: str, dest_path: str | Path, opener=urlopen, progress=None, max_bytes=_MAX_DOWNLOAD_BYTES
) -> Path:
    """Stream ``url`` to ``dest_path`` in chunks; call ``progress(done, total)`` as it goes.

    ``max_bytes`` aborts with :class:`DownloadTooLarge` once exceeded — defaults to a 2 GiB cap
    (lowered when following an installer's source repo, e.g. Erion's ~1 GB). Real network fetches
    are SSRF-checked by :func:`_validate_url`; an injected ``opener`` (a test double) skips that.
    """
    dest_path = Path(dest_path)
    if opener is urlopen:
        _validate_url(url)
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with opener(request) as response:
        total = int(response.headers.get("Content-Length", 0) or 0)
        done = 0
        with open(dest_path, "wb") as handle:
            while chunk := response.read(_DOWNLOAD_CHUNK):
                handle.write(chunk)
                done += len(chunk)
                if max_bytes is not None and done > max_bytes:
                    raise DownloadTooLarge(f"source exceeded {max_bytes // 1_000_000} MB")
                if progress is not None:
                    progress(done, total)
    return dest_path
