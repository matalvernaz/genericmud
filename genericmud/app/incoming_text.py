from __future__ import annotations

import re
from dataclasses import replace

from genericmud.bridge import protocol
from genericmud.model.buffer import Line, Link
from genericmud.protocol import telnet as T
from genericmud.protocol.msp import SoundCue, parse_msp_line
from genericmud.protocol.mxp import IMAGE_MARK, MxpParser, resolve_image_url
from genericmud.render.ansi import parse_ansi
from genericmud.safepath import is_unsafe
from genericmud.sound.bus import DEFAULT_CATEGORY, MUSIC_CATEGORY

_PROMPT_IDLE_FLUSH_SECONDS = 0.25  # emit a newline-less prompt if the server sends no more data


# How much text an MXP <P> may hold before it is emitted anyway. A paragraph is meant to
# join a room description's worth of lines, not to buffer a session: past this the client
# says what it has and starts again, so a server that never closes <P> costs nobody their
# memory or their speech.
_MXP_MAX_JOIN_CHARS = 4000
_MXP_MAX_JOIN_LINES = 50
# ...and how long it may hold them. A server sending one line every 45 seconds would reach
# the line cap after half an hour of silence, so size alone does not bound latency.
_MXP_MAX_JOIN_SECONDS = 30.0


def _redact_secret(text: str, secret: str) -> str:
    """``text`` with ``secret`` taken out of it, as a whole whitespace-separated token.

    A server that echoes what it reads will send a blind login straight back -- MUSH/MOO's
    "connect hero hunter2", or a "Password: hunter2" for Diku -- and that line would be
    spoken and shown, and written to the session log. Matching whole tokens rather than any
    substring keeps a one-character password from redacting half the output, while still
    catching both shapes.
    """
    if not secret:
        return text
    pattern = re.compile(r"(?<!\S)" + re.escape(secret) + r"(?!\S)")
    return pattern.sub("***", text)


def _strip_image_marks(text: str) -> tuple[str, list[int]]:
    """``text`` without its inline-image markers, and where each marker sat once they're gone.

    Kept separate from the parser because the offset is only knowable here: everything between
    the parser and this point (MXP tags, MSP markers, ANSI codes, password redaction) rewrites
    the text, so a position counted any earlier would point at the wrong place.
    """
    if IMAGE_MARK not in text:
        return text, []
    kept: list[str] = []
    positions: list[int] = []
    for char in text:
        if char == IMAGE_MARK:
            positions.append(len(kept))
        else:
            kept.append(char)
    return "".join(kept), positions


def _link_positions(
    text: str, links: list[Link]
) -> list[tuple[int, int, str, bool, list[list[str]], str]]:
    """Where each link's words sit: (start, end, command, insert, menu, hint).

    The parser reports a link by its words, not by position, because everything between the
    parser and here (ANSI, MSP, redaction) rewrites the text. Searching forward from the
    previous link keeps a word that appears twice ("a fountain by a fountain") pointing at the
    right occurrence, in the order the MUD sent them. A link whose words are gone -- a pack
    substituted the line -- is dropped: a wrong position would make the wrong words clickable.
    """
    spans: list[tuple[int, int, str, bool, list[list[str]], str]] = []
    cursor = 0
    for link in links:
        if not link.text:
            continue
        start = text.find(link.text, cursor)
        if start < 0:
            continue
        cursor = start + len(link.text)
        spans.append(
            (
                start,
                cursor,
                link.command,
                link.insert,
                [list(item) for item in link.choices],
                link.hint,
            )
        )
    return spans


