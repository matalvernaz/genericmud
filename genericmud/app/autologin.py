"""Automatic login, post-connect commands and hidden typing kept out of history."""

from __future__ import annotations

from genericmud.bridge import protocol
from genericmud.protocol import telnet as T
from genericmud.protocol.pueblo import CLIENT_LINE as PUEBLO_CLIENT_LINE
from genericmud.session.login import AutoLogin

# A MUD that never sends WONT ECHO after the login password would leave every command going
# out as a password. Mudlet's answer, copied here: during the first five minutes of a
# connection, a minute after the last line typed while hidden, stop hiding and say so. Later
# in a session (changing a password in-game) only WONT ECHO or Escape ends the hiding.
_LOGIN_PHASE_SECONDS = 300.0
_HIDDEN_INPUT_TIMEOUT_SECONDS = 60.0
# How long an automatic login waits for a login prompt before the world's after-connect
# commands are typed anyway. A MUD that asks sends its prompt within a second or two; this
# only exists so a MUD that never asks can't hold those commands for the whole session.
_POST_CONNECT_FALLBACK_SECONDS = 30.0
# A password prompt counts for the hiding the MUD starts after it while no more than this many
# other lines with words have arrived in between. A MUD sends its WILL ECHO with the prompt, so
# this only leaves room for a chat line or two slipping between them.
_PROMPT_LOOKBACK_LINES = 3


