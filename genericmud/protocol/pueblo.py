"""Reading Pueblo's HTML as the text a player would hear.

Pueblo has no telnet option: the whole exchange is ordinary text on the wire. A world that
supports it says so in its greeting, the client answers with a ``PUEBLOCLIENT`` line, and the
world then switches its output to HTML by sending ``<img xch_mode=purehtml>`` (PennMUSH's
wording; older worlds say ``html``). The same tag with ``text`` switches back.

Reading happens in two halves. Tags are dropped rather than spoken, entities become the
characters they stand for, and ``<br>`` becomes the line break the world meant, because Pueblo
sends a whole paragraph as one line with its breaks inside it. What a tag *looks* like --
colour, boldness, a picture -- is nothing a voice can carry, so none of it is spoken.

The second half is the one thing a voice can carry: ``<a xch_cmd="north">north</a>`` is a
command the world is offering to run, and the link keys this client already has (for MXP) work
on it unchanged. Sounds and images are still to come; until then they are dropped like any
other tag, which is the honest default: a picture nobody can see is not a word.

The announcement is real text at a login screen, where a world that does not speak Pueblo
would read ``PUEBLOCLIENT ...`` as a character name. That is why this module neither sends
anything nor decides to: the caller sends it only for a world the player switched it on for.

One thing Pueblo cannot do that MXP can: it has no secure line, so the world's markup and a
player's own words arrive by the same route. A world that escapes what players type (``&lt;``)
is safe; one that does not can put a tag, and with it a followable command, in front of a
reader. Nothing here can tell those apart -- that is the protocol, not this parser -- so a
world's escaping is worth knowing about before trusting its links.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from genericmud.model.buffer import Link

CLIENT_VERSION = "2.50"
# What the client announces. The checksum the spec allows is left out: a world may keep it to
# limit what a known client is trusted to do, and nothing here is guarded by it.
CLIENT_LINE = f"PUEBLOCLIENT {CLIENT_VERSION}"

# Which page mode the world wants, in the attribute it switches with, and only inside an <img>
# tag: that is the only place Pueblo defines it. Read anywhere in a line, a player who typed
# "xch_mode=html" at the MUD would switch every reader's parser -- Pueblo has no secure line to
# tell the world's markup from someone's words, so the tag is what has to be insisted on.
#
# Only the page modes count. Pueblo puts other things in xch_mode too -- xch_mode="sound" on an
# <img> is a sound to play, not a switch -- and reading those as a switch turned a sound tag into
# a mode change back to plain text.
_PAGE_MODES = {"html": True, "purehtml": True, "text": False, "plain": False}
_MODE = re.compile(r"<img\b[^>]*\bxch_mode\s*=\s*[\"']?(html|purehtml|text|plain)\b", re.I)
_MODE_TAG = re.compile(
    r"<img\b[^>]*\bxch_mode\s*=\s*[\"']?(?:html|purehtml|text|plain)\b[^>]*>", re.I
)
# A picture, or with xch_mode="sound" something to play: what a later stage would carry, and for
# now only noted, so that "does this MUD use them at all?" is a fact from a log.
_IMAGE = re.compile(r"<img\b([^>]*)>", re.IGNORECASE)
# Where Pueblo puts a line break. <p> and <hr> end a line as well: both mean "new line here",
# and reading them as one is what stops a paragraph arriving as a wall of text.
_BREAKS = re.compile(r"<br\s*/?>|</p\s*>|<p\b[^>]*>|<hr\s*/?>", re.IGNORECASE)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
# Fenced off entirely: a script aimed at a browser is code, and reading it aloud is noise.
_SILENT = re.compile(r"<(script|style|title)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]*>")
_ENTITY = re.compile(r"&(#[xX]?[0-9a-fA-F]+|[a-zA-Z]+);")
# A command link, as Pueblo writes it, with the older href form some worlds used instead:
# <a xch_cmd="north">north</a> and <a href="xch_cmd:north">north</a>. An unclosed one runs to
# the end of its line rather than being thrown away, which is what a browser would do.
_LINK = re.compile(r"<a\b([^>]*)>(.*?)(?=</a\s*>|$)", re.IGNORECASE | re.DOTALL)
_ATTRIBUTE = re.compile(r"\b([a-zA-Z_:-]+)\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s>]+))")
_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}


@dataclass(frozen=True)
class PuebloLine:
    """One line of a Pueblo world's HTML, read: the words, and the commands it offers.

    ``links`` are the same :class:`~genericmud.model.buffer.Link` objects MXP produces, so the
    review cursor's link keys, the follow key and the mouse all work without knowing which
    markup a world speaks.
    """

    text: str
    links: tuple[Link, ...] = ()


class PuebloParser:
    """One world's Pueblo stream: a line from the MUD in, the lines to show and speak out.

    Per connection, and starting inactive. A world that never switches to HTML never
    activates, so its output is passed through exactly as it arrived -- which is what keeps a
    MUD that merely *mentions* Pueblo in its greeting from having its text read as markup.
    """

    def __init__(self) -> None:
        self.active = False
        # The first picture or sound the world sent, or None. Kept for the diagnostics only:
        # nothing here shows or plays one, but "my MUD's pictures are missing" and "my MUD sends
        # no pictures" have very different answers, and this is what tells them apart.
        self.first_image: str | None = None

    def feed(self, line: str) -> list[PuebloLine]:
        """Read one line from the world; return the lines it should appear as.

        One line in can be several out: the world breaks lines with ``<br>``, not with
        newlines. A line that only carried the mode switch says nothing, so it comes back as
        no lines at all -- the reader should not hear the client's own handshake spoken.
        """
        mode = _MODE.search(line)
        if mode is not None:
            self.active = _PAGE_MODES[mode.group(1).lower()]
            line = _MODE_TAG.sub("", line)  # the switch itself is never spoken
            if not self.active:
                # Switched back to plain text: whatever else is on this line is the world's own
                # words, angle brackets and all, and none of it is markup to strip.
                return [PuebloLine(line)]
        elif not self.active:
            return [PuebloLine(line)]
        body = _SILENT.sub("", _COMMENTS.sub("", line))
        if self.first_image is None:
            found = _IMAGE.search(body)
            source = _attribute(found.group(1), "src").strip() if found is not None else ""
            if source:
                self.first_image = source
        pieces = [_read_piece(piece) for piece in _BREAKS.split(body)]
        while pieces and not pieces[-1].text.strip():
            pieces.pop()  # a break at the end is not a blank line of its own, and a mode
            # switch on its own leaves nothing to say at all
        return pieces


def _read_piece(piece: str) -> PuebloLine:
    """One piece of HTML as words plus the commands it offers."""
    links: list[Link] = []
    for match in _LINK.finditer(piece):
        attributes, inner = match.group(1), match.group(2)
        command = _attribute(attributes, "xch_cmd") or _command_from_href(
            _attribute(attributes, "href")
        )
        words = _plain(inner).strip()
        # A link with no command does nothing, and one with no words is nothing to stand on.
        if not command.strip() or not words:
            continue
        hint = _attribute(attributes, "xch_hint") or _attribute(attributes, "title")
        links.append(Link(words, command, hint=hint))
    return PuebloLine(_plain(piece), tuple(links))


def _command_from_href(href: str) -> str:
    """The command in ``href="xch_cmd:north"``, or "" for a link to somewhere else.

    A web address is not a command: following it by typing it into the MUD would be worse than
    not offering it at all, so a plain href is left as the words it shows.
    """
    prefix = "xch_cmd:"
    return href[len(prefix):] if href[: len(prefix)].lower() == prefix else ""


def _attribute(attributes: str, name: str) -> str:
    """One attribute's value, unquoted and with entities read, or "" when it is not there."""
    for match in _ATTRIBUTE.finditer(attributes):
        if match.group(1).lower() != name.lower():
            continue
        value = match.group(3) or match.group(4) or match.group(5) or ""
        return _read_entities(value)
    return ""


def _plain(piece: str) -> str:
    """The words of one piece of HTML: tags gone, then the characters entities stand for.

    Tags first, entities second. Decoding first would turn an entity standing for a ``<``
    into a real tag opener, and everything after it would be dropped as markup.
    """
    return _read_entities(_TAG.sub("", piece))


def _read_entities(text: str) -> str:
    """The characters behind ``&lt;``, ``&amp;``, ``&nbsp;`` and their numbered forms.

    A named entity this does not know is left as it stands: the world wrote something, and the
    reader is owed it rather than a silent gap. Pueblo uses the small HTML set.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if not name.startswith("#"):
            return _ENTITIES.get(name.lower(), match.group(0))
        hexadecimal = name[:2].lower() == "#x"
        try:
            return chr(int(name[2:] if hexadecimal else name[1:], 16 if hexadecimal else 10))
        except (ValueError, OverflowError):
            return match.group(0)

    return _ENTITY.sub(replace, text)
