"""Reviewing and following MUD links, and checking commands they offer to type."""

from __future__ import annotations

import re

from genericmud.app.typed_commands import CLIENT_PREFIX
from genericmud.bridge import protocol
from genericmud.model.buffer import Link
from genericmud.voice.router import REVIEW_CHANNEL

# An <A HREF> link is opened, never typed at the MUD: only these schemes are followed, so a
# MUD can't send the platform off to launch a local application through the shell.
_LINK_URL_SCHEMES = ("http://", "https://")

# A bare address in the text, rather than one a markup tagged: what people write in a MUD.
# Only the two schemes above are found, plus the "www." form, which is opened over https because
# it is the one address shape where the text does not get to choose the scheme. Characters that
# end an address in running prose -- a quote, an angle bracket, a backtick -- end it here too.
_BARE_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.IGNORECASE)
# What may follow an address without being part of it: a sentence's full stop, a comma, a
# markdown-ish asterisk.
_BARE_URL_TAIL = ".,;:!?*"
# A closing bracket after the address is the writer's, not the address's, unless the line opened
# one that the address is inside: "(see https://example.com)" ends before the round one.
_BARE_URL_PAIRS = {")": "(", "]": "[", "}": "{"}
# Bounds, so one line of a server's choosing cannot become a wall of links or one link a
# paragraph. Both are arbitrary and easy to change.
_MAX_BARE_LINKS = 8
_MAX_BARE_URL = 2000
# How many things that start like an address a line is checked for, found or not. Each is
# compared with the MUD's own links on the line, so a line of thousands of them must not cost
# thousands of comparisons each.
_MAX_BARE_CANDIDATES = 32


# The longest text a link may type into the command box. Starting a command is what the
# spec has in mind ("tell Ariadne "); nobody needs a paragraph begun for them.
_MAX_TYPED_LINK = 200


def _address_end(text: str, start: int, end: int) -> int:
    """Where the address that starts at ``start`` actually ends, given a candidate ``end``.

    The brackets are counted once and the count kept as closing ones come off the end, so a
    candidate ending in thousands of them costs what it says and not its length again for each.
    """
    candidate = text[start:end]
    closing = {close: candidate.count(close) for close in _BARE_URL_PAIRS}
    opening = {close: candidate.count(open_) for close, open_ in _BARE_URL_PAIRS.items()}
    while end > start:
        last = text[end - 1]
        if last in _BARE_URL_TAIL:
            end -= 1
            continue
        if last in closing and closing[last] > opening[last]:
            closing[last] -= 1
            end -= 1
            continue
        break
    return end


def _looks_like_address(address: str) -> bool:
    """Whether what is left is an address, rather than a word that starts like one."""
    lowered = address.lower()
    if lowered.startswith("www."):
        rest = address[len("www."):]
        return "." in rest.rstrip(".") and len(rest) > 3
    rest = address.split("://", 1)[1] if "://" in address else ""
    return bool(rest) and not rest.startswith("/")


def _opened_form(address: str) -> str:
    """The address to open: the one written, with https:// in front of a bare "www." one.

    Only the scheme this client already trusts is ever put on the front, so a line of text can
    never choose what kind of address is opened.
    """
    return address if "://" in address else "https://" + address


def bare_link_spans(
    text: str, covered: list[tuple[int, int, str, bool, list[list[str]], str]]
) -> list[tuple[int, int, str, bool, list[list[str]], str]]:
    """Where each bare address in ``text`` sits, in the shape a tagged link's position takes.

    ``covered`` is where the MUD's own links already are, exactly, from the parser's markers:
    its link wins, because following one may do something other than open a page, and two links
    over the same words would leave the reader choosing between them on one word.
    """
    taken = [(start, end) for start, end, *_rest in covered]
    spans: list[tuple[int, int, str, bool, list[list[str]], str]] = []
    for examined, match in enumerate(_BARE_URL_RE.finditer(text)):
        if len(spans) >= _MAX_BARE_LINKS or examined >= _MAX_BARE_CANDIDATES:
            break
        start = match.start()
        end = _address_end(text, start, match.end())
        if end - start > _MAX_BARE_URL:
            continue
        address = text[start:end]
        if not _looks_like_address(address):
            continue
        if any(start < other_end and other_start < end for other_start, other_end in taken):
            continue
        spans.append((start, end, _opened_form(address), False, [], ""))
    return spans



