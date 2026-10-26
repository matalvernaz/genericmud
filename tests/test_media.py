"""Server media: MSP U= and GMCP Client.Media downloads, the cache, and playback routing."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from genericmud.app import EngineApp
from genericmud.packs.vault import BlockedUrl
from genericmud.protocol.msp import parse_msp_line
from genericmud.protocol.telnet import OPT_GMCP, DataReceived, Subnegotiation
from genericmud.sound import media
from genericmud.sound.bus import SoundBackend
from genericmud.sound.media import MediaLibrary, http_fetch, is_downloadable, media_url
from genericmud.transport.connection import GMCP_SUPPORTS, MudConnection
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


class _Backend(SoundBackend):
    def __init__(self) -> None:
        self.played: list[tuple[str, str, float, bool]] = []
        self.musics: list[tuple[str, float]] = []
        self.stopped: list[str] = []

    def play(self, file, channel, gain, pan, loop):
        self.played.append((file, channel, gain, loop))

    def music(self, file, channel, gain):
        self.musics.append((file, gain))

    def stop(self, channel):
        self.stopped.append(channel)


class _Net:
    """A fake network: records requested URLs and writes a stub file; jobs can be held."""

    def __init__(self, *, hold: bool = False, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.hold = hold
        self.fail = fail
        self.pending: list[tuple] = []

    def fetch(self, url: str, dest: Path, trusted_address: str = "") -> None:
        self.urls.append(url)
        self.trusted_address = trusted_address
        if self.fail:
            raise OSError("404")
        dest.write_bytes(b"RIFF")

    def submit(self, job, done) -> None:
        if self.hold:
            self.pending.append((job, done))
        else:
            self._run(job, done)

    def release(self) -> None:
        pending, self.pending = self.pending, []
        for job, done in pending:
            self._run(job, done)

    @staticmethod
    def _run(job, done) -> None:
        try:
            job()
        except Exception as exc:  # noqa: BLE001
            done(exc)
        else:
            done(None)


def _library(tmp_path: Path, net: _Net, errors: list | None = None) -> MediaLibrary:
    return MediaLibrary(
        tmp_path / "cache",
        fetch=net.fetch,
        submit=net.submit,
        on_error=(lambda url, why: errors.append(url)) if errors is not None else None,
    )


def _app(tmp_path: Path, net: _Net, *, native: bool = True):
    backend = _Backend()
    posted: list[dict] = []
    app = EngineApp(
        VoiceRouter(RecordingBackend(), clock=lambda: 0.0),
        post=posted.append,
        sound_backend=backend if native else None,
        media=_library(tmp_path, net),
    )
    return app, backend, posted


def _gmcp(app: EngineApp, package: str, body: dict) -> None:
    app.on_telnet_event(Subnegotiation(OPT_GMCP, f"{package} {json.dumps(body)}".encode()))


# --- media_url / is_downloadable ---


def test_media_url_treats_the_url_as_the_files_folder():
    assert media_url("https://mud.example/sounds/", "hit.wav") == "https://mud.example/sounds/hit.wav"
    assert media_url("https://mud.example/sounds", "a b/hit.wav") == (
        "https://mud.example/sounds/a%20b/hit.wav"
    )


def test_media_url_keeps_a_url_that_already_names_the_file():
    assert media_url("https://mud.example/s/hit.wav", "hit.wav") == "https://mud.example/s/hit.wav"


def test_only_safe_audio_names_are_downloadable():
    assert is_downloadable("combat/hit.OGG")
    assert not is_downloadable("payload.exe")
    assert not is_downloadable("../escape.wav")
    assert not is_downloadable(r"\\host\share\x.wav")


def test_names_that_are_not_one_plain_file_on_windows_are_not_downloadable():
    # MSP's * and ? are wildcards, never one file (Mudlet never downloads such a name), and on
    # NTFS "evil.bat:x.wav" is a hidden stream written onto evil.bat, not a sound.
    assert not is_downloadable("alarm*.wav")
    assert not is_downloadable("berlioz/fantas?.mid")
    assert not is_downloadable("evil.bat:x.wav")
    assert not is_downloadable('say "hi".wav')


def test_msp_does_not_download_a_wildcard_name(tmp_path):
    net = _Net()
    app, _backend, _posted = _app(tmp_path, net)
    app.on_telnet_event(DataReceived(b"!!SOUND(alarm*.wav U=https://mud.example/sounds/)\r\n"))
    assert net.urls == []


# --- MediaLibrary ---


def test_library_downloads_into_the_cache_and_finds_it_next_time(tmp_path):
    net = _Net()
    library = _library(tmp_path, net)
    ready: list[str] = []
    assert library.fetch("weather/rain.wav", "https://mud.example/weather/rain.wav", ready.append)
    expected = (tmp_path / "cache" / "weather" / "rain.wav").resolve()
    assert ready == [str(expected)]
    assert library.locate("weather/rain.wav") == str(expected)
    assert not list(expected.parent.glob("*.part"))


def test_library_joins_a_download_already_in_flight(tmp_path):
    net = _Net(hold=True)
    library = _library(tmp_path, net)
    ready: list[str] = []
    library.fetch("hit.wav", "https://mud.example/hit.wav", ready.append)
    library.fetch("hit.wav", "https://mud.example/hit.wav", ready.append)
    net.release()
    assert net.urls == ["https://mud.example/hit.wav"]
    assert len(ready) == 2


def test_library_does_not_retry_a_failed_url(tmp_path):
    net = _Net(fail=True)
    errors: list[str] = []
    library = _library(tmp_path, net, errors)
    library.fetch("hit.wav", "https://mud.example/hit.wav")
    assert not library.fetch("hit.wav", "https://mud.example/hit.wav")
    assert net.urls == ["https://mud.example/hit.wav"]
    assert errors == ["https://mud.example/hit.wav"]
    assert not (tmp_path / "cache" / "hit.wav").exists()


def test_downloads_stop_once_the_worlds_media_folder_is_full(tmp_path, monkeypatch):
    # Each file had a cap and the folder none, so a MUD naming new files without end could
    # fill the disk 64 MB at a time.
    monkeypatch.setattr(media, "MAX_CACHE_BYTES", 10, raising=False)
    net = _Net()
    errors: list[str] = []
    library = _library(tmp_path, net, errors)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "old.wav").write_bytes(b"x" * 10)
    library.fetch("one.wav", "https://mud.example/one.wav")
    library.fetch("two.wav", "https://mud.example/two.wav")
    assert net.urls == []
    assert errors == ["https://mud.example/one.wav"]  # said once, not once per file


def test_two_tabs_on_one_world_fetching_one_file_land_a_whole_file(tmp_path):
    # Tabs on one world share its media folder. When both wrote through one fixed hit.wav.part,
    # the second truncated the first mid-write and what landed was neither download.
    first_half_written = threading.Event()
    second_opened = threading.Event()

    def tab_one(_url, dest, _trusted=""):
        with open(dest, "wb") as handle:
            handle.write(b"RIFF-one-first-half|")
            handle.flush()
            first_half_written.set()
            second_opened.wait(5)
            handle.write(b"one-second-half")

    def tab_two(_url, dest, _trusted=""):
        first_half_written.wait(5)
        with open(dest, "wb") as handle:
            second_opened.set()
            handle.write(b"RIFF-two")

    threads: list[threading.Thread] = []

    def threaded(job, done):
        def run():
            try:
                job()
            except Exception as exc:  # noqa: BLE001
                done(exc)
            else:
                done(None)

        thread = threading.Thread(target=run)
        thread.start()
        threads.append(thread)

    cache = tmp_path / "media" / "World"
    MediaLibrary(cache, fetch=tab_one, submit=threaded).fetch("hit.wav", "https://x.example/hit.wav")
    MediaLibrary(cache, fetch=tab_two, submit=threaded).fetch("hit.wav", "https://x.example/hit.wav")
    for thread in threads:
        thread.join(10)
    whole = (b"RIFF-one-first-half|one-second-half", b"RIFF-two")
    assert (cache / "hit.wav").read_bytes() in whole
    assert not list(cache.glob("*.part"))


def test_downloads_leave_the_loops_threads_free_and_dont_hold_the_app_open(tmp_path):
    # asyncio resolves the host name of every connect and reconnect on the loop's default
    # threads, and Python waits for those threads before it exits. Downloads stalled on a
    # slow server there kept every session from connecting, and genericMud from quitting.
    release = threading.Event()
    daemon: list[bool] = []
    landed: list[str] = []

    def stalled(_url, dest, _trusted=""):
        daemon.append(threading.current_thread().daemon)
        release.wait(10)
        dest.write_bytes(b"RIFF")

    async def until_landed() -> None:
        while len(landed) < 3:
            await asyncio.sleep(0.01)

    async def scenario() -> str:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        library = MediaLibrary(tmp_path, fetch=stalled)
        for number in range(3):
            library.fetch(f"s{number}.wav", f"https://slow.example/s{number}.wav", landed.append)
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, lambda: "free"), 2)
        finally:
            release.set()
            await asyncio.wait_for(until_landed(), 10)

    assert asyncio.run(scenario()) == "free"
    assert daemon == [True, True, True]


def test_library_hands_the_sessions_address_to_the_download(tmp_path):
    net = _Net()
    library = _library(tmp_path, net)
    library.trusted_address = lambda: "127.0.0.1"  # what the session's connection reached
    library.fetch("hit.wav", "http://localhost:8000/hit.wav")
    assert net.trusted_address == "127.0.0.1"


async def test_the_connection_reports_the_address_it_reached():
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # until the client goes
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    connection = MudConnection()
    assert connection.peer_address() == ""
    await connection.connect("localhost", port)
    assert connection.peer_address() == "127.0.0.1"
    await connection.close()
    assert connection.peer_address() == ""
    server.close()
    await server.wait_closed()


@pytest.fixture
def local_server():
    """A real HTTP server on 127.0.0.1 serving /hit.wav, /away redirecting to 10.0.0.1,
    /trickle sending one byte every 0.2 seconds for 3 seconds, /announced-big, whose
    Content-Length says 100 bytes while it sends 4, and /portal, a Wi-Fi sign-in page."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's naming
            if self.path == "/trickle":
                self.send_response(200)
                self.end_headers()
                for _ in range(15):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except OSError:
                        return  # the client gave up
                    time.sleep(0.2)
                return
            if self.path == "/portal":
                page = b"<!DOCTYPE html><html><body>Sign in to the hotel Wi-Fi</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if self.path == "/away":
                self.send_response(302)
                self.send_header("Location", "http://10.0.0.1/hit.wav")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "100" if self.path == "/announced-big" else "4")
            self.end_headers()
            self.wfile.write(b"RIFF")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_a_mud_on_this_machine_can_serve_its_own_media(tmp_path, local_server):
    dest = tmp_path / "hit.wav"
    http_fetch(f"{local_server}/hit.wav", dest, trusted_address="127.0.0.1")
    assert dest.read_bytes() == b"RIFF"


