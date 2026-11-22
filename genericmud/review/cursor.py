"""Review cursor over the scrollback Buffer.

Drives the read-on-demand review model: line/word/char navigation, jump to
top/bottom, search, copy-word, and VIPMud-style recall of the last N messages
(Ctrl+1-9, plus Ctrl+0 for the tenth). Methods return the text to speak; the
caller voices it on a review channel (interrupt=True) so each move barges in.
Pure and synchronous — the live output stream never moves this cursor (the
anti-flooding split).
"""

from __future__ import annotations

import re
from bisect import bisect_right

from genericmud.model.buffer import Buffer, Link

RECALL_MAX = 10  # VIPMud's Ctrl+1-9 covers nine; Ctrl+0 recalls the tenth


class ReviewCursor:
    def __init__(self, buffer: Buffer) -> None:
        self._buffer = buffer
        self._seq = 0  # all-time seq of the current line; survives appends AND ring eviction
        self._word = 0
        self._char = 0
        self.active = False
        # (seq, column, link): the character a link jump or the output box's caret put the cursor
        # on, finer than the word, so two links in one word ("NS") are each reachable, and for a
        # jump the very link it announced, so the follow key acts on that one and no other. It is
        # the line's own seq, never its index, so eviction can't move it onto another line, and
        # any other movement ends it: see _pinned_column.
        self._pin: tuple[int, int, Link | None] | None = None

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
        self._pin = None
        self.active = True
        self._set_line(len(self._buffer) - 1)
        self._word = self._char = 0
        return self._line_text()

    def exit(self) -> None:
        self._pin = None
        self.active = False

    def go_to(self, index: int) -> str:
        """Enter review on the buffer line at ``index`` (clamped), at its first word."""
        self._pin = None
        self.active = True
        self._set_line(index)
        self._word = self._char = 0
        return self._line_text()

    def go_to_column(self, column: int) -> str:
        """Enter the current line at the word holding ``column``, counted from its start.

        A window showing this buffer has a caret of its own, and the line it is on is not enough
        to act on: a line can carry several links, and the one the reader means is the one under
        the caret. Parking on its word is what makes the follow key act on that link rather than
        giving up on a line with more than one.
        """
        text = self._line_text()
        self._word = 0
        for number, match in enumerate(re.finditer(r"\S+", text)):
            if match.end() > max(0, column):
                self._word = number
                break
        else:
            self._word = max(0, len(text.split()) - 1)
        self._char = 0
        # The caret's own character, too: one word can hold two links ("NS"), and the one
        # under the caret is the one meant.
        self._pin_column(column, None)
        return self.current_word()

    def next_line(self) -> str:
        self._pin = None
        self._set_line(self._index() + 1)
        self._word = self._char = 0
        return self._line_text()

    def prev_line(self) -> str:
        self._pin = None
        self._set_line(self._index() - 1)
        self._word = self._char = 0
        return self._line_text()

    def top(self) -> str:
        self._pin = None
        self._set_line(0)
        self._word = self._char = 0
        return self._line_text()

    def bottom(self) -> str:
        self._pin = None
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
        self._pin = None
        self._word += 1
        return self.current_word()

    def prev_word(self) -> str:
        self._pin = None
        self._word -= 1
        return self.current_word()

    def current_char(self) -> str:
        text = self._line_text()
        if not text:
            return ""
        self._char = max(0, min(self._char, len(text) - 1))
        return text[self._char]

    def next_char(self) -> str:
        self._pin = None
        self._char += 1
        return self.current_char()

    def prev_char(self) -> str:
        self._pin = None
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
        self._pin = None
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

    def current_line(self) -> str:
        """The whole text of the line the cursor is on, for copying (Ctrl+Shift+C)."""
        return self._line_text()

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

    def links(self) -> list[Link]:
        """Each MXP link on the current line, as the words it shows and what it does."""
        if len(self._buffer) == 0:
            return []
        return list(self._buffer[self._index()].links)

    def _link_at(self, word: str, links: list[Link]) -> Link | None:
        """The link among ``links`` one of whose own words is ``word``.

        Compared word by word, not as a substring: "a" is inside "fountain", and a cursor on
        "a" is not on the link.
        """
        key = _word_key(word)
        if not key:
            return None
        for link in links:
            if key in {_word_key(part) for part in link.text.split()}:
                return link
        return None

    def link(self) -> Link | None:
        """The link under the cursor, or the line's only link, or ``None``.

        After a link jump, the link the jump announced. Otherwise by place: the character the
        caret put the cursor on, then the word the cursor is on. A link sits where its markup put
        it, or, where that isn't known (a pack replaced its line), where its words were found.
        One link in the word is the one meant; two in one word ("NS") are named by the follow key
        rather than guessed between. A link whose place isn't known is also matched by its words
        wherever the cursor meets them, which is what makes a line carrying several links usable
        from the keyboard: the reader walks to a word with Alt+Right, the way a mouse follows the
        word under its pointer, and presses the follow key. Where the cursor is on no link, a line
        with exactly one link still follows it -- so the plain "act on this line" gesture works --
        and with several the caller names them instead of guessing.
        """
        links = self.links()
        if not links:
            return None
        jumped = self._pinned_link()
        if jumped is not None:
            return jumped
        found = _link_words(self._line_text(), links)
        pinned = self._pinned_column()
        if pinned is not None:
            hit = next((link for link, _word, start, end in found if start <= pinned < end), None)
            if hit is not None:
                return hit
        word = self._word_span()
        if word is not None:
            covering = [
                link for link, _word, start, end in found if start < word[1] and word[0] < end
            ]
            if len(covering) == 1:
                return covering[0]
            if covering:
                return None
        text = self._line_text()
        unplaced = [link for link in links if not (link.placed and link.end <= len(text))]
        under = self._link_at(self.current_word(), unplaced)
        if under is not None:
            return under
        return links[0] if len(links) == 1 else None

    # --- jumping from link to link (Alt+PageDown / Alt+PageUp) ---

    def next_link(self, *, inclusive: bool = False) -> Link | None:
        """Move to the next MXP link after the cursor, on this line or a later one.

        The cursor parks on the link's first character, so the follow key acts on that link
        even when it shares a word with another. ``inclusive`` also counts a link that starts
        where the cursor is, for a reader who has only just arrived. None, with the cursor left
        where it was, at the end.
        """
        if len(self._buffer) == 0:
            return None
        start = self._index()
        here = self._here()
        for index in range(start, len(self._buffer)):
            line = self._buffer[index]
            for link, word, column, _end in _link_words(line.plain_text, line.links):
                if index == start and (column < here or (column == here and not inclusive)):
                    continue
                self._park(index, word, column, link)
                return link
        return None

    def prev_link(self, *, from_line_end: bool = False) -> Link | None:
        """Move to the link before the cursor, on this line or an earlier one.

        ``from_line_end`` counts every link on the cursor's own line, for a reader who has
        only just arrived at the newest line. None, with the cursor left where it was, at the
        start.
        """
        if len(self._buffer) == 0:
            return None
        start = self._index()
        here = self._here()
        for index in range(start, -1, -1):
            line = self._buffer[index]
            for link, word, column, _end in reversed(_link_words(line.plain_text, line.links)):
                if index == start and not from_line_end and column >= here:
                    continue
                self._park(index, word, column, link)
                return link
        return None

    def _park(self, index: int, word: int, column: int, link: Link) -> None:
        self.active = True
        self._set_line(index)
        self._word = word
        self._char = 0
        self._pin_column(column, link)

    # --- where in the line the cursor is, finer than the word ---

    def _pin_column(self, column: int, link: Link | None) -> None:
        self._pin = (self._buffer[self._index()].seq, column, link) if len(self._buffer) else None

    def _pinned_column(self) -> int | None:
        """The pinned character, while the cursor is still on the very line it was pinned on.

        Compared with the seq of the line actually under the cursor, not the cursor's own
        anchor: once that line is evicted the cursor shows the oldest line left, and the pin
        must not land on it.
        """
        if self._pin is None or len(self._buffer) == 0:
            return None
        seq, column, _link = self._pin
        if self._buffer[self._index()].seq != seq:
            return None
        return column

    def _pinned_link(self) -> Link | None:
        """The link a jump parked on, while the pin holds and the link is still on the line."""
        if self._pinned_column() is None or self._pin is None or self._pin[2] is None:
            return None
        jumped = self._pin[2]
        return jumped if any(link is jumped for link in self.links()) else None

    def _word_span(self) -> tuple[int, int] | None:
        """Where the cursor's word sits in its line, or None on a line with no words."""
        spans = [match.span() for match in re.finditer(r"\S+", self._line_text())]
        if not spans:
            return None
        return spans[max(0, min(self._word, len(spans) - 1))]

    def _here(self) -> int:
        """The character link jumps count from: the pinned one, or the start of the word."""
        pinned = self._pinned_column()
        if pinned is not None:
            return pinned
        word = self._word_span()
        return word[0] if word is not None else 0


