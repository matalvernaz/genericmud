"""Autocomplete from recent MUD output (the MUSHclient/MUDBall Tab-complete idea).

Two pure pieces: :class:`OutputWordIndex` collects words as displayed lines
arrive (most-recent-first, deduplicated), and :class:`CompletionCycler` holds
the cycling state for one completion session in the input box. The index is
written on the engine loop thread and read on the UI thread, so its state is
lock-guarded; the cycler lives on the UI thread only.
"""

from __future__ import annotations

import re
import threading

MIN_WORD_LENGTH = 3  # shorter words are cheaper to type than to cycle to
MAX_WORDS = 500  # names/items from the recent past; older words age out
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")


class OutputWordIndex:
    """Words seen in recent output, newest first, one entry per word."""

    def __init__(
        self, *, max_words: int = MAX_WORDS, min_length: int = MIN_WORD_LENGTH
    ) -> None:
        self._max = max_words
        self._min = min_length
        self._words: list[str] = []  # newest first; no duplicates (case-insensitive)
        self._lock = threading.Lock()

    def add_line(self, text: str) -> None:
        words = [w for w in _WORD_RE.findall(text) if len(w) >= self._min]
        if not words:
            return
        with self._lock:
            for word in words:
                key = word.lower()
                self._words = [w for w in self._words if w.lower() != key]
                self._words.insert(0, word)
            del self._words[self._max :]

    def complete(self, prefix: str) -> list[str]:
        """Words starting with ``prefix`` (case-insensitive), newest first."""
        if not prefix:
            return []
        needle = prefix.lower()
        with self._lock:
            return [
                w for w in self._words
                if w.lower().startswith(needle) and w.lower() != needle
            ]


class CompletionCycler:
    """Cycling state for one completion run: begin() once, then next()/prev().

    The caller resets on any other keypress so a fresh prefix starts a fresh run.
    """

    def __init__(self) -> None:
        self._candidates: list[str] = []
        self._index = -1
        self.prefix = ""

    @property
    def active(self) -> bool:
        return bool(self._candidates)

    def begin(self, prefix: str, candidates: list[str]) -> None:
        self.prefix = prefix
        self._candidates = list(candidates)
        self._index = -1

    def next(self) -> str | None:
        if not self._candidates:
            return None
        self._index = (self._index + 1) % len(self._candidates)
        return self._candidates[self._index]

    def prev(self) -> str | None:
        if not self._candidates:
            return None
        self._index = (self._index - 1) % len(self._candidates)
        return self._candidates[self._index]

    def reset(self) -> None:
        self._candidates = []
        self._index = -1
        self.prefix = ""
