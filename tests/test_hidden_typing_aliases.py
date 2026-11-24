"""Typing while a MUD hides it still reaches the soundpack, unless a password prompt is involved.

Issue #132: Erion's soundpack commands ("volup", "sound music") sometimes went to the game, which
answered "not a command", and the soundpack never heard them. While a MUD hides typing (IAC WILL
ECHO), genericMud sent every typed line straight to the game, past every alias. MUSHclient, which
these packs are written for, keeps running aliases while the MUD hides typing; it only stops
showing what was typed. Now a hidden line skips the aliases only when a password prompt belongs to
the hiding: the MUD's last words before it began, or anything it said since.
"""

from __future__ import annotations

import pytest

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.protocol import telnet as T
from genericmud.protocol.telnet import DataReceived
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend, RecordingDiag

# Two of Erion's soundpack commands, cut down: the real ones change a volume or a setting.
SOUNDPACK = """<?xml version="1.0"?>
<muclient><aliases>
 <alias match="volup" enabled="y" send_to="12" sequence="100">
  <send>Note("Increasing sound effects volume to 40.")</send></alias>
 <alias match="^sound music$" enabled="y" regexp="y" ignore_case="y" send_to="12" sequence="100">
  <send>Note("Disabling music.")</send></alias>
</aliases></muclient>"""

# A pack that pretends the MUD said something, with Simulate.
PRETENDER = """<?xml version="1.0"?>
<muclient><aliases>
 <alias match="remind" enabled="y" send_to="12" sequence="100">
  <send>Simulate("Remember to change your password now and then.")</send></alias>
 <alias match="chatter" enabled="y" send_to="12" sequence="100">
  <send>Simulate("one") Simulate("two") Simulate("three") Simulate("four")</send></alias>
</aliases></muclient>"""


def _app(**kwargs):
    sent: list[str] = []
    posted: list[dict] = []
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    # The keys a player has out of the box, Escape's unhide among them.
    app = EngineApp(
        voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"), **kwargs,
    )
    return app, sent, posted


def _with_soundpack(**kwargs):
    app, sent, posted = _app(**kwargs)
    MushclientPack(ScriptApi(app.engine, source="mushclient")).load_source(SOUNDPACK)
    return app, sent, posted


def _server(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode("utf-8") + b"\r\n"))


def _hide(app: EngineApp) -> None:
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))


def _unhide(app: EngineApp) -> None:
    app.on_telnet_event(T.Negotiation(T.WONT, T.OPT_ECHO))


def _type(app: EngineApp, text: str) -> None:
    app.on_ws_message({"type": "input", "text": text})


def _shown(posted: list[dict]) -> list[str]:
    shown_kinds = (protocol.LINE, protocol.ECHO)
    return [message["text"] for message in posted if message["type"] in shown_kinds]


def test_a_soundpack_command_typed_while_the_mud_hides_typing_reaches_the_soundpack():
    app, sent, posted = _with_soundpack()
    _server(app, "Bellsong asks The Giver for a quest.")
    _hide(app)
    _type(app, "volup")
    _type(app, "sound music")
    assert sent == []  # the game never hears the soundpack's own commands
    assert "Increasing sound effects volume to 40." in _shown(posted)
    assert "Disabling music." in _shown(posted)


def test_a_hidden_line_no_alias_takes_still_goes_out_exactly_as_typed():
    app, sent, posted = _with_soundpack()
    _hide(app)
    for text in ("abc;def", ".2news", "..3n", "/map", "n"):
        _type(app, text)
    assert sent == ["abc;def", ".2news", "..3n", "/map", "n"]  # not split, walked or run here
    assert app.nav.trail == []
    assert not any(line.startswith(">") for line in _shown(posted))  # and never shown


def test_a_hidden_soundpack_command_is_not_kept_for_auto_repeat():
    app, sent, _posted = _with_soundpack()
    app.autoretype = True
    _type(app, "look")
    _hide(app)
    _type(app, "volup")
    _unhide(app)
    _type(app, "")  # a blank Enter repeats the last command typed in the open
    assert sent == ["look", "look"]


def test_a_blank_enter_while_hidden_goes_out_as_it_is():
    app, sent, _posted = _app()
    taken: list[str] = []
    app.engine.add_alias("^.*$", lambda ctx: taken.append(ctx.line.plain_text), regex=True)
    _hide(app)
    _type(app, "")
    assert sent == [""]  # how a pager or a "press return" prompt is answered
    assert taken == []


