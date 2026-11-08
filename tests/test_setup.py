"""One-shot pack setup: install (multi-file entry) + derive world + enable/trust."""

from __future__ import annotations

import shutil
import zipfile

import pytest

from genericmud.packs.setup import detect_entry, entry_problem, setup_pack, update_pack
from genericmud.packs.store import PackError, PackStore, extract_pack

MCL = """<?xml version="1.0" encoding="iso-8859-1"?>
<!DOCTYPE muclient>
<muclient>
<world
   chat_port="4050"
   name="Cosmic Rage"
   site="cosmicrage.earth"
   port="7777"
   proxy_port="1080"
>
</world>
</muclient>"""


def _multi_file_pack(root):
    """A pack shaped like a real one: several .set scripts + a MUSHclient world file."""
    pack = root / "VIPMudCosmicRageScripts"
    (pack / "scripts").mkdir(parents=True)
    (pack / "scripts" / "main.set").write_text("#trigger {x} {#say {hi}}", encoding="utf-8")
    (pack / "scripts" / "keys.set").write_text("#key f1 {look}", encoding="utf-8")
    (pack / "worlds").mkdir()
    (pack / "worlds" / "cr.MCL").write_text(MCL, encoding="latin-1")
    return pack


def test_setup_multi_file_pack_creates_world_enables_and_trusts(tmp_path):
    store = PackStore(tmp_path / "store")
    pack = _multi_file_pack(tmp_path)
    result = setup_pack(store, pack, entry="scripts/main.set", sounds="/snd")
    assert result.world is not None
    assert (result.world.host, result.world.port) == ("cosmicrage.earth", 7777)
    assert result.world.sounds == "/snd"
    assert result.enabled_for == "Cosmic Rage"
    assert store.is_trusted(result.manifest.id)
    assert store.is_enabled(result.manifest.id, "Cosmic Rage")


def test_detect_entry_prefers_main_set(tmp_path):
    assert detect_entry(_multi_file_pack(tmp_path)) == "scripts/main.set"


def test_detect_entry_none_when_ambiguous(tmp_path):
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "a.xml").write_text("<muclient/>", encoding="utf-8")
    (pack / "b.xml").write_text("<muclient/>", encoding="utf-8")
    assert detect_entry(pack) is None


def test_detect_entry_finds_vipmud_loader(tmp_path):
    # No main.set, but one .set #loads the others -> that's the loader/entry.
    pack = tmp_path / "vippack" / "scripts"
    pack.mkdir(parents=True)
    (pack / "boot.set").write_text("#load {scripts/extra.set}\n#say {hi}", encoding="utf-8")
    (pack / "extra.set").write_text("#trigger {x} {look}", encoding="utf-8")
    assert detect_entry(tmp_path / "vippack") == "scripts/boot.set"


def test_detect_entry_prefers_the_main_loader(tmp_path):
    # Among several .set loaders, the one that #loads the most files is the main one.
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "buffers.set").write_text("#load {a.set}", encoding="utf-8")  # sorts first, 1 load
    (pack / "boot.set").write_text("#load {a.set}\n#load {b.set}\n#load {c.set}", encoding="utf-8")
    (pack / "a.set").write_text("#say {a}", encoding="utf-8")
    assert detect_entry(pack) == "boot.set"


def test_detect_entry_finds_plugin_named_after_pack(tmp_path):
    # A MUSHclient pack: pick the plugin named after the pack (toastush.xml in toastush/).
    plugins = tmp_path / "toastush" / "worlds" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "timer.xml").write_text("<muclient/>", encoding="utf-8")
    (plugins / "toastush.xml").write_text("<muclient/>", encoding="utf-8")
    assert detect_entry(tmp_path / "toastush") == "worlds/plugins/toastush.xml"


def test_detect_entry_prefers_mcl_world(tmp_path):
    # A MUSHclient pack: the lone .MCL world is the entry (it <include>s the plugins),
    # even though the plugin .xml files alone would be ambiguous.
    pack = tmp_path / "erionish"
    (pack / "worlds" / "plugins").mkdir(parents=True)
    (pack / "worlds" / "erion.MCL").write_text(MCL, encoding="latin-1")
    (pack / "worlds" / "plugins" / "gather.xml").write_text("<muclient/>", encoding="utf-8")
    (pack / "worlds" / "plugins" / "combat.xml").write_text("<muclient/>", encoding="utf-8")
    assert detect_entry(pack) == "worlds/erion.MCL"


