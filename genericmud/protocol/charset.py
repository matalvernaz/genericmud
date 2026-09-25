"""Per-world character encodings: decode what the MUD sends, encode what the player types.

Most MUDs speak UTF-8 now, but plenty of long-running ones never moved: Russian MUDs
offer Windows-1251 or KOI8-R, Chinese ones GBK or Big5, Western European ones
Windows-1252. A world's saved encoding picks one codec for both directions. ``auto``
keeps the heuristic every world used before the setting existed: UTF-8, latching to
Windows-1252 for the rest of the connection on the first byte that can't be UTF-8.

Telnet reserves byte 0xFF (IAC), and several of these encodings put a letter there
(Windows-1251's я, KOI8-R's Ъ). The connection doubles it on the way out and the
telnet parser undoes the doubling on the way in, so a codec here only ever sees data.
"""

from __future__ import annotations

import codecs
from collections.abc import Callable

AUTO = "auto"

# (setting value, what the World dialog shows). Values are Python codec names. The list
# is curated rather than every codec Python has, so the dialog's choice stays short
# enough to browse, and grouped by region so first-letter navigation lands usefully.
ENCODING_CHOICES: tuple[tuple[str, str], ...] = (
    (AUTO, "Automatic: UTF-8, or Windows Western European if the MUD sends anything else"),
    ("utf-8", "UTF-8"),
    ("cp1252", "Western European, Windows-1252"),
    ("iso-8859-1", "Western European, ISO-8859-1 (Latin-1)"),
    ("cp1250", "Central European, Windows-1250"),
    ("iso-8859-2", "Central European, ISO-8859-2"),
    ("cp1251", "Cyrillic, Windows-1251"),
    ("koi8-r", "Cyrillic, KOI8-R (Russian)"),
    ("koi8-u", "Cyrillic, KOI8-U (Ukrainian)"),
    ("cp866", "Cyrillic, DOS code page 866"),
    ("cp1253", "Greek, Windows-1253"),
    ("cp1254", "Turkish, Windows-1254"),
    ("cp1257", "Baltic, Windows-1257"),
    ("gb18030", "Chinese Simplified, GBK or GB18030"),
    ("big5", "Chinese Traditional, Big5"),
    ("cp932", "Japanese, Shift JIS"),
    ("euc-jp", "Japanese, EUC-JP"),
    ("cp949", "Korean, EUC-KR"),
)
_LABELS = dict(ENCODING_CHOICES)
# Codec-registry name -> setting, so any spelling Python accepts ("windows-1251",
# "KOI8_R", "latin-1") lands on the choice it means. The extra entries are older names
# whose superset is the one offered: GB18030 decodes GBK and GB2312, code page 932 is
# the Shift JIS Windows MUDs actually send, and 949 is EUC-KR plus Windows' additions.
_BY_CODEC_NAME = {
    codecs.lookup(value).name: value for value, _label in ENCODING_CHOICES if value != AUTO
}
_BY_CODEC_NAME.update(
    {
        codecs.lookup("gbk").name: "gb18030",
        codecs.lookup("gb2312").name: "gb18030",
        codecs.lookup("shift_jis").name: "cp932",
        codecs.lookup("euc_kr").name: "cp949",
    }
)

# What auto falls back to, and how: Latin-1 decodes every byte, then the C1 range
# (0x80-0x9F) is re-read as Windows-1252. MUDs advertising "Latin-1" almost always send
# Windows-1252, whose curly quotes and dashes live there; read as Latin-1 they are
# invisible control characters a screen reader garbles ("it\x92s"), and a trigger written
# with the intended punctuation never matches. The five bytes Windows-1252 leaves
# undefined stay as they are.
_AUTO_FALLBACK = "cp1252"
CP1252_C1_TABLE = {
    byte: char
    for byte in range(0x80, 0xA0)
    if (char := bytes([byte]).decode("cp1252", "ignore"))  # "" for the 5 undefined bytes
}
_UTF8_MAX_CONTINUATION = 3  # a truncated UTF-8 character is at most three bytes short


