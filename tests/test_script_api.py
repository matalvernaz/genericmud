"""ScriptApi path resolution: pack-dir anchoring + the Sounds-folder (@sppath) fallback."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from tests.helpers import RecordingDiag, RecordingSink


def _api(base_dir: str) -> ScriptApi:
    return ScriptApi(AutomationEngine(RecordingSink()), base_dir=base_dir)


def test_resolve_keeps_existing_pack_path(tmp_path):
    sound = tmp_path / "beep.wav"
    sound.write_bytes(b"RIFF")
    assert _api(str(tmp_path))._resolve("beep.wav")[0] == str(sound)  # exists -> used as-is


def test_resolve_falls_back_to_sounds_folder_by_basename(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "mysounds"
    pack.mkdir()
    (sounds / "fx").mkdir(parents=True)
    real = sounds / "fx" / "boom.wav"
    real.write_bytes(b"RIFF")
    api = _api(str(pack))
    api.set_var("sppath", str(sounds))
    # The pack points at <pack>/boom.wav (missing); the Sounds folder rescues it by basename.
    assert api._resolve("boom.wav")[0] == str(real)


def test_resolve_finds_windows_authored_path_by_basename(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "snd"
    pack.mkdir()
    sounds.mkdir()
    (sounds / "boom.wav").write_bytes(b"RIFF")
    api = _api(str(pack))
    api.set_var("sppath", str(sounds))
    # A MUSHclient/VIPMud pack may reference sounds with backslashes; the leaf must still
    # resolve via the Sounds folder on Linux, where "\" isn't a path separator.
    assert api._resolve("sub\\boom.wav")[0] == str(sounds / "boom.wav")


def test_resolve_returns_expected_path_when_unfound(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    api = _api(str(pack))
    api.set_var("sppath", str(tmp_path / "nope"))  # not a directory
    # Missing everywhere: return the expected path so the diagnostic can report where it looked.
    # Compare via Path so the separator matches the host (os.path.join emits "\" on Windows).
    assert api._resolve("ghost.wav")[0] == str(pack / "ghost.wav")


def test_resolve_without_sppath_is_unchanged(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    assert _api(str(pack))._resolve("ghost.wav")[0] == str(pack / "ghost.wav")


def test_resolve_traces_the_sppath_fallback(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "snd"
    pack.mkdir()
    sounds.mkdir()
    (sounds / "boom.wav").write_bytes(b"RIFF")
    engine = AutomationEngine(RecordingSink())
    engine.diag = RecordingDiag()
    api = ScriptApi(engine, base_dir=str(pack))
    api.set_var("sppath", str(sounds))
    api._resolve("boom.wav")  # missing in pack, rescued from the Sounds folder
    fields = engine.diag.fields("play.resolve")
    assert fields["fallback"] == "sppath" and fields["exists"] is True


def test_resolve_traces_a_total_miss(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    engine = AutomationEngine(RecordingSink())
    engine.diag = RecordingDiag()
    ScriptApi(engine, base_dir=str(pack))._resolve("ghost.wav")  # nowhere
    fields = engine.diag.fields("play.resolve")
    assert fields["exists"] is False and fields["fallback"] == "none"