class IncomingTextMixin:
    # --- inbound from the MUD (telnet events) ---

    def on_telnet_event(self, event: T.Event) -> None:
        if isinstance(event, T.DataReceived):
            self._feed_text(self._decode_server_bytes(event.data))
        elif isinstance(event, T.Subnegotiation):
            self._handle_subnegotiation(event)
        elif isinstance(event, T.Command) and event.command in (T.GA, T.EOR):
            self._flush_prompt()  # prompt arrived without a trailing newline
        elif (
            isinstance(event, T.Negotiation)
            and event.command == T.WILL
            and event.option == T.OPT_MSDP
        ):
            # Server offers MSDP (once per connection, so a reconnect re-arms too).
            self._msdp_offered = True
            self._dispatch_msdp_start()
        elif (
            isinstance(event, T.Negotiation)
            and event.option == T.OPT_MXP
            and event.command in (T.WILL, T.WONT)
        ):
            self._mxp_negotiated = event.command == T.WILL
            if not self._mxp_negotiated:
                self._mxp = MxpParser()
                self._release_mxp_hold()
        elif (
            isinstance(event, T.Negotiation)
            and event.option == T.OPT_ECHO
            and event.command in (T.WILL, T.WONT)  # DO/DONT ask whether WE echo: not this
        ):
            # The server takes over echo (IAC WILL ECHO) right before a password prompt and
            # releases it (WONT) afterward. While it holds echo, the input is a secret: keep
            # it out of the session log, the command history, and the autoretype buffer, and
            # send it exactly as typed (_dispatch_secret).
            self._set_input_masked(event.command == T.WILL)

    def _decode_server_bytes(self, data: bytes) -> str:
        """Decode server text in the world's encoding (see protocol/charset.py).

        Many legacy MUDs (notably Spanish-language ones) send an 8-bit encoding; the old
        hard utf-8/replace decode turned every accented letter into U+FFFD, which a screen
        reader speaks as garbage. ``auto`` guesses; a world set to its real encoding reads
        exactly what the MUD sent, including Cyrillic and CJK text the guess can't recover.
        """
        return self.codec.decode(data)

    def _on_encoding_latch(self, position: int) -> None:
        if self._diag is not None:
            self._diag.event("encoding.latch", encoding="cp1252", at=position)

    def _feed_text(self, text: str) -> None:
        self._pending += text
        while "\n" in self._pending:
            raw, self._pending = self._pending.split("\n", 1)
            self._emit_line(raw.rstrip("\r"))
        if self._pending:
            # A prompt with no trailing newline and no GA/EOR (some MUDs) would otherwise sit in
            # _pending unspoken forever, deadlocking auto-login. Flush it if no more bytes arrive
            # shortly; a newer chunk bumps the generation so this scheduled flush no-ops. GA/EOR
            # still flush immediately via _flush_prompt, making this only a backstop.
            self._prompt_gen += 1
            gen = self._prompt_gen
            self._schedule(_PROMPT_IDLE_FLUSH_SECONDS, lambda: self._idle_flush_prompt(gen))

    def _idle_flush_prompt(self, gen: int) -> None:
        if gen == self._prompt_gen and self._pending:
            self._flush_prompt()

    def _flush_prompt(self) -> None:
        if self._pending:
            self._emit_line(self._pending)
            self._pending = ""

    def _mxp_feed(
        self, text: str
    ) -> tuple[str, list[SoundCue], list[tuple[str, str]], list[tuple[str, str]]]:
        """Parse one line as MXP, returning plain text, its sound cues and its links.

        ``<NOBR>`` and an open ``<P>`` are the only MXP rules that span lines: both mean the
        next line belongs on this one, so the text is held rather than emitted. Everything
        else is decided within the line. Links are held with the text they came from, or a
        link tagged on a swallowed line would point at a paragraph that doesn't contain it.

        The hold is bounded. A ``<P>`` that is never closed would otherwise keep growing and
        keep the user in silence for as long as it lasted, so once enough has been gathered
        the paragraph is emitted in one piece and the stream carries on. Nothing is dropped:
        the join simply restarts, and the parser's own paragraph state is untouched.
        """
        if not self.mxp_enabled or not self._mxp_negotiated:
            return text, [], [], []
        # Links and images seen while a paragraph was being gathered are carried with the text
        # they came from; an image left a marker in that text, which survives the join.
        links = self._mxp_join_links
        self._mxp_join_links = []
        images = self._mxp_join_images
        self._mxp_join_images = []
        held = self._mxp_join
        self._mxp_join = ""
        # Each line from the MUD is parsed once, on its own, and the parsed text is what gets
        # joined. Feeding the held text back in with the next line decoded it twice (an
        # escaped "&lt;password&gt;" came back as a live tag) and hid the next line's own mode
        # escape behind it.
        outcome = self._mxp.feed(text)
        for payload in outcome.replies:
            self._mxp_reply(payload)
        for request in outcome.login:
            self._mxp_login(request)
        links.extend(outcome.links)
        images.extend(outcome.images)
        joined = held + outcome.text
        if not outcome.join_next:
            self._release_mxp_hold()
            return joined, outcome.cues, links, images
        self._mxp_held_lines += 1
        if self._mxp_held_lines >= _MXP_MAX_JOIN_LINES or len(joined) >= _MXP_MAX_JOIN_CHARS:
            self._release_mxp_hold()
            if self._diag is not None:
                self._diag.event("mxp.join_flushed", reason="size")
            return joined, outcome.cues, links, images
        self._mxp_join = joined
        self._mxp_join_links = links  # for a deadline flush, which has no text to re-parse
        self._mxp_join_images = images
        self._arm_mxp_deadline()
        return "", outcome.cues, [], []

    def _release_mxp_hold(self) -> None:
        """Forget a hold, and any deadline waiting on it."""
        self._mxp_join = ""
        self._mxp_join_links = []
        self._mxp_join_images = []
        self._mxp_held_lines = 0
        self._mxp_timer_armed = False
        self._mxp_timer_token += 1  # a timer already scheduled must now do nothing

    def _arm_mxp_deadline(self) -> None:
        """Start the clock on a hold, once, however many lines it swallows."""
        if self._mxp_timer_armed:
            return
        self._mxp_timer_armed = True
        self._mxp_timer_token += 1
        token = self._mxp_timer_token
        self._schedule(
            _MXP_MAX_JOIN_SECONDS, lambda: self._mxp_deadline_reached(token)
        )

    def _mxp_deadline_reached(self, token: int) -> None:
        """Say what a long-held paragraph has so far, rather than leave the user in silence."""
        if token != self._mxp_timer_token or not self._mxp_join or self._closed:
            return
        held = self._mxp_join
        held_links = self._mxp_join_links
        held_images = self._mxp_join_images
        self._release_mxp_hold()
        if self._diag is not None:
            self._diag.event("mxp.join_flushed", reason="timeout")
        self._emit_parsed_line(held, links=held_links, images=held_images)

    def _mxp_reply(self, payload: str) -> None:
        """Answer ``<VERSION>``/``<SUPPORT>`` on a secure line, as the spec requires.

        The secure line (``ESC[1z``) is how the MUD tells a client reply from something a
        player typed, so it is not optional.
        """
        if self._diag is not None:
            self._diag.event("mxp.reply", payload=payload)
        if self._raw_send is None:
            return  # headless: nothing to answer on, and the diag event records it
        try:
            self._raw_send(MxpParser.secure_line(payload).encode("utf-8"))
        except (OSError, ConnectionError):
            pass  # a dead socket must not break the line that provoked the question

    def _emit_line(self, text: str) -> None:
        # Strip markup before anything reads it; links and images ride along with the text.
        if self.pueblo_enabled:
            # Pueblo's HTML replaces MXP rather than nesting inside it: a world that switches
            # to HTML is not speaking MXP, and one line of it can be several lines of text.
            was_active = self._pueblo.active
            for piece in self._pueblo.feed(text):
                self._note_pueblo_links(piece.links)
                self._emit_parsed_line(piece.text, links=list(piece.links))
            if was_active != self._pueblo.active and self._diag is not None:
                self._diag.event("pueblo.mode", html=self._pueblo.active)
            self._note_pueblo_image()
            return
        text, mxp_cues, links, images = self._mxp_feed(text)
        self._emit_parsed_line(text, mxp_cues, links, images)

    def _emit_parsed_line(
        self,
        text: str,
        mxp_cues: list[SoundCue] | None = None,
        links: list[tuple[str, str]] | None = None,
        images: list[tuple[str, str]] | None = None,
    ) -> None:
        """Show and speak a line that has already been through the MXP parser.

        Kept separate because the deadline flushes a held paragraph: sending that text back
        through :meth:`_emit_line` would feed it to the parser again, find the paragraph still
        open, and hold it a second time -- the flush would emit nothing at all.
        """
        text, cues = parse_msp_line(text)  # strip MSP markers before colour parsing
        for cue in [*(mxp_cues or []), *cues]:
            channel = MUSIC_CATEGORY if cue.kind == "music" else DEFAULT_CATEGORY
            if cue.is_stop:
                if cue.url:  # the spec's way to set where later cues download from
                    self._msp_url = cue.url
                if self._diag is not None:
                    self._diag.event("msp.cue", kind=cue.kind, stop=True)
                self._stop_media(channel)
                continue
            # MSP filenames come straight from the untrusted server. Block the dangerous shapes
            # (absolute / UNC / drive / ..) so a hostile MUD can't open an arbitrary file -- a
            # Windows UNC path leaks the NTLM hash on open, before decode even fails. A safe
            # relative name passes through to the backend as before.
            if is_unsafe(cue.file):
                if self._diag is not None:
                    self._diag.event("msp.cue", kind=cue.kind, file=cue.file, blocked=True)
                continue
            if self._diag is not None:
                self._diag.event("msp.cue", kind=cue.kind, file=cue.file, volume=cue.volume)
            gain = cue.volume / 100.0
            url = cue.url or self._msp_url
            times = max(1, cue.repeats)  # L= plays that many times; -1 loops until stopped
            if cue.kind == "music":
                self._start_media(
                    cue.file, url, channel,
                    lambda path, gain=gain, times=-1 if cue.loops_forever else times: (
                        self.sound.music(path, MUSIC_CATEGORY, gain=gain, times=times)
                    ),
                )
            else:
                self._start_media(
                    cue.file, url, channel,
                    lambda path, gain=gain, loop=cue.loops_forever, times=times: self.sound.play(
                        path, DEFAULT_CATEGORY, gain=gain, loop=loop, times=times
                    ),
                )
        spans = parse_ansi(text)
        plain = "".join(span.text for span in spans)
        if self._password_guard:
            # A blind login is typed before the server has switched echo off, so a server
            # that echoes what it reads sends the password back. It must not reach the
            # output box, the screen reader, the session log or a soundpack.
            redacted = _redact_secret(plain, self._password_guard)
            if redacted != plain:
                plain, spans = redacted, []  # the colours belonged to the text that's gone
        # Inline images: the parser left a marker wherever one sits, and only now is its offset
        # knowable. An image whose address can't be worked out simply isn't there.
        plain, marks = _strip_image_marks(plain)
        if marks:
            spans = [replace(span, text=span.text.replace(IMAGE_MARK, "")) for span in spans]
            spans = [span for span in spans if span.text]
        shown: list[tuple[int, str]] = []
        for (file, base), offset in zip(images or [], marks, strict=False):
            url = resolve_image_url(
                file, base, self.images_url, self.media.trusted_address()
            )
            if url:
                shown.append((offset, url))
                if self._diag is not None:
                    self._diag.event("mxp.image", file=file, url=url)
        if not plain.strip() and not shown:
            return  # blank line: any sound cues already fired; don't show/speak "blank"
        for pack in self._mush_packs:
            pack.dispatch_line(plain)
        line_links = list(links or [])
        # A text client has no way to show a picture, so an image also becomes a link: the
        # same thing MUSHclient does with one (it turns <IMAGE> into a hyperlink), and the
        # only form in which a picture can be reached without seeing it.
        for _offset, url in shown:
            line_links.append(Link(url.rsplit("/", 1)[-1], url))
        line = Line(plain, spans=spans, links=line_links)
        self.engine.process_line(line)  # may set line.channel and gag flags
        self._log(line.plain_text)  # full session log, including gagged-from-speech
        if self._login is not None and not self._login.done:
            self._login.feed(line.plain_text)  # answer name/password prompts
            if self._login.done:
                # The password is away (or the wait gave up): what the world asked to type
                # once connected goes now.
                self._send_post_connect()
        if self._walk is not None and self._walk.active:
            self._walk.on_line(line.plain_text)  # halt the walk if a step was blocked
        if line.substitute is not None:
            # A pack replaced the line (VIPMud #Sub). The log, login and the walker keep what
            # the MUD sent; the player sees and hears the replacement.
            line.plain_text, line.spans = line.substitute, []
            if not line.plain_text.strip():
                return
        policy = self.channels.policy(line.channel)
        if policy.speak and not line.gagged and self.engine.line_speech and line.plain_text.strip():
            self.voice.speak(
                line.plain_text,
                channel=(policy.voice or line.channel),
                interrupt=policy.interrupt or self.interrupt_mode,
            )
        if policy.display and not (line.gagged and not line.display_when_gagged):
            self.buffer.append(line)
            self.word_index.add_line(line.plain_text)
            self._post(
                protocol.line(
                    line.plain_text,
                    gagged=line.gagged,
                    display_when_gagged=line.display_when_gagged,
                    # Positions are only meaningful for the words the MUD sent; a substituted
                    # line shows a pack's own text, so it gets none.
                    links=(
                        _link_positions(line.plain_text, line.links)
                        if line.substitute is None
                        else []
                    ),
                    images=shown if line.substitute is None else [],
                )
            )

    def _note_pueblo_links(self, links: tuple[Link, ...]) -> None:
        """Note the first command link a world offers, once per connection.

        "My exits are not followable" has two very different answers -- the world writes them
        as plain words, or it writes them as links this client dropped -- and one line in the
        diagnostics tells them apart without a session of guessing.
        """
        if not links or self._pueblo_links_noted or self._diag is None:
            return
        self._pueblo_links_noted = True
        self._diag.event("pueblo.links", first=links[0].command, count=len(links))

    def _note_pueblo_image(self) -> None:
        """Note the first picture or sound a world sends, once per connection.

        Nothing here shows or plays one yet, and that is the point of noting it: "my MUD's
        pictures are missing" and "my MUD sends no pictures" are different reports, and a reader
        who hears neither cannot tell them apart. One line answers it.
        """
        source = self._pueblo.first_image
        if not source or self._pueblo_image_noted or self._diag is None:
            return
        self._pueblo_image_noted = True
        self._diag.event("pueblo.image", src=source)