def test_a_download_that_trickles_in_is_given_up_on(tmp_path, local_server, monkeypatch):
    # The timeout covers one read at a time, so a server sending a byte every few seconds
    # never tripped it and could hold a download, and its thread, for as long as it liked.
    monkeypatch.setattr(media, "_DEADLINE_SECONDS", 0.5, raising=False)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        http_fetch(f"{local_server}/trickle", tmp_path / "t.wav", trusted_address="127.0.0.1")
    assert time.monotonic() - started < 2


def test_a_download_past_the_size_cap_is_refused(tmp_path, local_server, monkeypatch):
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 50)
    # On the length the server announces, before reading the body...
    with pytest.raises(ValueError, match="larger than"):
        http_fetch(
            f"{local_server}/announced-big", tmp_path / "a.wav", trusted_address="127.0.0.1"
        )
    # ...and, when it announces none, as soon as the body passes the cap.
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 3)
    with pytest.raises(ValueError, match="larger than"):
        http_fetch(f"{local_server}/trickle", tmp_path / "b.wav", trusted_address="127.0.0.1")


def test_a_web_page_answering_for_a_sound_is_not_kept_as_one(tmp_path, local_server):
    # A Wi-Fi sign-in page, or a server's error page sent as 200, answers any address. Saved
    # under the sound's name it was found there on every later cue and never fetched again.
    with pytest.raises(ValueError, match="web page"):
        http_fetch(f"{local_server}/portal", tmp_path / "hit.wav", trusted_address="127.0.0.1")


