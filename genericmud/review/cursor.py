"""Review cursor over the scrollback Buffer.

Drives the read-on-demand review model: line/word/char navigation, jump to
top/bottom, search, copy-word, and VIPMud-style recall of the last N messages
(Ctrl+1-9). Methods return the text to speak; the caller voices it on a review
channel (interrupt=True) so each move barges in. Pure and synchronous — the live
output stream never moves this cursor (the anti-flooding split).
"""

from __future__ import annotations

from genericmud.model.buffer import Buffer

RECALL_MAX = 9  # VIPMud recalls the last nine messages via Ctrl+1-9


class ReviewCursor:
    def __init__(self, buffer: Buffer) -> None:
        self._buffer = buffer
        self._line = 0
        self._word = 0
        self._char = 0
        self.active = False

    # --- line ---

    def enter(self) -> str:
        """Enter review at the most recent line; freezes auto-scroll for the caller."""
        self.active = True
        self._line = max(0, len(self._buffer) - 1)
        self._word = self._char = 0
        return self._line_text()

    def exit(self) -> None:
        self.active = False

    def next_line(self) -> str:
        self._line += 1
        self._word = self._char = 0
        return self._line_text()

    def prev_line(self) -> str:
        self._line -= 1
        self._word = self._char = 0
        return self._line_text()

    def top(self) -> str:
        self._line = 0
        self._word = self._char = 0
        return self._line_text()

    def bottom(self) -> str:
        self._line = max(0, len(self._buffer) - 1)
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

    def recall(self, n: int) -> str:
        """Return the n-th most recent line (1 = newest), or '' if out of range."""
        if not (1 <= n <= RECALL_MAX):
            return ""
        index = len(self._buffer) - n
        if index < 0:
            return ""
        return self._buffer[index].plain_text

    def search(self, term: str, *, forward: bool = False) -> str:
        count = len(self._buffer)
        if count == 0 or not term:
            return ""
        indices = range(self._line + 1, count) if forward else range(self._line - 1, -1, -1)
        needle = term.lower()
        for i in indices:
            if needle in self._buffer[i].plain_text.lower():
                self._line = i
                self._word = self._char = 0
                return self._line_text()
        return ""

    def copy_word(self) -> str:
        return self.current_word()

    def _line_text(self) -> str:
        if len(self._buffer) == 0:
            return ""
        self._line = max(0, min(self._line, len(self._buffer) - 1))
        return self._buffer[self._line].plain_text
