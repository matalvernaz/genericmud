"""Credential store + heuristic auto-login (name/password prompt answering)."""

from __future__ import annotations

import keyring
import pytest

from genericmud.app import EngineApp
from genericmud.protocol.telnet import DataReceived
from genericmud.session import credentials
from genericmud.session.credentials import (
    KeyringCredentialStore,
    PlaintextCredentialStore,
    migrate_plaintext_to_keyring,
    open_credential_store,
)
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


def test_credential_store_ignores_malformed_rows(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(
        '{"number": 7, "missing": {"username": "hero"}, '
        '"wrong": {"username": 1, "password": false}}',
        encoding="utf-8",
    )
    store = PlaintextCredentialStore(path)
    assert store.get("number") is None
    assert store.get("missing") is None
    assert store.get("wrong") is None


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


def test_autologin_ignores_password_word_in_a_sentence():
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append)
    login.feed("What is your name?")
    assert sent == ["hero"]
    login.feed("Never share your password with anyone.")  # a banner, not a prompt
    login.feed("Welcome, hero!")
    assert sent == ["hero"]  # the password was NOT leaked as a command
    assert not login.done


def test_autologin_window_expires_after_name():
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append, max_lines=3)
    login.feed("Enter your name:")
    assert sent == ["hero"]
    for _ in range(3):
        login.feed("just some game output")
    assert login.done  # stopped watching
    login.feed("Password:")  # arrives too late to matter
    assert sent == ["hero"]


def test_autologin_recognizes_real_password_prompt_variants():
    for prompt in ("Password:", "password >", "Your passphrase?", "PASSWORD"):
        sent: list[str] = []
        login = AutoLogin("hero", "secret", sent.append)
        login.feed("what is your name")
        login.feed(prompt)
        assert sent == ["hero", "secret"], prompt


def _app(store):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    # These tests are about answering prompts, which is no longer what a world does by
    # default -- a world now has to be told to log in at all.
    app = EngineApp(
        voice,
        send=sent.append,
        post=[].append,
        credentials=store,
        keymap={},
        connect_method="prompts",
    )
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


def test_autologin_name_watch_gives_up_after_a_budget():
    # If the name prompt never matches (misconfigured/wrong world), stop watching instead
    # of lurking all session -- else a later in-game "by what name..." line sends the name.
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append, max_lines_to_user=5)
    for _ in range(5):
        login.feed("some banner or menu line")
    assert login.done
    login.feed("By what name do the villagers call you?")  # 500 lines into play
    assert sent == []


def test_autologin_disarms_on_user_input(tmp_path):
    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", "hero", "secret")
    app, sent = _app(store)
    app.begin_login("gw")
    app._dispatch_command("look")  # the user is driving login/play themselves
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    assert "hero" not in sent  # auto-login stood down


def test_password_input_is_masked_in_the_log_while_server_echoes(tmp_path):
    from genericmud.protocol import telnet as T

    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(voice, send=sent.append, post=[].append, keymap={},
                    name="gw", log_dir=tmp_path)
    app._toggle_log()  # start logging
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))  # server takes over echo
    app._dispatch_command("hunter2")  # the password
    app.on_telnet_event(T.Negotiation(T.WONT, T.OPT_ECHO))  # server releases echo
    app._dispatch_command("look")
    log_text = next(app.log_dir.glob("gw-*.log")).read_text(encoding="utf-8")
    assert "hunter2" not in log_text
    assert "> ***" in log_text
    assert "> look" in log_text  # normal input still logged in the clear


def _app_at_a_password_prompt():
    """An engine whose server has just taken over echo, the way it does for a password."""
    from genericmud.protocol import telnet as T

    sent: list[str] = []
    posted: list[dict] = []
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap={})
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    return app, sent, posted


def test_auto_repeat_never_swaps_the_password_for_the_last_command():
    from genericmud.protocol import telnet as T

    sent: list[str] = []
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    app = EngineApp(voice, send=sent.append, post=[].append, keymap={})
    app.autoretype = True
    app.on_ws_message({"type": "input", "text": "hero"})  # the username, typed in the clear
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    app.on_ws_message({"type": "input", "text": "hunter2"})
    assert sent == ["hero", "hunter2"]


