"""Review cursor over the scrollback Buffer.

Drives the read-on-demand review model: line/word/char navigation, jump to
top/bottom, search, copy-word, and VIPMud-style recall of the last N messages
(Ctrl+1-9, plus Ctrl+0 for the tenth). Methods return the text to speak; the
caller voices it on a review channel (interrupt=True) so each move barges in.
Pure and synchronous — the live output stream never moves this cursor (the
anti-flooding split).
"""

from __future__ import annotations

from genericmud.model.buffer import Buffer

RECALL_MAX = 10  # VIPMud's Ctrl+1-9 covers nine; Ctrl+0 recalls the tenth


class ReviewCursor:
    def __init__(self, buffer: Buffer) -> None:
        self._buffer = buffer
        self._seq = 0  # all-time seq of the current line; survives appends AND ring eviction
        self._word = 0
        self._char = 0
        self.active = False

    def _index(self) -> int:
        """The current line's index into the live buffer, derived from its seq.

        Anchoring on seq (not a raw index) is what keeps the position on the line the user
        parked on: arriving lines don't move it, and once the ring is full and starts
        evicting, an evicted position clamps to the oldest surviving line instead of
        silently sliding onto a newer one.
        """
        count = len(self._buffer)
        if count == 0:
            return 0
        base = self._buffer[0].seq  # seq of the oldest line still buffered
        return max(0, min(self._seq - base, count - 1))

    def _set_line(self, index: int) -> None:
        count = len(self._buffer)
        self._seq = self._buffer[max(0, min(index, count - 1))].seq if count else 0

    # --- line ---

    def enter(self) -> str:
        """Enter review at the most recent line; freezes auto-scroll for the caller."""
        self.active = True
        self._set_line(len(self._buffer) - 1)
        self._word = self._char = 0
        return self._line_text()

    def exit(self) -> None:
        self.active = False

    def next_line(self) -> str:
        self._set_line(self._index() + 1)
        self._word = self._char = 0
        return self._line_text()

    def prev_line(self) -> str:
        self._set_line(self._index() - 1)
        self._word = self._char = 0
        return self._line_text()

    def top(self) -> str:
        self._set_line(0)
        self._word = self._char = 0
        return self._line_text()

    def bottom(self) -> str:
        self._set_line(len(self._buffer) - 1)
        self._word = self._char = 0
        return self._line_text()

    # --- word / char within the current line ---

    def current_word(self) -> str:
        words = self._line_text().split()
        if not words:
            return ""
        self._word = max(0, min(self._word, len(words) - 1))
        return words[self._word]

    def next_word(self) -> str:
        self._word += 1
        return self.current_word()

    def prev_word(self) -> str:
        self._word -= 1
        return self.current_word()

    def current_char(self) -> str:
        text = self._line_text()
        if not text:
            return ""
        self._char = max(0, min(self._char, len(text) - 1))
        return text[self._char]

    def next_char(self) -> str:
        self._char += 1
        return self.current_char()

    def prev_char(self) -> str:
        self._char -= 1
        return self.current_char()

    # --- recall / search / copy ---

    def recall(self, n: int, channel: str | None = None) -> str:
        """Return the n-th most recent line (1 = newest), optionally filtered to a channel."""
        if not (1 <= n <= RECALL_MAX):
            return ""
        lines = self._buffer.lines()
        if channel is not None:
            lines = [line for line in lines if line.channel == channel]
        index = len(lines) - n
        if index < 0:
            return ""
        return lines[index].plain_text

    def search(
        self,
        term: str,
        *,
        forward: bool = False,
        case_sensitive: bool = False,
        include_current: bool = False,
    ) -> str:
        """Move to the next line containing ``term`` and return it, or "" if there is none.

        Searches from the line the cursor is on, exclusive by default, so repeating a
        search walks through matches. ``include_current`` is for the first search after
        positioning at an end of the buffer. Does not wrap.
        """
        count = len(self._buffer)
        if count == 0 or not term:
            return ""
        current = self._index()
        if forward:
            start = current if include_current else current + 1
            indices = range(start, count)
        else:
            start = current if include_current else current - 1
            indices = range(start, -1, -1)
        needle = term if case_sensitive else term.lower()
        for i in indices:
            haystack = self._buffer[i].plain_text
            if needle in (haystack if case_sensitive else haystack.lower()):
                self._set_line(i)
                self._word = self._char = 0
                return self._line_text()
        return ""

    def copy_word(self) -> str:
        return self.current_word()

    def spell_line(self) -> str:
        """The current line spelled character by character (screen-reader review aid).

        Characters are comma-separated so the synth pauses between them; a space
        is named outright because most synths would skip it silently.
        """
        text = self._line_text()
        return ", ".join("space" if ch == " " else ch for ch in text)

    def _line_text(self) -> str:
        if len(self._buffer) == 0:
            return ""
        return self._buffer[self._index()].plain_text

    # --- MXP links ---

    def links(self) -> list[tuple[str, str]]:
        """(displayed text, command) for each MXP link on the current line."""
        if len(self._buffer) == 0:
            return []
        return list(self._buffer[self._index()].links)

    def link(self) -> tuple[str, str] | None:
        """The link the cursor's word sits in, or the line's only link, or ``None``.

        Matching on the word is what makes a line carrying several links usable from the
        keyboard: the reader walks to a word with Alt+Right (the same way a mouse follows
        the word under its pointer) and presses the follow key. Where the word belongs to
        no link, a line with exactly one link still follows it -- so the plain "act on this
        line" gesture works -- and with several the caller names them instead of guessing.
        """
        links = self.links()
        if not links:
            return None
        word = _word_key(self.current_word())
        if word:
            for text, command in links:
                if word in _word_key(text):
                    return (text, command)
        return links[0] if len(links) == 1 else None


def _word_key(text: str) -> str:
    """Letters and digits only, lowercased, so "north," and "North" both match "north"."""
    return "".join(ch for ch in text.lower() if ch.isalnum())
