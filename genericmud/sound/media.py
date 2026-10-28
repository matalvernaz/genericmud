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
import ipaddress
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request

from genericmud.safepath import (
    confine,
    is_allowed_host,
    is_unsafe,
    resolve_media,
    trusted_addresses,
)

# Formats the pygame mixer can play. Anything else is refused before a byte is written, so a
# hostile server can't drop an .exe or .bat into the user's config folder.
AUDIO_EXTENSIONS = frozenset(
    {".wav", ".ogg", ".oga", ".opus", ".mp3", ".flac", ".mid", ".midi"}
    | {".mod", ".xm", ".it", ".s3m"}
)
# Characters Windows can't keep in a file name, refused on every platform so a name means one
# plain file everywhere: "*" and "?" are MSP wildcards (Mudlet never downloads such a name), and
# on NTFS "evil.bat:x.wav" writes a hidden stream onto evil.bat instead of a sound file.
_NOT_IN_FILE_NAMES = frozenset('<>:"|?*') | frozenset(chr(code) for code in range(32))
MAX_MEDIA_BYTES = 64 * 1024 * 1024  # a long music track; nothing a sound cue needs is bigger
# A world's whole media folder, so a MUD naming new files without end can't fill the disk. It's
# checked before each download, so the downloads running at that moment can still finish.
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
MAX_IN_FLIGHT = 16  # a server spamming new names can't queue unbounded fetches
_AT_ONCE = 4  # of those, how many one session downloads at the same time
_TIMEOUT_SECONDS = 30  # for each read, so a server sending a byte at a time never trips it
_DEADLINE_SECONDS = 600  # for the whole body: 64 MB still lands at about 1 Mbit/s
_CHUNK = 65536
_USER_AGENT = "genericMud-media"
# What a Wi-Fi sign-in page or a server's error page is sent as. Nothing else is turned away
# by its type, since servers label sound files every which way.
_WEB_PAGE_TYPES = frozenset({"text/html", "application/xhtml+xml"})

Fetch = Callable[[str, Path, str], None]  # (url, destination, MUD's address): write it or raise
# Soundpacks' own sound downloads (a VIPMud pack fetching its missing .wav files), across every
# session, run this many at a time.
_PACK_SOUND_SLOTS = threading.BoundedSemaphore(_AT_ONCE)
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
    if is_unsafe(name) or any(character in _NOT_IN_FILE_NAMES for character in name):
        return False
    return Path(name.replace("\\", "/")).suffix.lower() in AUDIO_EXTENSIONS


def _trusted(address: str) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """See :func:`genericmud.safepath.trusted_addresses` -- the addresses a download may reach."""
    return trusted_addresses(address)


def is_allowed_media_host(host: str, trusted_address: str = "") -> bool:
    """Whether media may be fetched from ``host`` -- see :func:`safepath.is_allowed_host`."""
    return is_allowed_host(host, trusted_address)


def http_fetch(url: str, dest: Path, trusted_address: str = "") -> None:
    """Download ``url`` to ``dest`` with the soundpack downloader's SSRF checks and a size cap.

    ``trusted_address`` is the address the MUD session is connected to. It passes the
    private-address check, so a MUD on localhost or the LAN can serve its own sounds; every
    other private address is still refused, so a public MUD can't point downloads at the
    player's router or other machines.
    """
    from genericmud.packs.vault import _validate_url, trusting_opener

    trusted = _trusted(trusted_address)
    _validate_url(url, trusted)
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    deadline = time.monotonic() + _DEADLINE_SECONDS
    with trusting_opener(trusted).open(request, timeout=_TIMEOUT_SECONDS) as response:
        if response.headers.get_content_type() in _WEB_PAGE_TYPES:
            # Saved under the sound's name, it would be found there and never fetched again.
            raise ValueError(f"{url} sent a web page, not a sound")
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_MEDIA_BYTES:
            raise ValueError(f"{url} is larger than {MAX_MEDIA_BYTES // 1_000_000} MB")
        total = 0
        with open(dest, "wb") as handle:
            # read1 hands back whatever has arrived, where read() waits for a whole chunk, so
            # the deadline is checked as the bytes come in, however slowly.
            while chunk := response.read1(_CHUNK):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{url} was still downloading after {_DEADLINE_SECONDS:g} s")
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise ValueError(f"{url} is larger than {MAX_MEDIA_BYTES // 1_000_000} MB")
                handle.write(chunk)


class MediaFolderFull(Exception):
    """The world's media folder has reached :data:`MAX_CACHE_BYTES`."""


def _folder_bytes(root: Path) -> int:
    """Bytes in the files under ``root``, or 0 when it doesn't exist yet."""
    total = 0
    for folder, _folders, files in os.walk(root):
        for name in files:
            try:
                total += os.stat(os.path.join(folder, name)).st_size
            except OSError:
                pass  # removed while we counted
    return total


