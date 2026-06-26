"""Connection details for MUDs whose soundpacks ship no world file.

VIPMud ``.set`` packs (Star Conquest, Miriani, ...) carry sounds and scripts but no
host/port — the player is expected to already have the MUD set up in their client. When the
setup flow can't read a world out of a pack (:func:`~genericmud.packs.world_import.world_from_pack`
returns ``None``), it looks the MUD up here by name so the Connect dialog/world is still
fully populated instead of blank. Keyed on the vault's MUD name, matched case- and
punctuation-insensitively.

Values are each MUD's published telnet endpoint, verified from the MUD's own connection
page. Add an entry as new packs need one; an unknown MUD just falls back to a name-only
prefill, so this is best-effort, never load-bearing.
"""

from __future__ import annotations

import re

from genericmud.config.worlds import World

# Normalized MUD name -> (host, port, tls). Verified against each MUD's connection page
# (mudstats / the MUD's own site), June 2026. Non-TLS default ports; players can edit.
_KNOWN: dict[str, tuple[str, int, bool]] = {
    "starconquest": ("squidsoft.net", 7777, False),
    "miriani": ("toastsoft.net", 1234, False),
    "cosmicrage": ("cosmicrage.nathantech.net", 7777, False),
    "prometheustheeternalwars": ("prometheus-enterprises.com", 2223, False),
    "cogg": ("cogg.contrarium.net", 4001, False),
}


def _normalize(name: str) -> str:
    """Lowercase, strip non-alphanumerics: 'cosmic Rage' == 'Cosmic Rage' == 'cosmicrage'."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def lookup(mud_name: str) -> World | None:
    """The published connection for ``mud_name``, or ``None`` if it isn't in the table.

    The returned :class:`World` is named after the MUD; the caller persists and may edit it.
    """
    entry = _KNOWN.get(_normalize(mud_name or ""))
    if entry is None:
        return None
    host, port, tls = entry
    return World(name=mud_name, host=host, port=port, tls=tls)
