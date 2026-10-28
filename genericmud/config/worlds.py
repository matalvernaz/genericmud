"""Saved MUD worlds (connection, encoding, command stacking, logging), persisted as TOML.

tomllib is read-only, so saving hand-writes the small, well-defined schema.
"""

from __future__ import annotations

import sys
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from genericmud.config.atomic import atomic_write_text
from genericmud.protocol.charset import AUTO, normalize_encoding

MIN_PORT = 1
MAX_PORT = 65535
DEFAULT_PORT = 4000
DEFAULT_SEPARATOR = ";"
# genericMud's own commands start with this ("/goto"), so it can't separate commands too.
CLIENT_COMMAND_PREFIX = "/"
# How a world names its log files: a new file each session (the original behaviour), one per
# day, or one for the world. Each appends.
LOG_FILES = ("session", "day", "world")
# How a stored username and password are sent. "prompts" answers the MUD's own prompts --
# named that way rather than "automatic", which would promise detection of the right method
# that it does not do. The rest are MUSHclient's habits, for MUDs that expect the login typed
# blind. MXP is deliberately absent: the client does not parse MXP yet (see protocol/telnet.py
# and connection.py), so a MXP login could not be sent without advertising a protocol whose
# markup would then leak into the output.
CONNECT_METHODS = ("none", "prompts", "mxp", "mush", "diku")
# What an absent setting means, and what a new world starts on. "none" for both: with no
# login stored there is nothing to send, so a world that offered to log in and then did
# nothing would be worse than one that never offered. It also keeps the setting honest --
# nobody gets a login method they didn't choose.
DEFAULT_CONNECT_METHOD = "none"


@dataclass
class World:
    name: str
    host: str
    port: int
    tls: bool = False
    sounds: str | None = None
    # Character encoding for this MUD's text, both directions (protocol/charset.py). Always
    # a supported setting: anything else becomes "auto", so a typo can't break a world.
    encoding: str = AUTO
    # Command stacking: "n;n;look" sends three commands. The separator is one character, and
    # typed twice it's sent as itself (MUSHclient's rule). Off sends every line whole.
    command_stacking: bool = True
    command_separator: str = DEFAULT_SEPARATOR
    # Logging: start when the world connects, which files to write (LOG_FILES), and whether
    # the commands you type go in with the MUD's output.
    log_automatically: bool = False
    log_files: str = "session"
    log_commands: bool = True
    # Login: how the stored username and password are sent (CONNECT_METHODS), and commands
    # to type once the connection is up, one per line. The username and password themselves
    # live in the credential store, never here -- a world file is what "Export This World"
    # zips up and what people share.
    connect_method: str = DEFAULT_CONNECT_METHOD
    post_connect_commands: str = ""
    # Fetch the sounds a MUD offers to download (MSP U=, GMCP Client.Media). The MSP spec has
    # clients let the player switch that off.
    download_sounds: bool = True
    # Understand MXP markup. On by default because the parser strips every tag, so a server
    # sending MXP reads as ordinary text; off is the escape hatch for a server that sends
    # something this parser gets wrong.
    mxp: bool = True
    # Where an <IMAGE> looks for its file when the tag carries no URL of its own. Most MUDs
    # expect the client to have this configured, the same way MSP's U= expects a sounds
    # location -- without one an image can't be located, and is quietly dropped.
    images_url: str = ""

    def __post_init__(self) -> None:
        self.encoding = normalize_encoding(self.encoding)
        self.command_separator = normalize_separator(self.command_separator)
        self.log_files = normalize_log_files(self.log_files)
        self.connect_method = normalize_connect_method(self.connect_method)
        self.post_connect_commands = normalize_post_connect(self.post_connect_commands)
        self.images_url = normalize_images_url(self.images_url)
        # No real world name or host has a control character in it, and one gets in easily:
        # a shared world zip can carry a newline, Ctrl+Backspace in a Windows edit box
        # types a DEL. Keep them out of the name every rule, map and folder is filed under.
        self.name = _without_control_characters(self.name)
        self.host = _without_control_characters(self.host)


def separator_problem(value: str) -> str | None:
    """Why ``value`` can't separate commands, in words for the player; None when it can."""
    if len(value) != 1:
        return "The command separator has to be one character."
    if value.isspace() or unicodedata.category(value) == "Cc":
        return "The command separator can't be a space."
    if value == CLIENT_COMMAND_PREFIX:
        return "The command separator can't be /, which starts genericMud's own commands."
    return None


def normalize_separator(value: object) -> str:
    """A usable command separator: ``value``, or the default when it can't be one."""
    text = value if isinstance(value, str) else ""
    return text if separator_problem(text) is None else DEFAULT_SEPARATOR


def normalize_log_files(value: object) -> str:
    """One of LOG_FILES; anything else is a new file each session."""
    return value if value in LOG_FILES else "session"


def normalize_connect_method(value: object) -> str:
    """One of CONNECT_METHODS; anything else means no auto-connect."""
    return value if value in CONNECT_METHODS else DEFAULT_CONNECT_METHOD


def normalize_post_connect(value: object) -> str:
    """The commands to send after connecting: one per line, blank lines dropped.

    Reads either a TOML array of strings or one multi-line string, so a hand-edited world
    file can use whichever reads better.
    """
    if isinstance(value, (list, tuple)):
        raw = [item for item in value if isinstance(item, str)]
    elif isinstance(value, str):
        raw = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    else:
        raw = []
    return "\n".join(line.strip() for line in raw if line.strip())
