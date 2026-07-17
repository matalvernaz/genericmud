"""Channel browsing: a review layer over the user's chat channels (the MUDBall model).

Where :class:`~genericmud.review.cursor.ReviewCursor` walks the whole scrollback,
this cycles between the *channels* triggers have routed lines to (tells, chat,
gossip, ...) and scrolls within one channel's own history. Channels are
discovered dynamically — whatever non-main channels appear on buffered lines,
plus any the caller knows about from policies — so a soundpack's custom
channels browse the same as built-in ones. Pure and synchronous, like the
review cursor; the caller voices whatever comes back.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from genericmud.model.buffer import Buffer

MAIN_CHANNEL = "main"
# Plumbing channels that aren't conversation history; browsing them would be noise.
_EXCLUDED_CHANNELS = frozenset({MAIN_CHANNEL, "system", "review"})


class ChannelReview:
    """Cycle across channels and scroll within the current one.

    Position within a channel is an offset from its newest line (0 = newest), so
    it stays put as new lines arrive rather than sliding with the buffer.
    """

    def __init__(
        self, buffer: Buffer, known: Callable[[], Iterable[str]] | None = None
    ) -> None:
        self._buffer = buffer
        self._known = known or (lambda: ())
        self._channel: str | None = None
        self._offset = 0  # lines back from the channel's newest
        self._word = 0

    def channels(self) -> list[str]:
        """Browsable channels: seen on buffered lines or declared by policy, sorted."""
        seen = {line.channel for line in self._buffer.lines()}
        seen.update(self._known())
        return sorted(name for name in seen if name and name not in _EXCLUDED_CHANNELS)

    def next_channel(self) -> str:
        return self._step_channel(1)

    def prev_channel(self) -> str:
        return self._step_channel(-1)

    def older(self) -> str:
        """The previous (older) line in the current channel."""
        return self._scroll(1)

    def newer(self) -> str:
        """The next (newer) line in the current channel."""
        return self._scroll(-1)

    def recent(self, n: int) -> str:
        """The n-th newest line (1 = newest) of the current channel."""
        lines = self._lines()
        if self._channel is None:
            return "no channel selected"
        if not (1 <= n <= len(lines)):
            return "no message"
        self._offset = n - 1
        self._word = 0
        return lines[-n].plain_text

    def next_word(self) -> str:
        self._word += 1
        return self._current_word()

    def prev_word(self) -> str:
        self._word -= 1
        return self._current_word()

    def _step_channel(self, step: int) -> str:
        names = self.channels()
        if not names:
            self._channel = None
            return "no channels"
        if self._channel in names:
            index = (names.index(self._channel) + step) % len(names)
        else:
            index = 0 if step > 0 else len(names) - 1
        self._channel = names[index]
        self._offset = 0
        self._word = 0
        newest = self._lines()
        latest = newest[-1].plain_text if newest else "no messages"
        return f"{self._channel}: {latest}"

    def _scroll(self, step: int) -> str:
        if self._channel is None:
            return self._step_channel(1)  # entering the layer lands on the first channel
        lines = self._lines()
        if not lines:
            return "no messages"
        self._offset = max(0, min(self._offset + step, len(lines) - 1))
        self._word = 0
        return lines[len(lines) - 1 - self._offset].plain_text

    def _current_line(self) -> str:
        lines = self._lines()
        if not lines:
            return ""
        self._offset = max(0, min(self._offset, len(lines) - 1))
        return lines[len(lines) - 1 - self._offset].plain_text

    def _current_word(self) -> str:
        words = self._current_line().split()
        if not words:
            return ""
        self._word = max(0, min(self._word, len(words) - 1))
        return words[self._word]

    def _lines(self) -> list:
        if self._channel is None:
            return []
        return [line for line in self._buffer.lines() if line.channel == self._channel]
