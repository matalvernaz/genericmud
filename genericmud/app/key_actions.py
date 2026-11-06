"""Review keys, Find, channel history, clipboard requests and speech toggles."""

from __future__ import annotations

from genericmud.bridge import protocol
from genericmud.voice.router import REVIEW_CHANNEL

_REVIEW_VERBS = frozenset(
    {
        "prev_line", "next_line", "prev_word", "next_word", "prev_char", "next_char",
        "top", "bottom", "spell_line",
    }
)
# Alt+PageDown / Alt+PageUp: jump from MXP link to MXP link through the output.
_LINK_JUMPS = frozenset({"next_link", "prev_link"})
_CHANNEL_REVIEW_VERBS = frozenset(
    {"prev", "next", "older", "newer", "prev_word", "next_word"}
)


def _parse_count(text: str) -> int | None:
    """A recall count from a keymap action, or None if it isn't a number.

    Keymaps are user-editable, so a fat-fingered ``recall:oops`` reaches here; without
    this guard the int() raised out of key dispatch and the key silently died forever.
    """
    try:
        return int(text)
    except ValueError:
        return None
# Toggles reachable from the keymap: attribute -> what the toggle announces.
_TOGGLE_ACTIONS = {
    "follow_mode": "follow mode",
    "interrupt_mode": "interrupt mode",
    "autoretype": "auto-repeat",  # spoken as "auto-repeat on/off" (empty Enter resends last cmd)
}


