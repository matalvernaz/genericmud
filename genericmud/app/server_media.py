"""Sounds and music requested by the server through MSP or GMCP Client.Media."""

from __future__ import annotations

from collections.abc import Callable

from genericmud.bridge import protocol
from genericmud.safepath import is_unc, is_unsafe, within
from genericmud.sound.bus import MUSIC_CATEGORY, PAUSED, CueTiming
from genericmud.sound.direction import facing_bearing, stereo_pan
from genericmud.sound.media import media_url

_MEDIA_DEFAULT_VOLUME = 50  # Client.Media.Play's volume when the server leaves it out (spec)
_MEDIA_FADEAWAY_MS = 5000  # Client.Media.Stop's fadeaway when neither Play nor Stop says (spec)
_MEDIA_LONGEST_MS = 24 * 60 * 60 * 1000  # positions and fades past a day are a server's mistake
_MEDIA_TRACKED = 64  # Client.Media cues remembered for Stop before finished ones are dropped
_MEDIA_TURN_SECONDS = 0.25  # how long placed sounds take to swing round when the player turns


def _media_int(value: object, default, low: int, high: int):
    """A Client.Media number clamped to its range; servers send strings and floats too."""
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):  # OverflowError: infinity, 400-digit numbers
        return default
    return max(low, min(high, number))


def _media_is_zero(value: object) -> bool:
    """Whether a Client.Media number is exactly 0, the volume that asks for a preload."""
    try:
        return float(value) == 0.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _media_continues(value: object) -> bool:
    """Client.Media.Play's ``continue``: true unless the server sent false, either as a JSON
    boolean or as the text "false". The spec has clients take every value as a quoted string
    too, for servers that can't send JSON types, and Mudlet restarts the track on both."""
    if isinstance(value, str):
        return value.strip().lower() != "false"
    return value is not False


def _media_true(value: object) -> bool:
    """A Client.Media yes-or-no that's off unless the server sent true, as a JSON boolean or
    as the text "true" (servers that can't send JSON types quote every value)."""
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True


def _media_timing(body: dict) -> CueTiming | None:
    """Client.Media.Play's start, finish, fadein and fadeout (milliseconds), or None when it
    gave none of them."""
    start, finish, fade_in, fade_out = (
        _media_int(body.get(key), 0, 0, _MEDIA_LONGEST_MS)
        for key in ("start", "finish", "fadein", "fadeout")
    )
    if not (start or finish or fade_in or fade_out):
        return None
    return CueTiming(
        start=start / 1000, finish=finish / 1000, fade_in=fade_in / 1000, fade_out=fade_out / 1000
    )