def test_a_muds_name_now_resolving_to_this_machine_earns_no_trust(
    tmp_path, local_server, monkeypatch
):
    # The MUD's host name was resolved again for every download, and a MUD decides what its
    # own name resolves to: pointed at 127.0.0.1, or the player's router, it made that
    # address trusted. Only the address the session is connected to is trusted.
    port = int(local_server.rsplit(":", 1)[1])
    real_getaddrinfo = socket.getaddrinfo

    def muds_dns(host, *args, **kwargs):
        return real_getaddrinfo("127.0.0.1" if host == "mud.example" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", muds_dns)
    with pytest.raises(BlockedUrl):
        http_fetch(f"http://mud.example:{port}/hit.wav", tmp_path / "hit.wav", "mud.example")


def test_a_name_answering_public_to_the_check_and_local_to_the_connection_is_refused(
    tmp_path, local_server, monkeypatch
):
    # DNS rebinding: the guard looked the name up and saw a public address, then the
    # connection looked it up again and was handed 127.0.0.1. The address reached is checked.
    port = int(local_server.rsplit(":", 1)[1])
    real_getaddrinfo = socket.getaddrinfo
    answers = iter(["93.184.216.34"])

    def rebinding_dns(host, *args, **kwargs):
        if host == "rebind.example":
            host = next(answers, "127.0.0.1")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_dns)
    with pytest.raises(BlockedUrl):
        http_fetch(f"http://rebind.example:{port}/hit.wav", tmp_path / "hit.wav")


