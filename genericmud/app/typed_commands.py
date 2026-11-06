"""Typed input, command stacking, aliases and genericMud client commands."""

from __future__ import annotations

import re

from genericmud.automation.engine import Callback, MatchContext
from genericmud.bridge import protocol
from genericmud.model.buffer import Line
from genericmud.packs import user_rules
from genericmud.voice.router import SYSTEM_CHANNEL

CLIENT_PREFIX = "/"  # "/alias", "/trigger", ... ; unknown /verbs pass through to the MUD
MAX_ALIAS_DEPTH = 20  # guard against an alias/trigger that re-fires itself forever


# Interactive /alias and /trigger register under their own source so the soundpack builder's
# reload (which clears user_rules.SOURCE) can't silently delete them.
INTERACTIVE_SOURCE = "user-interactive"


class TypedCommandsMixin:
    """Typed input, command stacking, aliases and genericMud client commands.

    Part of EngineApp (engine_app.py). Every attribute below starts in its constructor.

    Writes: _client_error_spoken, _dispatch_depth, _last_input, _mxp_login_armed.

    Reads: _diag, _input_masked, _login, _post, _send, _user_aliases, _user_triggers, autoretype,
    buffer, command_separator, echo_input, engine, hub, log_commands, map, nav, voice.

    Calls: _speak_system, links (_activate_link_at, _follow_link), autologin (_dispatch_secret),
    mud_data (_use_secure_port), key_actions (_handle_find, _handle_key), session_log (_log,
    _speak_logs), soundpacks (update_packs), walking (_goto, _label_room, _on_player_moved,
    _safe_speedwalk, _speedwalk).

    _mxp_login_armed is shared: autologin arms it; typed input disarms it.
    """

    def on_ws_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == protocol.INPUT:
            text = message.get("text", "")
            if self._input_masked:
                self._dispatch_secret(text)
                return
            if text.strip():
                self._last_input = text
            elif self.autoretype and self._last_input:
                # A blank line normally goes to the MUD as-is (pagers use it); with
                # autoretype on, an empty Enter repeats the last typed input instead.
                text = self._last_input
            # A client command ("/alias x = a;b") must not be split on the separator.
            commands = [text] if text.startswith(CLIENT_PREFIX) else self._split_commands(text)
            for command in commands:
                self._dispatch_command(command)
        elif kind == protocol.KEY:
            self._handle_key(message.get("key", ""))
        elif kind == protocol.LINK:
            # The web renderer drew a real anchor and the player activated it. Only the
            # command comes back: what it may do is decided here, not in the renderer.
            self._follow_link(
                str(message.get("text", "")),
                str(message.get("command", "")),
                insert=message.get("insert") is True,
                hint=str(message.get("hint", "")),
            )
        elif kind == protocol.MENU_CHOICE:
            # The player picked one command from a menu. It is still the MUD's text, so it
            # goes down the same road as a followed link: no client commands, same speech.
            self._follow_link(
                str(message.get("text", "")), str(message.get("command", ""))
            )
        elif kind == protocol.LINK_AT:
            from_end = message.get("from_end")
            column = message.get("column")
            self._activate_link_at(
                str(message.get("text", "")),
                from_end if isinstance(from_end, int) else 0,
                column if isinstance(column, int) else -1,
            )
        elif kind == protocol.CLIENT_ERROR:
            self._handle_client_error(message)
        elif kind == protocol.FIND:
            self._handle_find(message)

    def _handle_client_error(self, message: dict) -> None:
        """A renderer-side failure (e.g. Web Audio couldn't load/decode a sound)."""
        scope = message.get("scope", "client")
        file = message.get("file", "")
        detail = message.get("error", "")
        if self._diag is not None:
            self._diag.event("client.error", scope=scope, file=file, error=detail)
        summary = f"{scope} error: {file} {detail}".strip()
        self._post(protocol.echo(f"* {summary}"))
        if not self._client_error_spoken:  # speak the first; the rest stay in the output
            self._client_error_spoken = True
            self.voice.speak(summary, channel=SYSTEM_CHANNEL, interrupt=False)

    def _split_commands(self, text: str) -> list[str]:
        """Split stacked input on the separator ("n;n;look"); an empty separator is off.

        The separator typed twice is sent as itself, as in MUSHclient: "say hi;; bye" is the
        one command "say hi; bye". Soundpacks' scripts never come through here.
        """
        separator = self.command_separator
        if not separator or separator not in text:
            return [text]
        commands: list[str] = []
        current: list[str] = []
        index = 0
        while index < len(text):
            if not text.startswith(separator, index):
                current.append(text[index])
                index += 1
            elif text.startswith(separator, index + len(separator)):
                current.append(separator)  # doubled: the character itself
                index += 2 * len(separator)
            else:
                commands.append("".join(current))
                current = []
                index += len(separator)
        commands.append("".join(current))
        return [command for command in commands if command != ""]

    def _dispatch_command(self, text: str, *, allow_client: bool = True) -> None:
        if text.strip():
            # The user is typing their own login (or already playing): stand down so a later
            # in-game line can't make auto-login, or an MXP login request, send the stored
            # credentials.
            self._mxp_login_armed = False
            if self._login is not None and not self._login.done:
                self._login.disarm()
            if self.log_commands:
                self._log("> ***" if self._input_masked else f"> {text}")
            if self.echo_input and not self._input_masked:
                self._echo_input(text)
        if allow_client and self._client_command(text):
            return
        if self._safe_speedwalk(text) or self._speedwalk(text):
            return
        for line in self.engine.process_input(text):
            self._send(line)
            if self.nav.record(line):  # build the breadcrumb trail from manual walking
                self._on_player_moved()

    def _echo_input(self, text: str) -> None:
        """Put what the player typed into the output too (the world's "echo input" setting).

        Buffered rather than only shown, so the review keys can read a command back after it
        has scrolled away -- the point of having it at all for someone listening. Not spoken:
        the player typed it, and hearing every command repeated is noise.

        Typed input takes _dispatch_secret while the MUD hides it, but a command sent from
        another tab, an alias or a trigger reaches _dispatch_command directly, so the guard at
        the call site is what keeps masked text out of the output on those paths.
        """
        line = Line(f"> {text}")
        self.buffer.append(line)
        self._post(protocol.line(line.plain_text))

    def _dispatch_remote(self, text: str) -> None:
        """Deliver a command sent from ANOTHER session (mud.send_to / mud.broadcast).

        Cross-session text must NOT run this session's client commands (/alias, /trigger, ...):
        a pack loaded for one world could otherwise reprogram the user's other sessions. It's
        treated as game input (aliases still expand); a leading '/' goes to the MUD literally.
        """
        self._dispatch_command(text, allow_client=False)

    def _client_command(self, text: str) -> bool:
        """Handle "/alias", "/trigger", "/to", ...; False = not one (send to the MUD)."""
        if not text.startswith(CLIENT_PREFIX):
            return False
        verb, _, rest = text[len(CLIENT_PREFIX) :].partition(" ")
        verb, rest = verb.lower(), rest.strip()
        if verb in ("alias", "trigger"):
            self._define_user_rule(verb, rest)
        elif verb == "unalias":
            self._remove_user_rule("alias", rest)
        elif verb == "untrigger":
            self._remove_user_rule("trigger", rest)
        elif verb == "aliases":
            self._list_user_rules("alias")
        elif verb == "triggers":
            self._list_user_rules("trigger")
        elif verb == "to":
            self._send_to_session(rest)
        elif verb == "goto":
            self._goto(rest)
        elif verb == "label":
            self._label_room(rest)
        elif verb == "map":
            self._speak_system(self.map.summary())
        elif verb == "update" and not rest:
            self.update_packs()
        elif verb == "logs":
            self._speak_logs()
        elif verb == "secure":
            self._use_secure_port()
        else:
            return False  # unknown /verb -> let the MUD have it (some use slash commands)
        return True

    def _user_rules(self, kind: str) -> dict[str, str]:
        return self._user_aliases if kind == "alias" else self._user_triggers

    def _define_user_rule(self, kind: str, rest: str) -> None:
        pattern, separator, command = rest.partition("=")
        pattern, command = pattern.strip(), command.strip()
        if not separator or not pattern or not command:
            self._speak_system(f"usage: /{kind} <text> = <command>")
            return
        self._user_rules(kind)[pattern] = command
        callback = self._user_rule_callback(command)
        if kind == "alias":
            self.engine.remove_alias(pattern)  # replace any existing alias for this text
            self.engine.add_alias(
                f"^{re.escape(pattern)}$", callback,
                regex=True, name=pattern, source=INTERACTIVE_SOURCE, keep_evaluating=False,
            )
            # Words typed after the shortcut go along on the end of the last command, as with
            # a plain alias made in the Automation Manager, but only when no other alias takes
            # the command. The same name, so /unalias removes both.
            self.engine.add_alias(
                user_rules.extra_words_pattern(pattern),
                self._user_rule_callback(command, carry_words=True),
                regex=True, name=pattern, source=INTERACTIVE_SOURCE,
                priority=user_rules.EXTRA_WORDS_PRIORITY, fallback=True,
            )
        else:
            self.engine.remove_trigger(pattern)
            self.engine.add_trigger(  # (?i): match the MUD's output regardless of case
                f"(?i){re.escape(pattern)}", callback, regex=True, name=pattern,
                source=INTERACTIVE_SOURCE,
            )
        self._speak_system(f"{kind} {pattern} added")

    def _remove_user_rule(self, kind: str, pattern: str) -> None:
        pattern = pattern.strip()
        if self._user_rules(kind).pop(pattern, None) is None:
            self._speak_system(f"no {kind} {pattern}")
            return
        if kind == "alias":
            self.engine.remove_alias(pattern)
        else:
            self.engine.remove_trigger(pattern)
        self._speak_system(f"{kind} {pattern} removed")

    def _list_user_rules(self, kind: str) -> None:
        rules = self._user_rules(kind)
        if not rules:
            self._speak_system("no aliases" if kind == "alias" else "no triggers")
            return
        self._speak_system("; ".join(f"{pattern} = {body}" for pattern, body in rules.items()))

    def _send_to_session(self, rest: str) -> None:
        session, _, command = rest.partition(" ")
        command = command.strip()
        if not session or not command:
            self._speak_system("usage: /to <session> <command>")
            return
        if self.hub is None or not self.hub.send_to(session, command):
            self._speak_system(f"no session {session}")

    def _user_rule_callback(self, command: str, *, carry_words: bool = False) -> Callback:
        """Run ``command`` when the rule fires; ``carry_words`` adds the words typed after it."""

        def callback(ctx: MatchContext) -> None:
            words = ctx.wildcards[1] if carry_words and len(ctx.wildcards) > 1 else ""
            self._run_user_command(command, extra_words=words)

        return callback

    def _run_user_command(self, command: str, *, extra_words: str = "") -> None:
        if self._dispatch_depth >= MAX_ALIAS_DEPTH:
            return  # an alias/trigger looped on itself; stop rather than hang
        self._dispatch_depth += 1
        try:
            pieces = self._split_commands(command)
            if extra_words and pieces:
                pieces[-1] = f"{pieces[-1].rstrip()} {extra_words}"
            for piece in pieces:
                self._dispatch_command(piece)
        finally:
            self._dispatch_depth -= 1