class AutologinMixin:
    """Automatic login, post-connect commands and hidden typing kept out of history.

    Part of EngineApp (engine_app.py). Every attribute below starts in its constructor.

    Writes: _hidden_answers, _hidden_timeout_token, _input_masked, _lines_since_password_prompt,
    _login, _login_phase_started, _mxp_login_armed, _mxp_login_sent, _password_asked,
    _password_guard, _password_prompt_seen, _pending_login, _post_connect_sent,
    _still_hidden_said.

    Reads: _clock, _diag, _post, _schedule, _send, _withdraw_option, connect_method, credentials,
    engine, log_commands, name, post_connect_commands, pueblo_enabled.

    Calls: _speak_system, incoming_text (_unread_text_asks_for_password), session_log (_log).

    _mxp_login_armed is shared: autologin arms it; typed input disarms it.
    """

    def begin_login(self, world: str) -> None:
        """Decide how this connection logs in. Sends nothing: the socket may not exist yet.

        ``on_connect`` runs *before* the transport connects, because the packs have to be
        armed before the first bytes arrive. A blind login typed here would therefore go
        nowhere -- _send raises ConnectionError and the line is dropped in silence -- so the
        up-front lines are queued, and :meth:`after_connect` types them once the socket is up.

        Four methods (``config.worlds.CONNECT_METHODS``). "prompts" answers the MUD's own
        prompts; "mush" and "diku" type the login blind, for MUDs that never ask; "none"
        sends no credentials at all.
        """
        self._login_phase_started = self._clock()  # a fresh connection means fresh prompts
        self._login = None
        self._pending_login = []
        self._password_guard = ""  # only once a password is actually going out
        self._post_connect_sent = False  # a reconnect types them again
        self._mxp_login_sent = set()  # which of USER/PASSWORD this link has
        # An MXP login request is only answered while the connection is logging in: the login
        # phase, and only until the player types something themselves, as with AutoLogin.
        self._mxp_login_armed = True
        creds = self.credentials.get(world) if self.credentials is not None else None
        if creds is None or self.connect_method == "none":
            # Nothing to send. A stored login under "none" is deliberately ignored -- that is
            # what "no auto-connect" means.
            return
        username, password = creds
        self._password_guard = password  # so a server that echoes it can't say it back
        if self.connect_method == "mxp":
            # The MUD asks over MXP, so nothing is typed until it does. If it never asks,
            # nothing is sent at all -- that is what "let the MUD ask" has to mean.
            return
        if self.connect_method == "mush":
            # Lowercase, as every MUSH/MOO connect screen prints it. Some servers compare this
            # command exactly: "CONNECT" or "Connect" makes them reprint the banner and ignore
            # the login, leaving the player at the connect screen with no error to explain it.
            # MUSHclient's documentation writes the command in capitals, which is where ours
            # came from; a server that doesn't care about case takes either form.
            self._pending_login = [f"connect {username} {password}"]  # MUSH/MOO's one line
        elif self.connect_method == "diku":
            self._pending_login = [username, password]  # Diku's name, then password
        else:
            self._login = AutoLogin(username, password, self._send_for_login)

    def after_connect(self) -> None:
        """The socket is up: type this world's up-front login, then its commands.

        Both reconnect paths reach this through :meth:`after_reconnect`; a first connection
        reaches it from the window, which is the only place that knows the transport opened.
        """
        if self.pueblo_enabled:
            # First, before any login or after-connect line: the world has to know what kind of
            # client is on the other end before it writes its greeting. Its own answer -- the
            # switch to HTML -- tells the parser when to start reading it.
            if self._diag is not None:
                self._diag.event("pueblo.announce", version=PUEBLO_CLIENT_LINE)
            self._send(PUEBLO_CLIENT_LINE)
        for line in self._pending_login:
            self._send_for_login(line)
        self._pending_login = []
        if self._login is not None or self.connect_method == "mxp":
            # A MUD that never asks for the login would otherwise hold the after-connect
            # commands for the whole session. The send is idempotent, so this is harmless
            # when the request arrives quickly.
            self._schedule(_POST_CONNECT_FALLBACK_SECONDS, self._send_post_connect)
        else:
            # No prompt to wait for, so nothing is pending between the login and the commands.
            self._send_post_connect()

    def _send_post_connect(self) -> None:
        """Type the world's after-connect commands, once per connection."""
        if self._post_connect_sent:
            return
        self._post_connect_sent = True
        for command in self.post_connect_commands.split("\n"):
            if command:
                self._send(command)  # straight to the wire: these are for the MUD, not /commands

    def _send_for_login(self, text: str) -> None:
        """Auto-login's typing. On a slow link its password goes out before the WILL ECHO
        that hides the prompt arrives, so it starts the hidden-input timeout regardless."""
        self._send(text)
        self._arm_hidden_timeout()

    def _mxp_login(self, request: str) -> None:
        """The MUD asked for the login over MXP: answer once, and only if told to.

        Strict on purpose. Only the "mxp" connect method answers, so a player who chose a
        different method never gets a login sent by a route they did not choose; and each of
        USER and PASSWORD is answered at most once per connection, so a repeated request
        cannot make the client type the password twice.
        """
        if self.connect_method != "mxp" or request in self._mxp_login_sent:
            return
        # Only while logging in. A request later in the session (a pack's Simulate, or text
        # that reached a secure line some other way) is not the MUD asking for the login.
        in_login_phase = self._clock() - self._login_phase_started < _LOGIN_PHASE_SECONDS
        if not self._mxp_login_armed or not in_login_phase:
            if self._diag is not None:
                self._diag.event("mxp.login_refused", request=request)
            return
        creds = self.credentials.get(self.name) if self.credentials is not None else None
        if creds is None:
            return
        username, password = creds
        self._mxp_login_sent.add(request)
        self._send_for_login(username if request == "user" else password)
        if request == "password":
            self._send_post_connect()  # the login is done, so what follows can go

    def _dispatch_secret(self, text: str) -> None:
        """Send what was typed while the server hides input, keeping it out of sight.

        The normal input path would rewrite or reroute it: auto-repeat swapped a password for
        the last command (the username), the separator split "abc;def" in two, ".2news" ran as
        a speedwalk, and client commands and the breadcrumb trail read it. None of that happens
        to a hidden line, and it is never shown or logged in the clear.

        A password also stays away from every alias, which could read it or swallow it. A
        hidden line is taken for one while a password prompt belongs to the hiding: the MUD's
        last words before it began, or anything it has said since, so a password changed on a
        busy MUD stays secret through the chatter that arrives before it is typed.

        Without a password prompt, the soundpack's aliases and the player's own still get the
        line, as they do in MUSHclient, which keeps running aliases while a MUD hides typing
        and only stops showing it. Some MUDs hide typing at other times, or forget to stop,
        and then Erion's soundpack commands went to the game instead (issue #132). Whatever
        no alias takes goes out exactly as typed.
        """
        if text.strip():
            self._mxp_login_armed = False  # the user is answering the prompt themselves
            if self._login is not None and not self._login.done:
                self._login.disarm()  # the user is answering the prompt themselves
            if self.log_commands:
                self._log("> ***")
        # One answer per asking: a MUD that asks for a password and then for it again (a new
        # character's retype, or a retry after a mismatch) gets each answer it asked for, and
        # only a line nobody asked for counts against the hiding.
        asked = self._password_asked
        self._password_asked = False
        if not self._password_prompt_seen and self._unread_text_asks_for_password():
            self._password_prompt_seen = True  # a prompt not yet read as a line counts too
        # A blank Enter answers a pager or a "press return" prompt, so it goes out as it is.
        secret = self._password_prompt_seen or not text.strip()
        if secret:
            outgoing = [text]
        else:
            outgoing = self.engine.process_input(text, hidden=True)
        for line in outgoing:
            self._send(line)
        if self._diag is not None:
            # Which way the line went, never what it said.
            self._diag.event(
                "input.hidden_line", secret=secret, taken_by_alias=not outgoing,
            )
        self._hidden_answers += 1
        if self._hidden_answers >= 2 and not asked and not self._still_hidden_said:
            # Once per hidden stretch, like Mudlet's box saying "still hidden" when it comes
            # back: a MUD that never ends the hiding leaves this the only clue to the way out.
            self._still_hidden_said = True
            self._speak_system(
                "The MUD is still hiding what you type. If it shouldn't be, press Escape.",
                interrupt=False,
            )
        self._arm_hidden_timeout()

    def _arm_hidden_timeout(self) -> None:
        """(Re)start the minute after which a login password prompt that never ended lets go.

        Measured from the last line sent, never from the prompt, so it can't fire while
        someone is still typing. A line past the login phase still cancels any earlier
        deadline, so that one can't fire mid-session either.
        """
        self._hidden_timeout_token += 1
        if self._clock() - self._login_phase_started >= _LOGIN_PHASE_SECONDS:
            return
        token = self._hidden_timeout_token
        self._schedule(_HIDDEN_INPUT_TIMEOUT_SECONDS, lambda: self._hidden_input_timed_out(token))

    def _hidden_input_timed_out(self, token: int) -> None:
        if token != self._hidden_timeout_token or not self._input_masked:
            return
        self._withdraw_option(T.OPT_ECHO)  # so the MUD's next WILL ECHO is a fresh request
        self.forget_hidden_input(reason="timeout")
        self._speak_system(
            "The MUD didn't say it had stopped hiding what you type, so genericMud stopped "
            "hiding it.",
            interrupt=False,
        )

    def _unhide_on_request(self) -> None:
        """Escape: treat typing as ordinary until the MUD hides it again (its next WILL ECHO)."""
        if not self._input_masked:
            return  # nothing hidden: Escape does nothing
        self.forget_hidden_input(reason="escape")
        self._speak_system("Typing is no longer hidden.")

    def forget_hidden_input(self, reason: str = "connection") -> None:
        """Treat typing as ordinary again, for when no WONT ECHO is coming.

        The socket went away or was replaced (the window's Disconnect reports itself
        through on_connection_status too); a login prompt timed out; or the user pressed
        Escape. ``reason`` says which, in the diagnostic log.

        A password prompt no hidden stretch has claimed goes too, even if nothing was hidden:
        it was asked on a connection that is gone, or for typing that is now ordinary.
        """
        self._lines_since_password_prompt = None
        self._set_input_masked(False, reason=reason)

    def _set_input_masked(self, masked: bool, *, reason: str = "mud") -> None:
        """Track whether the server hides typed input; tell the renderer when that flips.

        The renderer keeps its own Up-arrow history, so it has to know as well, or the
        password is one keypress away for anyone at the keyboard.

        Both flips go in the diagnostic log, the end with ``reason``: "mud", the usual way, for
        its WONT ECHO, or what forget_hidden_input was told. A soundpack that stops hearing
        commands is otherwise hard to tell apart from one that never got them.
        """
        if masked == self._input_masked:
            return
        self._input_masked = masked
        self._hidden_answers = 0
        self._still_hidden_said = False
        # A password prompt the MUD showed just before hiding belongs to this hidden stretch,
        # and to no other: once claimed it is gone, so the next hiding needs a prompt of its
        # own. One that arrives during the stretch joins it as it comes.
        self._password_prompt_seen = masked and self._lines_since_password_prompt is not None
        if masked:
            self._lines_since_password_prompt = None
        if not masked:
            # Hiding a prompt keeps a pending timeout: auto-login's password may have gone
            # out before the WILL ECHO for it arrived.
            self._hidden_timeout_token += 1
        if self._diag is not None:
            if masked:
                self._diag.event("input.hidden", password_prompt=self._password_prompt_seen)
            else:
                self._diag.event("input.unhidden", reason=reason)
        self._post(protocol.input_masked(masked))

    def _track_password_prompt(self, asks: bool, has_words: bool) -> None:
        """Note one line from the MUD: whether it asked for a password, and whether it has words.

        A prompt that arrives while typing is hidden joins that hidden stretch straight away
        (ROM's "Password: " comes after its WILL ECHO), so a password changed on a busy MUD
        stays secret through the chatter that arrives before it is typed. One that arrives in
        the open waits for the hiding the MUD starts after it, for a few lines with words.
        """
        if asks and self._input_masked:
            self._password_prompt_seen = True
        elif asks:
            self._lines_since_password_prompt = 0
        elif has_words and self._lines_since_password_prompt is not None:
            self._lines_since_password_prompt += 1
            if self._lines_since_password_prompt > _PROMPT_LOOKBACK_LINES:
                self._lines_since_password_prompt = None
