"""Windows artifacts stay GUI-subsystem builds that can speak, without a companion terminal."""

from __future__ import annotations

from pathlib import Path


def test_windows_builds_use_the_windowed_subsystem():
    root = Path(__file__).resolve().parents[1]
    build_script = (root / "build_windows.bat").read_text(encoding="utf-8")
    spec = (root / "genericMud.spec").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/build-windows.yml").read_text(encoding="utf-8").lower()

    # build_windows.bat freezes from the spec, so its GUI-subsystem choice lives there as
    # console=False; the workflow still spells the flags out itself.
    assert "genericMud.spec" in build_script
    assert "console=False" in spec
    assert "--windowed" in workflow
    assert "--console" not in workflow


def test_windows_builds_bundle_the_speech_library():
    # The hook is the only thing that gets prism's cffi bridge into the bundle, and each freeze
    # entry point registers it separately, so this is the pair that can drift and ship an exe
    # that imports prism, fails, and falls back to SAPI with nothing in the logs.
    root = Path(__file__).resolve().parents[1]
    hook = (root / "hooks/hook-prism.py").read_text(encoding="utf-8")
    spec = (root / "genericMud.spec").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/build-windows.yml").read_text(encoding="utf-8")

    assert "*.pyd" in hook  # the pattern PyInstaller's defaults leave out
    assert "hookspath=[os.path.join(SPECPATH, 'hooks')]" in spec
    assert "--additional-hooks-dir hooks" in workflow
    assert "--collect-all prism" in workflow
