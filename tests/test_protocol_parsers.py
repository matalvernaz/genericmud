"""Unit tests for GMCP, MSDP, MSSP, MSP parsers and OOB normalization."""

from __future__ import annotations

from genericmud.protocol import telnet as T
from genericmud.protocol.gmcp import GmcpMessage, parse_gmcp
from genericmud.protocol.msdp import (
    MSDP_ARRAY_CLOSE,
    MSDP_ARRAY_OPEN,
    MSDP_TABLE_CLOSE,
    MSDP_TABLE_OPEN,
    MSDP_VAL,
    MSDP_VAR,
    parse_msdp,
)
from genericmud.protocol.msp import parse_msp_line
from genericmud.protocol.mssp import MSSP_VAL, MSSP_VAR, parse_mssp
from genericmud.protocol.oob import OobMessage, ServerStatus, from_subnegotiation


def _wire(*items: int | str) -> bytes:
    out = bytearray()
    for item in items:
        if isinstance(item, int):
            out.append(item)
        else:
            out += item.encode("utf-8")
    return bytes(out)


# --- GMCP ---

def test_gmcp_package_with_json():
    assert parse_gmcp(b'Char.Vitals {"hp":42,"mp":7}') == GmcpMessage(
        "Char.Vitals", {"hp": 42, "mp": 7}
    )


def test_gmcp_package_without_body():
    assert parse_gmcp(b"Core.Ping") == GmcpMessage("Core.Ping", None)


def test_gmcp_bad_json_keeps_raw_body():
    msg = parse_gmcp(b"Weird.Thing not-json")
    assert msg.package == "Weird.Thing"
    assert msg.data == "not-json"


# --- MSDP ---

def test_msdp_flat_vars():
    payload = _wire(MSDP_VAR, "HEALTH", MSDP_VAL, "42", MSDP_VAR, "MAXHP", MSDP_VAL, "100")
    assert parse_msdp(payload) == {"HEALTH": "42", "MAXHP": "100"}


def test_msdp_nested_table_and_array():
    payload = _wire(
        MSDP_VAR, "ROOM", MSDP_VAL, MSDP_TABLE_OPEN,
        MSDP_VAR, "VNUM", MSDP_VAL, "123",
        MSDP_VAR, "EXITS", MSDP_VAL, MSDP_ARRAY_OPEN,
        MSDP_VAL, "north", MSDP_VAL, "south",
        MSDP_ARRAY_CLOSE,
        MSDP_TABLE_CLOSE,
    )
    assert parse_msdp(payload) == {"ROOM": {"VNUM": "123", "EXITS": ["north", "south"]}}


# --- MSSP ---

def test_mssp_flat_and_repeated():
    payload = _wire(
        MSSP_VAR, "NAME", MSSP_VAL, "GenericSpace",
        MSSP_VAR, "PLAYERS", MSSP_VAL, "7",
        MSSP_VAR, "WORLD", MSSP_VAL, "a",
        MSSP_VAR, "WORLD", MSSP_VAL, "b",
    )
    assert parse_mssp(payload) == {"NAME": "GenericSpace", "PLAYERS": "7", "WORLD": ["a", "b"]}


# --- MSP ---

def test_msp_sound_and_music_extracted():
    clean, cues = parse_msp_line("A thud !!SOUND(hit.wav V=80 P=30) and !!MUSIC(theme.mid L=-1)!")
    assert "!!SOUND" not in clean and "!!MUSIC" not in clean
    assert cues[0].kind == "sound" and cues[0].file == "hit.wav" and cues[0].volume == 80
    assert cues[0].priority == 30
    assert cues[1].kind == "music" and cues[1].file == "theme.mid" and cues[1].repeats == -1


def test_msp_line_without_tags_is_unchanged():
    clean, cues = parse_msp_line("just plain text")
    assert clean == "just plain text"
    assert cues == []


# --- OOB normalization ---

def test_oob_gmcp_normalizes_to_single_message():
    result = from_subnegotiation(T.OPT_GMCP, b'Char.Vitals {"hp":42}')
    assert result == [OobMessage("Char.Vitals", {"hp": 42}, "gmcp")]


def test_oob_msdp_normalizes_per_variable():
    payload = _wire(MSDP_VAR, "HEALTH", MSDP_VAL, "42", MSDP_VAR, "MAXHP", MSDP_VAL, "100")
    result = from_subnegotiation(T.OPT_MSDP, payload)
    assert result == [
        OobMessage("HEALTH", "42", "msdp"),
        OobMessage("MAXHP", "100", "msdp"),
    ]


def test_oob_mssp_normalizes_to_server_status():
    payload = _wire(MSSP_VAR, "NAME", MSSP_VAL, "GenericSpace")
    assert from_subnegotiation(T.OPT_MSSP, payload) == ServerStatus({"NAME": "GenericSpace"})


def test_oob_unknown_option_returns_none():
    assert from_subnegotiation(T.OPT_MXP, b"<send>") is None
