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
``<SOUND>``/``<MUSIC>`` cues, ``<IMAGE>``/``<IMG>`` (recorded so a window can show one, and a
text client can at least offer it as a link), and the ``<VERSION>``/``<SUPPORT>`` handshake.
Everything else is parsed, stripped and ignored -- including ``<SEND>``/``<A>`` links, which
are recorded so a later window can make them reachable without a mouse, but not acted on
here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

from genericmud.model.buffer import Link
from genericmud.protocol.msp import SoundCue, _parse_cue
from genericmud.safepath import is_allowed_host, is_unsafe

# Where an inline image sits in the text. The parser cannot report a character offset (the ANSI,
# MSP and redaction passes that follow all rewrite the text), so an image leaves this marker
# behind and the engine turns it into an offset once the line is finished. The marker is never
# spoken, logged or shown: it is stripped out before the line exists.
IMAGE_MARK = "\uf8ff"

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
# A numeric entity has at most seven digits (U+10FFFF is 1114111): anything longer is not a
# character, and int() on thousands of digits raises out of the whole line.
_ENTITY_RE = re.compile(r"&(?P<name>[A-Za-z][A-Za-z0-9]{0,31}|#\d{1,7});")

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
        "frame", "frameset", "dest", "destination", "user", "password", "image", "img",
        "filter",
        "relocate", "script", "tag", "var", "welcome", "h1", "h2", "h3", "h4", "h5", "h6",
        "hr", "small", "tt",
    }
)
# The named entities MXP inherits from HTML. Anything else is left alone: the server may
# have defined it with <!ENTITY>, and inventing a value would be worse than showing it.
_ENTITIES = {
    "lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'", "nbsp": " ",
}

