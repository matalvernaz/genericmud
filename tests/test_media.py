"""Server media: MSP U= and GMCP Client.Media downloads, the cache, and playback routing."""

from __future__ import annotations

import json
from pathlib import Path

from genericmud.app import EngineApp
from genericmud.protocol.msp import parse_msp_line
from genericmud.protocol.telnet import OPT_GMCP, DataReceived, Subnegotiation
from genericmud.sound.bus import SoundBackend
from genericmud.sound.media import MediaLibrary, is_downloadable, media_url
from genericmud.transport.connection import GMCP_SUPPORTS
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

    def fetch(self, url: str, dest: Path) -> None:
        self.urls.append(url)
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


def test_msp_plays_from_the_worlds_sounds_folder_before_downloading(tmp_path):
    net = _Net()
    app, backend, _posted = _app(tmp_path, net)
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    (sounds / "hit.wav").write_bytes(b"RIFF")
    app.engine.set_var("sppath", str(sounds))
    app.on_telnet_event(DataReceived(b"!!SOUND(hit.wav U=https://mud.example/)\r\n"))
    assert net.urls == []
    assert backend.played[0][0] == str((sounds / "hit.wav").resolve())


def test_music_stopped_while_downloading_does_not_start_late(tmp_path):
    net = _Net(hold=True)
    app, backend, _posted = _app(tmp_path, net)
    app.on_telnet_event(DataReceived(b"!!MUSIC(theme.ogg U=https://mud.example/)\r\n"))
    app.on_telnet_event(DataReceived(b"!!MUSIC(Off)\r\n"))
    net.release()
    assert backend.musics == []


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