def test_detect_entry_mcl_world_beats_a_stray_main_xml(tmp_path):
    # A MUSHclient pack whose plugin happens to be named main.xml: the .MCL world still
    # wins (no .set present), so we don't load a plugin as if it were the whole pack.
    pack = tmp_path / "p"
    (pack / "worlds").mkdir(parents=True)
    (pack / "worlds" / "world.MCL").write_text(MCL, encoding="latin-1")
    (pack / "main.xml").write_text("<muclient/>", encoding="utf-8")
    assert detect_entry(pack) == "worlds/world.MCL"


def test_detect_entry_vipmud_loader_wins_over_bundled_mcl(tmp_path):
    # A VIPMud pack may bundle a .MCL for connection info; its .set loader is the entry,
    # not the world (the .mcl rule is gated on the pack having no .set).
    pack = tmp_path / "vip"
    pack.mkdir()
    (pack / "boot.set").write_text("#load {extra.set}\n#say {hi}", encoding="utf-8")
    (pack / "extra.set").write_text("#say {x}", encoding="utf-8")
    (pack / "conn.MCL").write_text(MCL, encoding="latin-1")
    assert detect_entry(pack) == "boot.set"


def test_detect_entry_picks_the_richest_mcl_world(tmp_path):
    # A full MUSHclient-install bundle ships extra worlds (captures/sandbox); pick the one
    # that <include>s the plugin suite, not a bare capture world.
    pack = tmp_path / "bundle"
    (pack / "worlds").mkdir(parents=True)
    (pack / "worlds" / "captures.MCL").write_text(MCL, encoding="latin-1")  # 0 includes
    (pack / "worlds" / "Main World.MCL").write_text(
        MCL.replace("</muclient>", '<include name="a.xml"/><include name="b.xml"/></muclient>'),
        encoding="latin-1",
    )
    assert detect_entry(pack) == "worlds/Main World.MCL"


def test_detect_entry_uses_mud_plugin_instead_of_inert_world(tmp_path):
    pack = tmp_path / "bundle"
    (pack / "worlds" / "plugins").mkdir(parents=True)
    (pack / "worlds" / "Mystavaria.MCL").write_text(MCL, encoding="latin-1")
    (pack / "worlds" / "plugins" / "mystavaria.xml").write_text(
        "<muclient><plugin name='Mystavaria' id='myst'/><triggers>"
        "<trigger match='hit' enabled='y'/></triggers></muclient>",
        encoding="latin-1",
    )
    assert detect_entry(pack, mud_name="Mystavaria") == "worlds/plugins/mystavaria.xml"


def test_detect_entry_finds_soundpack_plugin_in_plugin_manager_repo(tmp_path):
    pack = tmp_path / "repository"
    pack.mkdir()
    (pack / "manager.xml").write_text(
        "<muclient><plugin name='Manager' id='manager'/><aliases>"
        "<alias match='manage' enabled='y'/></aliases></muclient>", encoding="latin-1"
    )
    (pack / "aard_soundpack.xml").write_text(
        "<muclient><plugin name='Aardwolf Soundpack' id='sounds'/><triggers>"
        "<trigger match='hit' enabled='y'/></triggers></muclient>", encoding="latin-1"
    )
    assert detect_entry(pack, mud_name="Aardwolf") == "aard_soundpack.xml"


def test_entry_problem_distinguishes_dead_ends(tmp_path):
    installer = tmp_path / "inst"
    installer.mkdir()
    (installer / "setup.exe").write_bytes(b"MZ")
    assert "installer" in entry_problem(installer)

    mush = tmp_path / "mush"
    mush.mkdir()
    (mush / "a.xml").write_text("<muclient/>", encoding="utf-8")
    assert "MUSHclient" in entry_problem(mush)

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "readme.txt").write_text("hi", encoding="utf-8")
    assert "no soundpack script" in entry_problem(empty)

    # A MUSHclient pack bundling git tooling (.exe) is MUSHclient, not an "installer".
    both = tmp_path / "both"
    both.mkdir()
    (both / "git.exe").write_bytes(b"MZ")
    (both / "LuaAudio.xml").write_text("<muclient/>", encoding="utf-8")
    assert "MUSHclient" in entry_problem(both)


