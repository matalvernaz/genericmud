"""EngineApp soundpack activation on connect, plus the packs management CLI."""

from __future__ import annotations

import json

from genericmud.app import EngineApp
from genericmud.packs import PackStore
from genericmud.packs.__main__ import main as packs_main
from genericmud.protocol.telnet import OPT_MSDP, WILL, DataReceived, Negotiation, Subnegotiation
from genericmud.session.diaglog import DiagnosticLog
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def _install_lua(store, tmp_path, name, body, world, *, trust=True):
    src = tmp_path / name
    src.write_text(body, encoding="utf-8")
    store.install(src, world=world, trust=trust)


def _app(store):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    posted: list[dict] = []
    app = EngineApp(voice, post=posted.append, packs=store)
    return app, backend, posted


def test_activate_packs_arms_triggers_and_announces(tmp_path):
    store = PackStore(tmp_path / "store")
    _install_lua(
        store, tmp_path, "hunt.lua",
        'mud.trigger("tells you", nil, {channel="tell"})', world="god-wars",
    )
    app, backend, _posted = _app(store)
    result = app.activate_packs("god-wars")
    assert result.loaded == ["hunt"]
    assert "1 soundpack loaded" in backend.spoken  # announced aloud, as a whole sentence
    app.on_telnet_event(DataReceived(b"Bob tells you hi\r\n"))
    assert any("Bob tells you hi" in s for s in backend.spoken)  # the routing rule is live


def test_activate_packs_without_store_is_noop():
    app = EngineApp(VoiceRouter(RecordingBackend(), clock=lambda: 0.0))
    assert app.activate_packs("anything") is None


def test_activate_packs_announces_a_conflict(tmp_path):
    store = PackStore(tmp_path / "store")
    _install_lua(store, tmp_path, "a.lua", 'mud.key("f1", function() end)', world="mud")
    _install_lua(store, tmp_path, "b.lua", 'mud.key("f1", function() end)', world="mud")
    app, backend, _posted = _app(store)
    result = app.activate_packs("mud")
    assert any(c.kind == "key" and c.token == "f1" for c in result.conflicts)
    assert any("f1" in s for s in backend.spoken)


def test_cli_install_list_conflicts_roundtrip(tmp_path, capsys):
    root = str(tmp_path / "store")
    pack = tmp_path / "hunt.lua"
    pack.write_text('mud.send("look")', encoding="utf-8")

    assert packs_main(["--root", root, "install", str(pack), "--world", "mud", "--trust"]) == 0
    assert "Installed hunt" in capsys.readouterr().out

    assert packs_main(["--root", root, "list"]) == 0
    assert "hunt" in capsys.readouterr().out

    assert packs_main(["--root", root, "conflicts", "mud"]) == 0  # clean world
    assert "No binding conflicts" in capsys.readouterr().out


def test_cli_conflicts_reports_key_clash_with_nonzero_exit(tmp_path, capsys):
    root = str(tmp_path / "store")
    for name in ("a.lua", "b.lua"):
        path = tmp_path / name
        path.write_text('mud.key("f1", function() end)', encoding="utf-8")
        packs_main(["--root", root, "install", str(path), "--world", "mud", "--trust"])
    capsys.readouterr()
    assert packs_main(["--root", root, "conflicts", "mud"]) == 1
    assert "CONFLICT key" in capsys.readouterr().out


def test_untrusted_pack_is_announced_and_not_armed(tmp_path):
    store = PackStore(tmp_path / "store")
    _install_lua(
        store, tmp_path, "hunt.lua",
        'mud.trigger("tells you", nil, {channel="tell"})', world="mud", trust=False,
    )
    app, backend, _posted = _app(store)
    result = app.activate_packs("mud")
    assert result.skipped_untrusted == ["hunt"]
    # Announced with the fix (trust it in Manage Soundpacks), not just "not trusted".
    assert any("isn't trusted" in s and "Manage Soundpacks" in s for s in backend.spoken)


def test_cli_trust_promotes_a_pack_to_loading(tmp_path, capsys):
    root = str(tmp_path / "store")
    pack = tmp_path / "hunt.lua"
    pack.write_text('mud.send("look")', encoding="utf-8")
    packs_main(["--root", root, "install", str(pack), "--world", "mud"])  # untrusted
    capsys.readouterr()
    packs_main(["--root", root, "conflicts", "mud"])
    assert "SKIPPED hunt" in capsys.readouterr().out
    assert packs_main(["--root", root, "trust", "hunt"]) == 0
    capsys.readouterr()
    packs_main(["--root", root, "conflicts", "mud"])
    assert "1 loaded clean" in capsys.readouterr().out