# How many commands a <SEND> menu may offer. A MUD could otherwise hand a player a list
# nobody could read out, and each entry is another line in a dialog.
_MAX_MENU_ITEMS = 20

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
    # One :class:`Link` for each link seen, for a window that can reach them.
    links: list[Link] = field(default_factory=list)
    # (file name, base URL) for each inline image, in the order they appeared, with an
    # IMAGE_MARK left in the text where each one goes. Resolving a file name to a real
    # address needs the world's own setting, so that is the caller's job, not the
    # parser's.
    images: list[tuple[str, str]] = field(default_factory=list)
    # <USER>/<PASSWORD>: the MUD asked for the login over MXP. Only ever set on a secure
    # line, because an open line is where players' own words arrive.
    login: list[str] = field(default_factory=list)
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
        # A <P> opened on an open line, where players can write, ends with that line as every
        # open tag does at the newline. Otherwise "<p>" in one chat line would hold everyone's
        # output until the server happened to send "</p>".
        self._paragraph_ends_with_line = False
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
            line = line[match.end():]
            self._apply_mode(int(match.group("mode")))

    def _apply_mode(self, code: int) -> None:
        """Act on one line-mode escape."""
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
        # must not reach the text either, which the callers already ensure.

    # --- one line ------------------------------------------------------------

    def feed(self, line: str) -> MxpOutcome:
        """Parse one line from the MUD. ``line`` has no newline; the caller joins."""
        # The image marker is this client's own, so one arriving in the text is somebody's
        # attempt to move where a real picture is drawn.
        line = self._apply_modes(line.replace(IMAGE_MARK, ""))
        cues: list[SoundCue] = []
        replies: list[str] = []
        links: list[Link] = []
        images: list[tuple[str, str]] = []
        login: list[str] = []
        # A server can mark its own text secure and then drop to open (or locked) partway
        # through a line, before a player's words. So the line is read in pieces, each in the
        # mode in force where it starts. Partway through, a line may only become less trusted:
        # escapes that would make what follows secure are ignored there, since a player who
        # got an escape into their own words could otherwise make them secure.
        pieces: list[str] = []
        position = 0
        for match in _LINE_MODE_RE.finditer(line):
            piece = line[position:match.start()]
            pieces.append(self._parse_piece(piece, cues, replies, links, images, login))
            code = int(match.group("mode"))
            if code in (0, 2, 3):
                self._apply_mode(code)
            position = match.end()
        pieces.append(self._parse_piece(line[position:], cues, replies, links, images, login))
        return self._finish("".join(pieces), cues, replies, links, images, login)

    def _parse_piece(
        self,
        text: str,
        cues: list[SoundCue],
        replies: list[str],
        links: list[Link],
        images: list[tuple[str, str]],
        login: list[str],
    ) -> str:
        """Read one stretch of a line in the current mode."""
        if self._mode == MODE_LOCKED:
            return text
        # Tags first, entities second. &lt; exists precisely to write a literal "<", so
        # expanding entities before parsing tags would turn it into one and eat it.
        text = self._parse_tags(text, cues, replies, links, images, login)
        return self._expand_entities(text)

    def _finish(
        self,
        text: str,
        cues: list[SoundCue],
        replies: list[str],
        links: list[Link],
        images: list[tuple[str, str]],
        login: list[str],
    ) -> MxpOutcome:
        """Apply the one spacing rule that outlives a line.

        The paragraph state does *not* reset per line -- that is the whole point of
        ``<P>``, which joins the newlines of several lines into one -- so it is cleared
        only by ``</P>`` or a reset.
        """
        no_break = self._nobr
        self._nobr = False
        if self._paragraph and self._paragraph_ends_with_line:
            self._paragraph = False
        self._paragraph_ends_with_line = False
        return MxpOutcome(
            text=text,
            cues=cues,
            replies=replies,
            links=links,
            images=images,
            login=login,
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
                return chr(code) if _printable_code_point(code) else ""
            return _ENTITIES.get(name.lower(), match.group(0))

        return _ENTITY_RE.sub(replace, text)

    def _parse_tags(
        self,
        text: str,
        cues: list[SoundCue],
        replies: list[str],
        links: list[Link],
        images: list[tuple[str, str]],
        login: list[str],
    ) -> str:
        """Remove every tag, acting on the few that mean something here."""
        out: list[str] = []
        position = 0
        # The text of a <SEND>/<A> link, so it can be paired with its command and with
        # whether following it sends that command or types it.
        pending_link: tuple[int, Link] | None = None
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
                self._act(
                    name, match.group("attrs"), closing, cues, replies, out, links, images,
                    login, secure=secure_ok,
                )
            # A secure tag on an open line is dropped without action, exactly as the
            # security rule requires; its text stays, which is the safe half.
            if name in ("send", "a") and not closing and secure_ok:
                pending_link = (len(out), _link(match.group("attrs")))
            elif name in ("send", "a") and closing and pending_link is not None:
                index, link = pending_link
                if link.command:
                    # An image inside a link leaves a marker where it sits; that is not words,
                    # so it must not become the link's name.
                    words = "".join(out[index:]).replace(IMAGE_MARK, "")
                    links.append(replace(link, text=words))
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
        links: list[Link],
        images: list[tuple[str, str]],
        login: list[str],
        *,
        secure: bool = False,
    ) -> None:
        """Do the handful of things this client acts on."""
        if name in ("br", "sbr"):
            out.append("\n")
        elif name == "p":
            self._paragraph = not closing
            self._paragraph_ends_with_line = not closing and not secure
        elif name == "nobr":
            self._nobr = True
        elif name in ("sound", "music") and not closing:
            cue = _sound_cue(name, attrs)
            if cue is not None:
                cues.append(cue)
        elif name in ("image", "img") and not closing:
            # Reachable only on a secure line: an image is exactly the "large graphics" the
            # spec says a player must not be able to force onto another player's client.
            image = _image_attrs(attrs)
            if image is not None:
                images.append(image)
                out.append(IMAGE_MARK)  # where it sits; the engine turns this into an offset
        elif name == "version" and not closing:
            replies.append(self._version_reply(attrs))
        elif name == "support" and not closing:
            replies.append(_support_reply(attrs))
        elif name in ("user", "password") and not closing:
            # The MUD is asking for the login. Only reachable on a secure line, because
            # every branch of _parse_tags runs _act for a secure tag or an open one, and
            # these are not open tags.
            login.append(name)

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


