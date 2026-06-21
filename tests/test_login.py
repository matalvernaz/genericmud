"""Credential store + heuristic auto-login (name/password prompt answering)."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.protocol.telnet import DataReceived
from genericmud.session.credentials import PlaintextCredentialStore
from genericmud.session.login import AutoLogin
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def test_credential_store_roundtrip_and_persist(tmp_path):
    path = tmp_path / "c.json"
    store = PlaintextCredentialStore(path)
    assert store.get("gw") is None
    store.set("gw", "hero", "secret")
    assert store.get("gw") == ("hero", "secret")
    assert PlaintextCredentialStore(path).get("gw") == ("hero", "secret")  # persisted to disk
    store.delete("gw")
    assert store.get("gw") is None


def test_autologin_password_only_sent_after_username():
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append)
    login.feed("the password vault is locked")  # 'password' mentioned pre-login
    assert sent == []  # username not sent yet, so this is ignored
    login.feed("What is your name?")
    assert sent == ["hero"]
    login.feed("Password:")
    assert sent == ["hero", "secret"]
    assert login.done


def test_autologin_sends_each_prompt_once():
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append)
    login.feed("enter your name")
    login.feed("enter your name")  # a repeated prompt does not resend
    assert sent == ["hero"]


def _app(store):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(voice, send=sent.append, post=[].append, credentials=store, keymap={})
    return app, sent


def test_autologin_answers_name_then_password(tmp_path):
    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", "hero", "secret")
    app, sent = _app(store)
    app.begin_login("gw")
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    assert sent == ["hero"]
    app.on_telnet_event(DataReceived(b"Password:\r\n"))
    assert sent == ["hero", "secret"]


def test_autologin_not_armed_without_credentials(tmp_path):
    app, sent = _app(PlaintextCredentialStore(tmp_path / "c.json"))  # empty store
    app.begin_login("gw")
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    assert sent == []


def test_on_connect_arms_login(tmp_path):
    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", "hero", "secret")
    app, sent = _app(store)
    app.on_connect("gw")  # packs=None, so this just arms login
    app.on_telnet_event(DataReceived(b"Enter your name: \r\n"))
    assert sent == ["hero"]