def test_a_password_goes_out_exactly_as_typed():
    app, sent, _posted = _app_at_a_password_prompt()
    aliased: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: aliased.append(ctx.line.plain_text))
    for password in ("abc;def", ".2news", "..3n", "hunter2", "n"):
        app.on_ws_message({"type": "input", "text": password})
    assert sent == ["abc;def", ".2news", "..3n", "hunter2", "n"]
    assert aliased == []
    assert app.nav.trail == []  # a password that spells a direction is not a step taken


def test_typed_input_goes_back_through_the_pipeline_once_the_server_echoes_again():
    from genericmud.protocol import telnet as T

    app, sent, _posted = _app_at_a_password_prompt()
    app.on_telnet_event(T.Negotiation(T.WONT, T.OPT_ECHO))
    app.on_ws_message({"type": "input", "text": "look;.2n"})
    assert sent == ["look", "n", "n"]


def test_the_renderer_hears_when_input_is_hidden_and_when_it_is_not():
    from genericmud.bridge import protocol
    from genericmud.protocol import telnet as T

    app, _sent, posted = _app_at_a_password_prompt()
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))  # a repeat changes nothing
    app.on_telnet_event(T.Negotiation(T.WONT, T.OPT_ECHO))
    flips = [message for message in posted if message["type"] == protocol.INPUT_MASKED]
    assert flips == [protocol.input_masked(True), protocol.input_masked(False)]


def test_a_dropped_connection_takes_the_hidden_input_with_it():
    from genericmud.bridge import protocol

    app, sent, posted = _app_at_a_password_prompt()
    app.on_connection_status("connection lost; reconnecting in 2s (attempt 1)")
    app.on_ws_message({"type": "input", "text": "look;look"})
    assert sent == ["look", "look"]  # split again: it's an ordinary command now
    assert posted.count(protocol.input_masked(False)) == 1


def test_the_server_asking_us_to_echo_leaves_a_password_hidden():
    # DO/DONT ECHO is about whether the CLIENT echoes. Treating DO like WONT unhid the
    # password: it was split on the separator, logged and kept for auto-repeat.
    from genericmud.protocol import telnet as T

    app, sent, _posted = _app_at_a_password_prompt()
    app.on_telnet_event(T.Negotiation(T.DO, T.OPT_ECHO))
    app.on_telnet_event(T.Negotiation(T.DONT, T.OPT_ECHO))
    app.on_ws_message({"type": "input", "text": "abc;def"})
    assert sent == ["abc;def"]


def _server_lines(app, count):
    from genericmud.protocol.telnet import DataReceived

    for number in range(count):
        app.on_telnet_event(DataReceived(f"Line {number} of the welcome text.\r\n".encode()))


def test_chat_while_a_password_prompt_is_open_does_not_unhide_it():
    # Changing a password on a busy LPMud: old password typed, then channel chatter and
    # combat keep arriving while the new-password prompt waits. Output says nothing about
    # whether the prompt is still open, so it must not end the hiding.
    app, sent, _posted = _app_at_a_password_prompt()
    app.on_ws_message({"type": "input", "text": "old;pass"})
    _server_lines(app, 60)
    app.on_ws_message({"type": "input", "text": "new;pass"})
    assert sent == ["old;pass", "new;pass"]


def test_a_retyped_password_stays_hidden_through_a_few_prompts():
    # ROM-style new character: one WILL ECHO, then the password, the retype, and a retry
    # loop if they don't match. A few prompt lines in between must not let go.
    app, sent, _posted = _app_at_a_password_prompt()
    app.on_ws_message({"type": "input", "text": "abc;def"})
    _server_lines(app, 2)  # "Passwords don't match." "Retype password:"
    app.on_ws_message({"type": "input", "text": "abc;deg"})
    assert sent == ["abc;def", "abc;deg"]