def test_install_records_origin(tmp_path):
    store = PackStore(tmp_path / "store")
    bare = tmp_path / "x.set"
    bare.write_text("#trigger {a} {look}", encoding="utf-8")
    manifest = store.install(bare, origin="https://example.test/x.zip")
    assert store.manifest(manifest.id).origin == "https://example.test/x.zip"


def test_update_pack_refetches_from_origin_and_preserves_state(tmp_path):
    store = PackStore(tmp_path / "store")
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "main.set").write_text("#trigger {old} {look}", encoding="utf-8")
    setup_pack(store, v1, entry="main.set", origin="https://example.test/pack.zip")
    store.enable("v1", "myworld")

    # A "new version" archive the fake fetch returns.
    new = tmp_path / "new"
    new.mkdir()
    (new / "main.set").write_text("#trigger {new} {look}", encoding="utf-8")
    new_zip = tmp_path / "new.zip"
    with zipfile.ZipFile(new_zip, "w") as bundle:
        bundle.write(new / "main.set", "main.set")

    update_pack(store, "v1", fetch=lambda _url, dest: shutil.copy(new_zip, dest))

    assert "new" in store.entry_path("v1").read_text(encoding="utf-8")  # content refreshed
    assert store.manifest("v1").origin == "https://example.test/pack.zip"  # origin kept
    assert store.is_enabled("v1", "myworld")  # enablement preserved
    assert store.is_trusted("v1")  # trust preserved


def _installed_for_update(tmp_path):
    store = PackStore(tmp_path / "store")
    source = tmp_path / "mypack"
    source.mkdir()
    (source / "main.set").write_text("#trigger {hello} {#say hi}", encoding="utf-8")
    setup_pack(store, source, entry="main.set", origin="https://example.test/mypack.zip")
    return store


def test_update_pack_opens_a_wrapper_of_nested_zips_like_the_first_install(tmp_path):
    # Miriani's download is a zip holding Scripts.zip and Sounds.zip. Installing it opens
    # the inner zips; updating it used a plain extract, found no script and always failed.
    store = _installed_for_update(tmp_path)
    inner = tmp_path / "Scripts.zip"
    with zipfile.ZipFile(inner, "w") as scripts:
        scripts.writestr("main.set", "#trigger {hello} {#say hi again}")

    def fetch(_url, dest):
        with zipfile.ZipFile(dest, "w") as wrapper:
            wrapper.write(inner, "Scripts.zip")

    update_pack(store, "mypack", fetch=fetch)

    assert "again" in store.entry_path("mypack").read_text(encoding="utf-8")


def test_update_pack_refuses_a_zip_bomb_like_every_other_install(tmp_path):
    store = _installed_for_update(tmp_path)

    def fetch(_url, dest):
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as bomb:
            bomb.writestr("main.set", "#trigger {x} {#say y}")
            bomb.writestr("padding.wav", b"\x00" * (5 * 1024 * 1024))  # inflates ~1000x

    with pytest.raises(PackError, match="compression ratio"):
        update_pack(store, "mypack", fetch=fetch)
    assert "hi}" in store.entry_path("mypack").read_text(encoding="utf-8")  # old pack intact



def _zip_of(folder, archive):
    """Zip ``folder``'s files under their paths inside it, the way a pack's download holds them."""
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(folder).as_posix())
    return archive


def test_an_update_that_brings_nothing_new_leaves_the_pack_alone(tmp_path):
    # The Vault's copy of a pack rarely changes, so Update fetched the same files, swapped the
    # whole folder for them and said "Updated". Anything the pack had saved in its own folder
    # went with the old copy, for nothing.
    store = _installed_for_update(tmp_path)
    saved = store.pack_dir("mypack") / "settings.ini"
    saved.write_text("volume=40", encoding="utf-8")
    download = _zip_of(tmp_path / "mypack", tmp_path / "same.zip")

    result = update_pack(store, "mypack", fetch=lambda _url, dest: shutil.copy(download, dest))

    assert saved.read_text(encoding="utf-8") == "volume=40"
    from genericmud.packs.setup import UpdateUnchanged

    assert result == UpdateUnchanged("mypack")