def test_a_download_through_the_players_proxy_still_works(tmp_path, local_server, monkeypatch):
    # Through a proxy the socket reaches the proxy, which the player set up, so the address
    # check after connecting stands aside; the check on the URL's own address still runs.
    monkeypatch.setenv("http_proxy", local_server)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    dest = tmp_path / "hit.wav"
    http_fetch("http://media.invalid/hit.wav", dest)
    assert dest.read_bytes() == b"RIFF"


def test_a_public_mud_still_cannot_reach_a_local_address(tmp_path, local_server):
    with pytest.raises(BlockedUrl):
        http_fetch(f"{local_server}/hit.wav", tmp_path / "hit.wav")
    with pytest.raises(BlockedUrl):
        http_fetch(
            f"{local_server}/hit.wav", tmp_path / "hit.wav", trusted_address="93.184.216.34"
        )


def test_trusting_the_muds_host_does_not_trust_other_private_addresses(tmp_path, local_server):
    # The MUD's own address is allowed; a redirect from it into the rest of the LAN is not.
    with pytest.raises(BlockedUrl):
        http_fetch(f"{local_server}/away", tmp_path / "hit.wav", trusted_address="127.0.0.1")


def test_library_refuses_non_audio_and_escaping_names(tmp_path):
    net = _Net()
    library = _library(tmp_path, net)
    assert not library.fetch("evil.bat", "https://mud.example/evil.bat")
    assert not library.fetch("../../evil.wav", "https://mud.example/evil.wav")
    assert net.urls == []


