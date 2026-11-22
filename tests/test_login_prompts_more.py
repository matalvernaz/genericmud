"""Login prompts a MUD asks in ways the first heuristics missed.

Issue #126: ROM's stock "Name:" and Spanish MUDs' "Nombre:" / "Contraseña:" were never
answered by the prompts method, so the player sat at the name prompt.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.protocol.telnet import DataReceived
from genericmud.session.credentials import PlaintextCredentialStore
from genericmud.session.login import AutoLogin
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _prompts_app(tmp_path):
    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", "hero", "secret")
    sent: list[str] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        send=sent.append,
        post=[].append,
        credentials=store,
        keymap={},
        connect_method="prompts",
    )
    app.begin_login("gw")
    return app, sent


def _feed(lines: list[str]) -> list[str]:
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append)
    for line in lines:
        login.feed(line)
    return sent


# --- #126: the prompts it now knows ---------------------------------------------------------


def test_a_bare_name_prompt_and_password_prompt_log_in():
    assert _feed(["Welcome to the realm.", "Name:", "Password:"]) == ["hero", "secret"]


def test_the_spanish_prompts_log_in():
    assert _feed(["Bienvenido a Crisol.", "Nombre:", "Contraseña:"]) == ["hero", "secret"]
    assert _feed(["Nombre: ", "Contrasena:"]) == ["hero", "secret"]


def test_the_spanish_prompts_log_in_from_the_bytes_a_latin_1_mud_sends(tmp_path):
    # Crisol and most older Spanish MUDs send Latin-1: "ñ" is the single byte 0xF1, which the
    # automatic encoding reads as Windows Western European.
    app, sent = _prompts_app(tmp_path)
    app.on_telnet_event(DataReceived(b"Nombre:\r\n"))
    assert sent == ["hero"]
    app.on_telnet_event(DataReceived(b"Contrase\xf1a:\r\n"))
    assert sent == ["hero", "secret"]


def test_a_banner_heading_that_ends_in_name_does_not_send_the_login():
    assert _feed(["Options for changing your name:", "Password reminders: see help"]) == []


def test_a_line_ending_in_a_tagged_name_does_not_send_the_login():
    assert _feed(["To see someone, type finger <name>", "connect <name> <password>"]) == []


def test_a_spanish_sentence_about_names_does_not_send_the_login():
    lines = ["Tu nombre es importante: elige bien.", "La contraseña: nunca la compartas."]
    assert _feed(lines) == []