def test_msdp_negotiation_flows_to_mushclient_pack(tmp_path):
    """End-to-end MSDP plumbing: on_connect dispatches OnPluginInstall, the server's
    WILL MSDP triggers the SENT_DO round (REPORT list -> send_raw verbatim), and each
    MSDP subnegotiation payload reaches OnPluginTelnetSubnegotiation byte-exact.

    Runs with a LIVE DiagnosticLog: the 0.6.6 silent-session bug was a TypeError
    inside a diag.event() call on this exact path, invisible with diag=None."""
    store = PackStore(tmp_path / "store")
    src = tmp_path / "pack.xml"
    src.write_text(
        '<muclient><plugin id="msdp"/><script><![CDATA[\n'
        'function OnPluginInstall() SetVariable("installed", 1) end\n'
        "function OnPluginTelnetRequest(t, data)\n"
        '  if t == 69 and data == "SENT_DO" then\n'
        '    SendPkt(string.char(255, 250, 69, 1) .. "REPORT" .. string.char(255, 240))\n'
        "  end\n"
        "  return t == 69\n"
        "end\n"
        "function OnPluginTelnetSubnegotiation(t, data)\n"
        '  if t == 69 then SetVariable("last_msdp", data) end\n'
        "end\n"
        "]]></script></muclient>",
        encoding="latin-1",
    )
    store.install(src, world="erion", trust=True)
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    raw: list[bytes] = []
    diag = DiagnosticLog(tmp_path / "diag.log")
    diag.start()
    app = EngineApp(
        voice, post=lambda _m: None, packs=store, send_raw=raw.append, diag=diag
    )

    app.on_connect("erion")
    assert app.engine.get_var("installed") == "1"  # OnPluginInstall dispatched

    app.on_telnet_event(Negotiation(WILL, OPT_MSDP))
    # Both subscribers ask, in order: the client's own LIST first, then the pack's REPORT.
    assert raw == [
        bytes([255, 250, 69, 1]) + b"LIST" + bytes([2]) + b"REPORTABLE_VARIABLES"
        + bytes([255, 240]),
        bytes([255, 250, 69, 1]) + b"REPORT" + bytes([255, 240]),
    ]

    payload = bytes([1]) + b"SOUND" + bytes([2]) + b"hit"
    app.on_telnet_event(Subnegotiation(OPT_MSDP, payload))
    assert app.engine.get_var("last_msdp") == payload.decode("latin-1")

    diag.stop()
    trace = (tmp_path / "diag.log").read_text()
    # The install/connect hooks must have run to completion, not died inside a
    # diag call: lifecycle.done is the end-of-dispatch marker.
    assert "plugin.lifecycle.done" in trace
    assert "plugin.lifecycle.error" not in trace
    assert "msdp.start" in trace


def test_a_vipmud_packs_onconnect_and_ondisconnect_follow_the_connection(tmp_path):
    store = PackStore(tmp_path / "store")
    src = tmp_path / "pack.set"
    src.write_text("#alias OnConnect {#say ready}\n#alias OnDisconnect {#say gone}\n",
                   encoding="latin-1")
    store.install(src, world="prom", trust=True)
    app, backend, _posted = _app(store)
    app.on_connect("prom")
    assert "ready" in backend.spoken
    app.on_connection_status("connection lost; reconnecting in 5s (attempt 1)")
    assert "gone" in backend.spoken
    backend.spoken.clear()
    app.on_connection_status("connection lost; reconnecting in 10s (attempt 2)")
    assert "gone" not in backend.spoken  # once per drop, not once per attempt
    app.on_connection_status("reconnected")
    assert "ready" in backend.spoken
    backend.spoken.clear()
    app.on_connection_status("disconnected")  # what the window's Disconnect reports
    assert "gone" in backend.spoken


