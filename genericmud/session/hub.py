"""SessionHub: a shared bus for concurrent character sessions.

VIPMud's "passing information between sessions": a session can send a command to
another by name, broadcast to the rest, and read/write shared variables. Each
:class:`~genericmud.app.EngineApp` registers a deliver callback (its input
dispatcher) under its world name; the hub is owned above the sessions (the wx
frame) and shared by all of them.
"""

from __future__ import annotations

from collections.abc import Callable


class SessionHub:
    def __init__(self) -> None:
        self._deliver: dict[str, Callable[[str], None]] = {}
        self._shared: dict[str, str] = {}

    def register(self, name: str, deliver: Callable[[str], None]) -> None:
        self._deliver[name] = deliver

    def unregister(self, name: str) -> None:
        self._deliver.pop(name, None)

    def sessions(self) -> list[str]:
        return list(self._deliver)

    def send_to(self, name: str, text: str) -> bool:
        """Deliver ``text`` to ``name`` as if typed there; False if no such session."""
        deliver = self._deliver.get(name)
        if deliver is None:
            return False
        deliver(text)
        return True

    def broadcast(self, text: str, *, exclude: str | None = None) -> int:
        """Deliver ``text`` to every session except ``exclude``; returns the count."""
        targets = [deliver for name, deliver in self._deliver.items() if name != exclude]
        for deliver in targets:
            deliver(text)
        return len(targets)

    def shared_get(self, key: str) -> str:
        return self._shared.get(key, "")

    def shared_set(self, key: str, value: object) -> None:
        self._shared[key] = str(value)
