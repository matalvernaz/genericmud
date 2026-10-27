"""MXP (MUD eXtension Protocol) line parsing.

MXP is telnet option 91. Servers that offer it send ``IAC WILL MXP`` and then mark
their lines with a line-mode escape, ``ESC [ N z``, which says how the rest of the
line is to be read:

====  ==================================================================
0     open line: only the open tags below may appear
1     secure line: every tag may appear (until the newline)
2     locked line: no tags at all; the text is verbatim
3     reset: close everything, back to open
4     the next tag only is secure
5-7   lock open, secure or locked as the new default
====  ==================================================================

The distinction is a security one, not a formatting one: players can get text into
open lines (chat, channels, their own names), so a client must never act on a
secure tag that arrives on an open line -- a player could otherwise make another
player's client play sounds, follow links or ask for their password.

Two rules keep the text trustworthy, and they are the whole point of this module
given that servers only see a benefit once nothing leaks:

* every tag is removed from the text, whether or not this client acts on it, and
* an unclosed tag is forgotten at the newline (open tags) or on a reset (all).

What this client acts on: line breaks (``<BR>``, ``<SBR>``, ``<P>``, ``<NOBR>``),
``<SOUND>``/``<MUSIC>`` cues, and the ``<VERSION>``/``<SUPPORT>`` handshake.
Everything else is parsed, stripped and ignored -- including ``<SEND>``/``<A>``
links, which are recorded so a later window can make them reachable without a
mouse, but not acted on here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from genericmud.protocol.msp import SoundCue, _parse_cue

# The line-mode escape. Servers send it as decimal text, and it can appear more than
# once on a line (lock a mode, then mark this line).
_LINE_MODE_RE = re.compile(r"\x1b\[(?P<mode>\d{1,2})z")

# A tag: <NAME ...>, </NAME>, <!ELEMENT ...>. Attributes may be quoted and may contain
# ">" inside quotes, which is why this is not a plain [^>]* match.
_TAG_RE = re.compile(
    r"<(?P<closing>/?)"
    r"(?P<name>[A-Za-z!][A-Za-z0-9!]*)"
    r"(?P<attrs>(?:[^<>\"']|\"[^\"]*\"|'[^']*')*)"
    r">"
)
_ENTITY_RE = re.compile(r"&(?P<name>[A-Za-z][A-Za-z0-9]*|#\d+);")

# Open tags are the only ones allowed on an open line (spec: "Only the tags described in
# this section are OPEN tags" -- text formatting and line spacing).
_OPEN_TAGS = frozenset(
    {
        "b", "bold", "strong", "i", "italic", "em", "u", "underline", "s", "strikeout",
        "c", "color", "h", "high", "font", "nobr", "p", "br", "sbr",
    }
)

# Every tag this client knows by name. A construct that is NOT in here is only treated as
# markup on a secure line, where the MUD has promised the line is tagged; on an open line it
# is left as text, because that is where a player's own words come from ("say I <build>
# things") and deleting them would be worse than showing a stray tag.
_KNOWN_TAGS = _OPEN_TAGS | frozenset(
    {
        "send", "a", "expire", "version", "support", "sound", "music", "gauge", "stat",
        "frame", "frameset", "dest", "destination", "user", "password", "image", "filter",
        "relocate", "script", "tag", "var", "welcome", "h1", "h2", "h3", "h4", "h5", "h6",
        "hr", "small", "tt",
    }
)
# The named entities MXP inherits from HTML. Anything else is left alone: the server may
# have defined it with <!ENTITY>, and inventing a value would be worse than showing it.
_ENTITIES = {
    "lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'", "nbsp": " ",
}

CLIENT_NAME = "genericMud"
MXP_VERSION = "1.0"

MODE_OPEN = "open"
MODE_SECURE = "secure"
MODE_LOCKED = "locked"


@dataclass
class MxpOutcome:
    """What one fed line produced."""

    text: str
    cues: list[SoundCue] = field(default_factory=list)
    # Secure-line payloads to send back to the MUD, without the escape or the newline;
    # the caller wraps them (see :meth:`MxpParser.secure_line`).
    replies: list[str] = field(default_factory=list)
    # (displayed text, command) for every link seen, for a window that can reach them.
    links: list[tuple[str, str]] = field(default_factory=list)
    # <NOBR>: the caller must join the next line onto this one instead of breaking.
    no_break: bool = False
    # The same instruction, plus an open <P>: either way the next line belongs on this one.
    join_next: bool = False


class MxpParser:
    """Turns tagged MXP lines into plain text plus the few things worth acting on."""

    def __init__(self, *, client_version: str = "") -> None:
        self._default_mode = MODE_OPEN  # what a line is read as when it says nothing
        self._mode = MODE_OPEN  # this line's mode, set by an escape on it
        self._temp_secure = False  # mode 4: the next tag only
        self._paragraph = False  # between <P> and </P> newlines are not breaks
        self._nobr = False  # <NOBR> swallows the next newline
        self._client_version = client_version

    # --- modes ---------------------------------------------------------------

    def _apply_modes(self, line: str) -> str:
        """Consume the line-mode escapes at the head of ``line`` and set the mode."""
        self._mode = self._default_mode
        self._temp_secure = False
        while True:
            match = _LINE_MODE_RE.match(line)
            if match is None:
                return line
            code = int(match.group("mode"))
            line = line[match.end():]
            if code == 0:
                self._mode = MODE_OPEN
            elif code == 1:
                self._mode = MODE_SECURE
            elif code == 2:
                self._mode = MODE_LOCKED
            elif code == 3:  # reset: everything closed, back to open
                self._mode = MODE_OPEN
                self._default_mode = MODE_OPEN
                self._paragraph = False
                self._nobr = False
            elif code == 4:
                self._temp_secure = True
            elif code in (5, 6, 7):
                self._default_mode = {5: MODE_OPEN, 6: MODE_SECURE, 7: MODE_LOCKED}[code]
                self._mode = self._default_mode
            # 10-99 are automapper/user-defined line tags: nothing to do here, but they
            # must not reach the text either, which the loop above already ensures.

    # --- one line ------------------------------------------------------------

    def feed(self, line: str) -> MxpOutcome:
        """Parse one line from the MUD. ``line`` has no newline; the caller joins."""
        line = self._apply_modes(line)
        if self._mode == MODE_LOCKED:
            return self._finish(line, [], [], [])

        cues: list[SoundCue] = []
        replies: list[str] = []
        links: list[tuple[str, str]] = []
        # Tags first, entities second. &lt; exists precisely to write a literal "<", so
        # expanding entities before parsing tags would turn it into one and eat it.
        text = self._parse_tags(line, cues, replies, links)
        text = self._expand_entities(text)
        return self._finish(text, cues, replies, links)

    def _finish(
        self,
        text: str,
        cues: list[SoundCue],
        replies: list[str],
        links: list[tuple[str, str]],
    ) -> MxpOutcome:
        """Apply the one spacing rule that outlives a line.

        The paragraph state does *not* reset per line -- that is the whole point of
        ``<P>``, which joins the newlines of several lines into one -- so it is cleared
        only by ``</P>`` or a reset.
        """
        no_break = self._nobr
        self._nobr = False
        return MxpOutcome(
            text=text,
            cues=cues,
            replies=replies,
            links=links,
            no_break=no_break,
            # <NOBR> is one line's instruction; an open <P> lasts until </P>.
            join_next=no_break or self._paragraph,
        )

    # --- the pieces ----------------------------------------------------------

    def _expand_entities(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            if name.startswith("#"):
                code = int(name[1:])
                return chr(code) if 32 <= code < 0x110000 else ""
            return _ENTITIES.get(name.lower(), match.group(0))

        return _ENTITY_RE.sub(replace, text)

    def _parse_tags(
        self,
        text: str,
        cues: list[SoundCue],
        replies: list[str],
        links: list[tuple[str, str]],
    ) -> str:
        """Remove every tag, acting on the few that mean something here."""
        out: list[str] = []
        position = 0
        # The text of a <SEND>/<A> link, so it can be paired with its command.
        pending_link: tuple[int, str] | None = None
        for match in _TAG_RE.finditer(text):
            name = match.group("name").lower()
            closing = bool(match.group("closing"))
            secure_ok = self._mode == MODE_SECURE or self._temp_secure
            self._temp_secure = False
            if (
                self._mode != MODE_SECURE
                and name not in _KNOWN_TAGS
                and name != "!"
            ):
                # Not markup this client knows, on a line where the MUD only promised open
                # tags. That is most likely a player's own words ("say I <build> things"),
                # so it stays as text rather than being quietly deleted.
                continue
            out.append(text[position:match.start()])
            position = match.end()
            if name == "!":
                continue  # <!ELEMENT>, <!ENTITY>, <!ATTLIST>: definitions, not output
            if name in _OPEN_TAGS or secure_ok:
                self._act(name, match.group("attrs"), closing, cues, replies, out, links)
            # A secure tag on an open line is dropped without action, exactly as the
            # security rule requires; its text stays, which is the safe half.
            if name in ("send", "a") and not closing and secure_ok:
                pending_link = (len(out), _link_command(match.group("attrs")))
            elif name in ("send", "a") and closing and pending_link is not None:
                index, command = pending_link
                if command:
                    links.append(("".join(out[index:]), command))
                pending_link = None
        out.append(text[position:])
        return "".join(out)

    def _act(
        self,
        name: str,
        attrs: str,
        closing: bool,
        cues: list[SoundCue],
        replies: list[str],
        out: list[str],
        links: list[tuple[str, str]],
    ) -> None:
        """Do the handful of things this client acts on."""
        if name in ("br", "sbr"):
            out.append("\n")
        elif name == "p":
            self._paragraph = not closing
        elif name == "nobr":
            self._nobr = True
        elif name in ("sound", "music") and not closing:
            cue = _sound_cue(name, attrs)
            if cue is not None:
                cues.append(cue)
        elif name == "version" and not closing:
            replies.append(self._version_reply(attrs))
        elif name == "support" and not closing:
            replies.append(_support_reply(attrs))

    def _version_reply(self, attrs: str) -> str:
        # The MUD may send <VERSION styleversion>; the client echoes the style back.
        style = attrs.strip().split()[0] if attrs.strip() else ""
        parts = [f"MXP={MXP_VERSION}"]
        if style:
            parts.append(f"STYLE={style}")
        parts.append(f"CLIENT={CLIENT_NAME}")
        if self._client_version:
            parts.append(f"VERSION={self._client_version}")
        return "<VERSION " + " ".join(parts) + ">"

    # --- what the caller sends back ------------------------------------------

    @staticmethod
    def secure_line(payload: str) -> str:
        """Wrap a reply so the MUD can tell it came from the client, not the player."""
        return "\x1b[1z" + payload + "\n"


def _support_reply(attrs: str) -> str:
    """Answer <SUPPORT>: what this client handles, and a "-" for what it doesn't.

    Only the tags this client does something with are advertised. Claiming support for
    the rest would invite servers to send what is then thrown away.
    """
    # "send"/"a" are here because the client can now follow a link, not because it ignores
    # one: advertising a tag it did nothing with would invite servers to send it for nothing.
    supported = {
        "br", "sbr", "p", "nobr", "sound", "music", "version", "support", "send", "a",
    }
    asked = attrs.replace("*", "").lower().split()
    if not asked:
        return "<SUPPORTS " + " ".join(f"+{name.upper()}" for name in sorted(supported)) + ">"
    answers = []
    for request in asked:
        base = request.split(".")[0]
        answers.append(("+" if base in supported else "-") + request.upper())
    return "<SUPPORTS " + " ".join(answers) + ">"


def _link_command(attrs: str) -> str:
    """The command a <SEND>/<A> link carries, from HREF= or the first bare argument."""
    href = re.search(
        r"HREF\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", attrs, re.IGNORECASE
    )
    if href is not None:
        return next((group for group in href.groups() if group is not None), "")
    tokens = _tokens(attrs.strip())
    return _unquote(tokens[0]) if tokens else ""


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _tokens(attrs: str) -> list[str]:
    """Split a tag's attributes into bare and K=V tokens, respecting quotes."""
    out: list[str] = []
    index = 0
    length = len(attrs)
    while index < length:
        char = attrs[index]
        if char.isspace():
            index += 1
            continue
        if char in "\"'":
            end = attrs.find(char, index + 1)
            end = length if end < 0 else end
            out.append(attrs[index:end + 1])
            index = end + 1
            continue
        end = index
        while end < length and not attrs[end].isspace():
            end += 1
        out.append(attrs[index:end])
        index = end
    return out


def _sound_cue(kind: str, attrs: str) -> SoundCue | None:
    """<SOUND>/<MUSIC> use MSP's own attributes, so MSP's parser reads them.

    MXP allows them by position as well as named (``<SOUND "oof.wav" 50 2>``), and MSP's
    parser wants the file name bare and first, so a ``FName=`` is unwrapped before it is
    handed over.
    """
    tokens = _tokens(attrs)
    if not tokens:
        return None
    first = tokens[0]
    if "=" in first:
        key, value = first.split("=", 1)
        if key.upper() != "FNAME":
            return None
        name = _unquote(value)
    else:
        name = _unquote(first)
    if not name:
        return None
    positional = ("V", "L", "P", "T", "U")
    parts = [name]
    position = 0
    for token in tokens[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            parts.append(f"{key.upper()}={_unquote(value)}")
        elif position < len(positional):
            parts.append(f"{positional[position]}={_unquote(token)}")
            position += 1
    return _parse_cue("music" if kind == "music" else "sound", " ".join(parts))