def test_a_vipmud_packs_variables_start_fresh_each_session(tmp_path):
    # VIP Mud keeps no variables between sessions unless a player asks it to, and packs
    # save what they want in their own files. Miriani warns at every login when variables
    # were kept; genericMud kept them all.
    store = PackStore(tmp_path / "store")
    src = tmp_path / "pack.set"
    src.write_text(
        "#if {%defined(aaah)=1} {#say kept} {#var aaah 0}\n"
        "#alias spsave {#file 6 {settings.set};#write 6 {1} 1;#close 6}\n",  # saves its own
        encoding="latin-1",
    )
    store.install(src, world="miriani", trust=True)

    def session():
        backend = RecordingBackend()
        voice = VoiceRouter(backend, clock=lambda: 0.0)
        return EngineApp(voice, packs=store, name="miriani"), backend

    first, _backend = session()
    first.on_connect("miriani")
    first.engine.set_var("UserSetting", "kept by genericMud")  # not the VIP Mud pack's
    first.shutdown()
    second, backend = session()
    second.on_connect("miriani")
    assert "kept" not in backend.spoken
    assert second.engine.get_var("UserSetting") == "kept by genericMud"


def test_a_vipmud_pack_that_keeps_no_files_keeps_its_variables(tmp_path):
    # COGG keeps its volume in @vol and never writes a file; VIP Mud's Simple mode and Keep
    # Variables saved that for it, and genericMud does the same.
    store = PackStore(tmp_path / "store")
    src = tmp_path / "cogg.set"
    src.write_text(
        "#if {%defined(vol)=0} {#var vol 10}\n#key F10 {#math vol {@vol + 5}}\n",
        encoding="latin-1",
    )
    store.install(src, world="cogg", trust=True)

    def session():
        voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
        return EngineApp(voice, packs=store, name="cogg")

    first = session()
    first.on_connect("cogg")
    first.engine.press_key("f10")
    first.shutdown()
    second = session()
    second.on_connect("cogg")
    second.engine.press_key("f10")
    assert second.engine.get_var("vol") == "20"


def _install_mush(store, tmp_path, xml: str, world: str = "mud") -> None:
    src = tmp_path / "pack.xml"
    src.write_text(xml, encoding="latin-1")
    store.install(src, world=world, trust=True)


def test_a_mushclient_packs_connect_disconnect_and_close_hooks_follow_the_session(tmp_path):
    # Miriani-Next fixes its settings up in OnPluginSaveState and OnPluginClose, and packs
    # stop their music in OnPluginDisconnect; genericMud ran none of them.
    store = PackStore(tmp_path / "store")
    _install_mush(
        store, tmp_path,
        "<muclient><plugin id='hooks' name='hooks' save_state='y'/><script><![CDATA[\n"
        "function OnPluginConnect() Note('hello') end\n"
        "function OnPluginDisconnect() Note('goodbye') end\n"
        "function OnPluginSaveState() SetVariable('volume', '70') end\n"
        "function OnPluginClose() Note('closing') end\n"
        "]]></script></muclient>",
    )

    def session():
        backend = RecordingBackend()
        voice = VoiceRouter(backend, clock=lambda: 0.0)
        return EngineApp(voice, packs=store, name="mud"), backend

    app, backend = session()
    app.on_connect("mud")
    assert backend.spoken.count("hello") == 1
    app.on_connection_status("connection lost; reconnecting in 5s (attempt 1)")
    app.on_connection_status("connection lost; reconnecting in 10s (attempt 2)")
    assert backend.spoken.count("goodbye") == 1  # once per drop, not once per attempt
    app.on_connection_status("reconnected")
    assert backend.spoken.count("hello") == 2
    app.shutdown()
    assert backend.spoken[-2:] == ["goodbye", "closing"]
    second, _backend = session()
    second.on_connect("mud")
    assert second.engine.get_var("volume") == "70"  # saved by its OnPluginSaveState


def _connect_hook_pack(store, tmp_path) -> None:
    _install_mush(
        store, tmp_path,
        "<muclient><plugin id='hooks' name='hooks'/><script><![CDATA[\n"
        "function OnPluginInstall() Note('installed') end\n"
        "function OnPluginConnect() Note('connected') Send('hello mud') end\n"
        "]]></script></muclient>",
    )


