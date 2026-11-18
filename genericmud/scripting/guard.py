"""Time-bound Lua execution so a runaway trigger/script can't hang the engine.

lupa won't accept a Python function as a Lua debug hook, so the hook is a Lua
closure (installed via ``install_hook``) that calls a Python deadline check every
N VM instructions; an overrun raises and aborts the running chunk. Script errors
and timeouts are contained so one bad pack doesn't take down the connection/UI.
A separate worker process would isolate further; this just prevents hangs.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

MAX_SCRIPT_SECONDS = 1.0
HOOK_INSTRUCTION_INTERVAL = 50_000

# Lua puts the chunk and line in front of its message, once per level an error passes through:
# [string "<python>"]:12: attempt to index a nil value. A player can act on the words after it;
# the diagnostic log keeps the whole thing.
_CHUNK_PREFIX = re.compile(r'^(?:\[string "[^"]*"\]:\d+:\s*)+')
_MAX_REASON = 200  # one line of speech, not a traceback


def error_reason(error: BaseException) -> str:
    """What went wrong, as one short line a player can hear.

    "A Lua script error occurred: LuaError" said that something broke but never what. An error
    with no words of its own, like Lua's error({}), still has its type to name it by.
    """
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    reason = _CHUNK_PREFIX.sub("", lines[0]) if lines else ""
    return (reason or type(error).__name__)[:_MAX_REASON]


class ScriptTimeout(Exception):
    pass


class ScriptGuardUnavailable(RuntimeError):
    """The Lua timeout hook couldn't be installed and the caller required it."""


class ScriptGuard:
    def __init__(
        self,
        install_hook: Callable | None,
        max_seconds: float = MAX_SCRIPT_SECONDS,
        *,
        require_hook: bool = False,
        report: Callable[[Exception], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._deadline = 0.0
        # What the budget is measured on. A pack's guard leaves out the time scripts spend
        # waiting on the player (ScriptApi.player_time), so answering a dialog slowly
        # doesn't stop the script that asked.
        self._clock = clock
        self._max_seconds = max_seconds
        self.last_error: Exception | None = None
        self._report = report  # called with a contained fault; else the cue vanishes silently
        self.enabled = install_hook is not None
        if require_hook and not self.enabled:
            # Fail closed: never run an untrusted pack with no runaway-loop protection at all.
            raise ScriptGuardUnavailable("no Lua timeout hook available; refusing untrusted code")
        if install_hook is not None:
            install_hook(self._check, HOOK_INSTRUCTION_INTERVAL)

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    def _check(self) -> None:
        if self._now() > self._deadline:
            raise ScriptTimeout("script exceeded its time budget")

    def run(self, fn: Callable, *args: object):
        """Run a fire-time Lua callback under the budget; contain errors/timeouts."""
        self._deadline = self._now() + self._max_seconds
        try:
            return fn(*args)
        except Exception as error:  # timeout or script error — keep the engine alive
            self.last_error = error
            if self._report is not None:
                self._report(error)
            return None

    def run_strict(self, fn: Callable, *args: object):
        """Time-bound a call but let errors propagate (for pack loading/import reports)."""
        self._deadline = self._now() + self._max_seconds
        return fn(*args)
