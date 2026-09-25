"""Per-world character encodings (issue #5): decoding, encoding, persistence, handshake."""

from __future__ import annotations

import argparse
import json
import zipfile

import pytest

from genericmud.__main__ import _parse_args
from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.config.worlds import World, load_worlds, save_worlds
from genericmud.packs.world_share import export_world, import_world
from genericmud.protocol import telnet as T
from genericmud.protocol.charset import (
    AUTO,
    ENCODING_CHOICES,
    ServerTextCodec,
    encoding_label,
    normalize_encoding,
)
from genericmud.protocol.telnet import DataReceived, Subnegotiation, TelnetParser
from genericmud.transport.connection import MTTS_BITS, MTTS_UTF8, MudConnection
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

PRIVET = "Привет, мир"  # "Hello, world"
PRIVET_CP1251 = PRIVET.encode("cp1251")
PRIVET_KOI8R = PRIVET.encode("koi8-r")


class _FakeWriter:
    def __init__(self) -> None:
        self.sent = bytearray()

    def write(self, data: bytes) -> None:
        self.sent.extend(data)

    def is_closing(self) -> bool:
        return False


class _RecordingDiag:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, stage: str, **fields: object) -> None:
        self.events.append((stage, fields))


def _app(encoding: str = AUTO, diag=None) -> EngineApp:
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    return EngineApp(voice, keymap=load_keymap("vipmud"), encoding=encoding, diag=diag)


def _last_line(app: EngineApp) -> str:
    return app.buffer.lines()[-1].plain_text


# --- the setting ---


@pytest.mark.parametrize(
    ("spelling", "setting"),
    [
        ("cp1251", "cp1251"),
        ("Windows-1251", "cp1251"),
        ("KOI8_R", "koi8-r"),
        ("utf8", "utf-8"),
        ("latin-1", "iso-8859-1"),
        ("gbk", "gb18030"),  # GB18030 is the superset offered for GBK MUDs
        ("shift_jis", "cp932"),
        ("", AUTO),
        ("auto", AUTO),
        ("no-such-codec", AUTO),
        ("cp1255", AUTO),  # a real codec, but not one the dialog offers
        (None, AUTO),
        (1251, AUTO),
    ],
)
def test_normalize_accepts_any_spelling_of_an_offered_encoding(spelling, setting):
    assert normalize_encoding(spelling) == setting


def test_every_offered_encoding_has_a_working_codec_and_a_label():
    for value, label in ENCODING_CHOICES:
        assert normalize_encoding(value) == value
        assert encoding_label(value) == label
        ServerTextCodec(value).decode(b"ascii stays ascii")


# --- decoding ---


def test_cyrillic_worlds_read_what_the_mud_sent():
    assert ServerTextCodec("cp1251").decode(PRIVET_CP1251) == PRIVET
    assert ServerTextCodec("koi8-r").decode(PRIVET_KOI8R) == PRIVET


def test_auto_garbles_cyrillic_which_is_why_the_setting_exists():
    # Without the setting, a Windows-1251 MUD latches the Western European fallback and
    # every Cyrillic letter comes out as an accented Latin one.
    garbled = ServerTextCodec(AUTO).decode(PRIVET_CP1251)
    assert garbled != PRIVET
    assert garbled == PRIVET_CP1251.decode("latin-1")


def test_explicit_utf8_never_latches_on_one_bad_byte():
    # Auto treats the first invalid byte as proof of a legacy MUD and mis-reads the rest of
    # the session; a world set to UTF-8 replaces that byte and keeps reading UTF-8.
    latched: list[int] = []
    codec = ServerTextCodec("utf-8", on_latch=latched.append)
    assert codec.decode(b"bad \xff byte") == "bad � byte"
    assert codec.decode("café".encode()) == "café"
    assert latched == []


def test_explicit_multibyte_character_split_across_chunks():
    for encoding, text in (("utf-8", "é"), ("gb18030", "中文"), ("big5", "中文")):
        data = text.encode(encoding)
        codec = ServerTextCodec(encoding)
        assert codec.decode(data[:1]) + codec.decode(data[1:]) == text


def test_app_decodes_in_the_world_encoding_and_never_latches():
    diag = _RecordingDiag()
    app = _app("koi8-r", diag=diag)
    app.on_telnet_event(DataReceived(PRIVET_KOI8R + b"\r\n"))
    assert _last_line(app) == PRIVET
    assert not [stage for stage, _fields in diag.events if stage == "encoding.latch"]