def test_the_password_a_prompt_asked_for_just_before_the_hiding_skips_every_alias():
    app, sent, _posted = _app()
    taken: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: taken.append(ctx.line.plain_text))
    _server(app, "Password:")
    _hide(app)
    _type(app, "hunter2")
    assert sent == ["hunter2"]
    assert taken == []


def test_a_password_prompt_that_arrives_after_the_hiding_began_counts_too():
    # ROM's "Password: " has no line ending: WILL ECHO, then the prompt, then GA flushes it.
    app, sent, _posted = _app()
    taken: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: taken.append(ctx.line.plain_text))
    _hide(app)
    app.on_telnet_event(DataReceived(b"Password: "))
    app.on_telnet_event(T.Command(T.GA))
    _type(app, "hunter2")
    assert sent == ["hunter2"]
    assert taken == []


def test_a_blank_line_between_the_prompt_and_the_hiding_does_not_lose_the_prompt():
    app, sent, _posted = _app()
    taken: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: taken.append(ctx.line.plain_text))
    _server(app, "Password:")
    _server(app, "")
    _hide(app)
    _type(app, "hunter2")
    assert sent == ["hunter2"]
    assert taken == []


def test_a_password_change_stays_away_from_the_aliases_through_chatter():
    # A busy LPMud: the new-password prompt waits while channel chatter keeps arriving.
    app, sent, _posted = _app()
    taken: list[str] = []
    app.engine.add_alias("new*", lambda ctx: taken.append(ctx.line.plain_text))
    _server(app, "Enter your old password:")
    _hide(app)
    _type(app, "old;pass")
    _server(app, "New password:")
    for number in range(60):
        _server(app, f"[chat] Someone says line {number}.")
    _type(app, "new;pass")
    assert sent == ["old;pass", "new;pass"]
    assert taken == []


def test_the_secret_ends_with_the_hiding_that_had_the_prompt():
    app, sent, posted = _with_soundpack()
    _server(app, "Password:")
    _hide(app)
    _type(app, "volup")  # this one is a password, whatever it spells
    _unhide(app)
    _server(app, "Welcome back to Erion.")
    _hide(app)
    _type(app, "volup")
    assert sent == ["volup"]  # only the password went to the game
    assert _shown(posted).count("Increasing sound effects volume to 40.") == 1


def test_the_diagnostic_log_records_the_hiding_but_never_what_was_typed():
    diag = RecordingDiag()
    app, _sent, _posted = _with_soundpack(diag=diag)
    _server(app, "Password:")
    _hide(app)
    _type(app, "hunter2")
    _unhide(app)
    _server(app, "Welcome back to Erion.")
    _hide(app)
    _type(app, "volup")
    _type(app, "dance")
    app.on_ws_message({"type": "key", "key": "escape"})
    hiding = [
        (stage, fields) for stage, fields in diag.events
        if stage in ("input.hidden", "input.hidden_line", "input.unhidden")
    ]
    assert hiding == [
        ("input.hidden", {"password_prompt": True}),
        ("input.hidden_line", {"secret": True, "taken_by_alias": False}),
        ("input.unhidden", {"reason": "mud"}),
        ("input.hidden", {"password_prompt": False}),
        ("input.hidden_line", {"secret": False, "taken_by_alias": True}),
        ("input.hidden_line", {"secret": False, "taken_by_alias": False}),
        ("input.unhidden", {"reason": "escape"}),
    ]
    assert not any("hunter2" in repr(fields) for _stage, fields in diag.events)


def test_a_chat_line_between_the_prompt_and_the_hiding_does_not_lose_the_prompt():
    app, sent, _posted = _app()
    taken: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: taken.append(ctx.line.plain_text))
    _server(app, "Password:")
    _server(app, "[chat] Bellsong says, 'anyone up for a quest?'")
    _hide(app)
    _type(app, "hunter2")
    assert sent == ["hunter2"]
    assert taken == []


def test_a_prompt_long_gone_before_the_hiding_does_not_make_it_secret():
    app, sent, posted = _with_soundpack()
    _server(app, "Password:")
    for number in range(4):
        _server(app, f"Line {number} of the welcome text.")
    _hide(app)
    _type(app, "volup")
    assert sent == []
    assert "Increasing sound effects volume to 40." in _shown(posted)