def _link_words(text: str, links: list[Link]) -> list[tuple[Link, int, int, int]]:
    """Each link on a line with the word it starts in and where its words start and end, in the
    order they sit.

    A link whose place is known starts where its words do. One whose place isn't (a pack
    replaced its line) is found by its words, searching on from the one before, as links always
    were; one whose words aren't in the line is left out.
    """
    spans = [match.span() for match in re.finditer(r"\S+", text)]
    ends = [end for _begin, end in spans]

    def word_at(column: int) -> int | None:
        number = bisect_right(ends, column)  # the first word ending after it
        return number if number < len(spans) else None

    found: list[tuple[Link, int, int, int]] = []
    searched_to = 0
    for link in links:
        if link.placed and link.end <= len(text):
            column, end = link.start, link.end
        else:
            if not link.text.strip():
                continue
            start = text.find(link.text, searched_to)
            if start < 0:
                continue
            searched_to = start + len(link.text)
            column = start + len(link.text) - len(link.text.lstrip())
            end = start + len(link.text.rstrip())
        word = word_at(column)
        if word is not None:
            found.append((link, word, column, end))
    found.sort(key=lambda item: item[2])
    return found

def _word_key(text: str) -> str:
    """Letters and digits only, lowercased, so "north," and "North" both match "north"."""
    return "".join(ch for ch in text.lower() if ch.isalnum())