def test_auto_app_still_latches_and_traces_it():
    diag = _RecordingDiag()
    app = _app(AUTO, diag=diag)
    app.on_telnet_event(DataReceived(b"caf\xe9\r\n"))
    assert _last_line(app) == "café"
    assert ("encoding.latch", {"encoding": "cp1252", "at": 3}) in diag.events


def test_ya_arrives_doubled_and_reads_as_ya():
    # 0xFF is the letter я in Windows-1251 and also telnet's IAC, so a correct server sends
    # it doubled. The parser undoes that; the world's codec turns the byte into the letter.
    events = TelnetParser().receive("моя".encode("cp1251").replace(b"\xff", b"\xff\xff") + b"\r\n")
    app = _app("cp1251")
    for event in events:
        app.on_telnet_event(event)
    assert _last_line(app) == "моя"


def test_reconnect_starts_a_fresh_stream():
    codec = ServerTextCodec("utf-8")
    codec.decode("é".encode()[:1])  # half a character left pending by a drop
    codec.reset()
    assert codec.decode(b"ok") == "ok"


# --- encoding what the player types ---


def test_commands_go_out_in_the_world_encoding_with_ya_doubled():
    conn = MudConnection()
    writer = _FakeWriter()
    conn._writer = writer
    conn.text_codec = ServerTextCodec("cp1251")
    conn.send_line("моя")
    assert bytes(writer.sent) == "мо".encode("cp1251") + b"\xff\xff\r\n"


def test_koi8r_command_bytes():
    conn = MudConnection()
    writer = _FakeWriter()
    conn._writer = writer
    conn.text_codec = ServerTextCodec("koi8-r")
    conn.send_line("привет")
    assert bytes(writer.sent) == "привет".encode("koi8-r") + b"\r\n"


def test_a_character_the_encoding_lacks_is_sent_as_a_question_mark():
    # Typing (or pasting) something the MUD's encoding can't hold must not raise out of
    # the input path and lose the whole command.
    assert ServerTextCodec("cp1251").encode("hi 中") == b"hi ?"
    assert ServerTextCodec("koi8-r").encode("naïve") == b"na?ve"


def test_connection_without_a_codec_still_sends_utf8():
    conn = MudConnection()
    writer = _FakeWriter()
    conn._writer = writer
    conn.send_line("café \ud800")  # a lone surrogate can't raise out of send_line either
    assert bytes(writer.sent) == "café ?".encode() + b"\r\n"


# --- the terminal-type handshake ---


def _mtts_reply(codec: ServerTextCodec | None) -> bytes:
    conn = MudConnection()
    writer = _FakeWriter()
    conn._writer = writer
    conn.text_codec = codec
    for _ in range(3):
        writer.sent.clear()
        conn._dispatch(Subnegotiation(T.OPT_TTYPE, bytes([1])))
    return bytes(writer.sent)


def test_mtts_claims_utf8_only_when_the_world_reads_utf8():
    assert f"MTTS {MTTS_BITS}".encode() in _mtts_reply(None)
    assert f"MTTS {MTTS_BITS}".encode() in _mtts_reply(ServerTextCodec(AUTO))
    assert f"MTTS {MTTS_BITS}".encode() in _mtts_reply(ServerTextCodec("utf-8"))
    koi8 = _mtts_reply(ServerTextCodec("koi8-r"))
    assert f"MTTS {MTTS_BITS & ~MTTS_UTF8}".encode() in koi8
    assert f"MTTS {MTTS_BITS}".encode() not in koi8


# --- persistence ---


def test_worlds_file_round_trips_the_encoding(tmp_path):
    path = tmp_path / "worlds.toml"
    save_worlds(
        [World("Adamant", "adamant.example", 4000, encoding="koi8-r"),
         World("Plain", "plain.example", 23)],
        path,
    )
    text = path.read_text(encoding="utf-8")
    assert 'encoding = "koi8-r"' in text
    assert text.count("encoding") == 1  # auto is the default and isn't written
    worlds = {world.name: world for world in load_worlds(path)}
    assert worlds["Adamant"].encoding == "koi8-r"
    assert worlds["Plain"].encoding == AUTO