def test_packs_hear_the_mud_connected_once_the_connection_opens(tmp_path):
    # The window gets packs ready before it opens the connection. MUSHclient calls
    # OnPluginConnect, and VIP Mud OnConnect, once the MUD is there: a pack that sends
    # something on connect had it dropped, sent to a connection not yet open.
    store = PackStore(tmp_path / "store")
    _connect_hook_pack(store, tmp_path)
    vip = tmp_path / "vip.set"
    vip.write_text("#alias OnConnect {#say greetings}\n", encoding="latin-1")
    store.install(vip, world="vip", trust=True)
    sent: list[str] = []
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    app = EngineApp(voice, packs=store, name="mud", send=sent.append)
    app.on_connect("mud", socket_open=False)
    assert "installed" in backend.spoken
    assert "connected" not in backend.spoken
    assert sent == []
    app.on_socket_open()
    assert backend.spoken.count("connected") == 1
    assert sent == ["hello mud"]
    vip_backend = RecordingBackend()
    vip_app = EngineApp(VoiceRouter(vip_backend, clock=lambda: 0.0), packs=store, name="vip")
    vip_app.on_connect("vip", socket_open=False)
    assert "greetings" not in vip_backend.spoken
    vip_app.on_socket_open()
    assert "greetings" in vip_backend.spoken


def test_packs_never_hear_connected_when_the_connection_fails(tmp_path):
    # Mystavaria starts its music when it hears the MUD connected; a MUD that's down never
    # connects, so its music mustn't start. A later reconnect is heard once.
    store = PackStore(tmp_path / "store")
    _connect_hook_pack(store, tmp_path)
    backend = RecordingBackend()
    app = EngineApp(VoiceRouter(backend, clock=lambda: 0.0), packs=store, name="mud")
    app.on_connect("mud", socket_open=False)
    assert "connected" not in backend.spoken
    app.on_connection_status("reconnected")
    assert backend.spoken.count("connected") == 1


def test_packs_hear_their_world_in_front_once_installed_and_then_each_change(tmp_path):
    # Miriani-Next plays room ambience only while its window has focus, which it learns
    # from OnPluginGetFocus: genericMud never sent it, so the ambience never played. Packs
    # installed while their world is in front hear so at once, before they hear it connect,
    # as in MUSHclient; after that, only changes.
    store = PackStore(tmp_path / "store")
    _install_mush(
        store, tmp_path,
        "<muclient><plugin id='focus' name='focus'/><script><![CDATA[\n"
        "function OnPluginGetFocus() Note('front') end\n"
        "function OnPluginLoseFocus() Note('behind') end\n"
        "function OnPluginConnect() Note('connected') end\n"
        "]]></script></muclient>",
    )
    backend = RecordingBackend()
    app = EngineApp(VoiceRouter(backend, clock=lambda: 0.0), packs=store, name="mud")
    app.on_focus_changed(True)  # the window was in front before the packs loaded
    app.on_connect("mud", socket_open=False)
    app.on_socket_open()
    app.on_focus_changed(True)
    app.on_focus_changed(False)
    app.on_focus_changed(False)
    app.on_focus_changed(True)
    heard = [text for text in backend.spoken if text in ("front", "behind", "connected")]
    assert heard == ["front", "connected", "behind", "front"]


def test_simulate_sends_text_through_triggers_the_output_and_speech(tmp_path):
    # Mush-Z strips its zchn: channel prefix and simulates the rest, so its other triggers
    # catch the line as if the MUD had sent it without one.
    store = PackStore(tmp_path / "store")
    _install_mush(
        store, tmp_path,
        "<muclient><plugin id='sim' name='sim'/><triggers>"
        "<trigger match='^zchn:(.+)$' regexp='y' enabled='y' omit_from_output='y' "
        "send_to='14' sequence='200'><send>Simulate(\"%1\\\\r\\\\n\")</send></trigger>"
        "<trigger match='^You warn the group$' regexp='y' enabled='y' send_to='12'>"
        "<send>Note('warned')</send></trigger>"
        "</triggers></muclient>",
    )
    app, backend, posted = _app(store)
    app.on_connect("mud")
    backend.spoken.clear()
    app.on_telnet_event(DataReceived(b"zchn:You warn the group\r\n"))
    assert "You warn the group" in backend.spoken
    assert "warned" in backend.spoken
    assert not any("zchn:" in text for text in backend.spoken)


