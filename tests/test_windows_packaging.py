"""Windows artifacts stay GUI-subsystem builds that can speak, without a companion terminal."""

from __future__ import annotations

from pathlib import Path


def test_windows_builds_use_the_windowed_subsystem():
    root = Path(__file__).resolve().parents[1]
    build_script = (root / "build_windows.bat").read_text(encoding="utf-8").lower()
    workflow = (root / ".github/workflows/build-windows.yml").read_text(encoding="utf-8").lower()

    for source in (build_script, workflow):
        assert "--windowed" in source
        assert "--console" not in source


def test_windows_builds_bundle_the_speech_library():
    # These two spell out the PyInstaller flags by hand, separately from tools/build_app.py,
    # so they are the pair that can silently drift and ship an exe that cannot speak.
    root = Path(__file__).resolve().parents[1]
    build_script = (root / "build_windows.bat").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/build-windows.yml").read_text(encoding="utf-8")

    for source in (build_script, workflow):
        assert "--collect-all prism" in source
