"""Extracting world connection details from a pack's MUSHclient world file."""

from __future__ import annotations

from genericmud.config.worlds import World
from genericmud.packs.world_import import world_from_pack

# The real Cosmic Rage .MCL layout: DOCTYPE, iso-8859-1, and three port attributes
# where only the standalone `port` is the connect port — chat_port and proxy_port
# must NOT be mistaken for it.
MCL = """<?xml version="1.0" encoding="iso-8859-1"?>
<!DOCTYPE muclient>
<muclient>
<world
   chat_name="Name-not-set"
   chat_port="4050"
   name="Cosmic Rage"
   site="cosmicrage.earth"
   port="7777"
   proxy_port="1080"
>
<triggers></triggers>
</world>
</muclient>"""


def test_world_from_mcl_file(tmp_path):
    path = tmp_path / "cosmic rage.MCL"
    path.write_text(MCL, encoding="latin-1")
    assert world_from_pack(path) == World(name="Cosmic Rage", host="cosmicrage.earth", port=7777)


def test_chat_and_proxy_ports_are_not_mistaken_for_port(tmp_path):
    path = tmp_path / "w.mcl"
    path.write_text(MCL, encoding="latin-1")
    assert world_from_pack(path).port == 7777  # not chat_port 4050 or proxy_port 1080


def test_scans_a_pack_directory_for_the_world_file(tmp_path):
    (tmp_path / "worlds").mkdir()
    (tmp_path / "worlds" / "cr.MCL").write_text(MCL, encoding="latin-1")
    world = world_from_pack(tmp_path)
    assert world is not None and (world.host, world.port) == ("cosmicrage.earth", 7777)


def test_pack_without_a_world_file_returns_none(tmp_path):
    (tmp_path / "main.set").write_text("#trigger {x} {#say {hi}}", encoding="utf-8")
    assert world_from_pack(tmp_path) is None


def test_plugin_xml_without_world_element_returns_none(tmp_path):
    path = tmp_path / "plugin.xml"
    path.write_text("<muclient><plugin name='x'></plugin></muclient>", encoding="utf-8")
    assert world_from_pack(path) is None
