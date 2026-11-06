"""One world's session, connecting the MUD, automation, sound and the window.

Import EngineApp from this package. Its constructor and connection lifecycle live in
engine_app.py; the sibling modules hold its methods grouped by job. Each mixin documents the
state it uses from EngineApp.__init__. The automation engine's side effects go through sink.py."""

from genericmud.app.engine_app import EngineApp

__all__ = ["EngineApp"]