def test_a_bad_encoding_in_the_file_means_auto_not_a_lost_world(tmp_path):
    path = tmp_path / "worlds.toml"
    path.write_text(
        '[[world]]\nname = "Typo"\nhost = "typo.example"\nport = 23\nencoding = "cp9999"\n\n'
        '[[world]]\nname = "Number"\nhost = "n.example"\nport = 23\nencoding = 1251\n',
        encoding="utf-8",
    )
    worlds = load_worlds(path)
    assert [(world.name, world.encoding) for world in worlds] == [
        ("Typo", AUTO), ("Number", AUTO),
    ]


def test_shared_world_zip_carries_the_encoding(tmp_path):
    zip_path = tmp_path / "shared.zip"
    export_world(World("Bylins", "bylins.example", 4000, encoding="cp1251"), None, zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        assert json.loads(archive.read("world.json"))["encoding"] == "cp1251"
    imported = import_world(zip_path, tmp_path / "userpacks")
    assert imported.encoding == "cp1251"


def test_a_shared_world_from_an_older_build_imports_as_auto(tmp_path):
    zip_path = tmp_path / "old.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("world.json", json.dumps({"name": "Old", "host": "old.example",
                                                   "port": 23, "tls": False}))
    assert import_world(zip_path, tmp_path / "userpacks").encoding == AUTO


# --- command line ---


def test_command_line_takes_an_encoding():
    assert _parse_args(["mud.example", "4000", "--encoding", "Windows-1251"]).encoding == "cp1251"
    assert _parse_args(["mud.example"]).encoding == AUTO


def test_command_line_rejects_an_unknown_encoding(capsys):
    with pytest.raises(SystemExit):
        _parse_args(["mud.example", "--encoding", "klingon"])
    assert "unsupported encoding" in capsys.readouterr().err


def test_encoding_arg_is_an_argparse_type():
    from genericmud.__main__ import _encoding_arg

    with pytest.raises(argparse.ArgumentTypeError):
        _encoding_arg("klingon")
    assert _encoding_arg("auto") == AUTO


def test_auto_sends_utf8_until_the_mud_proves_legacy_then_windows_1252():
    # A Latin-1 MUD reads a typed "señor" sent as UTF-8 as "seÃ±or": once the latch has
    # shown the MUD isn't UTF-8, commands have to go out the way its output comes in.
    codec = ServerTextCodec(AUTO)
    assert codec.encode("señor") == "señor".encode()
    codec.decode(b"caf\xe9\r\n")  # a Latin-1 MUD: the latch
    assert codec.encode("señor") == "señor".encode("cp1252")
    codec.reset()  # reconnected: UTF-8 again until proven otherwise
    assert codec.encode("señor") == "señor".encode()


# --- text inside GMCP, MSDP and MSSP payloads ---


def test_a_legacy_muds_msdp_room_name_reads_in_the_world_encoding():
    from genericmud.protocol import msdp

    app = _app("koi8-r")
    payload = bytes([msdp.MSDP_VAR]) + b"ROOM_NAME" + bytes([msdp.MSDP_VAL]) + "Площадь".encode(
        "koi8-r"
    )
    app.on_telnet_event(Subnegotiation(T.OPT_MSDP, payload))
    assert app.engine.get_mud_var("ROOM_NAME") == "Площадь"


def test_gmcp_stays_utf8_even_on_a_legacy_world():
    # GMCP is UTF-8 by spec; a compliant server on a KOI8-R world still sends UTF-8 JSON.
    app = _app("koi8-r")
    app.on_telnet_event(Subnegotiation(T.OPT_GMCP, 'Room.Info {"name":"Площадь"}'.encode()))
    assert app.engine.get_mud_var("Room.Info.name") == "Площадь"


def test_a_non_utf8_gmcp_payload_falls_back_to_the_world_encoding():
    app = _app("cp1251")
    app.on_telnet_event(
        Subnegotiation(T.OPT_GMCP, 'Room.Info {"name":"Площадь"}'.encode("cp1251"))
    )
    assert app.engine.get_mud_var("Room.Info.name") == "Площадь"


def test_auto_reads_a_latin1_mssp_name_instead_of_replacement_characters():
    app = _app(AUTO)
    app.on_telnet_event(Subnegotiation(T.OPT_MSSP, b"\x01NAME\x02Caf\xe9 del Mar"))
    assert app.engine.get_mud_var("NAME") == "Café del Mar"
    assert not app.codec.latched  # a payload never moves the text stream's latch
