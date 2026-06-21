"""Auto-login: watch incoming lines for the name/password prompts and answer them.

Login prompts vary by MUD, so this is heuristic — substring matches against the
incoming line, username first then password, each sent once. The default phrases
cover the common cases; they're overridable per world. Prompt detection is
deliberately ordered (password is only sent after the username) so a stray
"password" mention before login can't fire first.
"""

from __future__ import annotations

from collections.abc import Callable

DEFAULT_USER_PROMPTS = (
    "what is your name",
    "enter your name",
    "by what name",
    "account name",
    "character name",
    "login:",
)
DEFAULT_PASS_PROMPTS = ("password",)


class AutoLogin:
    def __init__(
        self,
        username: str,
        password: str,
        send: Callable[[str], None],
        *,
        user_prompts: tuple[str, ...] = DEFAULT_USER_PROMPTS,
        pass_prompts: tuple[str, ...] = DEFAULT_PASS_PROMPTS,
    ) -> None:
        self._username = username
        self._password = password
        self._send = send
        self._user_prompts = user_prompts
        self._pass_prompts = pass_prompts
        self._sent_user = False
        self._sent_pass = False

    def feed(self, line: str) -> None:
        low = line.lower()
        if not self._sent_user:
            if any(prompt in low for prompt in self._user_prompts):
                self._send(self._username)
                self._sent_user = True
        elif not self._sent_pass and any(prompt in low for prompt in self._pass_prompts):
            self._send(self._password)
            self._sent_pass = True

    @property
    def done(self) -> bool:
        return self._sent_pass
