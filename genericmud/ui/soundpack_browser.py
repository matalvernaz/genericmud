"""Browse soundpacks on mudsoundpack.com, download one and set it up."""

from __future__ import annotations

import shutil
import tempfile
import traceback
import webbrowser
import zipfile
from pathlib import Path

import wx

from genericmud.config.worlds import World
from genericmud.packs import (
    PackError,
    PackStore,
    SetupResult,
    detect_entry,
    entry_problem,
    git_sources,
    known_muds,
    manifest_sources,
    setup_pack,
    setup_pack_from_git,
    setup_pack_from_manifest,
    slugify,
    vault,
    world_from_pack,
)
from genericmud.packs.store import extract_pack
from genericmud.ui.background_work import _run_async

_SOURCE_MAX_BYTES = 3_000_000_000  # cap when following an installer's source repo (~3 GB)


class VaultBrowserDialog(wx.Dialog):
    """Browse mudsoundpack.com, download a pack, and run it through setup_pack.

    Network and the (potentially large) download run off the UI thread via
    :func:`_run_async`; progress marshals back with ``wx.CallAfter``. On success
    ``self.result`` holds the SetupResult and the dialog ends with ``wx.ID_OK`` so the
    frame can confirm the world and connect. Build-blind (no wx on the dev host).
    """

    def __init__(self, parent: wx.Window, store: PackStore, announce, diag=None) -> None:
        super().__init__(parent, title="Browse soundpacks (mudsoundpack.com)", size=(640, 480))
        self._store = store
        self._announce = announce  # speak status for screen-reader users
        self._diag = diag  # durable install trace (DiagnosticLog or None)
        self._last_milestone = 0  # throttles the spoken MB / file-count lines, not the gauge
        self._all_packs: list = []  # the full catalogue
        self._packs: list = []  # visible subset, parallel to the list box (what _selected indexes)
        self.result = None  # SetupResult once a pack is downloaded + set up
        self._alive = True  # a late download/setup callback must not touch a destroyed dialog

        sizer = wx.BoxSizer(wx.VERTICAL)
        # A read-only, focusable status LOG (not a StaticText): NVDA can Tab to it and
        # review every step, and each step is also spoken. Append-only, one line per step.
        sizer.Add(wx.StaticText(self, label="S&tatus:"), 0, wx.LEFT | wx.TOP, 8)
        self._status_log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
        self._status_log.SetName("Status")
        sizer.Add(self._status_log, 0, wx.EXPAND | wx.ALL, 8)

        sizer.Add(wx.StaticText(self, label="&Soundpacks:"), 0, wx.LEFT, 8)
        self._list = wx.ListBox(self, style=wx.LB_SINGLE)
        self._list.SetName("Soundpacks")
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)

        # Packs for clients genericMud can't load are hidden by default so the list is all
        # things that actually work here; tick this to see (and try) the rest anyway.
        self._show_all = wx.CheckBox(
            self, label="Also show packs for &other clients (may not work here)"
        )
        self._show_all.Bind(wx.EVT_CHECKBOX, self._on_toggle_unsupported)
        sizer.Add(self._show_all, 0, wx.LEFT | wx.BOTTOM, 8)

        self._gauge = wx.Gauge(self, range=100)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.ALL, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._setup_btn = wx.Button(self, label="&Download and set up")
        self._setup_btn.Bind(wx.EVT_BUTTON, self._on_download)
        self._setup_btn.Disable()
        browser_btn = wx.Button(self, label="Open in &browser")
        browser_btn.Bind(wx.EVT_BUTTON, self._on_open_browser)
        buttons.Add(self._setup_btn, 0, wx.RIGHT, 4)
        buttons.Add(browser_btn, 0, wx.RIGHT, 4)
        sizer.Add(buttons, 0, wx.ALL, 8)
        sizer.Add(self.CreateButtonSizer(wx.CLOSE), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        close = self.FindWindowById(wx.ID_CLOSE)
        if close is not None:
            close.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self._status("Loading the catalogue from mudsoundpack.com.")
        _run_async(vault.list_packs, self._on_listed)

    def _on_close(self, _event: wx.CommandEvent) -> None:
        self._alive = False  # a still-running catalogue/download thread must not touch us
        self.EndModal(wx.ID_CLOSE)

    def _status(self, message: str) -> None:
        """Append a step to the readable status log and speak it; safe from any thread."""
        if self._diag is not None:  # durable copy of every step, survives a later crash
            self._diag.event("vault", msg=message)
        wx.CallAfter(self._append_status, message)

    def _append_status(self, message: str) -> None:  # main thread
        if not self._alive:
            return
        self._status_log.AppendText(message + "\n")
        self._announce(message)

    def _on_listed(self, outcome) -> None:
        if not self._alive:
            return
        if isinstance(outcome, Exception):
            self._status(f"Couldn't load the catalogue: {outcome}")
            return
        self._all_packs = outcome
        shown, hidden = self._populate_list()
        hint = (
            f" {hidden} for other clients are hidden — tick Also show packs for other clients "
            "to see them."
            if hidden
            else ""
        )
        self._status(f"{shown} soundpacks loaded.{hint} Choose one, then Download and set up.")
        if self._packs:
            self._list.SetSelection(0)
            self._list.SetFocus()

    def _populate_list(self) -> tuple[int, int]:
        """Rebuild the list box from the catalogue; return (shown, hidden) counts.

        Other-client packs are hidden unless the checkbox is ticked. ``self._packs`` is kept
        parallel to the list box (the visible rows), since ``_selected`` indexes into it.
        """
        show_all = self._show_all.GetValue()
        self._packs = [pack for pack in self._all_packs if show_all or pack.supported]
        self._list.Clear()
        for pack in self._packs:
            version = f" v{pack.version}" if pack.version else ""
            tag = "" if pack.supported else " (other client)"
            # Commas, not dashes: the screen reader reads each " - " as "dash" (see _rule_summary).
            self._list.Append(
                f"{pack.name}, {pack.mud}, {pack.client}{version}, {pack.status}{tag}"
            )
        self._setup_btn.Enable(bool(self._packs))
        return len(self._packs), len(self._all_packs) - len(self._packs)

    def _on_toggle_unsupported(self, _event: wx.CommandEvent) -> None:
        shown, _hidden = self._populate_list()
        others = sum(1 for pack in self._all_packs if not pack.supported)
        if self._show_all.GetValue():
            self._status(f"Showing all {shown} packs, including {others} for other clients.")
        else:
            self._status(f"Showing {shown} supported packs; {others} for other clients hidden.")
        if self._packs:
            self._list.SetSelection(0)

    def _selected(self):
        index = self._list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._packs):
            return None
        return self._packs[index]

    def _on_download(self, _event: wx.CommandEvent) -> None:
        pack = self._selected()
        if pack is None:
            return
        if not pack.supported:
            warn = wx.MessageBox(
                f"{pack.client} packs aren't supported and probably won't work. Try anyway?",
                "Unsupported client", wx.YES_NO | wx.ICON_WARNING,
            )
            if warn != wx.YES:
                return
        self._setup_btn.Disable()
        self._last_milestone = 0
        self._status(f"Downloading {pack.name}. Large packs can take a while.")
        _run_async(lambda: self._fetch_and_setup(pack), self._on_setup_done)

    def _fill_world(self, result: SetupResult, mud_name: str) -> SetupResult:
        """Pack carried no world (a VIPMud .set): fall back to the known-MUD table, else a
        name-only stub, so the setup flow can still create and offer the world."""
        if result.world is not None:
            return result
        world = known_muds.lookup(mud_name) or World(name=mud_name, host="", port=0)
        return SetupResult(result.manifest, world, result.enabled_for)

    def _fetch_and_setup(self, pack):  # background thread
        source = manifest_sources.for_labels(pack.mud, pack.name)
        if source is not None:  # served as an HTTP file tree (Mush-Z): sync it, don't fetch a zip
            return self._setup_from_manifest(source, pack)
        git_source = git_sources.for_labels(pack.mud, pack.name)
        if git_source is not None:  # installer wrapping a git repo (Erion): fetch the repo directly
            self._status(f"Fetching {git_source.name} straight from its repository, no installer.")

            def fetch(url, dest, **kwargs):
                return vault.download(url, dest, progress=self._progress, **kwargs)

            result = setup_pack_from_git(self._store, git_source, download=fetch, diag=self._diag)
            if not self._store.is_trusted(result.manifest.id):
                self._status(
                    f"{git_source.name} is installed. Trust it in the Connect dialog so its "
                    "sounds load when you connect."
                )
            return self._fill_world(result, pack.mud)
        pack_id = slugify(pack.name)
        if pack_id in {manifest.id for manifest in self._store.installed()}:
            self._status(f"{pack.name} is already installed; using the cached copy.")
            world = world_from_pack(self._store.pack_dir(pack_id))
            return self._fill_world(
                SetupResult(
                    manifest=self._store.manifest(pack_id),
                    world=world,
                    enabled_for=world.name if world else None,
                ),
                pack.mud,
            )
        candidates = [item for item in vault.pack_downloads(pack.id) if item.installable]
        if not candidates:
            raise vault.SourceUnavailable(
                "no downloadable archive is published; use Open in browser for the author page"
            )
        tmp = Path(tempfile.mkdtemp(prefix="genericmud-pack-"))
        try:
            archive = tmp / "pack.zip"
            errors: list[str] = []
            selected = None
            for candidate in candidates:
                try:
                    vault.download(candidate.url, archive, progress=self._progress)
                except Exception as exc:  # noqa: BLE001 - try the pack's next published source
                    errors.append(f"{candidate.role}: {type(exc).__name__}: {exc}")
                    continue
                if zipfile.is_zipfile(archive):
                    selected = candidate
                    break
                errors.append(f"{candidate.role}: response was not a ZIP archive")
            if selected is None:
                raise vault.SourceUnavailable(
                    "the published archives are unavailable (" + "; ".join(errors) + ")"
                )
            extracted = tmp / slugify(pack.name)  # pack-named dir -> a stable, unique pack id
            self._status(f"Extracting {pack.name}.")
            try:
                # Route through the guarded extractor (zip-bomb quota + nested-zip descent), not a
                # bare extractall -- this is the primary download path and must not bypass the cap.
                extract_pack(archive, extracted)
            except zipfile.BadZipFile as exc:
                raise PackError(
                    "the download wasn't a ZIP (the site may have served a web page)"
                ) from exc
            entry = detect_entry(extracted, mud_name=pack.mud)
            origin = selected.url  # record the source that actually yielded a valid archive
            if entry is None:  # an installer bundle? follow the repo it clones
                extracted, entry, followed = self._follow_installer(extracted, tmp)
                if followed:
                    origin = followed  # update from the real source, not the installer
            if entry is None:
                raise PackError(entry_problem(extracted))
            self._status(f"Setting up {pack.name}.")
            return self._fill_world(
                setup_pack(self._store, extracted, entry=entry, origin=origin), pack.mud
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _setup_from_manifest(self, source, pack):  # background thread
        """Install/update a manifest-style pack (Mush-Z): sync its file tree in place.

        A fresh install pulls the whole tree; re-running fetches only what changed. The pack
        installs enabled-but-untrusted (it runs its own Lua), so the user is told to trust it.
        """
        installed = source.id in {manifest.id for manifest in self._store.installed()}
        verb = "Updating" if installed else "Downloading"
        self._status(f"{verb} {source.name}. The first install fetches the whole pack.")
        result = setup_pack_from_manifest(
            self._store, source, progress=self._sync_progress, diag=self._diag
        )
        if not self._store.is_trusted(result.manifest.id):
            self._status(
                f"{source.name} is installed. Open Manage Soundpacks and trust it so its "
                "sounds load when you connect."
            )
        return self._fill_world(result, pack.mud)

    def _sync_progress(self, done: int, total: int, relpath: str) -> None:  # background thread
        """Per-file sync progress: drive the gauge, speak file counts every 10%.

        The percentage is the gauge's job. The counts are spoken because a ~9000-file
        sync is long enough that "how many files, out of how many" is the reassuring
        part, and no progress bar conveys it.
        """
        if not total:
            return
        pct = min(int(done * 100 / total), 100)
        wx.CallAfter(self._set_gauge, pct)
        milestone = pct - pct % 10
        if milestone and milestone != self._last_milestone:
            self._last_milestone = milestone
            self._status(f"Synced {done} of {total} files.")

    def _follow_installer(self, extracted, tmp):  # background thread
        """If the download is just a Windows installer, fetch the repo it git-clones
        and retry from there. Size-capped, so a huge source aborts and surfaces its URL."""
        source = vault.installer_source(extracted)
        if not source:
            return extracted, None, None
        self._status(f"This is an installer. Fetching the pack from its source: {source}")
        src_dir = tmp / "source"
        self._last_milestone = 0  # reset progress for the second (source) download
        for archive_url in vault.git_archive_urls(source):
            try:
                src_zip = vault.download(
                    archive_url, tmp / "source.zip",
                    progress=self._progress, max_bytes=_SOURCE_MAX_BYTES,
                )
            except vault.DownloadTooLarge as exc:
                raise PackError(f"{exc}; get the pack directly from {source}") from exc
            except Exception:  # noqa: BLE001 - wrong branch / not found -> try the next URL
                continue
            try:
                extract_pack(src_zip, src_dir)  # guarded: quota + nested-zip descent
            except (zipfile.BadZipFile, PackError):
                continue  # wrong branch / not a zip / over quota -> try the next candidate URL
            entry = detect_entry(src_dir)
            if entry:
                return src_dir, entry, archive_url
        return src_dir, None, None  # the pack is copied into the store

    def _set_gauge(self, pct: int) -> None:  # main thread
        if self._alive:
            self._gauge.SetValue(pct)

    def _progress(self, done: int, total: int) -> None:  # background thread
        if total:
            wx.CallAfter(self._set_gauge, min(int(done * 100 / total), 100))
            return  # NVDA reports the gauge itself; see UpdateProgressDialog's docstring
        # No Content-Length (GitLab archives): nothing drives the gauge, so there's no
        # progress bar for NVDA to read and speech is the only channel left. Every 50 MB.
        milestone = done // 50_000_000
        if milestone and milestone != self._last_milestone:
            self._last_milestone = milestone
            self._status(f"Downloaded {done // 1_000_000} MB.")

    def _on_setup_done(self, outcome) -> None:
        if not self._alive:
            return
        if isinstance(outcome, Exception):
            if isinstance(outcome, vault.SourceUnavailable):
                if self._diag is not None:
                    self._diag.event("vault.unavailable", error=str(outcome))
                self._status(f"Source unavailable: {outcome}")
                self._setup_btn.Enable()
                return
            if self._diag is not None:
                self._diag.event("vault.failed", error=repr(outcome),
                                 trace="".join(traceback.format_exception(outcome)))
            self._status(f"Setup failed: {outcome}")
            self._setup_btn.Enable()
            return
        self.result = outcome
        if self._diag is not None:
            w = outcome.world
            world_str = f"{w.host}:{w.port}" if w and w.host else (w.name if w else "")
            self._diag.event("vault.done", id=outcome.manifest.id,
                             dialect=outcome.manifest.dialect, world=world_str)
        # speak directly (not via the deferred log) -- the dialog is about to close
        self._announce("Download and set up complete. Confirm the connection details.")
        self.EndModal(wx.ID_OK)

    def _on_open_browser(self, _event: wx.CommandEvent) -> None:
        pack = self._selected()
        if pack is not None:
            webbrowser.open(f"{vault.BASE_URL}/pack.php?id={pack.id}")
