"""The engine<->renderer message contract (localhost WebSocket, JSON).

These type strings and builders are the single source of truth for the protocol;
the frontend mirrors the same ``type`` values. Speech is NOT sent here — the
engine self-voices natively (NVDA/etc.); the renderer handles display, Web Audio
sounds, status, and input/keys.
"""

from __future__ import annotations

from typing import Any

# Engine -> renderer
LINE = "line"
SOUND = "sound"
STOP_SOUND = "stop_sound"
MUSIC = "music"
STATUS = "status"
ECHO = "echo"
REVIEW = "review"
# Engine -> renderer: offer this line for copying, all of it or any part (Ctrl+Shift+C).
COPY_LINE = "copy_line"
CONNECTED = "connected"
DISCONNECTED = "disconnected"
FIND_RESULT = "find_result"  # outcome of a FIND, so the renderer can follow with its caret
INPUT_MASKED = "input_masked"  # the server is hiding typed input (a password prompt)
# Engine -> renderer: put this text in the command box and send nothing. What <SEND PROMPT>
# asks for, and what a menu of commands will ask for later.
INPUT_TEXT = "input_text"
# Engine -> renderer: ask which of a <SEND> menu's commands the player meant. Nothing is sent
# until the answer comes back as a MENU_CHOICE.
MENU = "menu"
# Engine -> renderer: open this URL. Used where there is no native window to ask (the web UI
# runs inside the browser), so the resource is opened by the page rather than by the engine.
OPEN_URL = "open_url"

# Renderer -> engine
INPUT = "input"
KEY = "key"
CONNECT = "connect"
DISCONNECT = "disconnect"
CLIENT_ERROR = "client_error"  # renderer reporting a failure (e.g. Web Audio load/decode)
FIND = "find"  # search the scrollback buffer; the engine owns the search, the renderer follows
LINK = "link"  # the player activated an MXP link the renderer drew; the engine decides what it does
# Enter on a line of the output box: follow that line's link. Carries the line's text and how
# far it is from the end, since the box holds fewer lines than the buffer.
LINK_AT = "link_at"
# Renderer -> engine: the command picked from a MENU, sent by the engine and not the renderer.
MENU_CHOICE = "menu_choice"


def line(
    text: str,
    channel: str = "main",
    gagged: bool = False,
    display_when_gagged: bool = False,
    links: list[tuple[int, int, str, bool, list[list[str]]]] | None = None,
    images: list[tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """One rendered line. ``links`` are (start, end, command, insert, choices) into ``text``.

    Sent as offsets rather than as markup so the renderer builds real anchors from plain
    text -- the engine has already decided what is text and what is a link, and re-parsing
    markup in the renderer would be a second, divergent answer to that question. ``insert``
    says following the link types its command rather than sending it, and ``choices`` is
    empty for a plain link and a list of (command, caption) pairs for a menu.

    ``images`` are (offset, url) for inline graphics whose address the player's world could
    resolve. Only http/https arrives here: anything else is refused before it is sent.
    """
    return {
        "type": LINE,
        "text": text,
        "channel": channel,
        "gagged": gagged,
        "display_when_gagged": display_when_gagged,
        "links": [list(span) for span in (links or [])],
        "images": [list(span) for span in (images or [])],
    }


def sound(
    file: str, channel: str = "sound", gain: float = 1.0, pan: float = 0.0, loop: bool = False
) -> dict[str, Any]:
    return {"type": SOUND, "file": file, "channel": channel, "gain": gain, "pan": pan, "loop": loop}


def stop_sound(channel: str = "sound") -> dict[str, Any]:
    return {"type": STOP_SOUND, "channel": channel}


def music(file: str, channel: str = "music", gain: float = 1.0) -> dict[str, Any]:
    return {"type": MUSIC, "file": file, "channel": channel, "gain": gain}


def status(gauges: dict[str, Any]) -> dict[str, Any]:
    return {"type": STATUS, "gauges": gauges}


def echo(text: str, channel: str = "main") -> dict[str, Any]:
    return {"type": ECHO, "text": text, "channel": channel}


def review(text: str) -> dict[str, Any]:
    return {"type": REVIEW, "text": text}


def copy_line(text: str) -> dict[str, Any]:
    """The line the review cursor is on, for the renderer to offer for copying."""
    return {"type": COPY_LINE, "text": text}


def open_url(url: str) -> dict[str, Any]:
    return {"type": OPEN_URL, "url": url}


def find(
    term: str,
    *,
    forward: bool = False,
    case_sensitive: bool = False,
    restart: bool = False,
    from_bottom: bool = False,
    text: str = "",
    from_end: int = 0,
) -> dict[str, Any]:
    """Ask the engine to search the scrollback. Backwards by default: it is history.

    ``restart`` starts a new search rather than going on from the last hit. ``text`` and
    ``from_end`` are the line the reader is standing on -- the output box keeps a caret of its
    own, which the engine cannot see -- so the search runs from there; ``from_bottom`` is the
    reader who is below the newest line, where going back to the output puts them.
    """
    return {
        "type": FIND,
        "term": term,
        "forward": forward,
        "case_sensitive": case_sensitive,
        "restart": restart,
        "from_bottom": from_bottom,
        "text": text,
        "from_end": from_end,
    }


def find_result(text: str, *, found: bool) -> dict[str, Any]:
    """The matched line, so the renderer can put its own caret on the same text."""
    return {"type": FIND_RESULT, "text": text, "found": found}


def connected(world: str) -> dict[str, Any]:
    return {"type": CONNECTED, "world": world}


def disconnected() -> dict[str, Any]:
    return {"type": DISCONNECTED}


def input_masked(masked: bool) -> dict[str, Any]:
    """Whether what the user types now is a secret the renderer must not keep in history."""
    return {"type": INPUT_MASKED, "masked": masked}


def menu(text: str, choices: list[tuple[str, str]]) -> dict[str, Any]:
    """Ask which of a menu's commands the player meant. Nothing is sent by asking."""
    return {"type": MENU, "text": text, "choices": [list(choice) for choice in choices]}


def input_text(text: str) -> dict[str, Any]:
    """Put ``text`` in the command box, replacing what is there; nothing is sent.

    A link that carries half a command is the case this exists for: the player finishes it,
    or clears it, and decides when (and whether) it goes to the MUD.
    """
    return {"type": INPUT_TEXT, "text": text}


def input_message(text: str) -> dict[str, Any]:
    return {"type": INPUT, "text": text}


def key_message(key: str) -> dict[str, Any]:
    return {"type": KEY, "key": key}


def client_error(scope: str, file: str = "", error: str = "") -> dict[str, Any]:
    return {"type": CLIENT_ERROR, "scope": scope, "file": file, "error": error}