class _Clock:
    """Time that only moves when the test says so, firing the callbacks due by then."""

    def __init__(self) -> None:
        self.now = 0.0
        self.pending: list[tuple[float, object]] = []

    def schedule(self, delay, callback) -> None:
        self.pending.append((self.now + delay, callback))

    def advance(self, seconds: float) -> None:
        self.now += seconds
        due = sorted((entry for entry in self.pending if entry[0] <= self.now), key=lambda e: e[0])
        for entry in due:
            self.pending.remove(entry)
            entry[1]()


def _timed_app(**kwargs):
    from genericmud.config.keymap import load_keymap

    clock = _Clock()
    sent: list[str] = []
    withdrawn: list[int] = []
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    app = EngineApp(
        voice, send=sent.append, post=[].append, keymap=load_keymap("vipmud"),
        schedule=clock.schedule, clock=lambda: clock.now, withdraw_option=withdrawn.append,
        **kwargs,
    )
    return app, clock, sent, backend, withdrawn


def _hide(app) -> None:
    from genericmud.protocol import telnet as T

    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))


def test_escape_while_the_mud_hides_typing_makes_it_ordinary_again():
    # Mudlet's hidden-input box: Escape on it goes back to the ordinary command line until
    # the MUD hides input again. The way out when a MUD never sends WONT ECHO.
    app, _clock, sent, backend, _withdrawn = _timed_app()
    _hide(app)
    app.on_ws_message({"type": "key", "key": "escape"})
    app.on_ws_message({"type": "input", "text": "look;look"})
    assert sent == ["look", "look"]
    assert "Typing is no longer hidden." in backend.spoken


def test_escape_no_longer_stops_speech_and_does_nothing_while_typing_shows():
    # Screen readers stop speech with Ctrl, and genericMud speaks through them; F11 stays.
    from genericmud.config.keymap import load_keymap

    keymap = load_keymap("vipmud")
    assert keymap["escape"] == "input:unhide"
    assert keymap["f11"] == "voice:flush"
    app, _clock, sent, backend, _withdrawn = _timed_app()
    app.on_ws_message({"type": "key", "key": "escape"})
    assert backend.spoken == [] and sent == []


def test_a_login_password_prompt_the_mud_never_ends_stops_hiding_after_a_minute():
    from genericmud.protocol import telnet as T

    app, clock, sent, backend, withdrawn = _timed_app()
    _hide(app)
    clock.advance(5)
    app.on_ws_message({"type": "input", "text": "hunter2"})
    clock.advance(59)
    app.on_ws_message({"type": "input", "text": "still;hidden"})  # restarts the minute
    clock.advance(59)
    assert sent == ["hunter2", "still;hidden"]
    clock.advance(2)
    app.on_ws_message({"type": "input", "text": "look;look"})
    assert sent[-2:] == ["look", "look"]
    assert withdrawn == [T.OPT_ECHO]  # told to the MUD, so its next WILL ECHO is fresh
    assert any("stopped hiding" in spoken for spoken in backend.spoken)


def test_a_password_prompt_later_in_the_session_never_times_out():
    # Changing a password in-game: only the MUD's WONT ECHO or Escape ends the hiding.
    app, clock, sent, _backend, withdrawn = _timed_app()
    clock.advance(400)
    _hide(app)
    app.on_ws_message({"type": "input", "text": "old;pass"})
    clock.advance(3600)
    app.on_ws_message({"type": "input", "text": "new;pass"})
    assert sent == ["old;pass", "new;pass"]
    assert withdrawn == []


def test_an_auto_login_password_sent_before_the_prompt_hides_still_gets_the_minute(tmp_path):
    # On a slow link auto-login answers the password prompt before the WILL ECHO arrives.
    from genericmud.protocol import telnet as T

    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", "hero", "secret")
    app, clock, sent, _backend, withdrawn = _timed_app(
        credentials=store, name="gw", connect_method="prompts"
    )
    app.begin_login("gw")
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    app.on_telnet_event(DataReceived(b"Password:\r\n"))
    assert sent == ["hero", "secret"]
    _hide(app)
    clock.advance(61)
    assert withdrawn == [T.OPT_ECHO]


