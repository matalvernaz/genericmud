"""EngineApp soundpack activation on connect, plus the packs management CLI."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.packs import PackStore
from genericmud.packs.__main__ import main as packs_main
from genericmud.protocol.telnet import OPT_MSDP, WILL, DataReceived, Negotiation, Subnegotiation
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
    assert any("soundpack" in s for s in backend.spoken)  # announced aloud
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
    assert any("not trusted" in s for s in backend.spoken)


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
    MSDP subnegotiation payload reaches OnPluginTelnetSubnegotiation byte-exact."""
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
    app = EngineApp(voice, post=lambda _m: None, packs=store, send_raw=raw.append)

    app.on_connect("erion")
    assert app.engine.get_var("installed") == "1"  # OnPluginInstall dispatched

    app.on_telnet_event(Negotiation(WILL, OPT_MSDP))
    assert raw == [bytes([255, 250, 69, 1]) + b"REPORT" + bytes([255, 240])]

    payload = bytes([1]) + b"SOUND" + bytes([2]) + b"hit"
    app.on_telnet_event(Subnegotiation(OPT_MSDP, payload))
    assert app.engine.get_var("last_msdp") == payload.decode("latin-1")