class ServerMediaMixin:
    """Sounds and music requested by the server through MSP or GMCP Client.Media.

    Part of EngineApp (engine_app.py). Every attribute below starts in its constructor.

    Writes: _media_facing, _media_seq, _media_url.

    Reads: _diag, _media_cues, _media_gen, _native_audio, _post, download_media, engine, media,
    packs, sink, sound.

    Calls: nothing outside.
    """

    def _start_media(
        self, name: str, url: str, channel: str, play: Callable[[str], None]
    ) -> None:
        """Play ``name`` through ``play``: from disk when it's there, after a download when the
        server said where to get it, else by name as before (the backend reports it missing)."""
        gen = self._media_gen[channel] = self._media_gen.get(channel, 0) + 1
        if not self._native_audio:
            play(name)
            return
        path = self.media.locate(name, self._media_sounds_root())
        if path is not None:
            play(path)
            return
        if url and self.download_media:
            address = media_url(url, name)
            flushes = self.sound.flushes

            def ready(downloaded: str) -> None:
                # A stop, or a newer cue on this channel, arrived while the file downloaded,
                # or all sound was cut: the panic key, a dropped link, the tab closing.
                if self._media_gen.get(channel) == gen and self.sound.flushes == flushes:
                    play(downloaded)

            if self.media.fetch(name, address, ready):
                if self._diag is not None:
                    self._diag.event("media.download", file=name, url=address)
                return
        play(name)

    def _media_sounds_root(self) -> str | None:
        """The folder server media is looked for in before the download cache: @sppath when it
        lies inside the player's Sounds folder or an installed pack, else the Sounds folder.

        @sppath is a variable any pack, or MUD text a pack runs as script, can set, and
        ScriptApi._sounds_root holds packs to the same rule; taken as it is, a cue could name
        any file under "/". A UNC @sppath isn't even checked: on Windows, resolving one is
        enough to send the player's NTLM hash to that host.
        """
        sounds_dir = self.engine.sounds_dir
        sppath = self.engine.get_var("sppath")
        if not sppath or is_unc(sppath):
            return sounds_dir
        packs_dir = getattr(self.packs, "packs_dir", None)
        trusted = [root for root in (sounds_dir, packs_dir) if root]
        if any(within(root, sppath) for root in trusted):
            return sppath
        return sounds_dir

    def _stop_media(self, channel: str) -> None:
        self._media_gen[channel] = self._media_gen.get(channel, 0) + 1
        self._media_cues.pop(channel, None)
        self.sound.stop(channel)

    def _fade_away_media(self, channel: str, seconds: float) -> None:
        self._media_gen[channel] = self._media_gen.get(channel, 0) + 1  # still downloading: never
        self._media_cues.pop(channel, None)
        self.sound.fade_away(channel, seconds)

    def _media_failed(self, url: str, reason: str) -> None:
        if self._diag is not None:
            self._diag.event("media.download", url=url, failed=reason)
        self._post(protocol.echo(f"* sound download failed: {url} ({reason})"))

    def _client_media(self, package: str, data: object) -> None:
        """GMCP ``Client.Media.*`` (Mudlet's media spec): Default, Load, Play, Stop, Pause,
        Update and Listener. Bare Client.Media is Default's obsolete name."""
        verb = package.rsplit(".", 1)[-1].lower()
        body = data if isinstance(data, dict) else {}
        if verb in ("default", "media"):
            self._media_url = str(body.get("url") or "")
        elif verb == "load":
            name = str(body.get("name") or "")
            if not name or is_unsafe(name):
                return
            self._media_preload(name, self._media_source(body))
        elif verb == "play":
            self._media_play(body)
        elif verb == "stop":
            self._media_stop(body)
        elif verb == "pause":
            self._media_pause(body)
        elif verb == "update":
            self._media_update(body)
        elif verb == "listener":
            self._media_listener(body)

    def _media_play(self, body: dict) -> None:
        name = str(body.get("name") or "")
        kind = str(body.get("type") or "sound").lower()
        if not name or kind not in ("sound", "music"):  # video has nowhere to play
            return
        if is_unsafe(name):  # the same hostile shapes MSP refuses
            if self._diag is not None:
                self._diag.event("media.play", file=name, blocked=True)
            return
        if _media_is_zero(body.get("volume")):
            # Mudlet's preload: volume 0 asks for the file ahead of time, and plays nothing.
            self._media_preload(name, self._media_source(body))
            return
        gain = _media_int(body.get("volume"), _MEDIA_DEFAULT_VOLUME, 1, 100) / 100.0
        loops = _media_int(body.get("loops"), 1, -1, 1_000_000)
        loop = loops < 0  # -1 loops until stopped; otherwise it plays that many times
        times = max(1, loops)
        key = str(body.get("key") or "")
        if self._media_resume(name, kind, key, body):
            return
        priority = _media_int(body.get("priority"), 0, 0, 100)
        if not self._media_has_priority(name, kind, priority):
            if self._diag is not None:
                self._diag.event("media.play", file=name, refused="priority", priority=priority)
            return
        if kind == "music":
            channel = MUSIC_CATEGORY
            current = self._media_cues.get(channel)
            # "continue" (default true): the same track asked for again keeps playing rather
            # than restarting, which is what a server re-sending an area's music means.
            if _media_continues(body.get("continue")) and current and current["name"] == name:
                return
        elif key:
            channel = f"media:{key}"
        else:
            self._media_seq += 1
            channel = f"media-{self._media_seq}"
        self._forget_finished_media()
        timing = _media_timing(body)
        # Where the sound comes from (Mudlet 5.1): a pan given outright, or a direction turned
        # by the way the player faces. Music plays in the middle, as Mudlet plays it.
        pan = stereo_pan(body.get("pan"), body.get("direction"), self._media_facing)
        placed_by_direction = kind == "sound" and pan is not None and body.get("pan") is None
        self._media_cues[channel] = {
            "name": name,
            "type": kind,
            "key": key,
            "tag": str(body.get("tag") or ""),
            "priority": priority,
            "fadeout": timing.fade_out if timing is not None else 0.0,  # a fadeaway's length
            # A sound placed by direction turns when the player does; one given a pan stays.
            "direction": body.get("direction") if placed_by_direction else None,
        }
        url = self._media_source(body)
        if self._diag is not None:
            self._diag.event("media.play", file=name, kind=kind, channel=channel)
        caption = " ".join(str(body.get("caption") or "").split())
        if caption:
            # The spec's closed caption, for players who can't hear the sound. Shown in the
            # output, in brackets as Mudlet prints them, where the review keys find it, but not
            # spoken over the sound itself.
            self.sink.echo(f"[{caption}]")
        if kind == "music":
            self._start_media(
                name, url, channel,
                lambda path: self.sound.music(
                    path, channel, gain, times=-1 if loop else times, timing=timing
                ),
            )
        else:
            self._start_media(
                name, url, channel,
                lambda path: self.sound.play(
                    path, channel, gain=gain, pan=pan or 0.0, loop=loop, times=times,
                    timing=timing,
                ),
            )

    def _media_preload(self, name: str, url: str) -> None:
        """Client.Media's Load, and a Play at volume 0: fetch the file now, play nothing."""
        if not name or is_unsafe(name) or not url or not self.download_media:
            return
        if self._native_audio and self.media.locate(name, self._media_sounds_root()) is None:
            self.media.fetch(name, media_url(url, name))

    def _media_source(self, body: dict) -> str:
        """Where a Load or Play downloads from: its own url, which then stays the default for
        later cues, else the default. The spec lets Play leave url out once Default or Load
        has given one, and Mudlet keeps the last url a Load or Play gave."""
        url = str(body.get("url") or "")
        if url:
            self._media_url = url
        return self._media_url

    def _media_stop(self, body: dict) -> None:
        """Stop the Client.Media cues matching every field given; an empty body stops them all.
        ``priority`` matches cues at or below it, as the spec defines. With ``fadeaway`` they
        fade out first, over the fadeout their Play gave, else this Stop's, else five seconds."""
        wanted = {
            field: str(body[field])
            for field in ("name", "type", "key", "tag")
            if body.get(field) not in (None, "")
        }
        ceiling = _media_int(body.get("priority"), None, 0, 100)
        fade_away = _media_true(body.get("fadeaway"))
        stop_fade = _media_int(body.get("fadeout"), _MEDIA_FADEAWAY_MS, 0, _MEDIA_LONGEST_MS)
        for channel, cue in list(self._media_cues.items()):
            if any(str(cue[field]) != value for field, value in wanted.items()):
                continue
            if ceiling is not None and int(cue["priority"]) > ceiling:  # type: ignore[call-overload]
                continue
            if fade_away:
                seconds = float(cue.get("fadeout") or 0.0) or stop_fade / 1000  # type: ignore[arg-type]
                self._fade_away_media(channel, seconds)
            else:
                self._stop_media(channel)

    def _media_matching(self, body: dict):
        """The tracked cues matching every name, type, key and tag the body gives, as
        (channel, cue). A key or tag has to be the cue's own, so a sound with none of its own
        never matches one that names a key: Update needs that as much as Stop does."""
        wanted = {
            field: str(body[field])
            for field in ("name", "type", "key", "tag")
            if body.get(field) not in (None, "")
        }
        for channel, cue in list(self._media_cues.items()):
            if any(str(cue[field]) != value for field, value in wanted.items()):
                continue
            yield channel, cue

    def _media_has_priority(self, name: str, kind: str, priority: int) -> bool:
        """Mudlet's rule for a Play that gives a priority (1 to 100; 0 is none set, and always
        plays): a different sound of the same type, playing with an equal or higher priority,
        keeps this one from starting, and a lower one is stopped to make room."""
        if priority <= 0:
            return True
        rivals = [
            (channel, cue) for channel, cue in self._media_cues.items()
            if cue["type"] == kind and cue["name"] != name and int(cue["priority"]) > 0
            and self.sound.is_playing(channel)
        ]
        if any(int(cue["priority"]) >= priority for _channel, cue in rivals):
            return False
        for channel, _cue in rivals:
            self._stop_media(channel)
        return True

    def _media_resume(self, name: str, kind: str, key: str, body: dict) -> bool:
        """A Play for a sound or track that a Pause left paused carries on where it stopped,
        unless the Play says continue false. True when one was resumed."""
        if not _media_continues(body.get("continue")):
            return False
        for channel, cue in self._media_cues.items():
            same = cue["type"] == kind and cue["name"] == name and (not key or cue["key"] == key)
            if same and self.sound.state(channel) == PAUSED:
                self.sound.resume(channel)
                if self._diag is not None:
                    self._diag.event("media.resume", file=name, channel=channel)
                return True
        return False

    def _media_pause(self, body: dict) -> None:
        """Client.Media.Pause: pause the cues matching every field given, all of them when it
        gives none, as Stop matches. ``priority`` pauses those at or below it."""
        ceiling = _media_int(body.get("priority"), None, 0, 100)
        for channel, cue in self._media_matching(body):
            if ceiling is not None and int(cue["priority"]) > ceiling:  # type: ignore[call-overload]
                continue
            if self.sound.is_playing(channel):
                self.sound.pause(channel)

    def _media_update(self, body: dict) -> None:
        """Client.Media.Update: change a playing cue's volume, or where it comes from, gliding
        over ``duration`` milliseconds. It has to name the cue by name, key or tag: an Update
        naming none would move every sound the MUD is playing, so it's refused, as Mudlet does."""
        if not any(body.get(field) not in (None, "") for field in ("name", "key", "tag")):
            if self._diag is not None:
                self._diag.event("media.update", refused="names no sound")
            return
        volume = _media_int(body.get("volume"), None, 0, 100)
        placing = body.get("pan") is not None or body.get("direction") is not None
        seconds = _media_int(body.get("duration"), 0, 0, _MEDIA_LONGEST_MS) / 1000
        for channel, cue in self._media_matching(body):
            gain = volume / 100 if volume is not None else None
            pan = None
            if placing and cue["type"] == "sound":
                pan = stereo_pan(body.get("pan"), body.get("direction"), self._media_facing)
                # A new direction takes over from an earlier pan, and a pan from a direction.
                cue["direction"] = body.get("direction") if body.get("pan") is None else None
                pan = pan or 0.0
            if gain is None and pan is None:
                continue
            if seconds > 0:
                self.sound.slide(channel, gain=gain, pan=pan, seconds=seconds)
            else:
                self.sound.adjust(channel, gain=gain, pan=pan)

    def _media_listener(self, body: dict) -> None:
        """Client.Media.Listener: the way the player now faces. Every sound placed by compass
        direction turns with them, gliding over a quarter of a second."""
        facing = facing_bearing(body.get("facing"))
        if facing is None:
            if self._diag is not None:
                self._diag.event("media.listener", refused=str(body.get("facing")))
            return
        self._media_facing = facing
        for channel, cue in self._media_cues.items():
            if cue["type"] != "sound" or cue.get("direction") is None:
                continue
            if self.sound.is_playing(channel):
                pan = stereo_pan(None, cue["direction"], facing)
                self.sound.slide(channel, pan=pan or 0.0, seconds=_MEDIA_TURN_SECONDS)

    def _forget_finished_media(self) -> None:
        """Drop finished one-shots so a long session's keyless cues don't pile up forever."""
        if len(self._media_cues) < _MEDIA_TRACKED:
            return
        for channel in list(self._media_cues):
            if channel.startswith("media-") and not self.sound.is_playing(channel):
                self._media_cues.pop(channel, None)
                self._media_gen.pop(channel, None)