def _printable_code_point(code: int) -> bool:
    """Whether a numeric entity may become text: not a control character (C0, DEL or the C1
    range, which includes a one-byte ANSI escape), not half of a surrogate pair (which can't
    be written to a log), and not this client's own image marker."""
    if code < 32 or 0x7F <= code <= 0x9F:
        return False
    if 0xD800 <= code <= 0xDFFF:
        return False
    return code < 0x110000 and chr(code) != IMAGE_MARK


def _support_reply(attrs: str) -> str:
    """Answer <SUPPORT>: what this client handles, and a "-" for what it doesn't.

    Only the tags this client does something with are advertised. Claiming support for
    the rest would invite servers to send what is then thrown away.
    """
    # "send"/"a" are here because the client can now follow a link, not because it ignores
    # one: advertising a tag it did nothing with would invite servers to send it for nothing.
    supported = {
        "br", "sbr", "p", "nobr", "sound", "music", "version", "support", "send", "a",
        "image",
    }
    asked = attrs.replace("*", "").lower().split()
    if not asked:
        return "<SUPPORTS " + " ".join(f"+{name.upper()}" for name in sorted(supported)) + ">"
    answers = []
    for request in asked:
        base = request.split(".")[0]
        answers.append(("+" if base in supported else "-") + request.upper())
    return "<SUPPORTS " + " ".join(answers) + ">"


def _link(attrs: str) -> Link:
    """What a ``<SEND>``/``<A>`` tag asks for, before its words are known.

    The command is ``HREF=`` or the first bare argument. A bare ``PROMPT`` argument, which the
    spec puts after the command, asks for the text to land in the player's own command line
    instead of going to the MUD -- so a link can offer to start a command without finishing
    it, and the player completes or abandons it.

    A command with ``|`` in it is a menu of several, and ``HINT=`` captions them (see
    :func:`_menu_captions`). A menu lets a MUD offer a choice without spending a line on every
    option, and following one asks which rather than assuming the first.
    """
    tokens = _tokens(attrs.strip())
    href = re.search(
        r"HREF\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", attrs, re.IGNORECASE
    )
    command = ""
    if href is not None:
        command = next((group for group in href.groups() if group is not None), "")
    else:
        for token in tokens:
            if "=" in token and token[0] not in "\"'":
                continue  # EXPIRE= and any other named argument comes before or after
            command = _unquote(token)
            break
    captions = _attribute(attrs, "HINT").split("|")
    entries = command.split("|")
    parts = [entry for entry in entries if entry.strip()]
    choices: tuple[tuple[str, str], ...] = ()
    own_caption = captions[0]
    if len(parts) > 1:
        own_caption, entry_captions = _menu_captions(captions, len(entries))
        # Captions pair with entries by the position the MUD wrote them in, so an empty entry
        # (a separator in a mouse menu) takes its caption with it instead of shifting every
        # later one onto the wrong command. Entries are trimmed: the separators are what
        # matter, not the spacing round them. A command with no caption is offered as itself.
        pairs = [
            (entry.strip(), entry_captions[index] if index < len(entry_captions) else "")
            for index, entry in enumerate(entries)
            if entry.strip()
        ]
        choices = tuple(pairs[:_MAX_MENU_ITEMS])
        single = choices[0][0]
    else:
        # One command, kept exactly as the MUD wrote it: PROMPT's trailing space is the whole
        # point of that tag, and trimming here would glue the player's next word onto it.
        single = command if parts else ""
    return Link(
        "",
        single,
        # A menu is not a typed command: PROMPT belongs to the one command it describes.
        False if choices else _has_prompt_flag(attrs, command),
        own_caption,
        choices,
    )