def test_a_packs_save_state_writes_its_variables_soon_once_per_burst(tmp_path):
    store = PackStore(tmp_path / "store")
    _install_mush(
        store, tmp_path,
        "<muclient><plugin id='saver' name='saver' save_state='y'/><script><![CDATA[\n"
        "function OnPluginSaveState() SetVariable('volume', '70') end\n"
        "]]></script><aliases><alias match='save' enabled='y' send_to='12'>"
        "<send>SaveState() SaveState()</send></alias></aliases></muclient>",
    )
    scheduled: list[tuple[float, object]] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0), packs=store, name="mud",
        schedule=lambda delay, callback: scheduled.append((delay, callback)),
    )
    app.on_connect("mud")
    scheduled.clear()
    app.on_ws_message({"type": "input", "text": "save"})
    saves = [callback for _delay, callback in scheduled]
    assert len(saves) == 1  # two asks, one write
    saves[0]()
    data = json.loads(app._pack_vars_path().read_text(encoding="utf-8"))
    assert data["volume"] == "70"


def test_the_session_lists_its_packs_notepads(tmp_path):
    # Mush-Z writes its channel log to a notepad it never opens; the window lists them.
    store = PackStore(tmp_path / "store")
    _install_mush(
        store, tmp_path,
        "<muclient><plugin id='np' name='np'/><script><![CDATA[\n"
        "function OnPluginInstall() AppendToNotepad('comlog', 'Bob: hi', '\\r\\n') end\n"
        "]]></script></muclient>",
    )
    app, _backend, _posted = _app(store)
    app.on_connect("mud")
    assert app.pack_notepads() == {"comlog": "Bob: hi\r\n"}


class _TimerHandle:
    """What the event loop hands back for a scheduled callback: it can be cancelled."""

    def __init__(self, delay: float, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.fired = False

    def cancel(self) -> None:
        self.cancelled = True


def _heartbeat_session(tmp_path):
    store = PackStore(tmp_path / "store")
    _install_mush(
        store, tmp_path,
        "<muclient><plugin id='beat' name='beat'/><script><![CDATA[\n"
        "AddTimer('heartbeat', 0, 0, 1, '', timer_flag.Enabled, 'beat')\n"
        "function beat() Note('beat') end\n"
        "]]></script></muclient>",
    )
    handles: list[_TimerHandle] = []

    def schedule(delay, callback):
        handles.append(_TimerHandle(delay, callback))
        return handles[-1]

    backend = RecordingBackend()
    app = EngineApp(
        VoiceRouter(backend, clock=lambda: 0.0), packs=store, name="mud", schedule=schedule,
    )
    app.on_connect("mud")
    return app, backend, handles


def _heartbeats(handles: list[_TimerHandle]) -> list[_TimerHandle]:
    return [
        handle for handle in handles
        if handle.delay == 1 and not handle.cancelled and not handle.fired
    ]


def test_a_packs_timers_run_again_after_a_reconnect(tmp_path, monkeypatch):
    # A disconnect cancels every pending pack timer, and Toastush's heartbeat must come back
    # each time. Cancelled timers give their places under the pack's timer limit back, or
    # a few drops would use the limit up and the heartbeat would be refused for good.
    monkeypatch.setattr("genericmud.scripting.api._MAX_ACTIVE_TIMERS", 1)
    app, backend, handles = _heartbeat_session(tmp_path)
    for _drop in range(3):
        app.on_connection_status("disconnected")
        app.on_connection_status("reconnected")
        backend.spoken.clear()
        due = _heartbeats(handles)
        assert len(due) == 1
        due[0].fired = True
        due[0].callback()
        assert "beat" in backend.spoken


def test_a_drop_that_reconnects_by_itself_leaves_the_timers_running(tmp_path):
    # Losing the connection and getting it back doesn't cancel the pack's timers, so they
    # aren't scheduled again: that would restart every countdown.
    app, _backend, handles = _heartbeat_session(tmp_path)
    heartbeat = _heartbeats(handles)
    before = len(handles)
    app.on_connection_status("connection lost; reconnecting in 5s (attempt 1)")
    app.on_connection_status("reconnected")
    # Settle new presentation callbacks, without confusing their delay with the pack's
    # one-second heartbeat. A duplicate pack timer would reschedule itself and fail below.
    for handle in handles[before:]:
        handle.fired = True
        handle.callback()
    assert len(heartbeat) == 1
    assert _heartbeats(handles) == heartbeat
