"""The world's own scripting space: one Lua state per world, holding both vocabularies.

A world has one place to write automation -- the ``.lua`` files in its ``scripts`` folder, and the
code a rule sends to script -- and this is the Lua state they share, so a function defined in one
file is callable from another file or from a rule, and a value one of them leaves behind is there
for the next.

MUSHclient's own vocabulary is installed in that state as well: its ``Send``/``Note``/
``GetVariable``/``AddTrigger``/``DoAfter``/``PlaySound`` family, the ``world`` table it mirrors
them onto, and the constants and libraries a MUSHclient script expects (``trigger_flag``,
``sendto``, ``error_code``, ``bit``, ``utils``). That is what lets code written for MUSHclient be
used here -- including ``LoadPlugin``, which loads a plugin file kept in the world's own folder
into this same state, after which ``CallPlugin`` drives it. ``mud.*`` remains the documented way
to write your own automation; it is installed beside them, not instead of them.

A loaded soundpack is a state of its own, and this is not it: the two coexist and both answer the
MUD's lines, but a pack's variables, triggers and plugins are the pack's.

Both vocabularies, rather than one, is the point of that. Someone who learned MUSHclient from its
own documentation, or who has automation they made in MUSHclient that only ever ran there, can
write it here as it stands instead of translating it first; ``mud.*`` stays the shorter way to
write it, and the only one that speaks, follows the data the MUD sends, and reaches the other
open sessions.

The state stays sandboxed: MUSHclient's names are a vocabulary, not a grant of the operating
system.
"""

from __future__ import annotations

from pathlib import Path

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack


def world_runtime(
    engine: AutomationEngine, pack_dir: str | Path, *, source_prefix: str
) -> MushclientPack:
    """Build the one Lua state this world's own scripting runs in.

    ``source_prefix`` is the engine source every rule the scripts register is filed under, so a
    reload of the set can take its rules away without touching anything else.
    """
    pack = MushclientPack(
        ScriptApi(engine, source=source_prefix, base_dir=str(pack_dir)), player_scripts=True
    )
    # MUSHclient embeds Lua 5.1, and so does this state -- but 5.1 calls `unpack` a global and
    # every later Lua calls it `table.unpack`. One line buys the grace to be wrong about which
    # one a script was written for, and costs nothing otherwise.
    pack.run_source("if table.unpack == nil then table.unpack = unpack end")
    return pack
