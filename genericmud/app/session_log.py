"""Session log files, spoken log locations and the diagnostic summary."""

from __future__ import annotations

from datetime import datetime

from genericmud.bridge import protocol
from genericmud.logpaths import diagnostic_log_dir, log_count, world_log_dir
from genericmud.safepath import world_component
from genericmud.session.log import SessionLogger
from genericmud.voice.router import SYSTEM_CHANNEL


def _files(count: int) -> str:
    """A spoken file count: "1 file", "2 files". Read aloud, "1 files" sounds like a bug."""
    return f"{count} file" if count == 1 else f"{count} files"


class SessionLogMixin:
    def _speak_logs(self) -> None:
        """`/logs`: where each kind of log is kept, and how many are in each folder.

        Two folders, two audiences -- a session log to read back, a diagnostic file to send
        to someone when something is wrong -- so it is worth being able to ask which is
        which without opening a file manager and listening to a list.
        """
        worlds = world_log_dir()
        diagnostics = diagnostic_log_dir()
        self._speak_system(
            f"world logs in {worlds} ({_files(log_count(worlds))}); "
            f"diagnostics in {diagnostics} ({_files(log_count(diagnostics))})"
        )

    def _diag_where(self) -> None:
        """Speak the diagnostic log path + a one-line summary (often names the failure)."""
        triggers = sum(
            len(bucket["trigger"]) for bucket in self.engine.registrations_by_source().values()
        )
        packs = len(self._last_activation.loaded) if self._last_activation else 0
        name = self._diag.path.name if self._diag is not None else "off"
        self._speak_system(
            f"diagnostic log {name}; backend {self._diag_backend_kind}; "
            f"{packs} packs, {triggers} triggers; {self.sink.cues} cues attempted"
        )

    def _log(self, text: str) -> None:
        if self.logger is not None and self.logger.active and not self.logger.log(text):
            # The write failed and logging auto-stopped; say so once rather than let the
            # fault abort the line (which would silence and drop everything after it).
            self.voice.speak("Session logging stopped: write failed.", channel=SYSTEM_CHANNEL,
                             interrupt=True)
            self._post(protocol.echo("* Session logging stopped: write failed."))

    def _toggle_log(self) -> None:
        """Alt+Shift+L: start or stop logging by hand."""
        if self.logger is not None and self.logger.active:
            name = self.logger.path.name
            self.logger.stop()
            self.logger = None
            self._speak_system(f"logging stopped: {name}")
            return
        self.start_logging()

    def start_logging(self, *, announce: bool = True) -> None:
        """Log to the file the world's log_files setting names. Every log appends and begins
        with the time, so a day's or a world's file reads as one session after another."""
        if self.logger is not None and self.logger.active:
            return
        now = datetime.now()
        # Sanitize the world name -- a pack-derived world could be named "../.." and escape the
        # logs directory when joined onto the path.
        safe_name = world_component(self.name or "session")
        if self.log_files == "world":
            path = self.log_dir / f"{safe_name}.log"
        elif self.log_files == "day":
            path = self.log_dir / f"{safe_name}-{now:%Y-%m-%d}.log"
        else:
            path = self.log_dir / f"{safe_name}-{now:%Y%m%d-%H%M%S}.log"
        logger = SessionLogger(path)
        try:
            logger.start()
        except OSError as error:
            self._speak_system(f"Couldn't start logging: {error.strerror or error}")
            return
        self.logger = logger
        self._log(f"Session started {now:%Y-%m-%d %H:%M:%S}")
        if announce:
            self._speak_system(f"logging to {path.name}")
        else:
            self._post(protocol.echo(f"* Logging to {path.name}"))