def _menu_captions(captions: list[str], entries: int) -> tuple[str, list[str]]:
    """Split a menu's ``HINT=`` into the link's own caption and one caption per entry.

    Read the way Mudlet reads it (TTextEdit's hint offset): one more caption than entries
    means the first is the link's own and the rest go with the entries in order; otherwise
    they pair with the entries one to one and the link has none of its own. A single caption
    over a menu is the link's own, and its commands are offered as themselves.
    """
    if len(captions) > entries:
        return captions[0], captions[1:]
    if len(captions) == 1:
        return captions[0], []
    return "", captions


def _attribute(attrs: str, name: str) -> str:
    """One named argument's value, quoted or bare, or "" when the tag hasn't got it."""
    found = re.search(
        name + r"\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", attrs, re.IGNORECASE
    )
    if found is None:
        return ""
    return next((group for group in found.groups() if group is not None), "")


# A named argument with its value, quoted or bare: HINT="Buy it", EXPIRE=shop.
_NAMED_ARGUMENT = re.compile(r"""[A-Za-z_][\w-]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""")


def _has_prompt_flag(attrs: str, command: str) -> bool:
    """Whether a ``<SEND>`` asks for its command to be typed rather than sent.

    Only a bare keyword counts: not a quoted argument (the command itself is usually quoted,
    and a command that happens to read "tell prompt " is a command), and not the value of a
    named one (``hint=prompt`` is a caption). A tag whose whole command is the word "prompt"
    is sending that word, not asking for the flag.
    """
    outside = _NAMED_ARGUMENT.sub(" ", attrs)
    outside = re.sub(r"\"[^\"]*\"|'[^']*'", " ", outside)
    found = re.findall(r"(?<![\w-])prompt(?![\w-])", outside, re.IGNORECASE)
    if not found:
        return False
    return not (command.lower() == "prompt" and len(found) == 1)


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


def _image_attrs(attrs: str) -> tuple[str, str] | None:
    """(file name, base URL) from an ``<IMAGE>``/``<IMG>`` tag, or None if it names no file.

    The spec's own example is ``<IMAGE fname="mypic.gif" url="http://myserver.com/images/">``,
    and arguments may be named or positional in either case, so both are accepted. Every other
    argument (size, alignment, ISMAP) is visual and is not kept: an image map's coordinates are
    something a keyboard can't produce, and the image itself is offered as a link instead.
    """
    named: dict[str, str] = {}
    positional: list[str] = []
    for token in _tokens(attrs):
        if "=" in token and token[0] not in "\"'":
            key, value = token.split("=", 1)
            named[key.upper()] = _unquote(value)
        else:
            positional.append(_unquote(token))
    name = named.get("FNAME") or (positional[0] if positional else "")
    if not name:
        return None
    base = named.get("URL") or (positional[1] if len(positional) > 1 else "")
    return (name, base)


def resolve_image_url(file: str, base: str, fallback: str, trusted: str = "") -> str:
    """The address of an inline image, or "" when this client can't say where it lives.

    The spec builds the address by appending the file name to the tag's ``URL`` argument. Most
    MUDs leave ``URL`` out and expect the client to have an images location configured -- the
    same assumption MSP's ``U=`` makes for sounds -- so the world's own setting stands in for it.

    Only ``http``/``https`` is ever returned. An ``<img src>`` or a browser launch pointed at
    anything else is not a choice a MUD gets to make, and a file name that tries to be a path
    (absolute, drive-qualified, ``..``) is refused rather than cleaned up. ``trusted`` is the
    address the MUD session is on, which is the one non-public host a picture may come from --
    a MUD running on the player's own machine can serve its own images.
    """
    name = file.strip().replace("\\", "/")
    if not name or is_unsafe(name):
        return ""
    if name.lower().startswith(("http://", "https://")):
        url = name  # an absolute address in FName: legal for MSP, and http(s) only, same rule
    else:
        root = (base or fallback).strip()
        if not root.lower().startswith(("http://", "https://")):
            return ""
        url = root.rstrip("/") + "/" + name.lstrip("/")
    return url if is_allowed_host(urlparse(url).hostname or "", trusted) else ""


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


