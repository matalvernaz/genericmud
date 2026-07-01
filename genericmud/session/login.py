"""Auto-login: watch incoming lines for the name/password prompts and answer them.

Login prompts vary by MUD, so this is heuristic. Two guards keep the stored password
from leaking to the game as a command:

* the password is only sent AFTER the username prompt has been answered, and
* it is only sent to a line that actually looks like a password prompt (anchored:
  ``...password:`` / ``password >`` / a bare ``Password`` line), never any line that
  merely contains the word — a banner like "never share your password" won't fire it,
* and only within a few lines of the name prompt, so a later in-game line mentioning
  "password" can't trigger a send.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Substring cues for the (non-secret) username prompt.
DEFAULT_USER_PROMPTS = (
    "what is your name",
    "enter your name",
    "by what name",
    "account name",
    "character name",
    "login:",
)
# Anchored password-prompt patterns: the word followed by prompt punctuation, or a bare
# "Password" line. Deliberately NOT a bare substring, so a sentence mentioning "password"
# can't cause the credential to be sent.
DEFAULT_PASS_PROMPTS = (
    r"pass(?:word|phrase)\s*[:>?]",
    r"^\s*pass(?:word|phrase)\s*$",
)
_MAX_LINES_AFTER_USER = 6  # give up watching for the password prompt after this many lines


class AutoLogin:
    def __init__(
        self,
        username: str,
        password: str,
        send: Callable[[str], None],
        *,
        user_prompts: tuple[str, ...] = DEFAULT_USER_PROMPTS,
        pass_prompts: tuple[str, ...] = DEFAULT_PASS_PROMPTS,
        max_lines: int = _MAX_LINES_AFTER_USER,
    ) -> None:
        self._username = username
        self._password = password
        self._send = send
        self._user_prompts = tuple(re.compile(p, re.IGNORECASE) for p in user_prompts)
        self._pass_prompts = tuple(re.compile(p, re.IGNORECASE) for p in pass_prompts)
        self._max_lines = max_lines
        self._sent_user = False
        self._sent_pass = False
        self._lines_since_user = 0

    def feed(self, line: str) -> None:
        if not self._sent_user:
            if any(pattern.search(line) for pattern in self._user_prompts):
                self._send(self._username)
                self._sent_user = True
            return
        if self._sent_pass:
            return
        self._lines_since_user += 1
        if any(pattern.search(line) for pattern in self._pass_prompts):
            self._send(self._password)
            self._sent_pass = True
        elif self._lines_since_user >= self._max_lines:
            # No password prompt right after the name: stop watching so a later in-game line
            # containing "password" can't trigger a send. Disarm without sending anything.
            self._sent_pass = True

    @property
    def done(self) -> bool:
        return self._sent_pass
