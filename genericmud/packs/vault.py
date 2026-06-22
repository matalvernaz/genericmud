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
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

BASE_URL = "https://mudsoundpack.com"
_USER_AGENT = "genericMud-soundpack-browser"
_DOWNLOAD_CHUNK = 65536

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


def download(url: str, dest_path: str | Path, opener=urlopen, progress=None) -> Path:
    """Stream ``url`` to ``dest_path`` in chunks; call ``progress(done, total)`` as it goes."""
    dest_path = Path(dest_path)
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with opener(request) as response:
        total = int(response.headers.get("Content-Length", 0) or 0)
        done = 0
        with open(dest_path, "wb") as handle:
            while chunk := response.read(_DOWNLOAD_CHUNK):
                handle.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    return dest_path