def test_an_update_that_changes_a_file_installs_it_even_at_the_same_size(tmp_path):
    # The same size is not the same file: the comparison reads the contents.
    store = _installed_for_update(tmp_path)
    (tmp_path / "mypack" / "main.set").write_text("#trigger {hello} {#say ho}", encoding="utf-8")
    download = _zip_of(tmp_path / "mypack", tmp_path / "changed.zip")

    result = update_pack(store, "mypack", fetch=lambda _url, dest: shutil.copy(download, dest))

    assert "ho}" in store.entry_path("mypack").read_text(encoding="utf-8")
    assert result.manifest.id == "mypack"


def test_an_update_that_adds_a_file_installs_it(tmp_path):
    store = _installed_for_update(tmp_path)
    (tmp_path / "mypack" / "sounds").mkdir()
    (tmp_path / "mypack" / "sounds" / "hit.ogg").write_bytes(b"OggS")
    download = _zip_of(tmp_path / "mypack", tmp_path / "more.zip")

    result = update_pack(store, "mypack", fetch=lambda _url, dest: shutil.copy(download, dest))

    assert (store.pack_dir("mypack") / "sounds" / "hit.ogg").read_bytes() == b"OggS"
    assert result.manifest.id == "mypack"


def test_an_empty_download_is_never_taken_for_nothing_new(tmp_path):
    store = _installed_for_update(tmp_path)

    def fetch(_url, dest):
        with zipfile.ZipFile(dest, "w"):
            pass

    with pytest.raises(PackError, match="no load script"):
        update_pack(store, "mypack", fetch=fetch)
    assert "hi}" in store.entry_path("mypack").read_text(encoding="utf-8")

def test_setup_bare_set_pack_has_no_world(tmp_path):
    store = PackStore(tmp_path / "store")
    bare = tmp_path / "sounds.set"
    bare.write_text("#trigger {x} {#play {x.wav}}", encoding="utf-8")
    result = setup_pack(store, bare)
    assert result.world is None  # no world file -> caller prompts for host/port
    assert result.enabled_for is None
    assert store.is_trusted(result.manifest.id)  # still installed + trusted


def test_detect_entry_top_level_loader_beats_deeper_alias_loads(tmp_path):
    # The Star Conquest bug: the root loader uses one #ForAll/#load, but a deeper script has
    # more literal #loads inside reload aliases. Ranking shallowest-first keeps the real entry.
    pack = tmp_path / "p"
    (pack / "Scripts").mkdir(parents=True)
    (pack / "star conquest.set").write_text(
        "#ForAll {combat|keys} {#load {Scripts\\%I.set}}", encoding="utf-8"
    )
    (pack / "Scripts" / "combat.set").write_text(
        "#alias reload {#load Scripts/combat.set; #load Scripts/keys.set}", encoding="utf-8"
    )
    (pack / "Scripts" / "keys.set").write_text("#key f1 {look}", encoding="utf-8")
    assert detect_entry(pack) == "star conquest.set"


def test_detect_entry_mud_name_hint_picks_the_named_loader(tmp_path):
    # Two root-level loaders: without a hint the one with more #loads wins, but the MUD-name
    # hint selects the script named after the MUD (VIPMud's "star conquest.set" convention).
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "bigloader.set").write_text(
        "#load {a.set}\n#load {b.set}\n#load {c.set}", encoding="utf-8"
    )
    (pack / "star conquest.set").write_text("#load {a.set}", encoding="utf-8")
    for n in "abc":
        (pack / f"{n}.set").write_text(f"#say {{{n}}}", encoding="utf-8")
    assert detect_entry(pack) == "bigloader.set"
    assert detect_entry(pack, mud_name="Star Conquest") == "star conquest.set"


def test_extract_pack_descends_nested_zips(tmp_path):
    # Miriani ships a wrapper zip holding a separate scripts zip; extract_pack must descend
    # so the scripts are found, and must not leave the nested archive behind.
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as bundle:
        bundle.writestr("PackName/main.set", "#trigger {x} {#say {hi}}")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as bundle:
        bundle.write(inner, "PackName Scripts.zip")
    dest = tmp_path / "out"
    extract_pack(outer, dest)
    assert list(dest.rglob("main.set")), "nested .set was not extracted"
    assert not list(dest.rglob("*.zip")), "nested zip left behind"


