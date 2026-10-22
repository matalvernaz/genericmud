"""Server-supplied media: finding a cue's file on disk, and downloading it when the MUD says where.

MSP's ``U=`` parameter and GMCP ``Client.Media`` both let a server name a web address for a
sound the player doesn't have yet. Mudlet fetches those and plays them, so MUDs ship their audio
that way instead of as a separate pack download, and a client that ignores the URL is silent on
them. Downloads land in a per-world cache folder and are found there on every later cue.

Everything here is driven by the untrusted server, so every name goes through
:func:`~genericmud.safepath.confine`, only audio extensions are ever written, each file is capped
in size, and fetches reuse the soundpack downloader's SSRF guard (http(s) only, no private or
loopback hosts, re-checked on every redirect).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request

from genericmud.safepath import confine, is_unsafe, resolve_media

# Formats the pygame mixer can play. Anything else is refused before a byte is written, so a
# hostile server can't drop an .exe or .bat into the user's config folder.
AUDIO_EXTENSIONS = frozenset(
    {".wav", ".ogg", ".oga", ".opus", ".mp3", ".flac", ".mid", ".midi"}
    | {".mod", ".xm", ".it", ".s3m"}
)
MAX_MEDIA_BYTES = 64 * 1024 * 1024  # a long music track; nothing a sound cue needs is bigger
MAX_IN_FLIGHT = 16  # a server spamming new names can't queue unbounded fetches
_TIMEOUT_SECONDS = 30
_CHUNK = 65536
_USER_AGENT = "genericMud-media"

Fetch = Callable[[str, Path, str], None]  # (url, destination, MUD host): write the file or raise
Submit = Callable[[Callable[[], None], Callable[[BaseException | None], None]], None]


def media_url(base: str, name: str) -> str:
    """The address of ``name`` given a server's ``U=``/``url`` value.

    Both specs treat the URL as the folder the file lives in (``U=https://mud/sounds/``), but
    servers also send the full file address. A URL already ending in the file's name is taken
    as-is; otherwise the name is appended, with each path segment percent-encoded.
    """
    base = base.strip()
    if not base:
        return ""
    clean = name.replace("\\", "/")
    leaf = clean.rsplit("/", 1)[-1]
    if urlparse(base).path.rstrip("/").endswith("/" + leaf):
        return base
    return base.rstrip("/") + "/" + "/".join(quote(part) for part in clean.split("/"))


def is_downloadable(name: str) -> bool:
    """Whether ``name`` is a safe relative path to an audio file we'd be willing to write."""
    return not is_unsafe(name) and Path(name.replace("\\", "/")).suffix.lower() in AUDIO_EXTENSIONS


def http_fetch(url: str, dest: Path, trusted_host: str = "") -> None:
    """Download ``url`` to ``dest`` with the soundpack downloader's SSRF checks and a size cap.

    ``trusted_host`` is the MUD's own host. Its addresses pass the private-address check, so a
    MUD on localhost or the LAN can serve its own sounds; every other private address is still
    refused, so a public MUD can't point downloads at the player's router or other machines.
    """
    from genericmud.packs.vault import _validate_url, resolve_addresses, trusting_opener

    trusted = resolve_addresses(trusted_host) if trusted_host else frozenset()
    _validate_url(url, trusted)
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with trusting_opener(trusted).open(request, timeout=_TIMEOUT_SECONDS) as response:
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_MEDIA_BYTES:
            raise ValueError(f"{url} is larger than {MAX_MEDIA_BYTES // 1_000_000} MB")
        total = 0
        with open(dest, "wb") as handle:
            while chunk := response.read(_CHUNK):
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise ValueError(f"{url} is larger than {MAX_MEDIA_BYTES // 1_000_000} MB")
                handle.write(chunk)


def _loop_submit(job: Callable[[], None], done: Callable[[BaseException | None], None]) -> None:
    """Run ``job`` on a worker thread and ``done`` back on the event loop (or inline, no loop)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - reported to done() like a threaded failure
            done(exc)
        else:
            done(None)
        return
    future = loop.run_in_executor(None, job)
    future.add_done_callback(lambda f: done(None if f.cancelled() else f.exception()))


class MediaLibrary:
    """Finds a cue's file under the user's sounds folder or the download cache, fetching it once.

    ``fetch`` and ``submit`` are injectable so tests run without a network or threads.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        fetch: Fetch = http_fetch,
        submit: Submit = _loop_submit,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.trusted_host = ""  # the MUD's host: its own private address may serve media
        self._fetch = fetch
        self._submit = submit
        self._on_error = on_error  # (url, reason): a failed download, reported once per URL
        self._waiting: dict[Path, list[Callable[[str], None]]] = {}
        self._failed: set[str] = set()  # URLs not retried this session (a 404 recurs per cue)

    def locate(self, name: str, *roots: str | None) -> str | None:
        """An existing file for ``name`` under ``roots`` and then the cache, or None."""
        cache = str(self.cache_dir) if self.cache_dir is not None else None
        return resolve_media(name, *roots, cache)

    def fetch(self, name: str, url: str, on_ready: Callable[[str], None] | None = None) -> bool:
        """Download ``name`` from ``url`` into the cache, then call ``on_ready(path)``.

        Returns False when the download was refused outright (unsafe name, not audio, no
        cache, a URL that already failed, too many in flight). A second request for a file
        already downloading joins the first instead of fetching it twice.
        """
        if self.cache_dir is None or not url or url in self._failed or not is_downloadable(name):
            return False
        dest = confine(self.cache_dir, name.replace("\\", "/"))
        if dest is None:
            return False
        if dest in self._waiting:
            if on_ready is not None:
                self._waiting[dest].append(on_ready)
            return True
        if len(self._waiting) >= MAX_IN_FLIGHT:
            return False
        self._waiting[dest] = [on_ready] if on_ready is not None else []
        partial = dest.with_name(dest.name + ".part")
        trusted_host = self.trusted_host  # read now: the job runs on another thread

        def job() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._fetch(url, partial, trusted_host)
                os.replace(partial, dest)  # never leave a half-written file a later cue would play
            finally:
                partial.unlink(missing_ok=True)

        def done(error: BaseException | None) -> None:
            waiters = self._waiting.pop(dest, [])
            if error is not None or not dest.is_file():
                self._failed.add(url)
                if self._on_error is not None:
                    self._on_error(url, str(error) if error else "nothing was downloaded")
                return
            for callback in waiters:
                callback(str(dest))

        self._submit(job, done)
        return True
