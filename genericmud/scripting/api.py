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
        self._engine.sink.play(self._resolve(file), channel, gain, pan, loop)

    def stop(self, channel: str = "sound") -> None:
        self._engine.sink.stop(channel)

    def music(self, file: str, channel: str = "music") -> None:
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
        if self._base_dir and not os.path.isabs(file):
            file = os.path.join(self._base_dir, file)
        # Collapse the doubled slash MUSHclient packs build from GetInfo() (a trailing slash
        # plus a plugin's leading one). NOT os.path.normpath -- on Windows it flips / to \,
        # mangling the forward-slash paths packs use (and breaking exact-path tests).
        return re.sub(r"/{2,}", "/", file) if file else file