def _linked_vipmud_pack(root):
    """Shaped like Miriani's and the official Prometheus VIP Mud packs: a scripts folder and
    a sounds folder, each with the batch file VIP Mud users run to link it."""
    pack = root / "pack"
    scripts = pack / "Miriani Soundpack for VIP Mud" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "Link the scripts for VIP Mud 2.0.bat").write_text("@echo off\n")
    (scripts / "Main.set").write_text("#var linked {@ScriptPath}\n")
    sounds = pack / "Miriani Soundpack Sounds Version 7"
    (sounds / "Themes").mkdir(parents=True)
    (sounds / "Link the sounds folder.bat").write_text("@echo off\n")
    (sounds / "main.set").write_text("#Var SoundsVersion {7}\n")
    (sounds / "Themes" / "Link the themes folder.bat").write_text("@echo off\n")
    return pack


def test_detect_entry_takes_a_linked_vipmud_packs_scripts_not_its_sounds_marker(tmp_path):
    # The sounds folder's main.set only says which sounds version it is.
    pack = _linked_vipmud_pack(tmp_path)
    assert detect_entry(pack) == "Miriani Soundpack for VIP Mud/Scripts/Main.set"


def _git_installed(tmp_path, files: dict[str, bytes], *, wrapper: str = ""):
    """A pack installed from an archive (``wrapper`` is the folder the archive kept around
    everything, as Toastush's did), the way the vault browser leaves one."""
    store = PackStore(tmp_path / "store")
    source = tmp_path / "toastush"
    for path, data in files.items():
        target = source / wrapper / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    store.install(
        source, entry=wrapper + "worlds/Miriani.MCL", origin="https://example.test/t.zip",
        pack_id="toastush",
    )
    return store


def _fake_remote(files: dict[str, bytes]):
    """A repository's list and blob fetch, from ``files`` (path -> contents)."""
    from genericmud.packs import git_remote, git_sync

    by_sha = {git_sync.blob_sha(data): data for data in files.values()}
    fetched: list[set[str]] = []

    def list_tree(_url):
        return git_remote.RemoteTree(
            commit="c" * 40, files={path: git_sync.blob_sha(data) for path, data in files.items()}
        )

    def fetch_blobs(_url, blob_ids, destination, progress=None):
        wanted = set(blob_ids)
        fetched.append(wanted)
        landed = {}
        for sha in wanted:
            landed[sha] = destination / sha
            landed[sha].write_bytes(by_sha[sha])
        return landed

    return list_tree, fetch_blobs, fetched


def test_a_pack_updates_from_its_repository_file_by_file(tmp_path):
    from genericmud.packs import git_sources
    from genericmud.packs.setup import update_pack_from_git

    wrapper = "PsudoDeSudo-Toastush-e8cdb7d/"
    store = _git_installed(tmp_path, {
        "worlds/Miriani.MCL": b"<world mine/>",
        "scripts/a.lua": b"old a",
        "scripts/b.lua": b"b",
        "sounds/hit.ogg": b"hit",
    }, wrapper=wrapper)
    store.trust("toastush")
    store.enable("toastush", "Miriani")
    list_tree, fetch_blobs, fetched = _fake_remote({
        "worlds/Miriani.MCL": b"<world/>",
        "scripts/a.lua": b"new a",
        "scripts/b.lua": b"b",
        "sounds/hit.ogg": b"hit",
        "sounds/miss.ogg": b"miss",
    })
    source = git_sources.update_source("toastush")
    result = update_pack_from_git(
        store, "toastush", source, list_tree=list_tree, fetch_blobs=fetch_blobs,
    )
    pack = store.pack_dir("toastush") / wrapper
    assert (pack / "scripts" / "a.lua").read_bytes() == b"new a"
    assert (pack / "sounds" / "miss.ogg").read_bytes() == b"miss"
    assert (pack / "worlds" / "Miriani.MCL").read_bytes() == b"<world mine/>"  # the player's
    assert (pack / "worlds" / "Miriani.MCL.new").read_bytes() == b"<world/>"
    assert result.written == 2 and result.new_versions == ["worlds/Miriani.MCL"]
    assert len(fetched[0]) == 3  # only what changed: new a, miss, and the new world file
    assert store.is_trusted("toastush") and store.is_enabled("toastush", "Miriani")
    assert not list(store.pack_dir("toastush").glob(".gm-update-*"))

    # Nothing new upstream: nothing fetched, and it says so.
    again = update_pack_from_git(
        store, "toastush", source, list_tree=list_tree, fetch_blobs=fetch_blobs,
    )
    assert again.up_to_date and len(fetched) == 1