class KeyActionsMixin:
    """Review keys, Find, channel history, clipboard requests and speech toggles.

    Part of EngineApp (engine_app.py). Every attribute below starts in its constructor.

    Writes: follow_mode, interrupt_mode, autoretype (by name in _toggle).

    Reads: _post, buffer, chan_review, engine, keymap, map, pref_sink, review, sound, voice (and
    follow_mode, interrupt_mode, autoretype by name in _toggle).

    Calls: _speak_system, links (_activate_link, _announce_link_under_cursor, _jump_to_link),
    autologin (_unhide_on_request), session_log (_diag_where, _toggle_log), walking (_handle_nav).
    """

    def _handle_find(self, message: dict) -> None:
        """Search the scrollback and park the review cursor on the hit.

        The engine owns the search because the buffer is the deep copy: it holds far more
        history than the renderer's control does. The matched text goes back as a
        FIND_RESULT so the renderer can put its own caret on the same line where it can.
        """
        term = message.get("term", "")
        forward = bool(message.get("forward", False))
        restart = bool(message.get("restart", False)) or not self.review.active
        if self._anchor_review(message):
            # The reader's own line, in either direction: a search that ignored it both
            # skipped matches between the caret and the last hit and sent a fresh Find back
            # to the newest match, which is the line they had already read.
            include_current = restart
        elif message.get("from_bottom"):
            # The reader has been put back below the newest line, so the search goes on
            # from there: back, the newest line is the first one it looks at; forward,
            # there's nothing past it.
            self.review.enter()
            include_current = not forward
        else:
            if restart:
                # Nothing said where the reader is (the browser renderer has no caret of
                # its own), so a new Find starts at the chosen edge and includes that edge
                # line. Repeats remain exclusive, so F3 advances instead of finding the same
                # line forever.
                self.review.enter()
                if forward:
                    self.review.top()
            include_current = restart
        hit = self.review.search(
            term,
            forward=forward,
            case_sensitive=bool(message.get("case_sensitive", False)),
            include_current=include_current,
        )
        self._post(protocol.find_result(hit, found=bool(hit)))
        self._speak_review(hit or f"not found: {term}")

    def _anchor_review(self, message: dict) -> bool:
        """Put the review cursor on the output line the reader is standing on.

        The output box holds fewer lines than the buffer and keeps a caret of its own, so the
        window says which line it is: the line's text, and how far it sits from the end. The
        search then runs from there, which is what makes it go on from the reader's position
        rather than from wherever the last search left off -- and what stops a fresh Find from
        jumping back to the newest match the reader has already read.

        False when the window said nothing or the line is no longer in the buffer, leaving the
        caller to fall back on the edges. A line that was trimmed away is not a position.
        """
        text = str(message.get("text") or "")
        from_end = message.get("from_end")
        if not text or not isinstance(from_end, int):
            return False
        lines = self.buffer.lines()
        matches = [index for index, line in enumerate(lines) if line.plain_text == text]
        if not matches:
            return False
        near = len(lines) - 1 - max(0, from_end)
        self.review.go_to(min(matches, key=lambda index: abs(index - near)))
        return True

    def _handle_key(self, combo: str) -> None:
        action = self.keymap.get(combo)
        if action is None and ":" not in combo:
            self.engine.press_key(combo)  # user-defined macro
            return
        # A renderer control with a meaning of its own for a key (Enter in the output box,
        # where there is nothing to send) passes the action itself. Keymap combos never
        # contain a colon and actions always do, so the two can't be confused.
        namespace, _, argument = (action or combo).partition(":")
        if namespace == "review" and argument in _LINK_JUMPS and self.engine.has_key(combo):
            # Erion's history browser uses Alt+PageUp/PageDown: a soundpack's or the player's
            # own hotkey on the key keeps it in that world, as with the Alt+number tab keys.
            self.engine.press_key(combo)
            return
        if namespace == "tab":
            # Switching tabs is the window's job, and the window does it before the key gets
            # here. What reaches the engine is a key the world's soundpack or the player's own
            # hotkey claimed (Erion's channel history reads messages on Alt+1 to Alt+0), so it
            # goes to that binding, as it did before the key had a tab action.
            self.engine.press_key(combo)
            return
        if namespace == "recall":
            # "recall:N" or "recall:<channel>:N" (channel filters the scrollback).
            channel, _, count = argument.rpartition(":")
            n = _parse_count(count)
            if n is not None:
                self._speak_review(self.review.recall(n, channel=channel or None))
        elif namespace == "review" and argument == "link":
            self._activate_link()
        elif namespace == "review" and argument == "copy_line":
            self._copy_review_line()
        elif namespace == "review" and argument in _LINK_JUMPS:
            self._jump_to_link(forward=argument == "next_link")
        elif namespace == "review" and argument in _REVIEW_VERBS:
            if not self.review.active:
                self.review.enter()
            self._speak_review(getattr(self.review, argument)())
            self._announce_link_under_cursor()
        elif namespace == "voice" and argument == "flush":
            self.voice.flush()
        elif namespace == "voice" and argument in _TOGGLE_ACTIONS:
            self._toggle(argument)
        elif namespace == "input" and argument == "autoretype":
            self._toggle("autoretype")
        elif namespace == "input" and argument == "unhide":
            self._unhide_on_request()
        elif namespace == "chan":
            self._handle_channel_review(argument)
        elif namespace == "sound" and argument == "flush":
            self.sound.flush()  # panic key: cut all playing audio (Shift+F11)
        elif namespace == "nav":
            self._handle_nav(argument)
        elif namespace == "map" and argument == "where":
            self._speak_system(self.map.describe())
        elif namespace == "log" and argument == "toggle":
            self._toggle_log()
        elif namespace == "diag" and argument == "where":
            self._diag_where()

    def _copy_review_line(self) -> None:
        """Offer the reviewed line for copying, all of it or any part (Ctrl+Shift+C).

        Like every review key, it starts on the newest line when review isn't running yet. The
        window owns the clipboard and the box the words are picked from; the engine only says
        which line.
        """
        if not self.review.active:
            self.review.enter()
        text = self.review.current_line()
        if not text.strip():
            self._speak_review("nothing to copy")
            return
        self._post(protocol.copy_line(text))

    def _toggle(self, attr: str) -> None:
        """Flip a speech-ergonomics toggle, announce it, and persist via pref_sink."""
        value = not getattr(self, attr)
        setattr(self, attr, value)
        if self.pref_sink is not None:
            self.pref_sink(attr, value)
        self._speak_system(f"{_TOGGLE_ACTIONS[attr]} {'on' if value else 'off'}")

    def _handle_channel_review(self, action: str) -> None:
        # "chan:prev|next|older|newer|prev_word|next_word" or "chan:recall:N".
        verb, _, count = action.partition(":")
        if verb == "recall":
            n = _parse_count(count)
            if n is not None:
                self._speak_review(self.chan_review.recent(n))
        elif verb in _CHANNEL_REVIEW_VERBS:
            self._speak_review(getattr(self.chan_review, self._chan_method(verb))())

    @staticmethod
    def _chan_method(verb: str) -> str:
        # prev/next cycle channels; the rest are same-named ChannelReview methods.
        return {"prev": "prev_channel", "next": "next_channel"}.get(verb, verb)

    def _speak_review(self, text: str) -> None:
        # A review gesture on an empty buffer (fresh session, failed connect) returns "".
        # Speaking "" still interrupts, so it would silence the client and announce nothing;
        # give the user feedback instead. Non-blank lines are the only real content here --
        # blank server lines are never buffered.
        text = text or "no message"
        self.voice.speak(text, channel=REVIEW_CHANNEL, interrupt=True)
        self._post(protocol.review(text))