def _menu_note(link: Link) -> str:
    """What to say after a link's words when following it asks rather than acts."""
    return f" (menu of {len(link.choices)})" if link.choices else ""


def typed_refusal(command: str, separator: str) -> str | None:
    """Why text that would land in the command box as the player's typing may not, or None.

    Shared by a MUD's own links and by a rule's "send to command": both put text where the
    player's Enter will treat it as their own typing, so neither may start a client command or
    hide a second command behind the separator.
    """
    if command.lstrip().startswith(CLIENT_PREFIX):
        return "it would type a genericMud command"
    if separator and separator in command:
        return "it would type more than one command"
    if len(command) > _MAX_TYPED_LINK:
        return "what it would type is too long for a command"
    if not command.isprintable():
        return "what it would type has control characters in it"
    return None


class LinksMixin:
    """Reviewing and following MUD links, and checking commands they offer to type.

    Part of EngineApp (engine_app.py). Every attribute below starts in its constructor.

    Writes: _link_announced.

    Reads: _diag, _post, buffer, command_separator, pack_window, review, voice.

    Calls: _speak_system, key_actions (_speak_review), typed_commands (_dispatch_command).
    """

    def _announce_link_under_cursor(self) -> None:
        """Say what the link the follow key would act on does: the keyboard's version of hovering.

        The link here is the same one Alt+Enter would follow -- including a line's only link,
        which is followed from any word on it. Announcing the caption by a stricter rule than
        the one that follows it meant the key could act on a link the reader had never heard
        described.

        Said once per link, so walking the words of one link says it on arrival and not again.
        That is the link itself, not an equal one: the same exit on the next line is a link
        the reader has not arrived at yet.
        """
        link = self.review.link()
        if link is self._link_announced:
            return
        self._link_announced = link
        if link is not None and link.hint:
            # Queued behind the word rather than barging in on it: the word the cursor just
            # reached is the thing being read, and the caption explains it -- cutting it off
            # left the reader hearing only "link" and never the word it was about.
            self.voice.speak(f"link: {link.hint}", channel=REVIEW_CHANNEL, interrupt=False)

    def _jump_to_link(self, *, forward: bool) -> None:
        """Alt+PageDown / Alt+PageUp: on to the next MXP link, or back to the previous one.

        The review cursor parks on the link's first word, so Alt+Enter follows it, and the
        link's caption is said after it. Starting outside review, the jump counts the newest
        line's own links: back from the end of it, or on from its start.
        """
        fresh = not self.review.active
        if fresh:
            self.review.enter()
        if forward:
            link = self.review.next_link(inclusive=fresh)
        else:
            link = self.review.prev_link(from_line_end=fresh)
        if link is None:
            self._speak_review("no more links")
            return
        said = f"{link.text}{_menu_note(link)}"
        line = self.review.current_line()
        # The line too, unless the link is all of it: "north" alone doesn't say where it is.
        self._speak_review(said if line.strip() == link.text.strip() else f"{said}: {line}")
        self._announce_link_under_cursor()

    def _activate_link(self) -> None:
        """Follow the MXP link under the review cursor.

        The mouse version of this is "click the words"; the keyboard version is the same
        gesture one level down -- the review cursor's word picks the link, and on a line
        with a single link anywhere on it counts. Nothing is remembered between presses:
        the link lives on its line, so reading back through the scrollback still follows
        the right one.
        """
        if not self.review.active:
            self.review.enter()  # nothing reviewed yet, so act on the newest line
        links = self.review.links()
        if not links:
            self._speak_review("no link on this line")
            return
        chosen = self.review.link()
        if chosen is None:
            # Several links and the word isn't in one: name them rather than guess, so one
            # more Alt+Right lands on a word that picks the intended one. A link with no
            # words of its own (an image, or an empty <SEND>) is named by what it does.
            named = "; ".join(
                f"{number} {link.text or link.command}{_menu_note(link)}"
                for number, link in enumerate(links, 1)
            )
            self._speak_review(f"{len(links)} links: {named}")
            return
        if chosen.choices:
            # A menu is a question, not an answer: following it asks which command was meant,
            # and nothing goes to the MUD until the player picks one. The question is traced
            # as well as the answer, so a session's log says what was offered and what was
            # taken.
            if self._diag is not None:
                self._diag.event(
                    "mxp.menu", text=chosen.text, choices=len(chosen.choices)
                )
            self._post(protocol.menu(chosen.text or chosen.command, list(chosen.choices)))
            return
        self._follow_link(
            chosen.text, chosen.command, insert=chosen.insert, hint=chosen.hint
        )

    def _activate_link_at(self, text: str, from_end: int, column: int = -1) -> None:
        """Follow the link on the output line the reader is on (Enter in the output box).

        The output box holds fewer lines than the buffer and has a caret of its own, so the
        window says which line it is -- its text, and how far it sits from the end -- and which
        word of it the caret is on. The review cursor moves to that line and that word, so
        Enter never acts on some newer line the reader hasn't heard, and a line carrying
        several links follows the one under the caret rather than giving up. A line that can't
        be found follows nothing.
        """
        lines = self.buffer.lines()
        near = len(lines) - 1 - max(0, from_end)
        matches = [index for index, line in enumerate(lines) if line.plain_text == text]
        if not text.strip() or not matches:
            self._speak_review("no link on this line")
            return
        self.review.go_to(min(matches, key=lambda index: abs(index - near)))
        if column >= 0:
            self.review.go_to_column(column)
        self._activate_link()

    def _follow_link(
        self, text: str, command: str, *, insert: bool = False, hint: str = ""
    ) -> None:
        """Do what a link asks, whoever asked it: the review cursor or a click in the browser.

        ``hint`` is the caption the MUD gave the link -- what following it is meant to do. It is
        said with the command, because "a sword: buy sword" leaves out what the MUD was
        offering, and the reader is about to send a command on the strength of it.
        """
        if not command.strip():
            return
        if self._diag is not None:
            # Not "mxp.link": a link can come from Pueblo's <a xch_cmd> too, and this is the one
            # place every markup's links are followed from.
            self._diag.event("link.follow", text=text, command=command, insert=insert, hint=hint)
        if insert:
            refusal = self._typed_link_refusal(command)
            if refusal is not None:
                if self._diag is not None:
                    self._diag.event(
                        "link.refused", text=text, command=command, reason=refusal
                    )
                self._speak_system(f"Link not followed: {refusal}.")
                return
            # <SEND PROMPT>: the command is the opening of one the player finishes, so it
            # belongs in their command box, not on the wire -- sent as it stands it would
            # reach the MUD as a command they never gave. Nothing leaves until they press
            # Enter themselves. The trailing space the MUD wrote is kept: "tell Ariadne " is
            # meant to have a name typed after it.
            self._post(protocol.input_text(command))
            said = f"in the command box: {command.strip()}"
            self._speak_system(f"{said}. {hint}" if hint else said)
            return
        command = command.strip()
        if command.lower().startswith(_LINK_URL_SCHEMES):
            self._open_link(command)
            return
        # Said before the command goes, so the caption explains what is being sent rather
        # than arriving behind it.
        said = f"{text}: {command}" if text else f"link: {command}"
        self._speak_system(f"{said}. {hint}" if hint else said)
        # A link is the MUD's text, not the player's, so it may not run a client command:
        # a MUD could otherwise tag any word with <SEND HREF="/quit"> and end the session.
        self._dispatch_command(command, allow_client=False)

    def _typed_link_refusal(self, command: str) -> str | None:
        """Why a link's text may not be typed into the command box, or None when it may.

        Whatever lands in the box goes out as the player's own typing when they press Enter,
        with genericMud's commands allowed and the separator splitting it. So a MUD may start
        one plain command there and nothing more: not a genericMud command, which could plant
        a trigger or an alias to fire later, and not a second command hidden behind the
        separator, which the player would send without meaning to.
        """
        return typed_refusal(command, self.command_separator)

    def _open_link(self, url: str) -> None:
        """Open a link in the browser, saying the address before the focus goes with it.

        Said first because the browser takes the keyboard focus, and with it the screen reader:
        an address announced afterwards is one the player may never hear, which leaves a browser
        tab they cannot account for. Both kinds of link come through here -- one the MUD tagged
        and one found in its text -- so both are announced the same way.
        """
        self._speak_system(f"opening {url}")
        window = self.pack_window
        if window is not None:
            # announced=True: this method has just said the address, and the session's own line
            # about it is already in the output. The window saying it again put the same line
            # there twice, once from each side.
            if not window.open_url(url, announced=True):
                self._speak_system("no browser to open that link")
        else:
            # Nothing native to ask: the web UI runs inside a browser, so the renderer opens
            # the tab. Only the schemes checked by the caller ever reach here.
            self._post(protocol.open_url(url))
