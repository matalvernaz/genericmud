"""Every variable a world knows about, as rows a screen reader can read (issue #4).

Two kinds feed the list. MUD data is what the server sent over GMCP, MSDP or MSSP;
the engine keeps each value twice, under its bare name (``Char.Vitals``), which is what
``${mud:...}`` resolves against, and under a source prefix (``gmcp.Char.Vitals``) so the
source isn't lost. Rows are built from the bare names, so a row's reference reads what
the row shows, and the prefixed copy only says which protocol it came from. Script
variables are the values packs and automation scripts saved, read with ``${script:...}``.

A GMCP package arrives as one nested object. A player wants one number out of it,
not the object, so tables are flattened to the dotted paths ``resolve_mud_var``
already understands: ``Char.Vitals`` becomes ``Char.Vitals.hp``, ``Char.Vitals.mp``.
Three shapes stay whole instead, because a dotted path couldn't read them back: a list
(indexing into one is script territory, and a long inventory would swamp the list), a
table with a dot inside one of its keys, and a path a longer package name would answer
first (package ``Char``'s ``Vitals.hp`` when package ``Char.Vitals`` also exists).

The server controls how much of this there is, so the walk itself stops at the limit.
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


_ENCODER = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False)  # as format_value


def _display_value(value: object) -> str:
    """:func:`format_value`, but only as much of it as the dialog can show.

    A server can send one table or list big enough that serialising all of it, on the
    loop thread every session shares, is itself the problem, even though it's one row.
    The encoder is read chunk by chunk and stops just past MAX_VALUE_CHARS; anything
    longer ends up shortened for display anyway.
    """
    if isinstance(value, (dict, list, tuple)):
        parts: list[str] = []
        size = 0
        for chunk in _ENCODER.iterencode(value):
            parts.append(chunk)
            size += len(chunk)
            if size > MAX_VALUE_CHARS:
                break
        text = "".join(parts)
    else:
        text = format_value(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS + 1]


def _referenceable(name: str) -> bool:
    """Whether ``${scope:name}`` reads ``name`` back: a reference ends at the first brace
    and its name is trimmed, so a brace or edge space in a name has no reference."""
    return bool(name) and name == name.strip() and "{" not in name and "}" not in name


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


def _source_by_key(mud_vars: dict[str, object]) -> dict[str, str]:
    """Each bare key, with the protocol named by the source-prefixed copy stored beside it.

    A prefixed copy is ``<source>.<bare key>`` where that bare key exists too. A bare MSDP
    variable that merely looks prefixed (``gmcp.hack``) has no ``hack`` beside it, so it
    stays a bare key, and its own copy (``msdp.gmcp.hack``) names its real source.
    """
    by_folded = {str(key).casefold(): str(key) for key in mud_vars}
    candidates: dict[str, list[tuple[str, bool]]] = {}
    for key, value in mud_vars.items():
        source, separator, bare = str(key).partition(".")
        if separator and source in _MUD_SOURCES and bare:
            original = by_folded.get(bare.casefold())
            if original is not None:
                # The engine stores one object under both names, so the copy that IS the
                # bare value belongs to whichever protocol wrote it last.
                candidates.setdefault(original, []).append((source, mud_vars[original] is value))
    return {
        original: next((source for source, current in found if current), found[0][0])
        for original, found in candidates.items()
    }


class _Walk:
    """One flattening pass with a row budget, shared across every value it visits."""

    def __init__(self, budget: int, top_level: set[str]) -> None:
        self.budget = budget
        self.top_level = top_level  # casefolded bare keys, for the longest-key rule
        self.rows: list[VariableEntry] = []

    @property
    def full(self) -> bool:
        return len(self.rows) >= self.budget

    def add(self, path: str, value: object, source: str) -> None:
        if not self.full and _referenceable(path):
            self.rows.append(VariableEntry(path, _display_value(value), source))

    def flatten(
        self, path: str, value: object, source: str, key_parts: int, depth: int = 0
    ) -> None:
        if self.full:
            return
        if self._answered_by_a_longer_key(path, key_parts):
            return  # resolve_mud_var would read another package's value for this name
        if not (isinstance(value, dict) and value and depth < MAX_DEPTH):
            self.add(path, value, source)
            return
        whole_listed = False
        for key, child in value.items():
            if "." in str(key):
                # A dotted path can't reach this key, so the one reference that still reads
                # it is the table's own; list the table once, and its other keys as usual.
                if not whole_listed:
                    self.add(path, value, source)
                    whole_listed = True
                continue
            self.flatten(f"{path}.{key}", child, source, key_parts, depth + 1)
            if self.full:
                return

    def _answered_by_a_longer_key(self, path: str, key_parts: int) -> bool:
        """``resolve_mud_var`` tries the longest bare key that prefixes a name first."""
        parts = path.split(".")
        return any(
            ".".join(parts[:boundary]).casefold() in self.top_level
            for boundary in range(len(parts), key_parts, -1)
        )


def list_variables(
    mud_vars: dict[str, object],
    script_vars: dict[str, str],
    *,
    limit: int = MAX_ENTRIES,
) -> tuple[list[VariableEntry], bool]:
    """Rows for every variable, MUD data first (GMCP, MSDP, MSSP), then script values.

    Returns the rows and whether ``limit`` cut the list short. The walk stops one row past
    the limit, so an enormous table costs no more than a list of ``limit`` rows.
    """
    sources = _source_by_key(mud_vars)
    walk = _Walk(limit + 1, {key.casefold() for key in sources})
    for key, source in sources.items():
        walk.flatten(key, mud_vars[key], source, key.count(".") + 1)
        if walk.full:
            break
    for name, value in script_vars.items():
        if walk.full:
            break
        walk.add(str(name), value, SCRIPT)
    entries = walk.rows
    entries.sort(key=lambda entry: (_SOURCE_ORDER[entry.source], entry.name.casefold()))
    return entries[:limit], len(entries) > limit


def filter_variables(entries: list[VariableEntry], text: str) -> list[VariableEntry]:
    """The rows whose name contains ``text`` (any case); every row when it's blank."""
    needle = text.strip().casefold()
    if not needle:
        return list(entries)
    return [entry for entry in entries if needle in entry.name.casefold()]