# --- MSP ---


def test_msp_parses_the_download_url():
    _text, cues = parse_msp_line("!!SOUND(hit.wav U=https://mud.example/sounds/)")
    assert cues[0].url == "https://mud.example/sounds/"


def test_msp_takes_a_download_url_in_quotes():
    # The MSP spec: "The URL can also be enclosed in quotes".
    _text, cues = parse_msp_line('!!SOUND(hit.wav U="https://mud.example/sounds/")')
    assert cues[0].url == "https://mud.example/sounds/"


def test_msp_url_downloads_then_plays_the_cached_file(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    app.on_telnet_event(DataReceived(b"A thud !!SOUND(hit.wav V=80 U=https://mud.example/s/)\r\n"))
    assert net.urls == ["https://mud.example/s/hit.wav"]
    cached = str((tmp_path / "cache" / "hit.wav").resolve())
    assert backend.played == [(cached, "sound", 0.8, False)]
    # The next cue plays from the cache without touching the network.
    app.on_telnet_event(DataReceived(b"!!SOUND(hit.wav)\r\n"))
    assert net.urls == ["https://mud.example/s/hit.wav"]
    assert backend.played[-1][0] == cached


def test_msp_off_with_a_url_sets_the_default_download_url(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    app.on_telnet_event(DataReceived(b"!!MUSIC(Off U=https://mud.example/music/)\r\n"))
    app.on_telnet_event(DataReceived(b"!!MUSIC(tavern.ogg V=40)\r\n"))
    assert net.urls == ["https://mud.example/music/tavern.ogg"]
    assert backend.musics == [(str((tmp_path / "cache" / "tavern.ogg").resolve()), 0.4)]


def test_msp_off_with_a_url_sets_one_default_for_sounds_and_music(tmp_path):
    # The MSP spec: Off with U= sets "default URL for all requests", and Mudlet keeps one MSP
    # media location for both kinds.
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    app.on_telnet_event(DataReceived(b"!!SOUND(Off U=https://mud.example/media/)\r\n"))
    app.on_telnet_event(DataReceived(b"!!MUSIC(theme.mid)\r\n"))
    assert net.urls == ["https://mud.example/media/theme.mid"]


def test_msp_plays_from_the_worlds_sounds_folder_before_downloading(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    (sounds / "hit.wav").write_bytes(b"RIFF")
    app.engine.sounds_dir = str(sounds)  # what the window does with the world's Sounds folder
    app.engine.set_var("sppath", str(sounds))
    app.on_telnet_event(DataReceived(b"!!SOUND(hit.wav U=https://mud.example/)\r\n"))
    assert net.urls == []
    assert backend.played[0][0] == str((sounds / "hit.wav").resolve())


def test_server_media_isnt_looked_for_under_an_sppath_outside_the_players_folders(tmp_path):
    # @sppath is a variable a pack, or MUD text a pack runs as script, can set, so ScriptApi
    # only trusts it inside the pack or the player's Sounds folder. Taken as it was, a cue
    # could name any file under it: here a folder outside both, standing in for "/".
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.wav").write_bytes(b"RIFF")
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    app.engine.set_var("sppath", str(outside))
    app.on_telnet_event(DataReceived(b"!!SOUND(secret.wav)\r\n"))
    assert backend.played == [("secret.wav", "sound", 1.0, False)]


def test_a_unc_sppath_is_never_looked_at_for_server_media(tmp_path, monkeypatch):
    # On Windows, asking whether \\host\share is a folder already sends the player's NTLM hash.
    net = _Net()
    app, _backend, _posted = _app(tmp_path, net)
    app.engine.set_var("sppath", r"\\attacker.example\share")
    looked_at: list[str] = []
    real_isdir = os.path.isdir

    def recording_isdir(candidate):
        looked_at.append(str(candidate))
        return real_isdir(candidate)

    monkeypatch.setattr(os.path, "isdir", recording_isdir)
    app.on_telnet_event(DataReceived(b"!!SOUND(hit.wav)\r\n"))
    assert not [seen for seen in looked_at if "attacker" in seen]


def test_music_stopped_while_downloading_does_not_start_late(tmp_path):
    net = _Net(hold=True)
    app, backend, _posted = _app(tmp_path, net)
    app.on_telnet_event(DataReceived(b"!!MUSIC(theme.ogg U=https://mud.example/)\r\n"))
    app.on_telnet_event(DataReceived(b"!!MUSIC(Off)\r\n"))
    net.release()
    assert backend.musics == []


def test_a_download_that_lands_after_all_sound_was_cut_stays_silent(tmp_path):
    # The panic key, a dropped link and closing the tab all flush the bus. A looping cue whose
    # file lands after that would start with nothing left to stop it.
    net = _Net(hold=True)
    app, backend, _posted = _app(tmp_path, net)
    rain = {"name": "rain.wav", "url": "https://x.example/", "loops": -1, "key": "rain"}
    _gmcp(app, "Client.Media.Play", rain)
    app.on_telnet_event(DataReceived(b"!!MUSIC(theme.ogg U=https://x.example/)\r\n"))
    app.sound.flush()
    net.release()
    _gmcp(app, "Client.Media.Play", {**rain, "name": "wind.wav"})
    app.on_connection_status("disconnected")
    net.release()
    assert backend.played == [] and backend.musics == []


def test_web_renderer_keeps_getting_the_bare_name(tmp_path):
    net = _Net()
    app, _backend, posted = _app(tmp_path, net, native=False)
    app.on_telnet_event(DataReceived(b"!!SOUND(hit.wav U=https://mud.example/)\r\n"))
    assert net.urls == []
    assert [m["file"] for m in posted if m["type"] == "sound"] == ["hit.wav"]


# --- GMCP Client.Media ---


def test_client_media_is_advertised():
    assert "Client.Media 1" in GMCP_SUPPORTS


def test_client_media_play_downloads_from_the_default_url(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    _gmcp(app, "Client.Media.Default", {"url": "https://mud.example/media/"})
    _gmcp(app, "Client.Media.Play", {"name": "fx/sword.wav", "volume": 75, "key": "sword"})
    assert net.urls == ["https://mud.example/media/fx/sword.wav"]
    cached = str((tmp_path / "cache" / "fx" / "sword.wav").resolve())
    assert backend.played == [(cached, "media:sword", 0.75, False)]


def test_client_media_play_defaults_to_half_volume_and_honours_infinite_loops(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    _gmcp(app, "Client.Media.Play", {"name": "rain.wav", "url": "https://x.example/", "loops": -1})
    assert backend.played[0][2:] == (0.5, True)


def test_client_media_numbers_too_big_for_a_whole_number_fall_back_to_the_default(tmp_path):
    # JSON's 1e999 and Infinity both decode to float("inf"), and a 400-digit integer can't
    # become a float: int() raises OverflowError for all three, not ValueError.
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    play = {"name": "a.wav", "url": "https://x.example/", "volume": float("inf")}
    _gmcp(app, "Client.Media.Play", {**play, "loops": 10**400, "priority": float("-inf")})
    assert backend.played[0][2:] == (0.5, False)
    _gmcp(app, "Client.Media.Stop", {"priority": 10**400})
    assert backend.stopped == ["media-1"]


def test_client_media_keyless_sounds_overlap(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    for _ in range(2):
        _gmcp(app, "Client.Media.Play", {"name": "hit.wav", "url": "https://x.example/"})
    assert len({channel for _f, channel, _g, _l in backend.played}) == 2


def test_client_media_music_continues_when_the_same_track_is_asked_for(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    play = {"name": "area.ogg", "type": "music", "url": "https://x.example/"}
    _gmcp(app, "Client.Media.Play", play)
    _gmcp(app, "Client.Media.Play", play)
    assert len(backend.musics) == 1
    _gmcp(app, "Client.Media.Play", {**play, "continue": False})
    assert len(backend.musics) == 2


def test_client_media_continue_sent_as_text_is_read_as_true_or_false(tmp_path):
    # The spec has clients take every Client.Media value as a quoted string too, for servers
    # that can't send JSON booleans; Mudlet restarts the track on "false".
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    play = {"name": "area.ogg", "type": "music", "url": "https://x.example/"}
    _gmcp(app, "Client.Media.Play", play)
    _gmcp(app, "Client.Media.Play", {**play, "continue": "false"})
    assert len(backend.musics) == 2
    _gmcp(app, "Client.Media.Play", {**play, "continue": "true"})
    assert len(backend.musics) == 2


def test_client_media_stop_matches_fields_and_empty_stops_everything(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    url = "https://x.example/"
    _gmcp(app, "Client.Media.Play", {"name": "a.wav", "key": "a", "tag": "combat", "url": url})
    _gmcp(app, "Client.Media.Play", {"name": "b.wav", "key": "b", "tag": "weather", "url": url})
    _gmcp(app, "Client.Media.Play", {"name": "m.ogg", "type": "music", "url": url})
    _gmcp(app, "Client.Media.Stop", {"tag": "combat"})
    assert backend.stopped == ["media:a"]
    _gmcp(app, "Client.Media.Stop", {})
    assert sorted(backend.stopped) == ["media:a", "media:b", "music"]


def test_client_media_stop_by_priority_stops_at_or_below_it(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    url = "https://x.example/"
    _gmcp(app, "Client.Media.Play", {"name": "lo.wav", "key": "lo", "priority": 10, "url": url})
    _gmcp(app, "Client.Media.Play", {"name": "hi.wav", "key": "hi", "priority": 90, "url": url})
    _gmcp(app, "Client.Media.Stop", {"priority": 50})
    assert backend.stopped == ["media:lo"]


def test_client_media_url_from_a_load_or_play_is_where_later_cues_download_from(tmp_path):
    # The spec lets Play leave url out once "a URL was set by Default or Load", and Mudlet
    # keeps the last url a Load or Play gave as the one later cues download from.
    net = _Net()
    app, _backend, _posted = _app(tmp_path, net)
    _gmcp(app, "Client.Media.Load", {"name": "one.wav", "url": "https://mud.example/media/"})
    _gmcp(app, "Client.Media.Play", {"name": "two.wav"})
    _gmcp(app, "Client.Media.Play", {"name": "three.wav", "url": "https://cdn.example/fx/"})
    _gmcp(app, "Client.Media.Play", {"name": "four.wav"})
    assert net.urls == [
        "https://mud.example/media/one.wav",
        "https://mud.example/media/two.wav",
        "https://cdn.example/fx/three.wav",
        "https://cdn.example/fx/four.wav",
    ]


def test_client_media_load_downloads_without_playing(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    _gmcp(app, "Client.Media.Load", {"name": "later.wav", "url": "https://x.example/"})
    assert net.urls == ["https://x.example/later.wav"]
    assert backend.played == []


def test_client_media_refuses_unsafe_names_and_video(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    _gmcp(app, "Client.Media.Play", {"name": r"\\attacker\share\x.wav", "url": "https://x/"})
    _gmcp(app, "Client.Media.Play", {"name": "../x.wav", "url": "https://x/"})
    _gmcp(app, "Client.Media.Play", {"name": "intro.mp4", "type": "video", "url": "https://x/"})
    assert net.urls == []
    assert backend.played == [] and backend.musics == []
