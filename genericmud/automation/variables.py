"""Every variable a world knows about, as rows a screen reader can read (issue #4).

Two kinds feed the list. MUD data is what the server sent over GMCP, MSDP or MSSP;
the engine keeps each value twice, under its bare name (``Char.Vitals``) for
``${mud:...}`` lookups and under a source prefix (``gmcp.Char.Vitals``) so the source
isn't lost. The prefixed copies are the ones read here, which is how each row knows its
source without the list showing every value twice. Script variables are the values
packs and automation scripts saved, read with ``${script:...}``.

A GMCP package arrives as one nested object. A player wants one number out of it,
not the object, so tables are flattened to the dotted paths ``resolve_mud_var``
already understands: ``Char.Vitals`` becomes ``Char.Vitals.hp``, ``Char.Vitals.mp``.
A list stays one row: indexing into one is script territory, and a long inventory
list would otherwise swamp everything else.

The server controls how much of this there is, so the listing is capped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

GMCP, MSDP, MSSP, SCRIPT = "gmcp", "msdp", "mssp", "script"
_MUD_SOURCES = (GMCP, MSDP, MSSP)
_SOURCE_ORDER = {source: index for index, source in enumerate((*_MUD_SOURCES, SCRIPT))}
SOURCE_LABELS = {GMCP: "GMCP", MSDP: "MSDP", MSSP: "MSSP", SCRIPT: "script"}

MAX_ENTRIES = 5000  # rows listed at most; a hostile or chatty server can send far more
MAX_DEPTH = 16  # nested tables flattened this deep; anything deeper is one JSON row
ROW_VALUE_CHARS = 120  # a row carries a short form of its value; the full one is details
MAX_VALUE_CHARS = 4000  # the most of one value the dialog shows or speaks


def format_value(value: object) -> str:
    """A value as ``${...}`` expansion puts it into a command or speech.

    Tables and lists become compact JSON, booleans lowercase (``true``), a missing
    value the empty string, anything else ``str``. One function so the list shows
    exactly what a reference to the variable would produce.
    """
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    return "" if value is None else str(value)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


@dataclass(frozen=True)
class VariableEntry:
    """One readable variable: where it came from and what it holds right now."""

    name: str  # the dotted path or script variable name
    value: str  # format_value() of the current value
    source: str  # GMCP, MSDP, MSSP or SCRIPT

    @property
    def reference(self) -> str:
        """What to type in a command or speech field to use this value."""
        scope = SCRIPT if self.source == SCRIPT else "mud"
        return f"${{{scope}:{self.name}}}"

    @property
    def row(self) -> str:
        """The list row: commas between parts, since a screen reader says "dash"."""
        value = _shorten(self.value, ROW_VALUE_CHARS) if self.value else "empty"
        return f"{self.name}, {value}, {SOURCE_LABELS[self.source]}"

    @property
    def details(self) -> str:
        value = _shorten(self.value, MAX_VALUE_CHARS) if self.value else "(empty)"
        return (
            f"Value: {value}\n"
            f"From: {SOURCE_LABELS[self.source]}\n"
            f"Use it as: {self.reference}"
        )


def _flatten(
    path: str, value: object, source: str, out: list[VariableEntry], depth: int = 0
) -> None:
    if isinstance(value, dict) and value and depth < MAX_DEPTH:
        for key, child in value.items():
            _flatten(f"{path}.{key}", child, source, out, depth + 1)
        return
    out.append(VariableEntry(path, format_value(value), source))


def list_variables(
    mud_vars: dict[str, object],
    script_vars: dict[str, str],
    *,
    limit: int = MAX_ENTRIES,
) -> tuple[list[VariableEntry], bool]:
    """Rows for every variable, MUD data first (GMCP, MSDP, MSSP), then script values.

    Returns the rows and whether ``limit`` cut the list short.
    """
    entries: list[VariableEntry] = []
    for key, value in mud_vars.items():
        source, separator, name = str(key).partition(".")
        if separator and source in _MUD_SOURCES and name:
            _flatten(name, value, source, entries)
    entries.extend(
        VariableEntry(str(name), format_value(value), SCRIPT)
        for name, value in script_vars.items()
    )
    entries.sort(key=lambda entry: (_SOURCE_ORDER[entry.source], entry.name.casefold()))
    return entries[:limit], len(entries) > limit


def filter_variables(entries: list[VariableEntry], text: str) -> list[VariableEntry]:
    """The rows whose name contains ``text`` (any case); every row when it's blank."""
    needle = text.strip().casefold()
    if not needle:
        return list(entries)
    return [entry for entry in entries if needle in entry.name.casefold()]
