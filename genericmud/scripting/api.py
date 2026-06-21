"""Canonical scripting surface every dialect binds to.

The native Lua ``mud`` table, the MUSHclient compat globals, and the VIPMud
``.set`` interpreter all call through one :class:`ScriptApi` instance. It is a
thin facade over an :class:`AutomationEngine` plus the pack's base directory
(for resolving relative sound paths), so behaviour is identical no matter which
dialect authored a rule.
"""

from __future__ import annotations

import os
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

    def _resolve(self, file: str) -> str:
        if self._base_dir and not os.path.isabs(file):
            return os.path.join(self._base_dir, file)
        return file