def test_an_install_that_doesnt_line_up_with_its_repository_isnt_touched(tmp_path):
    from genericmud.packs import git_sources
    from genericmud.packs.git_sources import UpdateSource
    from genericmud.packs.setup import update_pack_from_git

    store = _git_installed(tmp_path, {"worlds/Miriani.MCL": b"<world/>"})
    elsewhere = UpdateSource("toastush", "Toastush", "https://example.test/t.git", "other/x.MCL")
    list_tree, fetch_blobs, fetched = _fake_remote({"other/x.MCL": b"x"})
    with pytest.raises(PackError, match="Reinstalling"):
        update_pack_from_git(
            store, "toastush", elsewhere, list_tree=list_tree, fetch_blobs=fetch_blobs,
        )
    assert fetched == []
    assert git_sources.update_source("toastush") is not None


def test_two_worlds_updating_one_pack_take_turns(tmp_path):
    # Two worlds can share a pack, and each can ask for an update at the same moment.
    import threading
    import time

    from genericmud.packs import git_sources
    from genericmud.packs.setup import update_pack_from_git

    store = _git_installed(tmp_path, {"worlds/Miriani.MCL": b"<world/>", "a.lua": b"old"})
    list_tree, fetch_blobs, _fetched = _fake_remote(
        {"worlds/Miriani.MCL": b"<world/>", "a.lua": b"new"}
    )
    inside = []
    overlapped = []

    def slow_list(url):
        inside.append(1)
        if len(inside) > 1:
            overlapped.append(True)
        time.sleep(0.2)
        inside.pop()
        return list_tree(url)

    source = git_sources.update_source("toastush")
    results = []

    def update():
        results.append(update_pack_from_git(
            store, "toastush", source, list_tree=slow_list, fetch_blobs=fetch_blobs,
        ))

    threads = [threading.Thread(target=update) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert overlapped == []
    assert sorted(result.up_to_date for result in results) == [False, True]


def test_a_damaged_record_stops_the_update_and_says_how_to_go_on(tmp_path):
    from genericmud.packs import git_sources, git_sync
    from genericmud.packs.setup import check_pack_update, update_pack_from_git

    store = _git_installed(tmp_path, {"worlds/Miriani.MCL": b"<world/>", "a.lua": b"mine"})
    (store.pack_dir("toastush") / git_sync.BASELINE_NAME).write_text("garbage\n", "utf-8")
    list_tree, fetch_blobs, fetched = _fake_remote(
        {"worlds/Miriani.MCL": b"<world/>", "a.lua": b"theirs"}
    )
    source = git_sources.update_source("toastush")
    with pytest.raises(PackError, match="damaged.*delete .gm_git.lst"):
        update_pack_from_git(
            store, "toastush", source, list_tree=list_tree, fetch_blobs=fetch_blobs,
        )
    with pytest.raises(PackError, match="damaged"):
        check_pack_update(store, "toastush", source, list_tree=list_tree)
    assert fetched == []
    assert (store.pack_dir("toastush") / "a.lua").read_bytes() == b"mine"


def test_an_update_says_what_couldnt_go_in_place():
    from genericmud.packs.setup import GitUpdateResult, describe_update

    said = describe_update(GitUpdateResult(
        "Toastush", True, failed=["a.ogg"], blocked=["maps/town.lua", "music"],
    ))
    assert "1 file couldn't be written; check there's space on the disk" in said
    assert "2 files couldn't go in place" in said and "maps/town.lua, music" in said
    assert "is up to date" not in said

