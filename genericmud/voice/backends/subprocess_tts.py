"""Self-voice via a platform TTS command: macOS ``say`` and Linux speech-dispatcher.

The screen-reader backends (prism / SAPI) queue utterances internally and clear on
interrupt; the router relies on that (``speak`` enqueues and returns, ``stop`` barges in).
This backend reproduces the same contract on macOS and Linux with a background worker that
runs one TTS subprocess at a time, so streamed MUD lines don't talk over each other, and a
``stop`` that drains the queue and cancels the utterance in progress.

macOS ``say`` blocks until the utterance finishes, so terminating it is enough to barge in.
Linux ``spd-say -w`` waits on the speech-dispatcher daemon (the same one Orca drives, so the
two share a queue instead of overlapping); killing the client doesn't stop the daemon, so a
separate ``spd-say -C`` cancels it.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading

from genericmud.voice.backends.base import VoiceBackend

_QUEUE_SENTINEL = None  # pushed by close() to end the worker
_CANCEL_TIMEOUT_SECONDS = 2  # bound the daemon-cancel subprocess (spd-say -C)


class QueuedTtsBackend(VoiceBackend):
    """Speak queued text through a TTS command line, one utterance at a time.

    ``command`` is the argv prefix; the text is appended as the final argument.
    ``cancel_command`` (optional) is run by :meth:`stop` for daemons that keep speaking
    after the client process is killed (speech-dispatcher).
    """

    def __init__(
        self, command: list[str], cancel_command: list[str] | None = None
    ) -> None:
        if not command or shutil.which(command[0]) is None:
            raise RuntimeError(f"TTS command not available: {command[0] if command else '?'}")
        self._command = command
        self._cancel_command = cancel_command
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._current: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def speak(self, text: str) -> None:
        if text:
            self._queue.put(text)

    def stop(self) -> None:
        self._drain()
        if self._cancel_command is not None:
            # The daemon keeps speaking after the client exits; cancel it explicitly.
            try:
                subprocess.run(
                    self._cancel_command, check=False, timeout=_CANCEL_TIMEOUT_SECONDS
                )
            except (OSError, subprocess.SubprocessError):
                pass
        with self._lock:
            proc = self._current
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def close(self) -> None:
        """Stop the worker thread (process teardown)."""
        self._drain()
        self._queue.put(_QUEUE_SENTINEL)

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is _QUEUE_SENTINEL:
                return
            try:
                proc = subprocess.Popen(
                    [*self._command, text],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                continue  # a transient spawn failure drops one utterance, not the worker
            with self._lock:
                self._current = proc
            try:
                proc.wait()
            except Exception:  # noqa: BLE001 - a wait fault must not kill the worker
                pass
            with self._lock:
                self._current = None


class MacSayBackend(QueuedTtsBackend):
    """macOS self-voice via the built-in ``say`` command (always present)."""

    def __init__(self) -> None:
        super().__init__(["say"])


class SpeechDispatcherBackend(QueuedTtsBackend):
    """Linux self-voice via speech-dispatcher's ``spd-say`` (present wherever Orca runs)."""

    def __init__(self) -> None:
        # -w waits until the utterance is spoken so the worker serializes; -C cancels.
        super().__init__(["spd-say", "-w"], cancel_command=["spd-say", "-C"])
