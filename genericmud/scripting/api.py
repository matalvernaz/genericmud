"""Canonical scripting surface every dialect binds to.

The native Lua ``mud`` table, the MUSHclient compat globals, and the VIPMud
``.set`` interpreter all call through one :class:`ScriptApi` instance. It is a
thin facade over an :class:`AutomationEngine` plus the pack's base directory
(for resolving relative sound paths), so behaviour is identical no matter which
dialect authored a rule.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from genericmud.automation.channels import ChannelPolicy
from genericmud.automation.engine import AutomationEngine, Callback


class ScriptApi:
    def __init__(
        self, engine: AutomationEngine, *, source: str = "", base_dir: str | None = None
    ) -> None:
        self._engine = engine
        self._source = source
        self._base_dir = base_dir
        self._sounds_index: dict[str, str] = {}  # basename(lower) -> full path under @sppath
        self._sounds_index_key: str | None = None  # the @sppath the index was built for

    # --- output ---

    def send(self, text: str) -> None:
        self._engine.sink.send(str(text))

    def echo(self, text: str, channel: str = "main") -> None:
        self._engine.sink.echo(str(text), channel)

    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None:
        self._engine.sink.speak(str(text), channel, interrupt)

    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
    ) -> None:
        if self._engine.diag is not None:
            self._engine.diag.event(
                "play.entry", source=self._source or "?", file=file,
                channel=channel, gain=gain, loop=loop,
            )
        self._engine.sink.play(self._resolve(file), channel, gain, pan, loop)

    def stop(self, channel: str = "sound") -> None:
        self._engine.sink.stop(channel)

    def music(self, file: str, channel: str = "music") -> None:
        if self._engine.diag is not None:
            self._engine.diag.event(
                "play.entry", source=self._source or "?", file=file, channel=channel, kind="music"
            )
        self._engine.sink.music(self._resolve(file), channel)

    # --- variables ---

    def get_var(self, name: str) -> str:
        return self._engine.get_var(name)

    def set_var(self, name: str, value: object) -> None:
        self._engine.set_var(name, value)

    # --- registration ---

    def add_trigger(self, pattern: str, callback: Callback, **opts: object) -> None:
        opts.setdefault("source", self._source)
        self._engine.add_trigger(pattern, callback, **opts)  # type: ignore[arg-type]

    def add_alias(self, pattern: str, callback: Callback, **opts: object) -> None:
        opts.setdefault("source", self._source)
        self._engine.add_alias(pattern, callback, **opts)  # type: ignore[arg-type]

    def add_key(self, key: str, callback: Callback) -> None:
        self._engine.add_key(key, callback, source=self._source)

    def add_timer(self, delay: float, callback: Callable[[], None]) -> None:
        self._engine.sink.schedule(delay, callback)

    def set_channel(
        self,
        name: str,
        *,
        speak: bool = True,
        display: bool = True,
        interrupt: bool = False,
        voice: str | None = None,
    ) -> None:
        self._engine.channels.set_policy(
            name, ChannelPolicy(speak=speak, display=display, interrupt=interrupt, voice=voice)
        )

    def set_volume(self, category: str, gain: float) -> None:
        self._engine.sound.set_volume(category, float(gain))

    def mute(self, category: str, muted: bool = True) -> None:
        self._engine.sound.set_muted(category, bool(muted))

    def flush(self) -> None:
        """Stop every playing cue (panic path; VIPMud ``#pc 0 stop``)."""
        self._engine.sound.flush()

    def set_master(self, gain: float) -> None:
        self._engine.sound.set_master(float(gain))

    # --- cross-session (multi-character play) ---

    def send_to(self, session: str, text: str) -> bool:
        if self._engine.hub is None:
            return False
        return self._engine.hub.send_to(session, str(text))

    def broadcast(self, text: str) -> int:
        if self._engine.hub is None:
            return 0
        return self._engine.hub.broadcast(str(text), exclude=self._engine.session_name)

    def sessions(self) -> list[str]:
        return self._engine.hub.sessions() if self._engine.hub is not None else []

    def shared_get(self, key: str) -> str:
        return self._engine.hub.shared_get(key) if self._engine.hub is not None else ""

    def shared_set(self, key: str, value: object) -> None:
        if self._engine.hub is not None:
            self._engine.hub.shared_set(key, value)

    @property
    def base_dir(self) -> str | None:
        """The pack's root dir, for dialects that resolve their own paths (e.g. GetInfo)."""
        return self._base_dir

    def _resolve(self, file: str) -> str:
        original = file
        if self._base_dir and not os.path.isabs(file):
            file = os.path.join(self._base_dir, file)
        # Collapse the doubled slash MUSHclient packs build from GetInfo() (a trailing slash
        # plus a plugin's leading one). NOT os.path.normpath -- on Windows it flips / to \,
        # mangling the forward-slash paths packs use (and breaking exact-path tests).
        resolved = re.sub(r"/{2,}", "/", file) if file else file
        exists = bool(resolved) and os.path.exists(resolved)
        fallback = self._find_in_sounds_dir(resolved) if resolved and not exists else None
        final = fallback if fallback is not None else resolved
        if self._engine.diag is not None:
            self._engine.diag.event(
                "play.resolve", input=original, resolved=final,
                exists=(exists or fallback is not None),
                fallback=("sppath" if fallback is not None else "none"),
                sppath=self._engine.get_var("sppath") or "",
            )
        return final

    def _find_in_sounds_dir(self, path: str) -> str | None:
        """Locate a missing sound by basename under the user's Sounds folder (``@sppath``).

        Packs hardcode where their audio lives; this lets the world's Sounds folder point at
        sounds kept elsewhere (e.g. Erion's separate sound repo), regardless of the pack's own
        path assumptions. Indexed once per folder; a basename collision keeps the first match
        (filenames are unique within a soundpack), and the walk only runs on a cache miss.
        """
        sounds_dir = self._engine.get_var("sppath")
        if not sounds_dir or not os.path.isdir(sounds_dir):
            return None
        if self._sounds_index_key != sounds_dir:
            index: dict[str, str] = {}
            for root, _dirs, files in os.walk(sounds_dir):
                for name in files:
                    index.setdefault(name.lower(), os.path.join(root, name))
            self._sounds_index = index
            self._sounds_index_key = sounds_dir
        # Split on both separators: a Windows-authored pack path keeps its backslashes when
        # resolved on Linux, where os.path.basename only honours "/" and would miss the leaf.
        leaf = path.replace("\\", "/").rsplit("/", 1)[-1]
        return self._sounds_index.get(leaf.lower())