def test_a_password_typed_before_its_prompt_is_read_stays_secret():
    # No line ending and no GA yet: the prompt waits for the idle flush, and Enter comes first.
    app, sent, _posted = _app()
    taken: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: taken.append(ctx.line.plain_text))
    _hide(app)
    app.on_telnet_event(DataReceived(b"\x1b[1;37mPassword:\x1b[0m "))
    _type(app, "hunter2")
    assert sent == ["hunter2"]
    assert taken == []


def test_a_prompt_already_answered_does_not_make_the_next_hiding_secret():
    app, sent, posted = _with_soundpack()
    _server(app, "Password:")
    _hide(app)
    _type(app, "s3cret")
    _unhide(app)
    _hide(app)  # nothing said in between
    _type(app, "volup")
    assert sent == ["s3cret"]
    assert "Increasing sound effects volume to 40." in _shown(posted)


def test_a_prompt_from_a_connection_that_dropped_is_forgotten():
    app, sent, posted = _with_soundpack()
    _server(app, "Password:")
    app.on_connection_status("connection lost; reconnecting in 2s (attempt 1)")
    _hide(app)
    _type(app, "volup")
    assert sent == []
    assert "Increasing sound effects volume to 40." in _shown(posted)


class _RunawayPattern:
    """Stands in for a pattern that runs out of its time to match."""

    pattern = "(a+)+$"

    def search(self, text, **kwargs):
        raise TimeoutError("match budget exceeded")

    def match(self, text, **kwargs):
        raise TimeoutError("match budget exceeded")


def test_a_hidden_line_stays_out_of_the_log_when_an_alias_runs_out_of_time_on_it():
    diag = RecordingDiag()
    app, sent, _posted = _app(diag=diag)
    app.engine.add_alias("runaway", lambda ctx: None, name="runaway")
    rule = next(rule for rule in app.engine._aliases if rule.name == "runaway")
    rule.pattern = _RunawayPattern()
    _hide(app)
    _type(app, "s3cret")
    assert sent == ["s3cret"]
    assert diag.fields("alias.timeout_disabled")["line"] == "***"
    assert not any("s3cret" in repr(fields) for _stage, fields in diag.events)


def _with_pretender(**kwargs):
    app, sent, posted = _with_soundpack(**kwargs)
    MushclientPack(ScriptApi(app.engine, source="pretender")).load_source(PRETENDER)
    return app, sent, posted


def test_a_soundpack_pretending_the_mud_said_password_does_not_make_the_hiding_secret():
    app, sent, posted = _with_pretender()
    _hide(app)
    _type(app, "remind")
    _type(app, "volup")
    assert sent == []
    assert "Remember to change your password now and then." in _shown(posted)
    assert "Increasing sound effects volume to 40." in _shown(posted)


def test_pretend_lines_from_a_soundpack_do_not_age_a_real_prompt():
    app, sent, _posted = _with_pretender()
    taken: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: taken.append(ctx.line.plain_text))
    _server(app, "Password:")
    _type(app, "chatter")  # four lines, none of them from the MUD
    _hide(app)
    _type(app, "hunter2")
    assert sent == ["hunter2"]
    assert taken == []


@pytest.mark.parametrize(
    "prompt",
    [
        "Passcode:",
        "Contrasenya:",
        "Senha:",
        "Mot de passe :",
        "Passwort:",
        "Kennwort eingeben:",
        "Wachtwoord:",
        "Lösenord:",
        "Passord:",
        "Adgangskode:",
        "Anna salasanasi:",
        "Podaj hasło:",
        "Heslo:",
        "Введите пароль:",
        "Şifrenizi girin:",
        "Jelszó:",
        "Enter your PIN:",
    ],
)
def test_a_password_prompt_in_another_language_keeps_the_password_from_the_aliases(prompt):
    app, sent, _posted = _app()
    taken: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: taken.append(ctx.line.plain_text))
    _server(app, prompt)
    _hide(app)
    _type(app, "hunter2")
    assert sent == ["hunter2"]
    assert taken == []


def test_a_pin_you_pick_up_is_not_a_password_prompt():
    app, sent, posted = _with_soundpack()
    _server(app, "You pick up a silver pin.")
    _hide(app)
    _type(app, "volup")
    assert sent == []
    assert "Increasing sound effects volume to 40." in _shown(posted)
