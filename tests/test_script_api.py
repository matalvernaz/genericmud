"""ScriptApi path resolution: pack-dir anchoring + the Sounds-folder (@sppath) fallback."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from tests.helpers import RecordingSink


def _api(base_dir: str) -> ScriptApi:
    return ScriptApi(AutomationEngine(RecordingSink()), base_dir=base_dir)


def test_resolve_keeps_existing_pack_path(tmp_path):
    sound = tmp_path / "beep.wav"
    sound.write_bytes(b"RIFF")
    assert _api(str(tmp_path))._resolve("beep.wav") == str(sound)  # exists -> used as-is


def test_resolve_falls_back_to_sounds_folder_by_basename(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "mysounds"
    pack.mkdir()
    (sounds / "fx").mkdir(parents=True)
    real = sounds / "fx" / "boom.wav"
    real.write_bytes(b"RIFF")
    api = _api(str(pack))
    api.set_var("sppath", str(sounds))
    # The pack points at <pack>/boom.wav (missing); the Sounds folder rescues it by basename.
    assert api._resolve("boom.wav") == str(real)


def test_resolve_returns_expected_path_when_unfound(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    api = _api(str(pack))
    api.set_var("sppath", str(tmp_path / "nope"))  # not a directory
    # Missing everywhere: return the expected path so the diagnostic can report where it looked.
    # Compare via Path so the separator matches the host (os.path.join emits "\" on Windows).
    assert api._resolve("ghost.wav") == str(pack / "ghost.wav")


def test_resolve_without_sppath_is_unchanged(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    assert _api(str(pack))._resolve("ghost.wav") == str(pack / "ghost.wav")
