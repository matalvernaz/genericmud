"""Auto-login: watch incoming lines for the name/password prompts and answer them.

Login prompts vary by MUD, so this is heuristic. Two guards keep the stored password
from leaking to the game as a command:

* the password is only sent AFTER the username prompt has been answered, and
* it is only sent to a line that actually looks like a password prompt (anchored:
  ``...password:`` / ``password >`` / a bare ``Password`` line), never any line that
  merely contains the word — a banner like "never share your password" won't fire it,
* and only within a few lines of the name prompt, so a later in-game line mentioning
  "password" can't trigger a send.

The name prompt is watched only near the start of the connection (a generous line
budget), and the whole thing disarms the moment the user sends their own input, so an
in-game line like "by what name do the villagers call you?" can't make it type the
stored username — or arm the password send — deep into a session.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Cues for the (non-secret) username prompt: substrings, plus a line that is nothing but the
# word for "name" and a colon, which is how Diku/Merc/ROM muds and Spanish ones (issue #126,
# Crisol: "Name:", "Nombre:") ask. Whole-line only, so a banner heading that happens to end in
# "name:" can't send the username and arm the password watch.
DEFAULT_USER_PROMPTS = (
    "what is your name",
    "enter your name",
    "by what name",
    "account name",
    "character name",
    "login:",
    r"^\s*(?:name|nombre)\s*[:?]\s*$",
)
# Anchored password-prompt patterns: the word followed by prompt punctuation, or a bare
# "Password" line. Deliberately NOT a bare substring, so a sentence mentioning "password"
# can't cause the credential to be sent. Spanish ("Contraseña:", issue #126) only as a whole
# line, the same shapes as the bare English one.
DEFAULT_PASS_PROMPTS = (
    r"pass(?:word|phrase)\s*[:>?]",
    r"^\s*pass(?:word|phrase)\s*$",
    r"^\s*contrase(?:ñ|n)a\s*[:>?]?\s*$",
)
# A line that mentions a password at all, in the languages MUDs are most often written in. It
# tells a hidden answer the MUD asked for (a password, its retype) from one it didn't, and it keeps
# what is typed while a MUD hides typing for a password away from every alias (_dispatch_secret).
# Loose on purpose: a wrong match only keeps a line secret, or a hint unspoken. PIN counts only in
# capitals, since a pin is also something you pick up, and a lowercase match in a game's own text
# would keep a soundpack's commands from it. Auto-login answers only DEFAULT_PASS_PROMPTS.
_PASSWORD_WORDS = (
    r"pass(?:word|phrase|code)s?",  # English
    r"contrase(?:ñ|n)as?",  # Spanish
    r"contraseny\w*",  # Catalan
    r"senhas?",  # Portuguese
    r"mots? de passe",  # French
    r"(?:pass|kenn)w(?:o|ö|oe)rt\w*",  # German
    r"wachtwoord\w*",  # Dutch
    r"l(?:ö|o)senord\w*",  # Swedish
    r"passord\w*",  # Norwegian
    r"(?:adgangskode|kodeord)\w*",  # Danish
    r"salasan\w*",  # Finnish
    r"has(?:ł|l)(?:o|a|em|u|ach)",  # Polish
    r"hesl(?:o|a|em|u|ech)",  # Czech and Slovak
    r"парол\w*",  # Russian, Ukrainian, Bulgarian
    r"(?:ş|s)ifre\w*",  # Turkish
    r"jelsz\w*",  # Hungarian
)
ASKS_FOR_A_PASSWORD = re.compile(
    r"(?i:\b(?:" + "|".join(_PASSWORD_WORDS) + r")\b)|\bPINs?\b"
)
_MAX_LINES_AFTER_USER = 6  # give up watching for the password prompt after this many lines
# Watch for the name prompt only near connect: covers banners/menus without leaving the
# credential armed for a later in-game "by what name..." line if the prompt never matched.
_MAX_LINES_TO_USER = 50


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
        max_lines_to_user: int = _MAX_LINES_TO_USER,
    ) -> None:
        self._username = username
        self._password = password
        self._send = send
        self._user_prompts = tuple(re.compile(p, re.IGNORECASE) for p in user_prompts)
        self._pass_prompts = tuple(re.compile(p, re.IGNORECASE) for p in pass_prompts)
        self._max_lines = max_lines
        self._max_lines_to_user = max_lines_to_user
        self._sent_user = False
        self._sent_pass = False
        self._lines_since_user = 0
        self._lines_before_user = 0

    def disarm(self) -> None:
        """Stop watching entirely (the user is driving login/play themselves)."""
        self._sent_user = True
        self._sent_pass = True

    def feed(self, line: str) -> None:
        if not self._sent_user:
            if any(pattern.search(line) for pattern in self._user_prompts):
                self._send(self._username)
                self._sent_user = True
            else:
                self._lines_before_user += 1
                if self._lines_before_user >= self._max_lines_to_user:
                    self.disarm()  # name prompt never came; don't lurk into the session
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