def test_the_second_line_typed_while_hidden_mentions_escape_once():
    app, _clock, _sent, backend, _withdrawn = _timed_app()
    _hide(app)
    for answer in ("one", "two", "three"):
        app.on_ws_message({"type": "input", "text": answer})
    hints = [spoken for spoken in backend.spoken if "Escape" in spoken]
    assert len(hints) == 1



# --- connect methods and after-connect commands -------------------------------


def _app_with(store, *, method="prompts", commands=""):
    """An engine for one world's login: no real timers, so the fallback can't fire."""
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(
        voice,
        send=sent.append,
        post=[].append,
        credentials=store,
        keymap={},
        name="gw",
        connect_method=method,
        post_connect_commands=commands,
        schedule=lambda _delay, _callback: None,
    )
    return app, sent


def _store_with_login(tmp_path, username="hero", password="secret"):
    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", username, password)
    return store


def test_mush_method_sends_one_connect_line_then_the_commands(tmp_path):
    # MUSHclient's MUSH/MOO habit: a single CONNECT command carrying both.
    app, sent = _app_with(_store_with_login(tmp_path), method="mush", commands="look\nscore")
    app.begin_login("gw")
    app.after_connect()
    assert sent == ["CONNECT hero secret", "look", "score"]


def test_a_blind_login_waits_for_the_socket_to_open(tmp_path):
    # on_connect runs before the transport connects, so anything typed at begin_login time
    # went into a socket that did not exist yet: _send raised ConnectionError and the line
    # vanished without a word. Every up-front login has to wait for the socket.
    app, sent = _app_with(_store_with_login(tmp_path), method="mush", commands="look")
    app.begin_login("gw")
    assert sent == []  # nothing may leave before the connection is up
    app.after_connect()
    assert sent == ["CONNECT hero secret", "look"]


def test_diku_method_sends_name_then_password_then_the_commands(tmp_path):
    app, sent = _app_with(_store_with_login(tmp_path), method="diku", commands="who")
    app.begin_login("gw")
    app.after_connect()
    assert sent == ["hero", "secret", "who"]


def test_no_auto_connect_sends_the_commands_but_never_the_login(tmp_path):
    # A stored login under "none" is deliberately ignored: that is what the mode means.
    app, sent = _app_with(_store_with_login(tmp_path), method="none", commands="look")
    app.begin_login("gw")
    app.after_connect()
    assert sent == ["look"]


def test_prompt_login_holds_the_commands_until_the_password_is_away(tmp_path):
    app, sent = _app_with(_store_with_login(tmp_path), commands="look")
    app.begin_login("gw")
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    assert sent == ["hero"]  # the command must not go before the login finishes
    app.on_telnet_event(DataReceived(b"Password:\r\n"))
    assert sent == ["hero", "secret", "look"]


def test_after_connect_commands_are_typed_once_per_connection(tmp_path):
    app, sent = _app_with(_store_with_login(tmp_path), commands="look")
    app.begin_login("gw")
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    app.on_telnet_event(DataReceived(b"Password:\r\n"))
    app._send_post_connect()  # a fallback timer firing late must not repeat them
    assert sent.count("look") == 1


def test_a_reconnect_types_the_after_connect_commands_again(tmp_path):
    # "Sent after connected" means every connection, which is what a world expects after a
    # drop as much as on the first login.
    app, sent = _app_with(_store_with_login(tmp_path), commands="look")
    app.begin_login("gw")
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    app.on_telnet_event(DataReceived(b"Password:\r\n"))
    app.after_reconnect()
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    app.on_telnet_event(DataReceived(b"Password:\r\n"))
    assert sent.count("look") == 2


def test_no_stored_login_still_sends_the_after_connect_commands(tmp_path):
    app, sent = _app_with(PlaintextCredentialStore(tmp_path / "c.json"), commands="look")
    app.begin_login("gw")
    app.after_connect()  # no login to wait for, so the commands go as soon as it is up
    assert sent == ["look"]


# --- the encrypted store -----------------------------------------------------