def normalize_images_url(value: object) -> str:
    """A usable images location: web only, or "" for none.

    The web UI loads whatever is set here and a text client opens it in the browser, so a
    scheme that isn't the web -- ``file:``, a bare folder path, anything with a colon in
    front of it -- is not kept rather than left for something further along to deal with.
    """
    text = value.strip() if isinstance(value, str) else ""
    return text if text.lower().startswith(("http://", "https://")) else ""
def parse_port(value: object) -> int:
    """Return a valid TCP port, or raise ``ValueError`` with a user-facing message."""
    if isinstance(value, bool):
        raise ValueError("Port must be a whole number from 1 to 65535.")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be a whole number from 1 to 65535.") from exc
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError("Port must be a whole number from 1 to 65535.")
    return port


def config_dir() -> Path:
    """Where genericMud keeps soundpacks, worlds, logs, and credentials.

    In the shipped (frozen) build this is a folder beside the executable, so the
    whole app is portable: unzip it and everything — including downloaded packs —
    lives in that one directory. From source it's ``~/.genericmud``.

    Exception: a macOS ``.app`` bundle is read-only (and code-signed), so writing beside
    the executable would land inside ``genericMud.app/Contents/MacOS`` and fail. There we
    use the platform's per-user data location instead.
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "genericMud"
        return Path(sys.executable).resolve().parent / "genericmud-data"
    return Path.home() / ".genericmud"


def worlds_path() -> Path:
    return config_dir() / "worlds.toml"


def load_worlds(path: Path | None = None) -> list[World]:
    target = path or worlds_path()
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("world", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return []
    worlds: list[World] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host = str(entry.get("host", "")).strip()
        if not host:
            continue
        sounds = entry.get("sounds")
        if sounds is not None and not isinstance(sounds, str):
            sounds = None
        try:
            port = parse_port(entry.get("port", DEFAULT_PORT))
        except ValueError:
            continue
        worlds.append(
            World(
                name=str(entry.get("name", "")).strip() or host,
                host=host,
                port=port,
                tls=entry.get("tls") is True,
                sounds=sounds,
                encoding=normalize_encoding(entry.get("encoding")),
                command_stacking=entry.get("command_stacking") is not False,
                command_separator=normalize_separator(entry.get("command_separator")),
                log_automatically=entry.get("log_automatically") is True,
                log_files=normalize_log_files(entry.get("log_files")),
                log_commands=entry.get("log_commands") is not False,
                download_sounds=entry.get("download_sounds") is not False,
                connect_method=normalize_connect_method(entry.get("connect_method")),
                post_connect_commands=normalize_post_connect(
                    entry.get("post_connect_commands")
                ),
                mxp=entry.get("mxp") is not False,
                images_url=normalize_images_url(entry.get("images_url")),
            )
        )
    return worlds


def save_worlds(worlds: list[World], path: Path | None = None) -> None:
    target = path or worlds_path()
    blocks: list[str] = []
    for world in worlds:
        port = parse_port(world.port)
        lines = [
            "[[world]]",
            f"name = {_quote(world.name)}",
            f"host = {_quote(world.host)}",
            f"port = {port}",
            f"tls = {'true' if world.tls else 'false'}",
        ]
        if world.sounds:
            lines.append(f"sounds = {_quote(world.sounds)}")
        encoding = normalize_encoding(world.encoding)
        if encoding != AUTO:  # absent means auto, which keeps files older builds wrote as-is
            lines.append(f"encoding = {_quote(encoding)}")
        # The same for the rest: only what differs from the default is written.
        if not world.command_stacking:
            lines.append("command_stacking = false")
        separator = normalize_separator(world.command_separator)
        if separator != DEFAULT_SEPARATOR:
            lines.append(f"command_separator = {_quote(separator)}")
        if world.log_automatically:
            lines.append("log_automatically = true")
        log_files = normalize_log_files(world.log_files)
        if log_files != "session":
            lines.append(f"log_files = {_quote(log_files)}")
        if not world.log_commands:
            lines.append("log_commands = false")
        if not world.download_sounds:
            lines.append("download_sounds = false")
        connect_method = normalize_connect_method(world.connect_method)
        if connect_method != DEFAULT_CONNECT_METHOD:
            lines.append(f"connect_method = {_quote(connect_method)}")
        post_connect = normalize_post_connect(world.post_connect_commands)
        if post_connect:  # an array, so a command with a quote in it needs no escaping
            items = ", ".join(_quote(line) for line in post_connect.split("\n"))
            lines.append(f"post_connect_commands = [{items}]")
        if not world.mxp:
            lines.append("mxp = false")
        if world.images_url:
            lines.append(f"images_url = {_quote(world.images_url)}")
        blocks.append("\n".join(lines))
    atomic_write_text(target, "\n\n".join(blocks) + "\n")


def _without_control_characters(value: str) -> str:
    """``value`` with line breaks and tabs as spaces and other control characters gone."""
    kept = []
    for char in value:
        if char in "\t\n\r\x0b\x0c":
            kept.append(" ")
        elif unicodedata.category(char) != "Cc":
            kept.append(char)
    return "".join(kept).strip()


def _quote(value: str) -> str:
    """A TOML basic string. Control characters are escaped, not written raw: TOML forbids
    them unescaped, so a single one made tomllib reject the whole file, every saved world
    vanished from the Connect list, and the next save kept only the world being saved."""
    out = ['"']
    for char in value:
        if char in '\\"':
            out.append("\\" + char)
        elif unicodedata.category(char) == "Cc":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)