def normalize_encoding(value: object) -> str:
    """The setting ``value`` names, or :data:`AUTO` if it names nothing offered.

    Worlds files are hand-editable and older builds never wrote the key, so an absent,
    misspelled, or unsupported encoding means the automatic behaviour rather than a
    world that can't load.
    """
    if not isinstance(value, str):
        return AUTO
    text = value.strip()
    if not text or text.lower() == AUTO:
        return AUTO
    try:
        name = codecs.lookup(text).name
    except LookupError:
        return AUTO
    return _BY_CODEC_NAME.get(name, AUTO)


def encoding_label(value: str) -> str:
    """The plain-language name the dialogs show for a setting."""
    return _LABELS[normalize_encoding(value)]


class ServerTextCodec:
    """One connection's text codec: server bytes in, typed commands out.

    Explicit encodings use Python's incremental decoders, which hold a multibyte
    character split across telnet chunks until the rest arrives, and replace bytes that
    aren't valid in the encoding rather than failing. ``auto`` keeps its latch, and once
    latched it sends Windows-1252 too: a MUD that proved not to speak UTF-8 reads a typed
    "señor" sent as UTF-8 as two garbage characters where the ñ belongs.
    """

    def __init__(self, encoding: str = AUTO, *, on_latch: Callable[[int], None] | None = None):
        self.encoding = normalize_encoding(encoding)
        self._on_latch = on_latch
        self.latched = False
        self._pending = b""
        self._decoder: codecs.IncrementalDecoder | None = None
        self.reset()

    def reset(self) -> None:
        """Start a fresh byte stream (a new socket): drop half-read characters and the latch."""
        self.latched = False
        self._pending = b""
        self._decoder = (
            None
            if self.encoding == AUTO
            else codecs.getincrementaldecoder(self.encoding)(errors="replace")
        )

    @property
    def advertises_utf8(self) -> bool:
        """Whether the terminal-type handshake may claim UTF-8.

        Servers that honour the MTTS UTF-8 bit switch their output to UTF-8, which would
        garble a world the player has set to KOI8-R, so only claim it when UTF-8 is what
        this codec reads.
        """
        return self.encoding in (AUTO, "utf-8")

    def decode(self, data: bytes) -> str:
        if self._decoder is not None:
            return self._decoder.decode(data)
        return self._decode_auto(data)

    def decode_payload(self, data: bytes) -> str:
        """Text inside a GMCP, MSDP or MSSP payload.

        GMCP must be UTF-8 and the other two name no encoding at all, so a payload that
        is valid UTF-8 is read as UTF-8, and one that isn't is read in the world's
        encoding: a KOI8-R MUD's MSDP room name arrives in KOI8-R. Cyrillic, Greek or
        Western European text in a legacy encoding is practically never valid UTF-8 by
        accident, so the order can't misread either kind. On ``auto`` the fallback is
        the same Windows-1252 the text stream latches to. Payloads never move the latch.
        """
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        if self.encoding == AUTO:
            return data.decode("latin-1").translate(CP1252_C1_TABLE)
        return data.decode(self.encoding, errors="replace")

    def encode(self, text: str) -> bytes:
        """Encode a typed command; a character the encoding lacks is sent as "?"."""
        if self.encoding != AUTO:
            codec = self.encoding
        else:
            codec = _AUTO_FALLBACK if self.latched else "utf-8"
        return text.encode(codec, errors="replace")

    def _decode_auto(self, data: bytes) -> str:
        """UTF-8 first, permanently falling back to Windows-1252 on the first invalid byte.

        A multibyte sequence split across telnet chunks is NOT evidence of a legacy
        encoding: the incomplete tail is held for the next chunk. A genuinely invalid byte
        latches the fallback for the rest of the connection, since a MUD's encoding doesn't
        change mid-stream. Decoded as Latin-1 then re-read through the C1 table.
        """
        if self.latched:
            return data.decode("latin-1").translate(CP1252_C1_TABLE)
        buf = self._pending + data
        self._pending = b""
        try:
            return buf.decode("utf-8")
        except UnicodeDecodeError as err:
            if (
                err.reason == "unexpected end of data"
                and err.start >= len(buf) - _UTF8_MAX_CONTINUATION
            ):
                # A multibyte character truncated at the chunk boundary: hold the tail.
                self._pending = buf[err.start :]
                return buf[: err.start].decode("utf-8")
            self.latched = True
            if self._on_latch is not None:
                self._on_latch(err.start)
            return buf.decode("latin-1").translate(CP1252_C1_TABLE)