class _MemoryKeyring(keyring.backend.KeyringBackend):
    """A credential store in a dict. The suite must never touch the real one."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.entries:
            raise keyring.errors.PasswordDeleteError(username)
        del self.entries[(service, username)]


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    backend = _MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


def test_keyring_store_round_trip(memory_keyring):
    store = KeyringCredentialStore()
    assert store.get("gw") is None
    store.set("gw", "hero", "secret")
    assert store.get("gw") == ("hero", "secret")
    assert memory_keyring.entries  # it really went through the backend
    store.delete("gw")
    assert store.get("gw") is None
    store.delete("gw")  # deleting twice must not raise


def test_login_migration_moves_the_plaintext_file_into_the_store(tmp_path, memory_keyring):
    path = tmp_path / "credentials.json"
    PlaintextCredentialStore(path).set("gw", "hero", "secret")
    store = KeyringCredentialStore()
    assert migrate_plaintext_to_keyring(path, store) == 1
    assert store.get("gw") == ("hero", "secret")
    assert not path.exists()  # the readable copy goes once it is safely moved


def test_a_corrupt_plaintext_file_is_left_alone_not_half_migrated(tmp_path, memory_keyring):
    path = tmp_path / "credentials.json"
    path.write_text("{not json", encoding="utf-8")
    assert migrate_plaintext_to_keyring(path, KeyringCredentialStore()) == 0
    assert path.exists()  # nothing moved, so nothing deleted


def test_no_credential_store_falls_back_and_says_why(tmp_path, monkeypatch):
    # The reason reaches the speaker, so it has to be a sentence, not an exception repr.
    monkeypatch.setattr(credentials, "_usable_keyring", lambda: (None, "no store here"))
    store, reason = open_credential_store(tmp_path / "credentials.json")
    assert isinstance(store, PlaintextCredentialStore)
    assert reason == "no store here"


def test_a_working_credential_store_is_preferred(tmp_path, memory_keyring):
    store, reason = open_credential_store(tmp_path / "credentials.json")
    assert isinstance(store, KeyringCredentialStore)
    assert reason is None


def test_a_blind_login_is_never_typed_into_a_closed_socket(tmp_path):
    # The real transport raises ConnectionError from send_line until connect() has run, and
    # the window swallows that, so a login typed too early vanishes without a word. This
    # fake refuses in exactly the same way: anything sent before after_connect() is a bug.
    sent: list[str] = []
    socket_open = {"value": False}

    def send(text: str) -> None:
        if not socket_open["value"]:
            raise ConnectionError("not connected")
        sent.append(text)

    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    app = EngineApp(
        voice,
        send=send,
        post=[].append,
        credentials=_store_with_login(tmp_path),
        keymap={},
        name="gw",
        connect_method="mush",
        post_connect_commands="look",
        schedule=lambda _delay, _callback: None,
    )
    app.begin_login("gw")  # on_connect time: the socket is not open yet
    assert sent == []
    socket_open["value"] = True  # the transport has connected
    app.after_connect()
    assert sent == ["CONNECT hero secret", "look"]


def test_an_echoed_password_is_hidden_from_the_output(tmp_path):
    # A blind login goes out before the server has switched echo off, so a server that echoes
    # what it reads sends the password straight back. It must never reach the output box, the
    # screen reader or the session log.
    app, _sent = _app_with(_store_with_login(tmp_path), method="diku")
    app.begin_login("gw")
    app.after_connect()
    app.on_telnet_event(DataReceived(b"Password: secret\r\n"))
    assert app.buffer.lines()[-1].plain_text == "Password: ***"


def test_the_redaction_matches_whole_words_only():
    from genericmud.app import _redact_secret

    assert _redact_secret("Password: hunter2", "hunter2") == "Password: ***"
    assert _redact_secret("CONNECT hero hunter2", "hunter2") == "CONNECT hero ***"
    # A one-character password must not redact half the output.
    assert _redact_secret("a whole line of it", "a") == "*** whole line of it"
    # Part of a word is not the password.
    assert _redact_secret("hunter22 and hunter2x", "hunter2") == "hunter22 and hunter2x"
    assert _redact_secret("nothing to do", "") == "nothing to do"
