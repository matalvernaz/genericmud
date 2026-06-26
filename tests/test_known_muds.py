"""The curated MUD -> connection table used when a pack ships no world file."""

from __future__ import annotations

from genericmud.packs import known_muds


def test_lookup_is_case_and_punctuation_insensitive():
    for name in ("Star Conquest", "star conquest", "STARCONQUEST"):
        world = known_muds.lookup(name)
        assert world is not None
        assert (world.host, world.port, world.tls) == ("squidsoft.net", 7777, False)


def test_lookup_matches_the_vault_mud_field_spellings():
    # The catalogue labels these "cosmic Rage" and "Prometheus: the Eternal Wars".
    assert known_muds.lookup("cosmic Rage").host == "cosmicrage.nathantech.net"
    assert known_muds.lookup("Prometheus: the Eternal Wars").port == 2223


def test_lookup_names_the_world_after_the_mud():
    assert known_muds.lookup("Miriani").name == "Miriani"


def test_lookup_unknown_mud_returns_none():
    assert known_muds.lookup("No Such MUD") is None
    assert known_muds.lookup("") is None