def _loop_submit(job: Callable[[], None], done: Callable[[BaseException | None], None]) -> None:
    """Run ``job`` on a thread of its own and ``done`` back on the event loop (or inline, no loop).

    Not the loop's default executor: asyncio resolves the host name of every connect and
    reconnect on those threads, so downloads stalled on a slow server kept every session from
    connecting. A daemon thread, because Python waits for executor threads before it exits,
    and a download still running would keep genericMud open after it was quit.
    """
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

    def run() -> None:
        error: BaseException | None = None
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - reported to done() on the loop
            error = exc
        try:
            loop.call_soon_threadsafe(done, error)
        except RuntimeError:
            pass  # the loop has closed: genericMud is quitting and nothing is left to play it

    threading.Thread(target=run, name="genericmud-media", daemon=True).start()


def run_in_own_thread[Result](job: Callable[[], Result]) -> asyncio.Future[Result]:
    """Run a soundpack's download ``job`` on a daemon thread; the future resolves on the loop.

    For the reasons :func:`_loop_submit` gives: not the loop's default executor, which asyncio
    needs for every connect's host lookup, and a daemon thread, so a download still running
    doesn't keep genericMud open after it's closed. Cancelling the future (the session
    closing) leaves the thread to finish on its own, with nothing waiting on its result.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Result] = loop.create_future()

    def settle(result: Result | None, error: BaseException | None) -> None:
        if future.cancelled():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)  # type: ignore[arg-type]

    def run() -> None:
        result: Result | None = None
        error: BaseException | None = None
        try:
            with _PACK_SOUND_SLOTS:
                result = job()
        except Exception as exc:  # noqa: BLE001 - handed to the future on the loop
            error = exc
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:
            pass  # the loop has closed: genericMud is quitting

    threading.Thread(target=run, name="genericmud-pack-sound", daemon=True).start()
    return future


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
        # Asked at each download for the address the session's connection reached: the one
        # non-public address that may serve media. Not the MUD's host name resolved again,
        # since the MUD decides what that resolves to and could point it at the player's router.
        self.trusted_address: Callable[[], str] = lambda: ""
        self._fetch = fetch
        self._submit = submit
        self._on_error = on_error  # (url, reason): a failed download, reported once per URL
        self._waiting: dict[Path, list[Callable[[str], None]]] = {}
        self._failed: set[str] = set()  # URLs not retried this session (a 404 recurs per cue)
        self._slots = threading.BoundedSemaphore(_AT_ONCE)
        self._full = False  # the folder reached its cap: said once, then nothing more is fetched

    def locate(self, name: str, *roots: str | None) -> str | None:
        """An existing file for ``name`` under ``roots`` and then the cache, or None."""
        cache = str(self.cache_dir) if self.cache_dir is not None else None
        return resolve_media(name, *roots, cache)

    def fetch(self, name: str, url: str, on_ready: Callable[[str], None] | None = None) -> bool:
        """Download ``name`` from ``url`` into the cache, then call ``on_ready(path)``.

        Returns False when the download was refused outright (unsafe name, not audio, no
        cache, a URL that already failed, too many in flight, a full folder). A second
        request for a file already downloading joins the first instead of fetching it twice.
        """
        if self.cache_dir is None or not url or url in self._failed or not is_downloadable(name):
            return False
        if self._full:
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
        trusted_address = self.trusted_address()  # asked now: the job runs on another thread
        cache_dir = self.cache_dir

        def download() -> None:
            if _folder_bytes(cache_dir) >= MAX_CACHE_BYTES:
                gigabytes = MAX_CACHE_BYTES / 1024**3
                raise MediaFolderFull(
                    f"this world's media folder holds {gigabytes:g} GB already; delete files "
                    f"in {cache_dir} to let it download more"
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            # A file of its own rather than dest + ".part": tabs on one world share this
            # folder, and through one fixed name a second download truncated the first.
            handle, temporary = tempfile.mkstemp(".part", ".genericmud-media-", dest.parent)
            os.close(handle)
            partial = Path(temporary)
            try:
                self._fetch(url, partial, trusted_address)
                os.replace(partial, dest)  # never leave a half-written file a later cue would play
            finally:
                partial.unlink(missing_ok=True)

        def job() -> None:
            with self._slots:  # the rest of this session's downloads wait their turn
                download()

        def done(error: BaseException | None) -> None:
            waiters = self._waiting.pop(dest, [])
            if isinstance(error, MediaFolderFull):
                self._full = True
            if error is not None or not dest.is_file():
                self._failed.add(url)
                if self._on_error is not None:
                    self._on_error(url, str(error) if error else "nothing was downloaded")
                return
            for callback in waiters:
                callback(str(dest))

        self._submit(job, done)
        return True
